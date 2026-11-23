"""Submission bookkeeping shared by the web routes and the grading worker.

The routes describe *what* the student/teacher did; this module owns the
invariants:

* one :class:`~portal.models.Submission` per assignment and student;
* ``first_submitted_at`` is written once and never moves, so a resubmission
  cannot erase a late penalty (or invent one);
* a revision has at most one active grading job, so a double click cannot buy
  two model calls;
* the grade a page shows is the one for the **current** revision - an old
  grade never leaks next to new, ungraded text.

Nothing here calls the engine. The worker does that through
:mod:`portal.grading.adapter`; this module only reads and writes portal tables.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portal.models import (
    ACTIVE_JOB_STATES,
    GRADE_SOURCE_CLASSROOM,
    SUBMISSION_DEFERRED,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    SUBMISSION_GRADING,
    SUBMISSION_QUEUED,
    SUBMISSION_REOPENED,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    GradingJob,
    Submission,
    User,
)
from portal.timeutil import utcnow

# Generous for a lesson's worth of code, small enough that a pasted binary or
# an accidental page dump is rejected before it reaches SQLite or the prompt.
MAX_SUBMISSION_CHARS = 200_000

STATUS_LABELS = {
    SUBMISSION_QUEUED: "ממתין לבדיקה",
    SUBMISSION_GRADING: "בבדיקה",
    SUBMISSION_GRADED: "נבדק",
    SUBMISSION_FAILED: "הבדיקה נכשלה",
    SUBMISSION_DEFERRED: "ממתין לבדיקה (עומס זמני)",
    SUBMISSION_REOPENED: "נפתח מחדש להמשך",
}
STATUS_BADGES = {
    SUBMISSION_QUEUED: "badge-draft",
    SUBMISSION_GRADING: "badge-type",
    SUBMISSION_GRADED: "badge-published",
    SUBMISSION_FAILED: "badge-danger",
    SUBMISSION_DEFERRED: "badge-draft",
    SUBMISSION_REOPENED: "badge-warning",
}
NOT_SUBMITTED_LABEL = "לא הוגש"

TASK_STATUS_LABELS = {
    "CORRECT": "נכון",
    "PARTIALLY_CORRECT": "נכון חלקית",
    "INCORRECT": "שגוי",
    "MISSING": "חסר",
}


class AttemptLimitReached(RuntimeError):
    """The assignment allows one hand-in and it was already used."""


@dataclass(frozen=True)
class SubmitOutcome:
    submission: Submission
    job: GradingJob | None
    created: bool
    text_changed: bool

    @property
    def accepted(self) -> bool:
        """True when this request produced work the worker still has to do."""
        return self.job is not None


def submission_for(
    db: Session, assignment_id: int, student_id: int
) -> Submission | None:
    return db.scalar(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student_id,
        )
    )


def _active_job(
    db: Session, submission_id: int, revision: int
) -> GradingJob | None:
    return db.scalar(
        select(GradingJob)
        .where(
            GradingJob.submission_id == submission_id,
            GradingJob.revision == revision,
            GradingJob.state.in_(ACTIVE_JOB_STATES),
        )
        .order_by(GradingJob.id.desc())
    )


def _new_job(db: Session, submission: Submission) -> GradingJob:
    job = GradingJob(
        submission_id=submission.id,
        revision=submission.revision,
        state=SUBMISSION_QUEUED,
    )
    db.add(job)
    db.flush()
    return job


def queue_job(
    db: Session, submission: Submission, *, force: bool = False
) -> GradingJob:
    """Return the active job for the current revision, creating one if needed.

    ``force`` is the teacher's "regrade" action: even a graded revision gets a
    fresh job, but an already active job is still reused.
    """
    existing = _active_job(db, submission.id, submission.revision)
    if existing is not None:
        return existing
    if not force:
        grade = grade_for_revision(db, submission.id, submission.revision)
        if grade is not None:
            # Resubmitting identical text after grading is a no-op, not a new
            # model call. The teacher can still ask for a regrade explicitly.
            raise ValueError("already graded")
    submission.status = SUBMISSION_QUEUED
    job = _new_job(db, submission)
    db.flush()
    return job


def submit_work(
    db: Session,
    assignment: Assignment,
    student: User,
    text: str,
    *,
    single_attempt: bool = False,
) -> SubmitOutcome:
    """Store a hand-in and queue grading.

    Identical text is idempotent: a double-click or a page reload reuses the
    active job (or the existing grade) instead of creating a new revision and
    a second model call.

    ``single_attempt`` is the exam rule: one hand-in per student per
    assignment. The check sits here, in the same call that writes the row, so
    two requests racing on the same exam cannot both create a revision.
    """
    now = utcnow()
    submission = submission_for(db, assignment.id, student.id)

    if single_attempt and submission is not None:
        if (
            submission.status in {SUBMISSION_QUEUED, SUBMISSION_GRADING,
                                  SUBMISSION_GRADED, SUBMISSION_DEFERRED}
            or grade_for_revision(db, submission.id, submission.revision)
            is not None
        ):
            raise AttemptLimitReached("the single attempt was already handed in")

    if submission is None:
        submission = Submission(
            assignment_id=assignment.id,
            student_id=student.id,
            text=text,
            revision=1,
            status=SUBMISSION_QUEUED,
            first_submitted_at=now,
            submitted_at=now,
        )
        db.add(submission)
        db.flush()
        job = _new_job(db, submission)
        return SubmitOutcome(submission, job, True, True)

    if text == submission.text:
        active = _active_job(db, submission.id, submission.revision)
        if active is not None:
            return SubmitOutcome(submission, active, False, False)
        if grade_for_revision(db, submission.id, submission.revision):
            return SubmitOutcome(submission, None, False, False)
        submission.status = SUBMISSION_QUEUED
        job = _new_job(db, submission)
        return SubmitOutcome(submission, job, False, False)

    submission.text = text
    # A reopened exam already sits on the fresh revision the teacher opened for
    # the continuation; handing in again fills that revision rather than opening
    # yet another one. Every other resubmission advances the revision as usual.
    if submission.status != SUBMISSION_REOPENED:
        submission.revision += 1
    submission.submitted_at = now
    submission.status = SUBMISSION_QUEUED
    # ``first_submitted_at`` is intentionally untouched: late-policy
    # resolution starts from the original hand-in.
    job = _new_job(db, submission)
    return SubmitOutcome(submission, job, False, True)


def latest_job(db: Session, submission: Submission) -> GradingJob | None:
    return db.scalar(
        select(GradingJob)
        .where(
            GradingJob.submission_id == submission.id,
            GradingJob.revision == submission.revision,
        )
        .order_by(GradingJob.id.desc())
    )


def grade_for_revision(
    db: Session, submission_id: int, revision: int
) -> Grade | None:
    return db.scalar(
        select(Grade).where(
            Grade.submission_id == submission_id,
            Grade.revision == revision,
        )
    )


def latest_grade(db: Session, submission_id: int) -> Grade | None:
    """Newest persisted grade, regardless of revision (teacher history view)."""
    return db.scalar(
        select(Grade)
        .where(Grade.submission_id == submission_id)
        .order_by(Grade.revision.desc(), Grade.id.desc())
    )


def current_grade(db: Session, submission: Submission) -> Grade | None:
    """The grade that belongs to the text currently shown to the student."""
    return grade_for_revision(db, submission.id, submission.revision)


def _map_exam_tasks(tasks: list, questions: list) -> None:
    """Give each exam task the key/points/earned/manual an override needs.

    Exam tasks and exam questions are written in the same order by the checker,
    so a task can carry the matching question's identity. Shared by the stored
    grade projection and the raw-record projection of an unapplied preview.
    """
    for index, task in enumerate(tasks):
        if not isinstance(task, dict) or index >= len(questions):
            continue
        question = questions[index]
        if not isinstance(question, dict):
            continue
        task["key"] = question.get("key")
        task["points"] = question.get("points")
        task["earned"] = question.get("earned")
        task["manual"] = question.get("manual")


def grade_view(grade: Grade | None) -> dict | None:
    """Template-safe projection of one stored grade (no rubric/answers)."""
    if grade is None:
        return None
    try:
        result = json.loads(grade.result_json or "{}")
    except (TypeError, ValueError):
        result = {}
    try:
        audit = json.loads(grade.audit_json) if grade.audit_json else None
    except (TypeError, ValueError):
        audit = None
    try:
        deductions = json.loads(grade.deduction_json or "[]")
    except (TypeError, ValueError):
        deductions = []
    tasks = result.get("task_evaluations") or []
    exam = result.get("exam") if isinstance(result.get("exam"), dict) else {}
    questions = exam.get("questions") or []
    _map_exam_tasks(tasks, questions)
    return {
        "score": grade.score,
        "feedback": grade.feedback_hebrew,
        "deductions": deductions,
        "tasks": tasks,
        "audit": audit,
        "late_penalty": grade.late_penalty_points,
        "model": grade.configured_model,
        "graded_at": grade.graded_at,
        "rubric_title": grade.rubric_title,
        "rubric_sha256": grade.rubric_sha256,
        "needs_review": grade.audit_needs_review,
        "audit_ok": grade.audit_ok,
        "audit_summary": grade.audit_summary,
        "reviewed_at": grade.reviewed_at,
        "review_note": grade.review_note,
        # Only an exam hand-in gets a per-question manual override.
        "is_exam": bool(questions) and grade.configured_model == "exam-checker",
        "exam_questions": questions,
        # A whole-paper fixed-score decision (disqualification). The teacher view
        # carries the reason; the student projection strips it below.
        "disqualification": (
            result.get("disqualification")
            if isinstance(result.get("disqualification"), dict)
            else None
        ),
        # Imported Classroom grades have no engine result to inspect or replace.
        "historical": grade.configured_model == GRADE_SOURCE_CLASSROOM,
    }


def student_grade_view(grade: Grade | None) -> dict | None:
    """The student's projection: every teacher-only note removed.

    A teacher's correction note (``manual.note`` on a question or part), the
    review note and the manual-override metadata exist to calibrate the model
    and to document the teacher's decision - they are never the student's to
    read. The student templates already render none of them, but stripping them
    here makes that privacy *structural*: the fields are absent from the data a
    student page receives, so a future template change cannot surface them by
    accident. The model's own explanation (``task.notes``), the score and the
    general feedback stay - those are the student's feedback.

    The teacher pages and the teacher preview keep the full :func:`grade_view`.
    """
    view = grade_view(grade)
    if view is None:
        return None
    view["review_note"] = ""
    for task in view.get("tasks") or []:
        if isinstance(task, dict):
            task.pop("manual", None)
    for question in view.get("exam_questions") or []:
        if not isinstance(question, dict):
            continue
        question.pop("manual", None)
        for part in question.get("parts") or []:
            if isinstance(part, dict):
                part.pop("manual", None)
    # A disqualification is the student's to know (their paper was disqualified
    # and a fixed score was set), but the teacher's reason is not. Keep only the
    # flag and the fixed score; the reason, author and timestamp stay teacher-only.
    disq = view.get("disqualification")
    if isinstance(disq, dict):
        view["disqualification"] = {
            "active": True,
            "score": int(disq.get("score") or 0),
        }
    else:
        view["disqualification"] = None
    return view


def record_view(
    result: dict | None,
    *,
    model: str = "exam-checker",
    graded_at: Any = None,
    late_penalty: int = 0,
    rubric_title: str = "",
    rubric_sha256: str = "",
    reviewed_at: Any = None,
    review_note: str = "",
) -> dict | None:
    """Template-safe projection of a raw result record (e.g. an unapplied preview).

    Mirrors :func:`grade_view`, but reads the score, feedback, audit and
    deductions from the record itself instead of the denormalised ``Grade``
    columns, so a model-check preview that has not been written to a grade yet
    is shown identically. The audit is recomputed from the questions when the
    record does not carry one (a fresh checker record has none until refresh).
    """
    if not isinstance(result, dict):
        return None
    tasks = result.get("task_evaluations") or []
    exam = result.get("exam") if isinstance(result.get("exam"), dict) else {}
    questions = exam.get("questions") or []
    _map_exam_tasks(tasks, questions)
    audit = result.get("audit")
    if not isinstance(audit, dict):
        review = [
            question
            for question in questions
            if isinstance(question, dict) and question.get("needs_review")
        ]
        audit = {
            "ok": not review,
            "needs_review": bool(review),
            "summary": (
                "כל השאלות נבדקו אוטומטית"
                if not review
                else f"{len(review)} שאלות דורשות מבט של המורה"
            ),
        }
    deductions = result.get("deduction_breakdown") or []
    return {
        "score": int(result.get("score") or 0),
        "feedback": str(result.get("feedback_hebrew") or ""),
        "deductions": deductions,
        "tasks": tasks,
        "audit": audit,
        "late_penalty": late_penalty,
        "model": model,
        "graded_at": graded_at,
        "rubric_title": rubric_title,
        "rubric_sha256": rubric_sha256,
        "needs_review": bool(audit.get("needs_review")),
        "audit_ok": bool(audit.get("ok", True)),
        "audit_summary": str(audit.get("summary") or ""),
        "reviewed_at": reviewed_at,
        "review_note": review_note,
        "is_exam": bool(questions) and model == "exam-checker",
        "exam_questions": questions,
        "disqualification": (
            result.get("disqualification")
            if isinstance(result.get("disqualification"), dict)
            else None
        ),
        "historical": model == GRADE_SOURCE_CLASSROOM,
    }


def status_label(
    submission: Submission | None, grade: Grade | None = None
) -> str:
    if submission is None:
        return NOT_SUBMITTED_LABEL
    base = STATUS_LABELS.get(submission.status, submission.status)
    if submission.status == SUBMISSION_GRADED and grade is not None:
        return f"{base} · {grade.score}"
    return base


def student_status_map(
    db: Session, student_id: int, assignment_ids: list[int]
) -> dict[int, dict]:
    """Per-assignment status for the student's class/dashboard lists.

    An exam is marked at hand-in but its result stays hidden until the teacher
    releases it. The feed must not leak the number through the status label:
    while the exam is held the status reads "הוגש", and the score and the
    "needs review" flag stay out of the student's payload entirely.
    """
    if not assignment_ids:
        return {}
    assignments = {
        item.id: item
        for item in db.scalars(
            select(Assignment).where(Assignment.id.in_(assignment_ids))
        ).all()
    }
    submissions = db.scalars(
        select(Submission).where(
            Submission.student_id == student_id,
            Submission.assignment_id.in_(assignment_ids),
        )
    ).all()
    result: dict[int, dict] = {}
    for submission in submissions:
        grade = current_grade(db, submission)
        assignment = assignments.get(submission.assignment_id)
        held_exam = bool(
            assignment is not None
            and str(assignment.exam_config or "").strip()
            and not assignment.exam_score_visible
        )
        if held_exam and submission.status == SUBMISSION_GRADED:
            # The work is in and checked; only the number is the teacher's to
            # publish, so say the hand-in arrived and nothing more.
            label = "הוגש"
        else:
            label = status_label(submission, None if held_exam else grade)
        result[submission.assignment_id] = {
            "status": submission.status,
            "label": label,
            "badge": STATUS_BADGES.get(submission.status, "badge-type"),
            "score": (
                None if held_exam or grade is None else grade.score
            ),
            "needs_review": bool(
                not held_exam
                and grade is not None
                and grade.audit_needs_review
            ),
        }
    return result


def class_grading_counts(
    db: Session, class_id: int
) -> dict[int, dict[str, int]]:
    """Counts for the teacher overview, one row per assignment in a class."""
    assignments = db.scalars(
        select(Assignment).where(Assignment.class_id == class_id)
    ).all()
    counts: dict[int, dict[str, int]] = {
        assignment.id: {
            "submitted": 0,
            "pending": 0,
            "graded": 0,
            "failed": 0,
            "needs_review": 0,
            "reviewed": 0,
        }
        for assignment in assignments
    }
    if not counts:
        return counts

    rows = db.execute(
        select(
            Submission.assignment_id,
            Submission.id,
            Submission.revision,
            Submission.status,
        ).where(Submission.assignment_id.in_(counts))
    ).all()
    current: dict[int, tuple[int, str]] = {}
    for assignment_id, submission_id, revision, status in rows:
        current[submission_id] = (int(assignment_id), str(status))
        bucket = counts[int(assignment_id)]
        bucket["submitted"] += 1
        if status == SUBMISSION_GRADED:
            bucket["graded"] += 1
        elif status == SUBMISSION_FAILED:
            bucket["failed"] += 1
        else:
            bucket["pending"] += 1

    if current:
        grades = db.scalars(
            select(Grade).where(Grade.submission_id.in_(current))
        ).all()
        for grade in grades:
            assignment_id, _status = current[grade.submission_id]
            submission = db.get(Submission, grade.submission_id)
            if submission is None or grade.revision != submission.revision:
                continue
            bucket = counts[assignment_id]
            if grade.audit_needs_review:
                bucket["needs_review"] += 1
            if grade.reviewed_at is not None:
                bucket["reviewed"] += 1
    return counts


def assignment_grading_rows(
    db: Session, assignment: Assignment
) -> list[dict]:
    """Every enrolled student with their current submission and grade.

    Students who never handed in still appear, so the teacher sees the whole
    roster rather than only the people who submitted.
    """
    students = db.scalars(
        select(User)
        .join(Enrollment, Enrollment.student_id == User.id)
        .where(Enrollment.class_id == assignment.class_id)
        .order_by(User.display_name)
    ).all()
    submissions = {
        item.student_id: item
        for item in db.scalars(
            select(Submission).where(
                Submission.assignment_id == assignment.id
            )
        ).all()
    }
    rows: list[dict] = []
    for student in students:
        submission = submissions.get(student.id)
        grade = current_grade(db, submission) if submission else None
        job = latest_job(db, submission) if submission else None
        rows.append(
            {
                "student": student,
                "submission": submission,
                "grade": grade,
                "grade_view": grade_view(grade),
                "job": job,
                "status_label": status_label(submission, grade),
                "status": submission.status if submission else "",
            }
        )
    return rows


def waiting_count(db: Session, teacher_id: int) -> int:
    """How many hand-ins across the teacher's classes still need attention."""
    return int(
        db.scalar(
            select(func.count(Submission.id))
            .join(Assignment, Assignment.id == Submission.assignment_id)
            .join(ClassGroup, ClassGroup.id == Assignment.class_id)
            .where(
                ClassGroup.teacher_id == teacher_id,
                Submission.status.in_(
                    (
                        SUBMISSION_QUEUED,
                        SUBMISSION_GRADING,
                        SUBMISSION_DEFERRED,
                    )
                ),
            )
        )
        or 0
    )


def format_task_status(status: str) -> str:
    return TASK_STATUS_LABELS.get(status, status)


__all__ = [
    "MAX_SUBMISSION_CHARS",
    "NOT_SUBMITTED_LABEL",
    "STATUS_BADGES",
    "STATUS_LABELS",
    "TASK_STATUS_LABELS",
    "SubmitOutcome",
    "assignment_grading_rows",
    "class_grading_counts",
    "current_grade",
    "format_task_status",
    "grade_for_revision",
    "grade_view",
    "latest_grade",
    "latest_job",
    "queue_job",
    "status_label",
    "student_grade_view",
    "student_status_map",
    "submission_for",
    "submit_work",
    "waiting_count",
]
