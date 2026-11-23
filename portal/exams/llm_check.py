"""Unified hand-in check: pick papers, mark deterministically + with the model.

This is the single "בדיקת הגשות" screen. The teacher chooses a few hand-ins (or
all of them); the deterministic checker always runs - part א (multiple choice)
and the rules fallback for every open part - and, when ``EXAM_LLM_REVIEW`` is on,
the model additionally judges the open parts ב/ג/ד against the same rubric. Every
paper ends in one of four states:

* ``reviewed`` - the model judged every open item; this grade is applicable;
* ``off``      - a finished deterministic-only result: the model is disabled, or
  no open item was answered, so the rules are the final mark; also applicable;
* ``waiting``  - the daily quota ran out, so the deterministic marks were kept
  and the paper waits for a model pass once the quota resets;
* ``failed``   - a non-quota error kept the rules; retryable.

``reviewed`` and ``off`` papers are written into the grades on apply. ``waiting``
and ``failed`` papers stay in the preview so the teacher can retry them later -
exactly like the old classroom evaluator, where some submissions succeeded and
some waited for another run. Nothing here re-grades the whole class by default.

It reuses the one :class:`~portal.models.ExamRecheckPreview` row per assignment,
tagged ``mode="llm"`` in the payload, so no new table or migration is needed. The
deterministic recheck library (:mod:`portal.exams.recheck`) shares the row but
never crosses: each ``payload`` ignores the other's mode, and each apply path
refuses a payload it does not own.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from portal.exams import auto_check
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
    ExamRecheckPreview,
    Grade,
    Submission,
    User,
)
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

MODE = "llm"

# The preview row's lifecycle states are shared with the deterministic recheck.
RUNNING = recheck.RUNNING
READY = recheck.READY
FAILED = recheck.FAILED

# Per-paper model states, mirroring ``checker.ExamReport.llm_state``.
REVIEWED = "reviewed"
WAITING = "waiting"
LLM_FAILED = "failed"
OFF = "off"

STATE_LABELS = {
    REVIEWED: "נבדק במודל",
    WAITING: "ממתין למכסה",
    LLM_FAILED: "נכשל — אפשר לנסות שוב",
    OFF: "נבדק אוטומטית",
}

# A selective run is a subset the teacher picked, not the whole school. The cap
# only guards against a pathological selection; the normal case is a few papers.
MAX_ROWS = 500

# States that still need a model pass and are therefore kept for retry.
_RETRY_STATES = frozenset({WAITING, LLM_FAILED})

# Finished results that apply may write: a full model pass, or a deterministic-
# only mark (model off, or no open item answered). Both are final for the paper.
APPLICABLE_STATES = frozenset({REVIEWED, OFF})


class LLMCheckError(RuntimeError):
    """The selective model check cannot be started, applied or read."""


# ------------------------------------------------------------- live progress
# A re-check grades a paper's open questions in one model pass, but a cold start
# behind load can still take a little while, and a multi-paper run advances per
# paper. The preview row only advances per *paper*, which made a single-paper run
# look frozen at "0 מתוך 1". This process-local store exposes the finer grain -
# which paper, which stage, how long - to the status route the screen polls. The
# portal runs a single uvicorn worker, so an in-memory dict is enough; nothing
# here is persisted or needs a migration. It is purely observational and is
# cleared the moment the run ends.
class _RunProgress:
    """Mutable snapshot of one background re-check, read by the status route."""

    def __init__(self, submission_total: int) -> None:
        self.started = time.monotonic()
        self.submission_total = submission_total
        self.submission_index = 0
        self.student_name = ""
        self.question_index = 0
        self.question_total = 0
        self.question_label = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": int(time.monotonic() - self.started),
            "submission_index": self.submission_index,
            "submission_total": self.submission_total,
            "student_name": self.student_name,
            "question_index": self.question_index,
            "question_total": self.question_total,
            "question_label": self.question_label,
        }


_PROGRESS_LOCK = threading.Lock()
_PROGRESS: dict[int, _RunProgress] = {}


def progress_for(preview_id: int) -> dict[str, Any] | None:
    """The live progress of a running re-check, or ``None`` when not running.

    The lock guards only the dict membership; the snapshot's fields are plain
    attributes whose individual reads are atomic under the GIL. A momentarily
    mixed pair (a new index with the previous label) is harmless for a progress
    display and self-corrects on the next poll, so no per-field locking is needed.
    """
    with _PROGRESS_LOCK:
        prog = _PROGRESS.get(preview_id)
        return prog.snapshot() if prog is not None else None


# ------------------------------------------------------------------ internals
def _load_json(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _questions(record: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(record, dict):
        return {}
    exam = record.get("exam") or {}
    questions = exam.get("questions") if isinstance(exam, dict) else None
    if not isinstance(questions, list):
        return {}
    return {
        str(question.get("key")): question
        for question in questions
        if isinstance(question, dict)
    }


def owns(preview: ExamRecheckPreview | None) -> bool:
    """True when the shared preview row currently holds a model check.

    Read from the payload's ``mode`` tag, which is written at start, so the two
    screens can tell whose run is in progress even before it is ready.
    """
    if preview is None:
        return False
    data = _load_json(preview.payload_json)
    return bool(data) and data.get("mode") == MODE


def payload(preview: ExamRecheckPreview | None) -> dict[str, Any] | None:
    """The ready model-check payload, or ``None`` (wrong mode / not ready)."""
    if preview is None or preview.state != READY or not owns(preview):
        return None
    value = _load_json(preview.payload_json)
    return value if value and isinstance(value.get("rows"), list) else None


# A run lives only in this process's memory (``_PROGRESS``). If the server is
# restarted mid-run the thread dies but the row stays "running" forever, leaving
# the teacher on a frozen progress screen with no way out. The grace window covers
# the brief gap between ``start`` committing the row and ``run`` registering its
# progress, so a freshly started run is never mistaken for an interrupted one.
ORPHAN_GRACE_SECONDS = 60


def interrupted(preview: ExamRecheckPreview | None) -> bool:
    """True when a "running" row's background thread is no longer alive.

    Single-worker only: the progress registry is process-local, exactly like the
    live progress display itself. A running row with no live progress, older than
    the grace window, was orphaned by a restart (or a mid-run crash) and must be
    offered for recovery instead of spinning forever.
    """
    if preview is None or preview.state != RUNNING or not owns(preview):
        return False
    if progress_for(preview.id) is not None:
        return False
    created = preview.created_at
    if created is None:
        return True
    return (utcnow() - created).total_seconds() > ORPHAN_GRACE_SECONDS


def _selected_submissions(
    db: Session, assignment: Assignment, submission_ids: list[int]
) -> list[Submission]:
    """The chosen hand-ins that belong to this exam and were already checked.

    A hand-in the teacher already signed off ("סימון ההגשה כנבדקה") is skipped:
    the model must not overwrite a paper a human already reviewed. This is the
    server-side twin of the selection screen hiding approved rows, so a posted
    id cannot sneak an approved paper back into a run.
    """
    if not submission_ids:
        return []
    wanted = {int(item) for item in submission_ids}
    chosen = db.scalars(
        select(Submission).where(
            Submission.assignment_id == assignment.id,
            Submission.id.in_(wanted),
            Submission.status.in_((SUBMISSION_GRADED, SUBMISSION_FAILED)),
        )
    ).all()
    kept: list[Submission] = []
    for submission in chosen:
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        if grade is not None and grade.reviewed_at is not None:
            continue
        kept.append(submission)
    return kept


# --------------------------------------------------------------------- start
def start(
    db: Session,
    assignment: Assignment,
    submission_ids: list[int],
    *,
    teacher_id: int | None,
) -> ExamRecheckPreview:
    """Create (or restart) the model-check preview for the chosen hand-ins."""
    config = exam_config.for_assignment(assignment)
    if config is None:
        raise LLMCheckError("המטלה הזו אינה מבחן עם מפתח תשובות.")
    submissions = _selected_submissions(db, assignment, submission_ids)
    if not submissions:
        raise LLMCheckError("לא נבחרו הגשות שנבדקו. סמנו לפחות הגשה אחת.")
    ids = [submission.id for submission in submissions]

    preview = db.scalar(
        select(ExamRecheckPreview).where(
            ExamRecheckPreview.assignment_id == assignment.id
        )
    )
    if preview is None:
        preview = ExamRecheckPreview(assignment_id=assignment.id)
        db.add(preview)
    preview.created_by = teacher_id
    preview.state = RUNNING
    preview.total = len(ids)
    preview.done = 0
    preview.error = ""
    preview.fingerprint = recheck.fingerprint(config)
    preview.created_at = utcnow()
    preview.finished_at = None
    # The mode tag is written now so both screens know whose run this is while
    # it is still running; prior_rows is empty for a fresh selection.
    preview.payload_json = json.dumps(
        {"mode": MODE, "submission_ids": ids, "prior_rows": [], "rows": []},
        ensure_ascii=False,
    )
    db.commit()
    db.refresh(preview)
    return preview


def retry(
    db: Session,
    assignment: Assignment,
    preview: ExamRecheckPreview,
    *,
    teacher_id: int | None,
) -> ExamRecheckPreview:
    """Re-run only the papers still waiting or failed, keeping the reviewed ones.

    The reviewed rows are carried forward untouched so a later apply still
    includes them; only the waiting/failed subset is marked again. This is the
    "another time" the quota-exhausted papers wait for.
    """
    config = exam_config.for_assignment(assignment)
    if config is None:
        raise LLMCheckError("המטלה הזו אינה מבחן עם מפתח תשובות.")
    data = payload(preview)
    if data is None:
        raise LLMCheckError("אין בדיקת מודל מוכנה. הריצו בדיקה חדשה.")
    rows = data["rows"]
    waiting_ids = [
        int(row["submission_id"])
        for row in rows
        if isinstance(row, dict)
        and (row.get("llm_state") in _RETRY_STATES or row.get("error"))
        and row.get("submission_id") is not None
    ]
    if not waiting_ids:
        raise LLMCheckError("אין הגשות שממתינות לבדיקה חוזרת.")
    preview.created_by = teacher_id
    preview.state = RUNNING
    preview.total = len(waiting_ids)
    preview.done = 0
    preview.error = ""
    preview.fingerprint = recheck.fingerprint(config)
    preview.created_at = utcnow()
    preview.finished_at = None
    preview.payload_json = json.dumps(
        {
            "mode": MODE,
            "submission_ids": waiting_ids,
            "prior_rows": rows,
            "rows": [],
        },
        ensure_ascii=False,
    )
    db.commit()
    db.refresh(preview)
    return preview


def extend(
    db: Session,
    assignment: Assignment,
    preview: ExamRecheckPreview,
    submission_ids: list[int],
    *,
    teacher_id: int | None,
) -> ExamRecheckPreview:
    """Check additional hand-ins, keeping the rows already in the preview.

    This is the "check more students" path on the results screen: the teacher
    picks a few more papers after a run, and only those are marked again. The
    rows already in the preview are carried forward as ``prior_rows`` so a later
    apply still includes them; a re-picked paper simply replaces its own row.
    """
    config = exam_config.for_assignment(assignment)
    if config is None:
        raise LLMCheckError("המטלה הזו אינה מבחן עם מפתח תשובות.")
    data = payload(preview)
    if data is None:
        raise LLMCheckError("אין בדיקת מודל מוכנה. הריצו בדיקה חדשה.")
    rows = data["rows"]
    submissions = _selected_submissions(db, assignment, submission_ids)
    if not submissions:
        raise LLMCheckError("לא נבחרו הגשות שנבדקו. סמנו לפחות הגשה אחת.")
    ids = [submission.id for submission in submissions]
    preview.created_by = teacher_id
    preview.state = RUNNING
    preview.total = len(ids)
    preview.done = 0
    preview.error = ""
    preview.fingerprint = recheck.fingerprint(config)
    preview.created_at = utcnow()
    preview.finished_at = None
    preview.payload_json = json.dumps(
        {
            "mode": MODE,
            "submission_ids": ids,
            "prior_rows": rows,
            "rows": [],
        },
        ensure_ascii=False,
    )
    db.commit()
    db.refresh(preview)
    return preview


def run_in_thread(session_factory: sessionmaker, preview_id: int) -> None:
    """Mark the selection in the background so the request can return."""

    def _target() -> None:
        try:
            run(session_factory, preview_id)
        except Exception:  # noqa: BLE001 - the row carries the failure
            logger.exception("LLM check preview %s failed", preview_id)

    thread = threading.Thread(
        target=_target, name=f"exam-llm-check-{preview_id}", daemon=True
    )
    thread.start()


# ----------------------------------------------------------------------- run
def run(session_factory: sessionmaker, preview_id: int) -> str:
    """Mark the selected hand-ins and store the preview. Returns final state."""
    with session_factory() as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        if preview is None:
            return FAILED
        assignment = db.get(Assignment, preview.assignment_id)
        config = exam_config.for_assignment(assignment) if assignment else None
        if assignment is None or config is None:
            return _fail(db, preview, "המטלה או מפתח התשובות לא נמצאו.")
        data = _load_json(preview.payload_json) or {}
        ids = [int(item) for item in data.get("submission_ids") or []]
        prior_rows = [
            row for row in data.get("prior_rows") or [] if isinstance(row, dict)
        ]
        submissions = _selected_submissions(db, assignment, ids)
        preview.total = len(submissions)
        preview.done = 0
        db.commit()

        # Pacing only matters when the model is actually called; a deterministic-
        # only run (model off) must not pause between papers.
        enabled = llm_review.enabled()
        batch = submissions[:MAX_ROWS]
        new_rows: list[dict[str, Any]] = []
        # Publish live progress for the status route while this run is in flight,
        # and always clear it afterwards - even if a paper raises - so a finished
        # or failed run never leaves a stale "running" snapshot behind.
        prog = _RunProgress(len(batch))
        with _PROGRESS_LOCK:
            _PROGRESS[preview_id] = prog
        try:
            for index, submission in enumerate(batch, start=1):
                prog.submission_index = index
                new_rows.append(
                    _row(db, assignment, config, submission, progress=prog)
                )
                preview.done += 1
                db.commit()
                # Spread the load across the key pool; the transport stretches this
                # pause when a provider answers 429, so a burst does not park every
                # key at once.
                if enabled:
                    llm_review.pace()
        finally:
            with _PROGRESS_LOCK:
                _PROGRESS.pop(preview_id, None)

        # Merge: a re-run paper replaces its prior row; reviewed papers from an
        # earlier run are kept exactly as they were.
        merged: dict[int, dict[str, Any]] = {
            int(row["submission_id"]): row
            for row in prior_rows
            if row.get("submission_id") is not None
        }
        for row in new_rows:
            merged[int(row["submission_id"])] = row
        rows = sorted(
            merged.values(),
            key=lambda item: (item.get("student_name") or "", item["submission_id"]),
        )
        preview.done = len(rows)
        preview.payload_json = json.dumps(
            {
                "mode": MODE,
                "submission_ids": [int(row["submission_id"]) for row in rows],
                "prior_rows": rows,
                "rows": rows,
                "generated_at": utcnow().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        )
        preview.state = READY
        preview.finished_at = utcnow()
        db.commit()
        return READY


def _fail(db: Session, preview: ExamRecheckPreview, message: str) -> str:
    preview.state = FAILED
    preview.error = message[:1000]
    preview.finished_at = utcnow()
    db.commit()
    return FAILED


def _row(
    db: Session,
    assignment: Assignment,
    config: exam_config.ExamConfig,
    submission: Submission,
    *,
    progress: _RunProgress | None = None,
) -> dict[str, Any]:
    """Mark one hand-in with the model and build its old→new preview row."""
    student = db.get(User, submission.student_id)
    name = (student.display_name if student else "") or (
        student.username if student else ""
    ) or f"#{submission.student_id}"
    if progress is not None:
        progress.student_name = name
        progress.question_index = 0
        progress.question_total = 0
        progress.question_label = ""

    def _tick(question_index: int, question_total: int, label: str) -> None:
        if progress is not None:
            progress.question_index = question_index
            progress.question_total = question_total
            progress.question_label = label

    row: dict[str, Any] = {
        "submission_id": submission.id,
        "student_id": submission.student_id,
        "student_name": name,
        "revision": submission.revision,
        "old_score": None,
        "new_score": None,
        "maximum": config.total_points,
        "version_label": "",
        "llm_state": OFF,
        "questions": [],
        "changed": 0,
        "manual_kept": 0,
        "error": "",
    }
    grade = grading_service.grade_for_revision(
        db, submission.id, submission.revision
    )
    old_record = _load_json(grade.result_json) if grade is not None else None
    if isinstance(old_record, dict):
        row["old_score"] = int(old_record.get("score") or 0)
    try:
        report = exam_service.mark(
            db,
            assignment,
            submission,
            student_name=name,
            config=config,
            progress=_tick if progress is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 - one bad hand-in must not stop the rest
        row["error"] = str(exc)[:300]
        row["llm_state"] = LLM_FAILED
        return row
    record = report.to_record(name)
    kept = exam_manual.carry_over(old_record, record)
    row["manual_kept"] = kept
    row["new_score"] = int(record.get("score") or 0)
    row["version_label"] = report.version_label
    row["llm_state"] = report.llm_state
    row["record"] = record

    changed = _changed_questions(old_record, record)
    row["questions"] = changed
    row["changed"] = len(changed)
    return row


def _changed_questions(
    old_record: dict[str, Any] | None, record: dict[str, Any]
) -> list[dict[str, Any]]:
    """The per-question old→new diff shown on the results screen.

    A question appears when its score moved, or when the teacher's manual
    override is in play (so the screen always shows a hand-adjusted question).
    Shared by the initial mark and by a later manual edit of the preview row.
    """
    old_by_key = _questions(old_record)
    new_tasks = record.get("task_evaluations") or []
    changed: list[dict[str, Any]] = []
    for index, question in enumerate((record.get("exam") or {}).get("questions") or []):
        key = str(question.get("key"))
        old = old_by_key.get(key) or {}
        old_earned = int(old.get("earned") or 0)
        new_earned = int(question.get("earned") or 0)
        manual = question.get("manual")
        parts = [
            part for part in question.get("parts") or [] if isinstance(part, dict)
        ]
        part_manuals = [
            part for part in parts if isinstance(part.get("manual"), dict)
        ]
        notes = ""
        if index < len(new_tasks) and isinstance(new_tasks[index], dict):
            notes = str(new_tasks[index].get("notes") or "")
        manual_kept = isinstance(manual, dict) or bool(part_manuals)
        if old_earned == new_earned and not manual_kept:
            continue
        if isinstance(manual, dict):
            auto_points = int(manual.get("auto_points") or 0)
        elif part_manuals:
            # A part override: the question's automatic total is the sum of the
            # parts' automatic scores (an overridden part keeps its snapshot,
            # an untouched part keeps its current earned).
            auto_points = sum(
                int(part["manual"].get("auto_points") or 0)
                if isinstance(part.get("manual"), dict)
                else int(part.get("earned") or 0)
                for part in parts
            )
        else:
            auto_points = new_earned
        changed.append(
            {
                "key": key,
                "title": str(question.get("title") or key),
                "points": int(question.get("points") or 0),
                "old_earned": old_earned,
                "new_earned": new_earned,
                "auto_points": auto_points,
                "old_status": str(old.get("status") or ""),
                "new_status": str(question.get("status") or ""),
                "old_review": bool(old.get("needs_review")),
                "new_review": bool(question.get("needs_review")),
                "notes": notes,
                "manual_kept": manual_kept,
            }
        )
    return changed


def _has_manual(question: dict[str, Any]) -> bool:
    """True when the question or any of its scored parts was hand-adjusted."""
    if isinstance(question.get("manual"), dict):
        return True
    return any(
        isinstance(part, dict) and isinstance(part.get("manual"), dict)
        for part in question.get("parts") or []
    )


def _manual_count(record: dict[str, Any]) -> int:
    return sum(
        1
        for question in (record.get("exam") or {}).get("questions") or []
        if isinstance(question, dict) and _has_manual(question)
    )


def _find_row(rows: list[dict[str, Any]], submission_id: int) -> dict[str, Any] | None:
    for row in rows:
        if isinstance(row, dict) and int(row.get("submission_id") or 0) == int(submission_id):
            return row
    return None


def row_for(
    preview: ExamRecheckPreview | None, submission_id: int
) -> dict[str, Any] | None:
    """The ready model-check row for one hand-in, or ``None``."""
    data = payload(preview)
    if data is None:
        return None
    return _find_row(data["rows"], submission_id)


def preview_record(
    preview: ExamRecheckPreview | None, submission_id: int
) -> dict[str, Any] | None:
    """The latest model result record for one hand-in, for the review view.

    This is what the "צפייה בטופס ההגשה" page shows while a model check is ready
    but not yet applied: the freshest verdict, not the older stored grade.
    """
    row = row_for(preview, submission_id)
    if row is None:
        return None
    record = row.get("record")
    return record if isinstance(record, dict) else None


def _preview_generated_at(
    preview: ExamRecheckPreview | None, data: dict[str, Any] | None = None
) -> datetime | None:
    """When this preview's rows were computed, or ``None`` when unusable."""
    if data is None:
        data = payload(preview)
    if data is None:
        return None
    return recheck.payload_generated_at(data)


def preview_is_stale(
    db: Session, preview: ExamRecheckPreview | None, submission_id: int
) -> bool:
    """True when one hand-in's LIVE grade is newer than its preview row.

    The automatic model pass can re-mark a paper while a manual preview is still
    open (only a *running* manual check pauses it now). Both apply paths drop
    exactly those rows, because writing the older preview would regress the
    newer verdict - so the screens must stop presenting the old row as the last
    word: the review page shows the newer live grade instead, and the check
    screen marks the row as already overtaken.
    """
    generated = _preview_generated_at(preview)
    if generated is None:
        return False
    submission = db.get(Submission, int(submission_id or 0))
    if submission is None:
        return False
    grade = grading_service.grade_for_revision(db, submission.id, submission.revision)
    return recheck.row_is_stale(grade, generated)


def stale_submission_ids(
    db: Session, preview: ExamRecheckPreview | None
) -> set[int]:
    """Every row of this preview that a live newer grade already overtook.

    Same rule as :func:`preview_is_stale` and the apply paths, evaluated once
    for the whole payload so the check screen can tag the rows it lists.
    """
    data = payload(preview)
    generated = _preview_generated_at(preview, data)
    if generated is None or data is None:
        return set()
    stale: set[int] = set()
    for row in data["rows"]:
        if not isinstance(row, dict):
            continue
        submission_id = int(row.get("submission_id") or 0)
        if not submission_id:
            continue
        submission = db.get(Submission, submission_id)
        if submission is None:
            continue
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        if recheck.row_is_stale(grade, generated):
            stale.add(submission_id)
    return stale


def _row_apply_outcome(
    db: Session, row: dict[str, Any], generated: datetime | None
) -> str:
    """What :func:`apply` would do with this preview row *right now*.

    ``"write"`` - the verdict is still unwritten and would land on the live
    grade. ``"drop"`` - the row is no longer this preview's business: the paper
    was resubmitted, signed off, disqualified, or re-marked by the automatic pass
    after ``generated``. ``"keep"`` - the apply leaves the row for another model
    pass (it never got a verdict, or the row carries no record to write).

    The order of the tests mirrors :func:`apply` exactly, so a screen that counts
    "what will change" cannot promise a write the apply then silently drops.
    """
    state = row.get("llm_state")
    if state in _RETRY_STATES or row.get("error"):
        return "keep"
    if state not in APPLICABLE_STATES:
        return "drop"
    if not isinstance(row.get("record"), dict):
        return "keep"  # there is no verdict here to write
    submission = db.get(Submission, int(row.get("submission_id") or 0))
    if submission is None or submission.revision != int(row.get("revision") or 0):
        return "drop"  # a newer hand-in made this row meaningless
    grade = grading_service.grade_for_revision(db, submission.id, submission.revision)
    if grade is not None and exam_manual.is_disqualified(
        _load_json(grade.result_json)
    ):
        return "drop"  # the teacher locked this paper to a fixed score
    if grade is not None and grade.reviewed_at is not None:
        return "drop"  # a human already signed it off
    if recheck.row_is_stale(grade, generated):
        return "drop"  # the live verdict is newer than this row
    return "write"


def preview_workload(
    db: Session, preview: ExamRecheckPreview | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a manual check into what it can still do right now.

    Returns ``(to_write, still_pending)`` in preview order. ``to_write`` are the
    rows whose verdict an apply would land on the live grade today;
    ``still_pending`` are the rows this screen is genuinely the next step for -
    the model never finished judging the paper and no newer verdict already
    answered it. Everything else (approved, disqualified, resubmitted, overtaken
    by a newer live verdict, or a paper whose stored mark already carries a
    model verdict) is work this preview no longer represents.

    Read-only. The results screen counts from here, so it can never advertise a
    change the apply would then silently skip, and a check whose two lists are
    both empty has nothing left to show.
    """
    data = payload(preview)
    if data is None:
        return [], []
    generated = recheck.payload_generated_at(data)
    to_write: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for row in data["rows"]:
        if not isinstance(row, dict):
            continue
        outcome = _row_apply_outcome(db, row, generated)
        if outcome == "write":
            to_write.append(row)
        elif outcome == "keep" and not _kept_row_is_covered(db, row, generated):
            pending.append(row)
    return to_write, pending


def preview_is_exhausted(db: Session, preview: ExamRecheckPreview) -> bool:
    """True when a ready manual check can no longer change anything.

    A finished preview is only worth showing while it still holds work. Once
    every row resolves to "nothing to write" - already applied elsewhere, signed
    off by the teacher, resubmitted, or overtaken by a newer live verdict - the
    results card, the per-student table and the stale-row notes are all dead
    weight, and the screen should look like the same screen in a class that
    never ran a manual check.
    """
    to_write, pending = preview_workload(db, preview)
    return not to_write and not pending


def _kept_row_is_covered(
    db: Session, row: dict[str, Any], generated: datetime | None
) -> bool:
    """True when a row the apply would keep is already answered by a live verdict.

    A waiting/failed row normally *is* this screen's business: "ניסיון חוזר" is
    how a teacher gets a quota-starved paper re-marked on demand. But once the
    automatic pass has rewritten the live grade after this preview was computed
    - or the stored mark already carries a model verdict - re-running the manual
    check would only re-do work that is already on the paper, and the row is as
    obsolete as a dropped one.
    """
    submission = db.get(Submission, int(row.get("submission_id") or 0))
    if submission is None or submission.revision != int(row.get("revision") or 0):
        return True  # the hand-in this row pointed at is gone: it can never be work
    grade = grading_service.grade_for_revision(db, submission.id, submission.revision)
    if grade is None:
        return False  # no stored mark at all: this screen is still its only run
    if recheck.row_is_stale(grade, generated):
        return True  # a newer verdict - the automatic pass's - already landed
    return auto_check.stored_llm_state(grade) == REVIEWED


def retire_if_exhausted(db: Session, preview: ExamRecheckPreview) -> bool:
    """Delete a finished manual check that has nothing left to do.

    Returns True when the preview was removed. No grade is written or touched:
    this only clears the leftover rows of a check whose every verdict was already
    overtaken, already approved, or is already the automatic pass's job.
    """
    if preview.state != READY or not owns(preview):
        return False
    if not preview_is_exhausted(db, preview):
        return False
    db.delete(preview)
    db.commit()
    return True


def _persist_rows(
    db: Session, preview: ExamRecheckPreview, data: dict[str, Any]
) -> None:
    """Write the (possibly edited) rows back into the shared preview payload."""
    rows = data["rows"]
    preview.payload_json = json.dumps(
        {
            "mode": MODE,
            "submission_ids": [
                int(row["submission_id"])
                for row in rows
                if row.get("submission_id") is not None
            ],
            "prior_rows": rows,
            "rows": rows,
            "generated_at": data.get("generated_at")
            or utcnow().isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )
    db.commit()


def _edit_row(
    db: Session,
    preview: ExamRecheckPreview,
    submission_id: int,
    item_key: str,
    *,
    clear: bool = False,
    part_key: str | None = None,
    points: int | None = None,
    note: str = "",
    teacher_id: int | None = None,
    teacher_name: str = "",
    full_credit: bool = False,
) -> dict[str, Any]:
    """Apply (or undo) a manual override on one preview row and persist it.

    The teacher adjusts the model's *latest* verdict while it is still unapplied,
    so the review page, the diff and a later apply all read one consistent record.
    With ``part_key`` the override lands on one scored part of a multi-part
    question (the question total becomes the sum of its parts); without it, on
    the whole question. Returns the info dict from :mod:`portal.exams.manual`
    (the automatic score the teacher overrode, the new score, the answer
    snapshot) for feedback capture.
    """
    data = payload(preview)
    if data is None:
        raise LLMCheckError("אין בדיקת מודל מוכנה. הריצו בדיקה חדשה.")
    row = _find_row(data["rows"], submission_id)
    if row is None:
        raise LLMCheckError("ההגשה הזו אינה בבדיקת המודל הנוכחית.")
    record = row.get("record")
    if not isinstance(record, dict):
        raise LLMCheckError("תוצאת הבדיקה של ההגשה הזו חסרה.")
    if clear:
        if part_key:
            info = exam_manual.clear_part_record(
                record, item_key, part_key,
                teacher_id=teacher_id, teacher_name=teacher_name,
            )
        else:
            info = exam_manual.clear_record(
                record, item_key, teacher_id=teacher_id, teacher_name=teacher_name
            )
    elif part_key:
        info = exam_manual.adjust_part_record(
            record, item_key, part_key,
            points=points, note=note,
            teacher_id=teacher_id, teacher_name=teacher_name,
            full_credit=full_credit,
        )
    else:
        info = exam_manual.adjust_record(
            record,
            item_key,
            points=points,
            note=note,
            teacher_id=teacher_id,
            teacher_name=teacher_name,
            full_credit=full_credit,
        )
    exam_manual.refresh(record)
    grade = grading_service.grade_for_revision(
        db, submission_id, int(row.get("revision") or 0)
    )
    old_record = _load_json(grade.result_json) if grade is not None else None
    row["new_score"] = int(record.get("score") or 0)
    changed = _changed_questions(old_record, record)
    row["questions"] = changed
    row["changed"] = len(changed)
    row["manual_kept"] = _manual_count(record)
    _persist_rows(db, preview, data)
    return info


def adjust_row(
    db: Session,
    preview: ExamRecheckPreview,
    submission_id: int,
    item_key: str,
    *,
    points: int | None,
    part_key: str | None = None,
    note: str = "",
    teacher_id: int | None = None,
    teacher_name: str = "",
    full_credit: bool = False,
) -> dict[str, Any]:
    """Override a question's (or one part's) points on a ready preview row."""
    return _edit_row(
        db,
        preview,
        submission_id,
        item_key,
        points=points,
        part_key=part_key,
        note=note,
        teacher_id=teacher_id,
        teacher_name=teacher_name,
        full_credit=full_credit,
    )


def clear_row(
    db: Session,
    preview: ExamRecheckPreview,
    submission_id: int,
    item_key: str,
    *,
    part_key: str | None = None,
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> dict[str, Any]:
    """Undo a manual override on a ready preview row (see :func:`_edit_row`)."""
    return _edit_row(
        db,
        preview,
        submission_id,
        item_key,
        clear=True,
        part_key=part_key,
        teacher_id=teacher_id,
        teacher_name=teacher_name,
    )


# --------------------------------------------------------------------- apply
def apply(db: Session, preview: ExamRecheckPreview) -> int:
    """Write the finished papers (reviewed or deterministic-only); keep the rest.

    Returns the number of grades written. ``reviewed`` and ``off`` papers are
    final and are applied; ``waiting``/``failed`` papers stay in the preview so
    the teacher can retry them once the quota resets. The preview is deleted
    only when nothing is left to check. Manual overrides are kept, and a paper
    whose revision changed since the preview is skipped. A row whose live grade
    was rewritten after the preview was generated (teacher sign-off,
    disqualification, or a newer automatic verdict) is dropped as stale.
    """
    data = payload(preview)
    if data is None:
        raise LLMCheckError("אין בדיקת מודל מוכנה להחלה. הריצו בדיקה.")
    assignment = db.get(Assignment, preview.assignment_id)
    config = exam_config.for_assignment(assignment) if assignment else None
    if config is None:
        raise LLMCheckError("מפתח התשובות של המבחן לא נמצא.")
    if preview.fingerprint and preview.fingerprint != recheck.fingerprint(config):
        raise LLMCheckError(
            "מפתח התשובות השתנה מאז הבדיקה. הריצו בדיקה חדשה לפני ההחלה."
        )
    applied = 0
    remaining: list[dict[str, Any]] = []
    generated = recheck.payload_generated_at(data)
    for row in data["rows"]:
        if not isinstance(row, dict):
            continue
        state = row.get("llm_state")
        if state in _RETRY_STATES or row.get("error"):
            # Still waiting for a model pass - keep it for the next retry.
            remaining.append(row)
            continue
        if state not in APPLICABLE_STATES:
            # An unknown state is never written. ``off`` is a finished
            # deterministic-only result, so it is applied like ``reviewed``.
            continue
        record = row.get("record")
        if not isinstance(record, dict):
            remaining.append(row)
            continue
        submission = db.get(Submission, int(row.get("submission_id") or 0))
        if submission is None or submission.revision != int(row.get("revision") or 0):
            # The student resubmitted between the preview and the apply; this
            # row no longer matches the live hand-in, so it is dropped.
            continue
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        if grade is not None and exam_manual.is_disqualified(
            _load_json(grade.result_json)
        ):
            # The teacher locked this hand-in to a fixed score (disqualification).
            # An apply must never overwrite that decision, so the row is dropped.
            continue
        if grade is not None and grade.reviewed_at is not None:
            # The teacher signed this hand-in off after the preview was made
            # ("סימון ההגשה כנבדקה"). A human already judged it: the apply keeps
            # both the stored score and the approval, and drops the stale row.
            continue
        if recheck.row_is_stale(grade, generated):
            # The live grade was rewritten after this preview was generated
            # (e.g. by the automatic model pass). Applying the old row would
            # regress a newer verdict, so it is dropped.
            continue
        if grade is None:
            grade = Grade(
                submission_id=submission.id,
                revision=submission.revision,
                late_penalty_points=0,
            )
            db.add(grade)
        exam_manual.write_record(grade, record)
        # Keep the exam marker so the review screen still treats this as an
        # exam hand-in (per-question parts, manual override). The model state
        # travels inside result_json["exam"]["llm_state"]. The approval fields
        # are deliberately untouched: a sign-off survives every automatic write
        # on the same revision (only a new student revision starts unapproved).
        grade.configured_model = "exam-checker"
        grade.graded_at = utcnow()
        applied += 1

    if remaining:
        preview.state = READY
        preview.total = len(remaining)
        preview.done = len(remaining)
        preview.payload_json = json.dumps(
            {
                "mode": MODE,
                "submission_ids": [
                    int(row["submission_id"])
                    for row in remaining
                    if row.get("submission_id") is not None
                ],
                "prior_rows": remaining,
                "rows": remaining,
                "generated_at": utcnow().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        )
    else:
        db.delete(preview)
    db.commit()
    return applied


def discard(db: Session, preview: ExamRecheckPreview) -> None:
    db.delete(preview)
    db.commit()


__all__ = [
    "APPLICABLE_STATES",
    "FAILED",
    "LLM_FAILED",
    "MODE",
    "OFF",
    "ORPHAN_GRACE_SECONDS",
    "READY",
    "REVIEWED",
    "RUNNING",
    "STATE_LABELS",
    "WAITING",
    "LLMCheckError",
    "adjust_row",
    "apply",
    "clear_row",
    "discard",
    "extend",
    "interrupted",
    "owns",
    "payload",
    "preview_is_exhausted",
    "preview_is_stale",
    "preview_record",
    "preview_workload",
    "retire_if_exhausted",
    "retry",
    "row_for",
    "run",
    "run_in_thread",
    "start",
    "stale_submission_ids",
]