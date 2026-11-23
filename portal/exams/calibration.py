"""Distill a teacher's correction log into a small grading-style profile.

The correction log (:mod:`portal.exams.feedback`) keeps *every* override the
teacher makes, without limit. Showing the model all of them on every paper would
swell the request into a 429 - the exact failure the selective re-check avoids.
So instead of injecting raw examples forever, the portal periodically *distills*
a question's corrections into one short, bounded profile: the teacher's general
grading style for that question, not specific students. Grading then carries only
that small profile, so the per-paper prompt stays tiny no matter how large the log
grows.

The distillation is one background model call through the same routed transport
as grading (key rotation, Gemini fallback, pacing, quota signal). It fires when a
question gathers :data:`DISTILL_THRESHOLD` new corrections, or when the teacher
asks. If the quota is exhausted it raises :class:`llm_review.QuotaExhausted` and
the previous profile stays in place - grading is never blocked by a failed
distill. A teacher-edited profile is ``locked`` so automatic distillation does not
clobber it.

The DB helpers live here; the pure renderers (:func:`render_distill_input`,
:func:`render_profile_block`) are what the transport and the DB-free checker
ultimately receive, already bounded.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from portal.exams import feedback as exam_feedback
from portal.exams import llm_review
from portal.models import ExamGradingProfile, ExamLlmFeedback
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

# The profile injected at grading time is hard-bounded, like the example block.
MAX_PROFILE_CHARS = 500
MAX_PROFILE_BLOCK_CHARS = 600
# Distill once a question has this many corrections (auto); a manual refresh may
# distill fewer. Re-distill only when new corrections change the fingerprint.
DISTILL_THRESHOLD = 3
# The distiller is a one-off background call, so its input may be larger than a
# grading prompt - but still bounded, so a huge log cannot exhaust the request.
MAX_DISTILL_EXAMPLES = 40
MAX_DISTILL_INPUT_CHARS = 6000

_PROFILE_HEADER = (
    "סגנון הניקוד של המורה בשאלה זו (מזוקק מתיקונים קודמים - פעל לפיו, אך בדוק "
    "כל תשובה לגופה ואל תעתיק ניקוד בלי לבדוק):"
)

_INFLIGHT: set[tuple[str, str]] = set()
_INFLIGHT_LOCK = threading.Lock()


# --------------------------------------------------------------------- read
def all_for_item(
    db: Session, exam_config: str, item_key: str
) -> list[ExamLlmFeedback]:
    """Every correction for one question, newest first (the uncapped log)."""
    name = (exam_config or "").strip()
    key = (item_key or "").strip()
    if not name or not key:
        return []
    return list(
        db.scalars(
            select(ExamLlmFeedback)
            .where(
                ExamLlmFeedback.exam_config == name,
                ExamLlmFeedback.item_key == key,
            )
            .order_by(ExamLlmFeedback.created_at.desc(), ExamLlmFeedback.id.desc())
        ).all()
    )


def profile_for(
    db: Session, exam_config: str, item_key: str
) -> ExamGradingProfile | None:
    name = (exam_config or "").strip()
    key = (item_key or "").strip()
    if not name or not key:
        return None
    return db.scalar(
        select(ExamGradingProfile).where(
            ExamGradingProfile.exam_config == name,
            ExamGradingProfile.item_key == key,
        )
    )


def profiles_for_exam(db: Session, exam_config: str) -> dict[str, str]:
    """Non-empty distilled profiles for one exam, keyed by question."""
    name = (exam_config or "").strip()
    if not name:
        return {}
    rows = db.scalars(
        select(ExamGradingProfile).where(ExamGradingProfile.exam_config == name)
    ).all()
    return {
        row.item_key: row.profile
        for row in rows
        if (row.profile or "").strip()
    }


def fingerprint(corrections: list[ExamLlmFeedback]) -> str:
    """A stable hash of the corrections a profile reflects, to detect staleness."""
    payload = [
        {
            "id": row.id,
            "answer": row.answer,
            "model": row.model_points,
            "teacher": row.teacher_points,
            "note": row.note,
        }
        for row in corrections
    ]
    import json

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def needs_distill(db: Session, exam_config: str, item_key: str) -> bool:
    """True when enough new corrections have piled up to warrant a distill."""
    corrections = all_for_item(db, exam_config, item_key)
    if len(corrections) < DISTILL_THRESHOLD:
        return False
    profile = profile_for(db, exam_config, item_key)
    if profile is None:
        return True
    if profile.locked:
        return False  # the teacher pinned their own wording
    return profile.source_fingerprint != fingerprint(corrections)


# ------------------------------------------------------------------ render
def render_distill_input(corrections: list[ExamLlmFeedback]) -> str:
    """The bounded ledger fed to the distiller (one-off, larger than a grade)."""
    lines: list[str] = []
    total = 0
    for row in corrections[:MAX_DISTILL_EXAMPLES]:
        answer = exam_feedback._trim(row.answer, exam_feedback.MAX_ANSWER_CHARS)
        note = exam_feedback._trim(row.note, exam_feedback.MAX_NOTE_CHARS)
        maximum = int(row.maximum or 0)
        line = (
            f'- תשובה: "{answer}" | אוטומטי: {int(row.model_points or 0)}/{maximum}'
            f" | המורה: {int(row.teacher_points or 0)}/{maximum}"
        )
        if note:
            line += f" | הערה: {note}"
        if total + len(line) + 1 > MAX_DISTILL_INPUT_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def render_profile_block(profile: str) -> str:
    """The bounded block injected at grading time in place of raw examples."""
    text = (profile or "").strip()
    if not text:
        return ""
    # Leave room for the header, the newline and the trim ellipsis inside the cap,
    # so the profile is clipped on a boundary rather than sliced mid-word.
    budget = MAX_PROFILE_BLOCK_CHARS - len(_PROFILE_HEADER) - 2
    text = exam_feedback._trim(text, max(0, budget))
    block = f"{_PROFILE_HEADER}\n{text}"
    return block[:MAX_PROFILE_BLOCK_CHARS]


def ledger(
    db: Session, exam_config: str, *, order: list[tuple[str, str]] | None = None
) -> list[dict[str, Any]]:
    """The correction log organized by question order, for the teacher to read.

    ``order`` is the exam's question sequence as ``(key, title)`` pairs; questions
    with no corrections still appear (empty) so the ledger mirrors the paper. When
    no order is given, questions are taken from the corrections themselves.
    """
    name = (exam_config or "").strip()
    if not name:
        return []
    rows = db.scalars(
        select(ExamLlmFeedback)
        .where(ExamLlmFeedback.exam_config == name)
        .order_by(ExamLlmFeedback.created_at.desc(), ExamLlmFeedback.id.desc())
    ).all()
    by_item: dict[str, list[ExamLlmFeedback]] = {}
    for row in rows:
        by_item.setdefault(row.item_key, []).append(row)

    if order is None:
        order = [(key, key) for key in by_item]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, title in order:
        seen.add(key)
        corrections = by_item.get(key, [])
        result.append(
            {
                "key": key,
                "title": title or key,
                "count": len(corrections),
                "profile": profile_for(db, name, key),
                "corrections": [
                    {
                        "answer": row.answer,
                        "model_points": row.model_points,
                        "teacher_points": row.teacher_points,
                        "maximum": row.maximum,
                        "note": row.note,
                        "created_at": row.created_at,
                        "submission_id": row.submission_id,
                    }
                    for row in corrections
                ],
            }
        )
    # Any corrected question not in the supplied order still shows, at the end.
    for key, corrections in by_item.items():
        if key in seen:
            continue
        result.append(
            {
                "key": key,
                "title": key,
                "count": len(corrections),
                "profile": profile_for(db, name, key),
                "corrections": [
                    {
                        "answer": row.answer,
                        "model_points": row.model_points,
                        "teacher_points": row.teacher_points,
                        "maximum": row.maximum,
                        "note": row.note,
                        "created_at": row.created_at,
                        "submission_id": row.submission_id,
                    }
                    for row in corrections
                ],
            }
        )
    return result


# ----------------------------------------------------------------- distill
def distill(
    db: Session,
    exam_config: str,
    item_key: str,
    *,
    title: str = "",
    force: bool = False,
    client: Any = None,
) -> ExamGradingProfile | None:
    """Summarize one question's corrections into its profile (one model call).

    ``force`` is the teacher's manual refresh: it distills even below the
    threshold and even over a locked profile. Otherwise a locked or already-fresh
    profile is left alone, and fewer than :data:`DISTILL_THRESHOLD` corrections is
    a no-op. Returns the stored profile, the unchanged previous one, or ``None``.
    :class:`llm_review.QuotaExhausted` propagates so the caller can wait and retry.
    Does not commit - the caller owns the transaction.
    """
    name = (exam_config or "").strip()
    key = (item_key or "").strip()
    if not name or not key:
        return None
    corrections = all_for_item(db, name, key)
    if not corrections:
        return None
    existing = profile_for(db, name, key)
    if not force:
        if len(corrections) < DISTILL_THRESHOLD:
            return existing
        if existing is not None and existing.locked:
            return existing
    fp = fingerprint(corrections)
    if not force and existing is not None and existing.source_fingerprint == fp:
        return existing  # already reflects these corrections

    input_text = render_distill_input(corrections)
    if not input_text:
        return existing
    profile_text = llm_review.summarize_corrections(
        title or key, input_text, max_chars=MAX_PROFILE_CHARS, client=client
    )
    if profile_text is None:
        # Generic failure (model off, malformed reply): keep the previous profile.
        return existing

    if existing is None:
        existing = ExamGradingProfile(exam_config=name, item_key=key)
        db.add(existing)
    existing.profile = profile_text
    existing.source_count = len(corrections)
    existing.source_fingerprint = fp
    existing.locked = False
    existing.distilled_at = utcnow()
    db.flush()
    return existing


def set_profile(
    db: Session,
    exam_config: str,
    item_key: str,
    text: str,
    *,
    teacher_id: int | None = None,
) -> ExamGradingProfile | None:
    """Store a teacher-edited profile and lock it against automatic distills."""
    name = (exam_config or "").strip()
    key = (item_key or "").strip()
    if not name or not key:
        return None
    profile = profile_for(db, name, key)
    if profile is None:
        profile = ExamGradingProfile(exam_config=name, item_key=key)
        db.add(profile)
    profile.profile = exam_feedback._trim(text, MAX_PROFILE_CHARS)
    profile.locked = True
    profile.updated_by = teacher_id
    profile.distilled_at = utcnow()
    corrections = all_for_item(db, name, key)
    profile.source_count = len(corrections)
    profile.source_fingerprint = fingerprint(corrections)
    db.flush()
    return profile


# --------------------------------------------------------------- background
def distill_in_thread(
    session_factory: sessionmaker,
    exam_config: str,
    item_key: str,
    *,
    title: str = "",
    force: bool = False,
) -> bool:
    """Distill one question in the background so a save request can return.

    Returns False when a distill for this question is already in flight (so a
    burst of saves does not spawn duplicate model calls). Quota exhaustion is
    logged and the previous profile kept; the next save past the threshold retries.
    """
    name = (exam_config or "").strip()
    key = (item_key or "").strip()
    if not name or not key:
        return False
    token = (name, key)
    with _INFLIGHT_LOCK:
        if token in _INFLIGHT:
            return False
        _INFLIGHT.add(token)

    def _target() -> None:
        try:
            with session_factory() as db:
                distill(db, name, key, title=title, force=force)
                db.commit()
        except llm_review.QuotaExhausted:
            logger.info(
                "Calibration distill waiting for quota: %s/%s", name, key
            )
        except Exception:  # noqa: BLE001 - a failed distill must never crash
            logger.exception("Calibration distill failed: %s/%s", name, key)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(token)

    thread = threading.Thread(
        target=_target, name=f"exam-calibrate-{name}-{key}", daemon=True
    )
    thread.start()
    return True


__all__ = [
    "DISTILL_THRESHOLD",
    "MAX_DISTILL_EXAMPLES",
    "MAX_DISTILL_INPUT_CHARS",
    "MAX_PROFILE_BLOCK_CHARS",
    "MAX_PROFILE_CHARS",
    "all_for_item",
    "distill",
    "distill_in_thread",
    "fingerprint",
    "ledger",
    "needs_distill",
    "profile_for",
    "profiles_for_exam",
    "render_distill_input",
    "render_profile_block",
    "set_profile",
]