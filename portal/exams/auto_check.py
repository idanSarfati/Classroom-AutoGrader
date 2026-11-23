"""Automatic, scheduled model pass over exam hand-ins and failed assignments.

The portal marks an exam deterministically the moment it is handed in and, when
the model pass is on (``EXAM_LLM_REVIEW``), judges the open parts in the same
call. A paper can still end the day without a model verdict: the daily provider
quota ran out (stored state ``waiting``), a non-quota error kept the rules
(``failed``), or the paper was marked ``off`` before its answer key grew open
questions. Regular assignments can also end ``failed`` when the grading worker
could not reach a provider. Re-checking all of those by hand is a chore on the
teacher's screens - exactly the chore this module removes.

Every tick (once every 3 hours by default) it picks the hand-ins that still need
work - up to 5 per pass. Exams and quizzes go first (oldest within that group),
then failed regular assignments, so a busy queue never starves the exams:

* exam/quiz papers whose stored model state is ``waiting``/``failed`` are
  re-marked with the model (the deterministic marks are always the fallback)
  and the finished result is written into the same ``Grade`` row the manual
  apply button writes;
* legacy exam/quiz papers with **no stored model state at all** (graded before
  the model pass existed) are re-marked once like any other paper, so an old
  hand-in is never silently left out of the model check;
* exam/quiz papers stored ``off`` are re-marked **once**: if the answer key now
  has open questions they go to the model like any other paper; if they stay
  ``off`` the pass records ``off_confirmed`` and never touches them again;
* regular assignments whose grading failed are re-enqueued for the normal
  grading worker, which owns the model call and the quota/rate-limit retries.

Nothing here publishes results: ``exam_release`` still starts at "held" and the
teacher releases when they are ready.

Safety rules, mirrored from the manual screen:

* a finished paper (``reviewed``, or ``off`` already confirmed) is never
  re-scored;
* a paper the teacher already signed off (``reviewed_at`` set) or disqualified
  is skipped;
* an assignment whose manual re-check preview is actively ``running`` is
  skipped, so the automatic pass never races a check in progress. A ``ready``
  preview left open does NOT freeze the assignment: the pass may re-mark its
  papers, and both apply buttons drop any preview row whose live grade was
  rewritten after the preview was generated (a stale row can never regress a
  newer verdict);
* the pass is a no-op when the model is disabled - there is nothing to judge.

The scheduler is a single daemon thread inside the portal service (like the
grading and import workers), so it runs on the cloud server with or without
anyone at a computer. A CLI entry point (:mod:`portal.scripts.exam_auto_check`)
runs one tick now, for cron-style scheduling or a manual trigger.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from portal.exams import config as exam_config
from portal.exams import llm_review
from portal.exams import manual as exam_manual
from portal.exams import recheck
from portal.exams import service as exam_service
from portal.grading import service as grading_service
from portal.models import (
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    Assignment,
    ExamAutoCheckRun,
    ExamRecheckPreview,
    Grade,
    Submission,
    User,
)
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

# Per-paper model states, mirroring ``checker.ExamReport.llm_state``.
REVIEWED = "reviewed"
WAITING = "waiting"
FAILED = "failed"
OFF = "off"

# A paper the model could not judge yet; these are the papers the pass retries.
NEEDS_MODEL_STATES = frozenset({WAITING, FAILED})

# Results that may be written as final: a real model verdict, or a
# deterministic-only result (model disabled / no open item answered).
APPLICABLE_STATES = frozenset({REVIEWED, OFF})

DEFAULT_INTERVAL_SECONDS = 3 * 60 * 60  # once every 3 hours
DEFAULT_BATCH_SIZE = 5  # the teacher asked for 5 hand-ins per pass
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 10
STARTUP_GRACE_SECONDS = 60

# Retry backoff for papers the model cannot judge yet. A paper that is merely
# waiting for the daily quota is worth retrying soon, but a paper that keeps
# failing is not worth hammering the provider over every hour. Each deferred
# attempt doubles the wait, capped at one day, until a run finally succeeds and
# clears the backoff.
BACKOFF_BASE_SECONDS = 60 * 60
BACKOFF_MAX_SECONDS = 24 * 60 * 60

# How many tick rows the activity log keeps. One row per 3 hours is ~8 a day,
# so 200 rows is about a month of history - enough for the teacher to see what
# happened, small enough that the table never needs manual cleanup.
RUN_KEEP = 200


class AutoCheckError(RuntimeError):
    """The scheduled pass could not start or run."""


# ---------------------------------------------------------------- internals
def _load_json(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def stored_llm_state(grade: Grade | None) -> str:
    """The model state already recorded on a hand-in, or ``""`` when absent."""
    if grade is None:
        return ""
    result = _load_json(grade.result_json)
    if result is None:
        return ""
    exam = result.get("exam")
    return str(exam.get("llm_state") or "") if isinstance(exam, dict) else ""


def _auto_check_meta(grade: Grade) -> dict[str, Any]:
    """The ``auto_check`` metadata block on a stored result, or ``{}``."""
    record = _load_json(grade.result_json)
    meta = record.get("auto_check") if isinstance(record, dict) else None
    return meta if isinstance(meta, dict) else {}


def stored_state_needs_pass(state: str, meta: dict[str, Any]) -> bool:
    """Whether a stored model state still belongs to the automatic pass.

    ``waiting``/``failed`` are retried on their own schedule, ``reviewed`` is
    final, and both ``off`` and the legacy empty state (papers graded before the
    model ever saw them) earn exactly one re-mark - after that the
    ``off_confirmed`` marker keeps them out of the pool.
    """
    return (
        state in NEEDS_MODEL_STATES
        or (state == "" and not meta.get("off_confirmed"))
        or (state == OFF and not meta.get("off_confirmed"))
    )


def paper_needs_model_pass(grade: Grade | None) -> bool:
    """Same rule as the selector, for one already stored grade."""
    if grade is None:
        return False
    return stored_state_needs_pass(stored_llm_state(grade), _auto_check_meta(grade))


def _deferred_until(meta: dict[str, Any]) -> datetime | None:
    """The ``next_at`` instant stored by a deferred attempt, or ``None``."""
    raw = str(meta.get("next_at") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _mark_deferred(
    db: Session, submission: Submission, reason: str = ""
) -> int:
    """Advance the retry backoff on a hand-in the model could not judge yet.

    Writes the attempt count and the next allowed time into the ``auto_check``
    metadata of the stored result (never the score), so ``candidate_papers`` can
    skip a paper that keeps failing instead of retrying it every single hour.
    ``reason`` (a short, already-sanitised provider failure) is stored alongside
    so the activity screen can say *why* this paper is still waiting.
    Returns the new attempt count.
    """
    grade = grading_service.grade_for_revision(
        db, submission.id, submission.revision
    )
    if grade is None:
        return 0
    record = _load_json(grade.result_json)
    if record is None:
        record = {}
    meta = _auto_check_meta(grade)
    attempts = int(meta.get("attempts") or 0) + 1
    delay = min(
        BACKOFF_MAX_SECONDS,
        BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)),
    )
    meta["attempts"] = attempts
    meta["next_at"] = (utcnow() + timedelta(seconds=delay)).isoformat(
        timespec="seconds"
    )
    if reason:
        meta["last_reason"] = reason[:300]
        meta["last_at"] = utcnow().isoformat(timespec="seconds")
    record["auto_check"] = meta
    grade.result_json = json.dumps(record, ensure_ascii=False)
    return attempts


def _failure_since(started: datetime) -> str:
    """The provider failure(s) recorded since ``started``, as one short line.

    The transport journals every failed request (quota, 413/401, timeout). A
    single paper can hit more than one provider on the way to giving up, so the
    distinct reasons are joined in the order they happened - the root cause
    first - and capped to a readable length. Empty when nothing was journalled.
    """
    floor = started.replace(microsecond=0)
    seen: list[str] = []
    # recent_failures() is newest-first; walk it backwards so the joined line
    # reads in the order the failures happened (root cause first).
    for entry in reversed(llm_review.recent_failures()):
        raw = str(entry.get("at") or "")
        try:
            at = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if at < floor:
            continue
        provider = str(entry.get("provider") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        if not reason:
            continue
        line = f"{reason} ({provider})" if provider else reason
        if line not in seen:
            seen.append(line)
    return "; ".join(seen)[:300]


def _confirm_off(db: Session, submission: Submission) -> bool:
    """Record that a re-marked paper truly has nothing for the model to judge.

    An ``off`` paper is re-marked once after a definition change; when it stays
    ``off`` there is no open question to review, so this flag keeps the pass
    from re-marking it every tick. Only the ``auto_check`` metadata is written -
    the stored score, the teacher's review state and any manual overrides are
    untouched. A later successful model write carries a fresh record, which
    clears the flag naturally.
    """
    grade = grading_service.grade_for_revision(
        db, submission.id, submission.revision
    )
    if grade is None:
        return False
    record = _load_json(grade.result_json) or {}
    meta = _auto_check_meta(grade)
    meta["off_confirmed"] = True
    meta.pop("next_at", None)
    record["auto_check"] = meta
    grade.result_json = json.dumps(record, ensure_ascii=False)
    return True


def candidate_papers(
    db: Session, *, limit: int = DEFAULT_BATCH_SIZE
) -> list[tuple[Assignment, Submission]]:
    """The hand-ins that still need the pass, up to ``limit``.

    Exams and quizzes go FIRST (oldest hand-in within that group), then failed
    regular assignments (also oldest first): a model verdict on an exam paper
    is worth more than re-queueing homework, so a busy queue never starves the
    exams.

    * an exam/quiz paper is picked when its stored model state is ``waiting``
      or ``failed``, or ``off`` but never confirmed - a definition fix can turn
      an "off" paper into a model-eligible one, so it earns one re-mark;
    * a legacy exam/quiz paper with no stored model state (graded before the
      model pass existed) is picked too, so old hand-ins are not left out;
    * a regular assignment is picked when its hand-in ended ``failed``; the
      pass re-enqueues it for the normal grading worker.

    Only an assignment whose manual re-check preview is actively running is
    skipped entirely: the automatic pass must not race a check in progress. A
    ready preview left open does not block the pass - the apply buttons drop
    rows whose live grade is newer than the preview, so a stale preview can
    never overwrite a fresh verdict.
    """
    if limit <= 0:
        return []
    assignments = db.scalars(select(Assignment)).all()
    busy = set(
        db.scalars(
            select(ExamRecheckPreview.assignment_id).where(
                ExamRecheckPreview.state == recheck.RUNNING
            )
        ).all()
    )
    picked: list[tuple[bool, datetime, int, Assignment, Submission]] = []
    for assignment in assignments:
        is_exam = bool(assignment.exam_config)
        if is_exam:
            # Existing hand-ins are what matters here, not ``published``: a
            # teacher who hides an exam to protect the next sitting still needs
            # its open answers model-checked, otherwise the marks they want to
            # publish stay incomplete. A hidden exam nobody has sat simply has
            # no hand-ins, so it costs one empty query.
            if assignment.id in busy:
                continue
            if exam_config.for_assignment(assignment) is None:
                continue
        statuses = (
            (SUBMISSION_GRADED, SUBMISSION_FAILED)
            if is_exam
            else (SUBMISSION_FAILED,)
        )
        submissions = db.scalars(
            select(Submission).where(
                Submission.assignment_id == assignment.id,
                Submission.status.in_(statuses),
            )
        ).all()
        for submission in submissions:
            if not is_exam:
                # A failed regular hand-in goes back to the grading worker;
                # there is no stored model state to inspect.
                picked.append(
                    (
                        not is_exam,
                        submission.submitted_at,
                        submission.id,
                        assignment,
                        submission,
                    )
                )
                continue
            grade = grading_service.grade_for_revision(
                db, submission.id, submission.revision
            )
            if grade is None or grade.reviewed_at is not None:
                # No grade yet, or the teacher already signed this paper off.
                continue
            state = stored_llm_state(grade)
            meta = _auto_check_meta(grade)
            if not stored_state_needs_pass(state, meta):
                # ``reviewed`` is final; ``off_confirmed`` already had its one
                # re-mark (also when the record carries no model state at all).
                # An empty state is a legacy paper that never saw the model -
                # it earns a pass like any waiting paper.
                continue
            if exam_manual.is_disqualified(_load_json(grade.result_json)):
                continue
            # A paper that failed a recent run is cooling down (exponential
            # backoff); do not retry it again before its next allowed time.
            deferred_until = _deferred_until(meta)
            if deferred_until is not None and deferred_until > utcnow():
                continue
            picked.append(
                (
                    not is_exam,
                    submission.submitted_at,
                    submission.id,
                    assignment,
                    submission,
                )
            )
    # Exams/quizzes (False) sort before regular assignments (True); within each
    # group the oldest hand-in wins.
    picked.sort(key=lambda row: (row[0], row[1], row[2]))
    return [
        (assignment, submission)
        for _, _, _, assignment, submission in picked[:limit]
    ]


def _apply_result(
    db: Session, submission: Submission, record: dict[str, Any]
) -> bool:
    """Write one finished model result into its ``Grade`` row.

    Mirrors the manual apply: manual overrides are carried over and the exam
    source marker is kept. A teacher sign-off ("סימון ההגשה כנבדקה") belongs to
    the hand-in, not to one grading pass: selection already skips reviewed
    papers, and the write-time re-check below catches a sign-off that landed
    while the model was marking. The approval fields are never touched here.
    """
    grade = grading_service.grade_for_revision(
        db, submission.id, submission.revision
    )
    if grade is None:
        return False
    old_record = _load_json(grade.result_json)
    if exam_manual.is_disqualified(old_record):
        # A human locked this paper; the automatic pass must never overwrite it.
        return False
    if grade.reviewed_at is not None:
        # The teacher signed this paper off after it was selected (the model
        # call takes minutes). Their decision wins over the fresh verdict.
        return False
    exam_manual.carry_over(old_record, record)
    exam_manual.write_record(grade, record)
    grade.configured_model = "exam-checker"
    grade.graded_at = utcnow()
    return True


def tick(
    session_factory: sessionmaker,
    *,
    limit: int = DEFAULT_BATCH_SIZE,
    source: str = "scheduler",
) -> dict[str, int]:
    """One automatic pass: re-mark and write up to ``limit`` hand-ins.

    Exam/quiz papers are re-marked with the model and a finished verdict is
    written into the ``Grade`` row; a failed regular assignment is re-enqueued
    for the grading worker instead. Returns
    ``{"selected", "written", "deferred", "failed"}`` where ``deferred`` counts
    papers that still wait for the model (quota / error) and will be retried on
    the next tick. Re-enqueued assignments and confirmed ``off`` papers appear
    in the per-paper detail (outcomes ``requeued`` / ``off_confirmed``). Every
    pass also appends one :class:`ExamAutoCheckRun` row so the teacher can see
    what the automatic check did and what failed; the log is pruned to the most
    recent ``RUN_KEEP`` rows.
    """
    stats = {"selected": 0, "written": 0, "deferred": 0, "failed": 0}
    if not llm_review.enabled():
        # Without a model there is nothing to judge; deterministic marks are
        # already stored, so a pass would only rewrite identical numbers.
        return stats
    started = utcnow()
    details: list[dict[str, Any]] = []
    with session_factory() as db:
        papers = candidate_papers(db, limit=limit)
        stats["selected"] = len(papers)
        for assignment, submission in papers:
            student = db.get(User, submission.student_id)
            name = (
                (student.display_name if student else "")
                or (student.username if student else "")
                or f"#{submission.student_id}"
            )
            detail: dict[str, Any] = {
                "submission_id": submission.id,
                "assignment_id": assignment.id,
                "student": name,
                "outcome": "failed",
                "llm_state": "",
                "reason": "",
            }
            if not assignment.exam_config:
                # A failed regular assignment goes back to the grading worker:
                # it owns the model call and the quota / rate-limit retries.
                try:
                    grading_service.queue_job(db, submission, force=True)
                except Exception as exc:  # noqa: BLE001 - one paper must not stop the pass
                    logger.exception(
                        "Automatic pass: hand-in %s could not be re-enqueued",
                        submission.id,
                    )
                    stats["failed"] += 1
                    detail["reason"] = str(exc)[:300]
                    details.append(detail)
                    continue
                detail["outcome"] = "requeued"
                details.append(detail)
                continue
            config = exam_config.for_assignment(assignment)
            if config is None:
                stats["failed"] += 1
                detail["reason"] = "exam definition missing"
                details.append(detail)
                continue
            paper_start = utcnow()
            try:
                report = exam_service.mark(
                    db,
                    assignment,
                    submission,
                    student_name=name,
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001 - one paper must not stop the pass
                logger.exception(
                    "Automatic exam pass: hand-in %s failed to re-mark",
                    submission.id,
                )
                stats["failed"] += 1
                reason, _url = llm_review.failure_reason(exc)
                detail["reason"] = reason
                details.append(detail)
                continue
            detail["llm_state"] = report.llm_state
            detail["score"] = report.score
            if report.llm_state == OFF:
                # Re-marked and still nothing open for the model: confirm it
                # once so the pass never re-marks this paper again. The stored
                # deterministic score is left exactly as it was.
                _confirm_off(db, submission)
                detail["outcome"] = "off_confirmed"
                details.append(detail)
                continue
            if report.llm_state not in APPLICABLE_STATES:
                # Still waiting for the provider, or an error kept the rules.
                # Advance the retry backoff so a persistently failing paper is
                # re-tried less often than every single hour.
                reason = _failure_since(paper_start) or (
                    "daily quota exhausted"
                    if report.llm_state == WAITING
                    else "no usable model verdict"
                )
                attempts = _mark_deferred(db, submission, reason)
                stats["deferred"] += 1
                detail["outcome"] = "deferred"
                detail["reason"] = reason
                detail["attempts"] = attempts
                details.append(detail)
                continue
            record = report.to_record(name)
            if _apply_result(db, submission, record):
                # A successful write carries a fresh record without backoff, so
                # the cooldown is effectively cleared for the next attempt.
                stats["written"] += 1
                detail["outcome"] = "written"
            else:
                stats["failed"] += 1
                detail["reason"] = "paper locked by the teacher"
            details.append(detail)
        _record_run(db, started, source, stats, details)
        db.commit()
    return stats


def _record_run(
    db: Session,
    started: datetime,
    source: str,
    stats: dict[str, int],
    details: list[dict[str, Any]],
) -> None:
    """Append one activity-log row and prune the log to ``RUN_KEEP`` rows.

    Written in the same transaction as the marks themselves, so a crash cannot
    leave a run recorded without its results (or vice versa).
    """
    db.add(
        ExamAutoCheckRun(
            ran_at=started,
            source=source,
            selected=stats["selected"],
            written=stats["written"],
            deferred=stats["deferred"],
            failed=stats["failed"],
            detail_json=json.dumps(details, ensure_ascii=False),
        )
    )
    db.flush()
    keep = select(ExamAutoCheckRun.id).order_by(
        ExamAutoCheckRun.ran_at.desc(), ExamAutoCheckRun.id.desc()
    ).limit(RUN_KEEP)
    db.execute(delete(ExamAutoCheckRun).where(ExamAutoCheckRun.id.not_in(keep)))


# ----------------------------------------------------------------- scheduler
class ExamAutoCheckScheduler:
    """Daemon thread that runs the automatic exam pass on an interval.

    Single-worker only, like the other portal workers. The first pass waits a
    short grace so a restart does not spike the provider at boot; afterwards it
    runs once every ``interval_seconds``.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        startup_delay_seconds: int = STARTUP_GRACE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._interval = max(60, int(interval_seconds))
        self._batch = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, int(batch_size)))
        self._startup_delay = max(0, int(startup_delay_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="portal-exam-auto-check", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        # A pass already inside a model call cannot be interrupted; the short
        # join only waits for the loop to notice the stop.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        wait = self._startup_delay
        while not self._stop.wait(wait):
            try:
                stats = tick(self._session_factory, limit=self._batch)
                if stats["selected"]:
                    logger.info(
                        "Automatic exam model pass: %s",
                        ", ".join(f"{key}={value}" for key, value in stats.items()),
                    )
            except Exception:  # noqa: BLE001 - a scheduler must never die
                logger.exception("Automatic exam model pass failed")
            wait = self._interval


__all__ = [
    "APPLICABLE_STATES",
    "AutoCheckError",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_INTERVAL_SECONDS",
    "ExamAutoCheckScheduler",
    "FAILED",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_SIZE",
    "NEEDS_MODEL_STATES",
    "OFF",
    "REVIEWED",
    "RUN_KEEP",
    "STARTUP_GRACE_SECONDS",
    "WAITING",
    "candidate_papers",
    "paper_needs_model_pass",
    "stored_llm_state",
    "stored_state_needs_pass",
    "tick",
]
