"""Read-only aggregation for the teacher's automatic-check activity screen.

The automatic exam pass (:mod:`portal.exams.auto_check`) works quietly in the
background. This module turns its two traces - the append-only run log
(``portal_exam_auto_check_runs``) and the per-paper ``auto_check`` metadata on
each stored grade - into one self-explanatory view model:

* what the pass is (on/off, interval, batch) and when it last ran / runs next;
* per exam assignment: how many papers are finished, waiting, failed or legacy,
  whether the pass is paused because a manual re-check is running right now,
  and the list of papers still pending with their attempt count, next retry
  time and a plain-Hebrew reason;
* regular assignments whose grading failed and the pass will re-enqueue;
* the recent run history with a per-paper outcome.

Nothing here writes, re-marks or calls a model. Provider failure reasons are
already sanitised by the transport (status + provider name only, never a key or
a URL); this module only translates them into the teacher's language.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portal.exams import auto_check, llm_review, recheck
from portal.exams import config as exam_config
from portal.grading import service as grading_service
from portal.models import (
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    Assignment,
    ClassGroup,
    ExamAutoCheckRun,
    ExamRecheckPreview,
    Submission,
    User,
)
from portal.timeutil import format_local, utcnow

# How many past runs the screen lists. The log keeps more (RUN_KEEP); the screen
# shows the recent window so a phone page stays short.
RUNS_SHOWN = 30

# The flat "last day" report: one row per paper the pass touched, newest first.
RECENT_HOURS = 24
RECENT_MAX_ROWS = 200

# Re-exported so the screen can state the log's retention without importing the
# pass itself.
RUN_KEEP = auto_check.RUN_KEEP

# Per-paper model states, as stored on the grade (checker.ExamReport.llm_state).
STATE_LABELS = {
    "reviewed": "נבדק במודל",
    "waiting": "ממתין למכסה יומית",
    "failed": "נכשל — ינוסה שוב",
    "off": "אין שאלות פתוחות",
    "": "רישום ישן — ייבדק במודל",
}


def reason_hebrew(raw: str) -> str:
    """Translate a sanitised provider failure into plain Hebrew.

    The raw string is one or more ``"reason (Provider)"`` parts joined by
    ``"; "`` (root cause first). Each part is mapped independently; an
    unmapped part is shown verbatim - it is already key-free and short.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    translated: list[str] = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        provider = ""
        body = part
        if part.endswith(")") and "(" in part:
            body, _, tail = part.rpartition("(")
            provider = tail[:-1].strip()
            body = body.strip()
        low = body.lower()
        if "413" in low:
            he = "הבקשה גדולה מדי לספק — התשובות הארוכות חורגות ממגבלת הגודל (413)"
        elif "401" in low:
            he = "מפתח הגישה של הספק אינו תקף (401)"
        elif "403" in low:
            he = "הגישה לספק נדחתה (403)"
        elif "429" in low:
            he = "יותר מדי בקשות לספק בפרק הזמן (429)"
        elif "quota" in low:
            he = "המכסה היומית של הספק מוצתה"
        elif "prompt too large" in low:
            he = "הפרומפט גדול מהמותר בבדיקה המקומית"
        elif "timed out" in low or "timeout" in low:
            he = "תם הזמן שהוקצב לבקשה"
        elif "network error" in low:
            he = "תקלת רשת בגישה לספק"
        elif "not usable json" in low or "no usable model verdict" in low:
            he = "המודל לא החזיר פסק דין תקין"
        elif "exam definition missing" in low:
            he = "הגדרת המבחן חסרה"
        elif "paper locked" in low:
            he = "ההגשה נעולה על ידי המורה"
        else:
            he = body
        translated.append(f"{he} ({provider})" if provider else he)
    return " · ".join(translated)


def _load_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _meta(grade: Any) -> dict[str, Any]:
    meta = _load_json(grade.result_json).get("auto_check")
    return meta if isinstance(meta, dict) else {}


def _parse_at(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None


def _teacher_assignments(db: Session, teacher_id: int) -> list[Assignment]:
    """Every exam this teacher owns, published or hidden.

    An exam kept as a draft so a later class cannot see it still has hand-ins
    that were checked, and the teacher has to be able to read that activity -
    hiding the paper is not a reason to hide the report.
    """
    return list(
        db.scalars(
            select(Assignment)
            .join(ClassGroup, Assignment.class_id == ClassGroup.id)
            .where(
                ClassGroup.teacher_id == teacher_id,
                Assignment.exam_config != "",
            )
            .order_by(Assignment.id)
        ).all()
    )


def _scan_assignment(
    db: Session, assignment: Assignment, group: ClassGroup | None, tz: str
) -> dict[str, Any]:
    """Classify every graded hand-in of one exam assignment (read-only)."""
    preview = db.scalar(
        select(ExamRecheckPreview).where(
            ExamRecheckPreview.assignment_id == assignment.id
        )
    )
    # Only an actively running manual re-check pauses the automatic pass for
    # this assignment. A finished preview left unapplied (``ready``) used to
    # freeze the assignment for days and starve exam papers of model quota; the
    # apply paths now drop preview rows that a newer verdict has already
    # overwritten, so an open preview is no longer a reason to stop.
    busy = preview is not None and preview.state == recheck.RUNNING
    counts = {
        "reviewed": 0,
        "waiting": 0,
        "failed": 0,
        "off": 0,
        "legacy": 0,
        "signed": 0,
        "disqualified": 0,
    }
    pending: list[dict[str, Any]] = []
    submissions = db.scalars(
        select(Submission).where(
            Submission.assignment_id == assignment.id,
            Submission.status.in_((SUBMISSION_GRADED, SUBMISSION_FAILED)),
        )
    ).all()
    now = utcnow()
    for submission in submissions:
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        if grade is None:
            continue
        record = _load_json(grade.result_json)
        exam = record.get("exam")
        state = (
            str(exam.get("llm_state") or "") if isinstance(exam, dict) else ""
        )
        disqualified = bool(record.get("disqualification"))
        signed = grade.reviewed_at is not None
        if disqualified:
            counts["disqualified"] += 1
            continue
        if signed:
            counts["signed"] += 1
        if state in ("reviewed", "waiting", "failed", "off"):
            counts[state] += 1
        else:
            counts["legacy"] += 1
        meta = _meta(grade)
        if signed or state == "reviewed":
            continue
        if state in ("", "off") and meta.get("off_confirmed"):
            # The pass already re-marked this paper and confirmed there is
            # nothing open for the model; it is finished, not pending. A paper
            # whose record carries no model state at all gets the same flag,
            # because a legacy re-mark that came back "off" keeps the record
            # shape it had.
            continue
        # Waiting, failed, unconfirmed-off or legacy and not signed off: still
        # needs a model verdict, and this is exactly the set the automatic pass
        # selects from - waiting/failed are retried, an unconfirmed-off paper
        # and a legacy paper (graded before the model pass existed) each get
        # their one automatic re-mark.
        student = db.get(User, submission.student_id)
        name = (
            (student.display_name if student else "")
            or (student.username if student else "")
            or f"#{submission.student_id}"
        )
        next_at = _parse_at(meta.get("next_at"))
        cooling = next_at is not None and next_at > now
        pending.append(
            {
                "submission_id": submission.id,
                "student_name": name,
                "score": grade.score,
                "state": state,
                "state_label": (
                    "ממתין לבדיקת מודל"
                    if state == "off"
                    else STATE_LABELS.get(state, STATE_LABELS[""])
                ),
                "legacy": state == "",
                "off_unconfirmed": state == "off",
                "attempts": int(meta.get("attempts") or 0),
                "next_at_label": (
                    format_local(next_at, tz) if next_at is not None else ""
                ),
                "cooling": cooling,
                "reason_he": reason_hebrew(str(meta.get("last_reason") or "")),
            }
        )
    pending.sort(key=lambda row: (row["legacy"], row["student_name"]))
    return {
        "id": assignment.id,
        "title": assignment.title,
        "class_name": group.name if group else "",
        "exam": assignment.exam_config,
        "busy": busy,
        "busy_state": preview.state if preview is not None else "",
        "counts": counts,
        "pending": pending,
        "pending_count": len(pending),
    }


def _failed_regular_assignments(
    db: Session, teacher_id: int, tz: str
) -> list[dict[str, Any]]:
    """Regular (non-exam) hand-ins whose grading failed, oldest first.

    These are the assignments the automatic pass re-enqueues for the grading
    worker. Read-only: the last job's error is translated into plain Hebrew.
    """
    rows = db.execute(
        select(Submission, Assignment, ClassGroup, User)
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .join(ClassGroup, Assignment.class_id == ClassGroup.id)
        .outerjoin(User, Submission.student_id == User.id)
        .where(
            ClassGroup.teacher_id == teacher_id,
            Assignment.exam_config == "",
            Submission.status == SUBMISSION_FAILED,
        )
        .order_by(Submission.submitted_at.asc(), Submission.id.asc())
    ).all()
    result: list[dict[str, Any]] = []
    for submission, assignment, group, student in rows:
        job = grading_service.latest_job(db, submission)
        result.append(
            {
                "submission_id": submission.id,
                "assignment_id": assignment.id,
                "assignment_title": assignment.title,
                "class_name": group.name,
                "student_name": (
                    (student.display_name if student else "")
                    or (student.username if student else "")
                    or f"#{submission.student_id}"
                ),
                "submitted_label": format_local(submission.submitted_at, tz),
                "reason_he": reason_hebrew(str(job.error or "")) if job else "",
            }
        )
    return result


def _kind_label(assignment: Assignment) -> str:
    """מטלה / מבחן / בוחן — from the exam definition's own title."""
    if not assignment.exam_config:
        return "מטלה"
    config = exam_config.for_assignment(assignment)
    title = config.title if config is not None else ""
    return "בוחן" if "בוחן" in title else "מבחן"


def _requeued_outcome(
    db: Session, submission_id: Any
) -> tuple[str, str, str, Any]:
    """The real fate of a homework the pass sent back to the grading worker.

    ``requeued`` only records the hand-over; whether the model check actually
    succeeded is decided later by the grading job. Read that job so the report
    says הצליח / נכשל / בבדיקה instead of stopping at "נשלח מחדש".
    Returns ``(verdict, label, reason_he, score)``.
    """
    submission = (
        db.get(Submission, submission_id) if submission_id else None
    )
    job = (
        grading_service.latest_job(db, submission)
        if submission is not None
        else None
    )
    if job is None:
        return "pending", "נשלח לבדיקה מחדש", "", None
    if job.state == "graded":
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        return (
            "ok",
            "הצליח — נבדק ונשמר",
            "",
            grade.score if grade is not None else None,
        )
    if job.state == "failed":
        return "failed", "נכשל", reason_hebrew(str(job.error or "")), None
    if job.state == "deferred":
        return (
            "pending",
            "ממתין לניסיון חוזר",
            reason_hebrew(str(job.error or "")),
            None,
        )
    return "pending", "בבדיקה כרגע", "", None


def recent_report(
    db: Session, teacher_id: int, tz: str, *, hours: int = RECENT_HOURS
) -> dict[str, Any]:
    """One flat row per paper the automatic pass touched in the last ``hours``.

    The teacher asked for a small table: class, assignment/exam/quiz, student,
    and whether the model check succeeded — with the reason when it did not.
    Rows come from the same run log as the history below, newest first; only
    this teacher's assignments are listed. Read-only, like the whole screen.
    """
    now = utcnow()
    since = now - timedelta(hours=hours)
    runs = list(
        db.scalars(
            select(ExamAutoCheckRun)
            .where(ExamAutoCheckRun.ran_at >= since)
            .order_by(ExamAutoCheckRun.ran_at.desc(), ExamAutoCheckRun.id.desc())
        ).all()
    )
    parsed: list[tuple[ExamAutoCheckRun, list[Any]]] = []
    wanted: set[Any] = set()
    for run in runs:
        try:
            items = json.loads(run.detail_json or "[]")
        except (TypeError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        parsed.append((run, items))
        for item in items:
            if isinstance(item, dict) and item.get("assignment_id"):
                wanted.add(item["assignment_id"])
    owned: dict[Any, tuple[Assignment, ClassGroup]] = {}
    if wanted:
        for assignment, group in db.execute(
            select(Assignment, ClassGroup)
            .join(ClassGroup, Assignment.class_id == ClassGroup.id)
            .where(
                ClassGroup.teacher_id == teacher_id,
                Assignment.id.in_(wanted),
            )
        ).all():
            owned[assignment.id] = (assignment, group)

    kinds: dict[int, str] = {}
    rows: list[dict[str, Any]] = []
    for run, items in parsed:
        for item in items:
            if not isinstance(item, dict):
                continue
            pair = owned.get(item.get("assignment_id"))
            if pair is None:
                continue
            assignment, group = pair
            if assignment.id not in kinds:
                kinds[assignment.id] = _kind_label(assignment)
            outcome = str(item.get("outcome") or "")
            score = item.get("score")
            reason = reason_hebrew(str(item.get("reason") or ""))
            if outcome == "written":
                verdict, label = "ok", "הצליח — נבדק ונשמר"
            elif outcome == "off_confirmed":
                verdict, label = "ok", "הושלם — אין שאלות פתוחות"
            elif outcome == "deferred":
                verdict, label = "pending", "נדחה — ינוסה שוב"
            elif outcome == "requeued":
                verdict, label, reason, score = _requeued_outcome(
                    db, item.get("submission_id")
                )
            else:
                verdict, label = "failed", "נכשל"
            rows.append(
                {
                    "ran_at_label": format_local(run.ran_at, tz),
                    "class_name": group.name,
                    "assignment_id": assignment.id,
                    "assignment_title": assignment.title,
                    "kind": kinds[assignment.id],
                    "student": str(item.get("student") or ""),
                    "submission_id": item.get("submission_id"),
                    "verdict": verdict,
                    "status_label": label,
                    "reason_he": reason,
                    "score": score,
                }
            )
    rows = rows[:RECENT_MAX_ROWS]
    counts = {"ok": 0, "failed": 0, "pending": 0}
    for row in rows:
        counts[row["verdict"]] += 1
    return {
        "hours": hours,
        "since_label": format_local(since, tz),
        "rows": rows,
        "counts": counts,
    }


def build_status(db: Session, teacher: User, cfg: Any) -> dict[str, Any]:
    """The full view model for ``/teacher/exam-auto-check`` (read-only)."""
    tz = getattr(cfg, "timezone", "Asia/Jerusalem")
    groups = {
        group.id: group
        for group in db.scalars(
            select(ClassGroup).where(ClassGroup.teacher_id == teacher.id)
        ).all()
    }
    assignments = [
        _scan_assignment(db, assignment, groups.get(assignment.class_id), tz)
        for assignment in _teacher_assignments(db, teacher.id)
    ]
    assignment_retries = _failed_regular_assignments(db, teacher.id, tz)
    totals = {
        "pending": (
            sum(row["pending_count"] for row in assignments)
            + len(assignment_retries)
        ),
        "waiting": sum(row["counts"]["waiting"] for row in assignments),
        "failed": sum(row["counts"]["failed"] for row in assignments),
        "legacy": sum(row["counts"]["legacy"] for row in assignments),
        "reviewed": sum(row["counts"]["reviewed"] for row in assignments),
        "failed_assignments": len(assignment_retries),
    }

    runs = list(
        db.scalars(
            select(ExamAutoCheckRun)
            .order_by(ExamAutoCheckRun.ran_at.desc(), ExamAutoCheckRun.id.desc())
            .limit(RUNS_SHOWN)
        ).all()
    )
    run_rows: list[dict[str, Any]] = []
    for run in runs:
        try:
            items = json.loads(run.detail_json or "[]")
        except (TypeError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        details = []
        requeued = 0
        off_confirmed = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            outcome = str(item.get("outcome") or "")
            if outcome == "requeued":
                requeued += 1
            elif outcome == "off_confirmed":
                off_confirmed += 1
            details.append(
                {
                    "student": str(item.get("student") or ""),
                    "submission_id": item.get("submission_id"),
                    "assignment_id": item.get("assignment_id"),
                    "outcome": outcome,
                    "outcome_label": {
                        "written": "הושלם ונשמר",
                        "deferred": "נדחה — ינוסה שוב",
                        "failed": "נכשל",
                        "requeued": "נשלח לבדיקה מחדש",
                        "off_confirmed": "אומת — אין שאלות פתוחות",
                    }.get(outcome, outcome),
                    "score": item.get("score"),
                    "reason_he": reason_hebrew(str(item.get("reason") or "")),
                }
            )
        run_rows.append(
            {
                "id": run.id,
                "ran_at_label": format_local(run.ran_at, tz),
                "source": run.source,
                "source_label": "אוטומטי" if run.source == "scheduler" else "הרצה ידנית",
                "selected": run.selected,
                "written": run.written,
                "deferred": run.deferred,
                "failed": run.failed,
                "requeued": requeued,
                "off_confirmed": off_confirmed,
                "details": details,
            }
        )
    # Written total over the whole kept log, not only the shown window.
    written_total = int(
        db.scalar(select(func.sum(ExamAutoCheckRun.written))) or 0
    )

    last_run = runs[0] if runs else None
    interval = int(getattr(cfg, "exam_auto_check_interval_seconds", 3 * 60 * 60))
    next_label = ""
    if last_run is not None:
        next_label = format_local(
            last_run.ran_at + timedelta(seconds=interval), tz
        )
    return {
        "enabled": bool(getattr(cfg, "exam_auto_check_enabled", True)),
        "llm_enabled": llm_review.enabled(),
        "interval_minutes": max(1, interval // 60),
        "interval_hours": round(interval / 3600, 1),
        "batch_size": int(getattr(cfg, "exam_auto_check_batch_size", 5)),
        "last_run_label": (
            format_local(last_run.ran_at, tz) if last_run is not None else ""
        ),
        "next_run_label": next_label,
        "totals": totals,
        "written_total": written_total,
        "assignments": assignments,
        "assignment_retries": assignment_retries,
        "runs": run_rows,
        "recent": recent_report(db, teacher.id, tz),
    }


def pending_total(db: Session, teacher_id: int) -> int:
    """Cheap badge count for the dashboard: hand-ins still needing the pass.

    Counts unsigned ``waiting``/``failed`` exam papers, ``off`` papers the pass
    has not confirmed yet, legacy papers the pass has not yet given their one
    automatic re-mark, and failed regular assignments (the ones the automatic
    pass actually retries). The set mirrors ``auto_check.candidate_papers``.
    """
    total = 0
    for assignment in _teacher_assignments(db, teacher_id):
        submissions = db.scalars(
            select(Submission).where(
                Submission.assignment_id == assignment.id,
                Submission.status.in_((SUBMISSION_GRADED, SUBMISSION_FAILED)),
            )
        ).all()
        for submission in submissions:
            grade = grading_service.grade_for_revision(
                db, submission.id, submission.revision
            )
            if grade is None or grade.reviewed_at is not None:
                continue
            record = _load_json(grade.result_json)
            if record.get("disqualification"):
                continue
            # One shared rule with the selector (``stored_state_needs_pass``,
            # wrapped for a stored grade): waiting/failed papers still wait, and
            # an unconfirmed ``off`` or a legacy paper (no model state at all)
            # earns exactly one automatic re-mark.
            if auto_check.paper_needs_model_pass(grade):
                total += 1
    total += len(_failed_regular_assignments(db, teacher_id, "Asia/Jerusalem"))
    return total


__all__ = [
    "RECENT_HOURS",
    "RECENT_MAX_ROWS",
    "RUNS_SHOWN",
    "RUN_KEEP",
    "STATE_LABELS",
    "build_status",
    "pending_total",
    "reason_hebrew",
    "recent_report",
]