"""The one and only bridge between the portal and the existing grading engine.

The portal does **not** re-implement grading. Every grade produced here comes
from :func:`src.evaluator.core.evaluate_student_submission`, reached through
the public facade :mod:`src.llm_evaluator` so the test seams keep working
(``src.llm_evaluator._create_completion`` and friends). The adapter adds only
what a web application needs around that call:

* a :class:`RubricBinding` that pins the rubric file by SHA-256;
* the late-policy decision, made by :mod:`src.late_policy` from portal
  timestamps instead of Classroom ones;
* an atomic capture of the deterministic audit verdict, which lives in a
  destructive sidecar keyed by ``id(result)``;
* a plain-dict artifact that the DB/worker can persist without the ORM ever
  touching engine models.

Fidelity rules (enforced by ``portal/tests/test_parity.py``):

1. The student's text is passed **verbatim**. No trimming, escaping,
   whitespace normalisation or reformatting happens in the portal.
2. The rubric object comes from the same loader the CLI/dashboard use
   (``src.main.load_rubric``), not a portal-specific parser.
3. The prompt is not modified. That is why the prompt still calls the
   submission a "Google Doc" even when it came from the portal: changing that
   sentence would change grades.
4. Late points are computed by ``src.late_policy.penalty_for`` and then handed
   to the engine, which applies them through ``apply_late_penalty`` - exactly
   the path used by ``app.py`` and ``src/main.py``.

Import direction is strictly ``portal -> src``. Nothing in ``src/`` imports
this module.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grading_engine.settings import settings
from grading_engine.late_policy import penalty_for
from grading_engine.llm_evaluator import (
    LLMAPIError,
    LLMEvaluationError,
    Rubric,
    evaluate_student_submission,
    last_audit_for,
    paced_pause,
    rubric_title,
)
from grading_engine.llm_router import DailyQuotaExhausted
from grading_engine.models import CourseWork, GradingResult, StudentSubmission

from portal.paths import PROJECT_ROOT, RUBRIC_ROOTS

logger = logging.getLogger(__name__)


class RubricBindingError(RuntimeError):
    """The portal cannot safely bind to the requested rubric file."""


class UnsafeRubricPath(RubricBindingError):
    """A rubric path escaped the teacher-managed rubric directories."""


class RubricHashMismatch(RubricBindingError):
    """The rubric file changed after the assignment was published."""


@dataclass(frozen=True)
class RubricBinding:
    """A validated rubric plus the fingerprint recorded at publish time.

    ``sha256`` is the hash of the *file bytes*. Every grade row stores it, so a
    rubric edited mid-term can be detected and the teacher asked to republish
    instead of silently grading against a different set of answers.
    """

    rubric: Rubric
    sha256: str
    path: Path | None = None

    @property
    def title(self) -> str:
        return rubric_title(self.rubric)

    @property
    def question_count(self) -> int:
        """Number of gradable items, for teacher-facing summaries only."""
        if hasattr(self.rubric, "sections"):
            return sum(
                len(section.questions)  # type: ignore[union-attr]
                for section in self.rubric.sections  # type: ignore[union-attr]
            )
        return len(self.rubric.tasks)  # type: ignore[union-attr]


@dataclass(frozen=True)
class GradeArtifact:
    """Engine output with its audit verdict already captured.

    ``audit`` is deliberately a plain object (``AuditVerdict`` from
    :mod:`src.grade_audit`) rather than being popped by the caller: the sidecar
    behind ``last_audit_for`` is a *destructive* ``id(result)``-keyed pop, so
    the bridge pops it once, immediately, on the same thread, and carries it
    here. A second pop returns ``None``.
    """

    result: GradingResult
    audit: Any | None
    rubric_sha256: str
    rubric_title: str
    late_penalty_points: int
    configured_model: str
    graded_at: datetime

    def audit_dict(self) -> dict[str, Any] | None:
        """JSON-safe audit verdict (``None`` when the audit is disabled)."""
        return audit_to_dict(self.audit)

    def to_record(self) -> dict[str, Any]:
        """Flat, ORM-free record for the worker to persist."""
        return {
            "student_name": self.result.student_name,
            "score": self.result.score,
            "feedback_hebrew": self.result.feedback_hebrew,
            "deduction_breakdown": [
                item.model_dump() for item in self.result.deduction_breakdown
            ],
            "task_evaluations": [
                item.model_dump() for item in self.result.task_evaluations
            ],
            "audit": self.audit_dict(),
            "rubric_sha256": self.rubric_sha256,
            "rubric_title": self.rubric_title,
            "late_penalty_points": self.late_penalty_points,
            "configured_model": self.configured_model,
            "graded_at": self.graded_at.isoformat(),
        }


def hash_file(path: Path) -> str:
    """SHA-256 of a file's exact bytes, lowercase hex."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_to_dict(verdict: Any | None) -> dict[str, Any] | None:
    """Serialise an ``AuditVerdict`` without importing its dataclass here.

    Used both for persistence records and by the parity tests, so "identical
    audit" means the same fields the worker would store - not just ``ok``.
    """
    if verdict is None:
        return None
    payload: dict[str, Any] = dataclasses.asdict(verdict)
    payload["needs_review"] = bool(getattr(verdict, "needs_review", False))
    payload["badge"] = str(getattr(verdict, "badge", lambda: "")())
    payload["summary"] = str(getattr(verdict, "summary", lambda: "")())
    return payload


def _resolve_rubric_path(path: Path | str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def _is_allowed(candidate: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            if candidate.is_relative_to(root.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def load_rubric(
    path: Path | str,
    *,
    expected_sha256: str | None = None,
    allowed_roots: Sequence[Path] | None = None,
) -> RubricBinding:
    """Load a rubric through the project's own loader and fingerprint it.

    ``expected_sha256`` is checked *before* parsing, so a stale or swapped file
    fails as a binding error rather than grading with different answers.
    ``allowed_roots`` defaults to :data:`portal.paths.RUBRIC_ROOTS`; pass ``()``
    only in tests that intentionally use a temporary directory.
    """
    resolved = _resolve_rubric_path(path)
    roots = RUBRIC_ROOTS if allowed_roots is None else tuple(allowed_roots)
    if roots and not _is_allowed(resolved, roots):
        raise UnsafeRubricPath(
            f"Rubric path is outside the allowed directories: {resolved}"
        )
    if not resolved.is_file():
        raise RubricBindingError(f"Rubric file not found: {resolved}")

    digest = hash_file(resolved)
    if expected_sha256:
        expected = expected_sha256.strip().lower()
        if digest != expected:
            raise RubricHashMismatch(
                "Rubric file changed after publish: "
                f"expected {expected}, found {digest}. Republish the assignment."
            )

    # Imported lazily: src.main pulls in the Google client stack for the
    # classroom workflow, which the portal does not need at web startup.
    # The function itself is the project's single schema-aware rubric loader,
    # so reusing it is the fidelity-preserving choice.
    from grading_engine.main import load_rubric as _load_rubric

    try:
        rubric = _load_rubric(resolved)
    except Exception as exc:  # noqa: BLE001 - surfaced as a binding error
        raise RubricBindingError(
            f"Rubric file is not valid: {resolved}: {exc}"
        ) from exc
    return RubricBinding(rubric=rubric, sha256=digest, path=resolved)


def bind_rubric(
    rubric: Rubric,
    *,
    sha256: str | None = None,
    path: Path | None = None,
) -> RubricBinding:
    """Bind an already-validated rubric (tests, imports, future editors).

    With no file to hash, the fingerprint is the canonical JSON of the model.
    Production publishing should always prefer :func:`load_rubric`, whose hash
    pins the exact bytes the teacher approved.
    """
    digest = sha256 or hashlib.sha256(
        rubric.model_dump_json().encode("utf-8")
    ).hexdigest()
    return RubricBinding(rubric=rubric, sha256=digest, path=path)


def late_penalty_points_for(
    *,
    student_id: str,
    student_name: str,
    submission_id: str,
    due_at: datetime | None,
    first_submitted_at: datetime | None,
    submitted_at: datetime | None = None,
    penalty_points: int | None = None,
) -> int:
    """Ask :mod:`src.late_policy` how many points a portal hand-in loses.

    The synthetic ``StudentSubmission`` deliberately sets ``late_state`` and
    ``is_late`` to ``None``: those are Classroom verdicts, and inventing them
    would make the portal look authoritative about a deadline it only knows
    from its own clock. Resolution therefore starts at submission history -
    ``first_submitted_at``, which the portal must lock at the *first* hand-in
    and never move on resubmission. ``submitted_at`` (the latest version) is
    only the fallback the engine already defines.

    ``penalty_points`` defaults to the existing ``LATE_PENALTY_POINTS``
    setting, so both systems charge the same amount.
    """
    submission = StudentSubmission(
        student_id=student_id,
        student_name=student_name,
        submission_id=submission_id,
        state="TURNED_IN",
        late_state=None,
        is_late=None,
        submitted_at=submitted_at,
        first_submitted_at=first_submitted_at,
    )
    coursework = CourseWork(
        id=f"portal:{submission_id}",
        title=f"Portal submission {submission_id}",
        due_datetime=due_at,
    )
    configured = (
        settings.late_penalty_points
        if penalty_points is None
        else int(penalty_points)
    )
    return penalty_for(
        submission,
        coursework,
        configured,
        first_submitted_at=first_submitted_at,
    )


def _park_spent_fallback_pool(exc: BaseException) -> bool:
    """Record a fallback pool's daily-quota death so routing can move on.

    The engine's transport raises ``DailyQuotaExhausted`` the moment a
    *fallback pool* key answers with a daily-quota 429, but it never parks
    that key and never consults the tertiary pool afterwards: every retry
    picks the same dead fallback key again, and a configured tertiary
    provider (Cloudflare) never gets a turn. ``src/`` is read-only, so the
    portal records the evidence itself - park the fallback pool for the same
    window the router uses for daily caps. The next ``pick()`` then skips the
    dead pool and reaches the tertiary provider. Only router state changes;
    prompts, parsing and scores stay exactly the engine's.
    """
    text = str(exc)
    if "fallback" not in text or "daily quota" not in text.lower():
        return False
    try:
        import time

        from grading_engine import llm_evaluator
        from grading_engine.llm_router import DAILY_QUOTA_COOLDOWN_SECONDS

        router = getattr(llm_evaluator, "_ROUTER", None)
        pool = getattr(router, "_fallback_keys", None)
        if not pool:
            return False
        until = time.monotonic() + DAILY_QUOTA_COOLDOWN_SECONDS
        for slot in pool:
            slot.daily_until = until
            slot.cooldown_until = until
        logger.warning(
            "Fallback pool reported a daily quota; parked %d key(s) so the "
            "tertiary provider can serve: %s",
            len(pool),
            " ".join(text.split())[:200],
        )
        return True
    except Exception:  # noqa: BLE001 - a shim must never break grading
        logger.exception("Could not park the spent fallback pool")
        return False


def grade_submission(
    *,
    student_id: str,
    student_name: str,
    submission_id: str,
    raw_doc_text: str,
    rubric: RubricBinding,
    due_at: datetime | None = None,
    first_submitted_at: datetime | None = None,
    submitted_at: datetime | None = None,
    late_penalty_points: int | None = None,
) -> GradeArtifact:
    """Grade one portal submission through the untouched engine.

    This is synchronous and may take minutes on a cold model. Phase 3 calls it
    from the single grading worker, never from a request handler. ``raw_doc_text``
    must be the student's text exactly as stored - see the module docstring.
    """
    points = late_penalty_points_for(
        student_id=student_id,
        student_name=student_name,
        submission_id=submission_id,
        due_at=due_at,
        first_submitted_at=first_submitted_at,
        submitted_at=submitted_at,
        penalty_points=late_penalty_points,
    )
    try:
        result = evaluate_student_submission(
            student_name=student_name,
            raw_doc_text=raw_doc_text,
            rubric=rubric.rubric,
            late_penalty_points=points,
        )
    except DailyQuotaExhausted as exc:
        if not _park_spent_fallback_pool(exc):
            raise
        # One bounded retry: with the dead pool parked, the router now reaches
        # the tertiary provider. If that one is spent too, the engine's own
        # error path surfaces and the worker defers the job as usual.
        result = evaluate_student_submission(
            student_name=student_name,
            raw_doc_text=raw_doc_text,
            rubric=rubric.rubric,
            late_penalty_points=points,
        )
    # Destructive pop: do it once, here, on the calling thread, before any
    # other code can hold the result. If this is ever moved after an await or
    # into another thread, the verdict is silently lost.
    audit = last_audit_for(result)
    artifact = GradeArtifact(
        result=result,
        audit=audit,
        rubric_sha256=rubric.sha256,
        rubric_title=rubric.title,
        late_penalty_points=points,
        configured_model=settings.groq_model,
        graded_at=datetime.now(timezone.utc),
    )
    logger.info(
        "Portal grade: %s / %s -> %d (late=%d, audit=%s, rubric=%s)",
        artifact.rubric_title,
        student_name,
        result.score,
        points,
        "yes" if audit is not None else "no",
        rubric.sha256[:12],
    )
    return artifact


def between_grades_pause(on_tick: Any = None) -> None:
    """Pace sequential grades exactly like the existing batch entry points.

    The engine owns the adaptive pacer: it stretches after a 429 and relaxes
    after a clean streak. Calling its public ``paced_pause`` keeps the portal
    from inventing a second, possibly conflicting, rate limiter.
    """
    paced_pause(on_tick)


__all__ = [
    "DailyQuotaExhausted",
    "GradeArtifact",
    "LLMAPIError",
    "LLMEvaluationError",
    "RubricBinding",
    "RubricBindingError",
    "RubricHashMismatch",
    "UnsafeRubricPath",
    "audit_to_dict",
    "between_grades_pause",
    "bind_rubric",
    "grade_submission",
    "hash_file",
    "late_penalty_points_for",
    "load_rubric",
]
