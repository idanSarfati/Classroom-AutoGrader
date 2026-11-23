"""Re-mark an exam into a preview, then apply it only when the teacher agrees.

Fixing an answer key is normal: a question was too narrow, an explanation the
teacher would accept did not match a keyword. Running the checker again is
cheap and deterministic, but it replaces every score in the class. Before that
happens the teacher sees exactly what would change - per student and per
question - and can cancel, or apply and keep every manual override.

The preview is a dry run: it never writes ``portal_grades``. Only
:func:`apply_preview` does, and only from a payload marked ``ready``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from portal.exams import config as exam_config
from portal.exams import manual as exam_manual
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

RUNNING = "running"
READY = "ready"
FAILED = "failed"

MAX_ROWS = 2000


class RecheckError(RuntimeError):
    """The preview cannot be started, applied or read."""


# ------------------------------------------------------------------ snapshot
def fingerprint(config: exam_config.ExamConfig) -> str:
    """Identify the answer key this preview was computed against.

    Applying a preview built from an older key would write numbers the teacher
    never saw. The hash covers the key file and the exam definition, which is
    everything that decides a score.
    """
    digest = hashlib.sha256()
    digest.update(config.check_path.read_bytes())
    definition = exam_config.EXAM_DIR / f"{config.name}.json"
    if definition.exists():
        digest.update(definition.read_bytes())
    return digest.hexdigest()


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


# --------------------------------------------------------------------- start
def start(db: Session, assignment: Assignment, *, teacher_id: int | None) -> ExamRecheckPreview:
    """Create (or restart) the one preview row of this assignment."""
    config = exam_config.for_assignment(assignment)
    if config is None:
        raise RecheckError("המטלה הזו אינה מבחן עם מפתח תשובות.")
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
    preview.total = 0
    preview.done = 0
    preview.error = ""
    preview.payload_json = ""
    preview.fingerprint = fingerprint(config)
    preview.created_at = utcnow()
    preview.finished_at = None
    db.commit()
    db.refresh(preview)
    return preview


def run_in_thread(session_factory: sessionmaker, preview_id: int) -> None:
    """Mark the class in the background so the request can return."""

    def _target() -> None:
        try:
            run(session_factory, preview_id)
        except Exception:  # noqa: BLE001 - the row carries the failure
            logger.exception("Recheck preview %s failed", preview_id)

    thread = threading.Thread(
        target=_target, name=f"exam-recheck-{preview_id}", daemon=True
    )
    thread.start()


# ----------------------------------------------------------------------- run
def run(session_factory: sessionmaker, preview_id: int) -> str:
    """Compute every row of a preview and store it. Returns the final state."""
    with session_factory() as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        if preview is None:
            return FAILED
        assignment = db.get(Assignment, preview.assignment_id)
        config = exam_config.for_assignment(assignment) if assignment else None
        if assignment is None or config is None:
            return _fail(db, preview, "המטלה או מפתח התשובות לא נמצאו.")
        submissions = db.scalars(
            select(Submission).where(
                Submission.assignment_id == assignment.id,
                Submission.status.in_((SUBMISSION_GRADED, SUBMISSION_FAILED)),
            )
        ).all()
        preview.total = len(submissions)
        preview.done = 0
        db.commit()

        rows: list[dict[str, Any]] = []
        for submission in submissions[:MAX_ROWS]:
            row = _row(db, assignment, config, submission)
            rows.append(row)
            preview.done += 1
            db.commit()

        rows.sort(key=lambda item: (item.get("student_name") or "", item["submission_id"]))
        preview.done = len(rows)
        preview.payload_json = json.dumps(
            {"rows": rows, "generated_at": utcnow().isoformat(timespec="seconds")},
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
) -> dict[str, Any]:
    student = db.get(User, submission.student_id)
    name = (student.display_name if student else "") or (
        student.username if student else ""
    ) or f"#{submission.student_id}"
    row: dict[str, Any] = {
        "submission_id": submission.id,
        "student_id": submission.student_id,
        "student_name": name,
        "revision": submission.revision,
        "old_score": None,
        "new_score": None,
        "maximum": config.total_points,
        "version_label": "",
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
        )
    except Exception as exc:  # noqa: BLE001 - one bad hand-in must not stop the class
        row["error"] = str(exc)[:300]
        return row
    record = report.to_record(name)
    kept = exam_manual.carry_over(old_record, record)
    row["manual_kept"] = kept
    row["new_score"] = int(record.get("score") or 0)
    row["version_label"] = report.version_label
    row["record"] = record

    old_by_key = _questions(old_record)
    new_tasks = record.get("task_evaluations") or []
    changed: list[dict[str, Any]] = []
    for index, question in enumerate((record.get("exam") or {}).get("questions") or []):
        key = str(question.get("key"))
        old = old_by_key.get(key) or {}
        old_earned = int(old.get("earned") or 0)
        new_earned = int(question.get("earned") or 0)
        manual = question.get("manual")
        notes = ""
        if index < len(new_tasks) and isinstance(new_tasks[index], dict):
            notes = str(new_tasks[index].get("notes") or "")
        manual_kept = isinstance(manual, dict)
        if old_earned == new_earned and not manual_kept:
            continue
        changed.append(
            {
                "key": key,
                "title": str(question.get("title") or key),
                "points": int(question.get("points") or 0),
                "old_earned": old_earned,
                "new_earned": new_earned,
                "auto_points": int((manual or {}).get("auto_points") or 0)
                if isinstance(manual, dict)
                else new_earned,
                "old_status": str(old.get("status") or ""),
                "new_status": str(question.get("status") or ""),
                "old_review": bool(old.get("needs_review")),
                "new_review": bool(question.get("needs_review")),
                "notes": notes,
                "manual_kept": manual_kept,
            }
        )
    row["questions"] = changed
    row["changed"] = len(changed)
    return row


# --------------------------------------------------------------------- apply
def payload(preview: ExamRecheckPreview) -> dict[str, Any] | None:
    if preview.state != READY:
        return None
    value = _load_json(preview.payload_json)
    if not value or not isinstance(value.get("rows"), list):
        return None
    # The selective model check (portal.exams.llm_check) shares this row but
    # tags its payload mode="llm". The deterministic apply must never write
    # those rows: only reviewed model papers may be applied, and only by
    # llm_check.apply. Ignoring the other mode keeps the two paths apart.
    if value.get("mode") == "llm":
        return None
    return value


def payload_generated_at(data: dict[str, Any]) -> datetime | None:
    """When the preview rows were computed, or ``None`` when not recorded.

    Both apply paths use this to drop stale rows: if the live grade was
    rewritten after the preview was generated (for example by the automatic
    model pass), applying the old row would regress a newer verdict.
    """
    raw = str(data.get("generated_at") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def row_is_stale(grade: Grade | None, generated: datetime | None) -> bool:
    """True when the live grade is newer than the preview row that targets it.

    ``generated_at`` is stored with whole-second precision, so the comparison
    floors the grade timestamp too: a write inside the same second as the
    preview generation is ambiguous and is treated as not newer (the apply
    keeps its historical behaviour in that edge).
    """
    return (
        grade is not None
        and generated is not None
        and grade.graded_at is not None
        and grade.graded_at.replace(microsecond=0) > generated
    )


def apply_preview(db: Session, preview: ExamRecheckPreview) -> int:
    """Write a ready preview into the grades. Returns the rows applied."""
    data = payload(preview)
    if data is None:
        raise RecheckError("אין השוואה מוכנה להחלה. הריצו בדיקה מחדש.")
    assignment = db.get(Assignment, preview.assignment_id)
    config = exam_config.for_assignment(assignment) if assignment else None
    if config is None:
        raise RecheckError("מפתח התשובות של המבחן לא נמצא.")
    if preview.fingerprint and preview.fingerprint != fingerprint(config):
        raise RecheckError(
            "מפתח התשובות השתנה מאז הבדיקה. הריצו השוואה חדשה לפני ההחלה."
        )
    applied = 0
    generated = payload_generated_at(data)
    for row in data["rows"]:
        if not isinstance(row, dict) or row.get("error"):
            continue
        record = row.get("record")
        if not isinstance(record, dict):
            continue
        submission = db.get(Submission, int(row.get("submission_id") or 0))
        if submission is None or submission.revision != int(row.get("revision") or 0):
            # A student resubmitted between the preview and the apply.
            continue
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        if grade is not None and exam_manual.is_disqualified(
            _load_json(grade.result_json)
        ):
            # A disqualification locks the score; a re-mark must not undo it.
            continue
        if grade is not None and grade.reviewed_at is not None:
            # The teacher signed this hand-in off; a deterministic re-mark
            # keeps both the stored score and the approval badge.
            continue
        if row_is_stale(grade, generated):
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
        grade.configured_model = "exam-checker"
        grade.graded_at = utcnow()
        applied += 1
    db.delete(preview)
    db.commit()
    return applied


def discard(db: Session, preview: ExamRecheckPreview) -> None:
    db.delete(preview)
    db.commit()


__all__ = [
    "FAILED",
    "READY",
    "RUNNING",
    "RecheckError",
    "apply_preview",
    "discard",
    "fingerprint",
    "payload",
    "payload_generated_at",
    "row_is_stale",
    "run",
    "run_in_thread",
    "start",
]
