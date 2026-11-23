"""Exam lifecycle for one assignment: window, allocation, paper, marking.

The three pieces live apart on purpose:

* :mod:`portal.exams.config` says *which* versions exist and where their paper,
  rubric and answer key are;
* :mod:`portal.exams.paper` decides *which* version a student sits and in what
  order they see it;
* :mod:`portal.exams.checker` marks the answers.

This module is the only place that joins them to an assignment and a student,
so the page, the submit route and the worker cannot disagree about the window,
the version or the attempt rule.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal import drafts, worksheets
from portal.exams import calibration as exam_calibration
from portal.exams import checker
from portal.exams import feedback as exam_feedback
from portal.exams.config import ExamConfig, for_assignment
from portal.exams.paper import (
    Paper,
    allocation_for,
    ensure_allocation,
    paper_for,
    paper_for_allocation,
)
from portal.models import (
    EXAM_RELEASE_FULL,
    EXAM_RELEASE_HELD,
    EXAM_RELEASE_LABELS,
    EXAM_RELEASE_SCORES,
    SUBMISSION_DEFERRED,
    SUBMISSION_GRADED,
    SUBMISSION_GRADING,
    SUBMISSION_QUEUED,
    SUBMISSION_REOPENED,
    Assignment,
    ExamAllocation,
    ExamStudentWindow,
    Submission,
    User,
)
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

BEFORE = "before"
OPEN = "open"
CLOSED = "closed"

# Mirrors of the model constants, so exam code reads in exam terms.
RELEASE_HELD = EXAM_RELEASE_HELD
RELEASE_SCORES = EXAM_RELEASE_SCORES
RELEASE_FULL = EXAM_RELEASE_FULL
RELEASE_LABELS = EXAM_RELEASE_LABELS

# States in which the student's one attempt is used up. ``failed`` is not one
# of them: a failed check is the portal's problem, so the student may retry.
_ATTEMPT_STATES = frozenset(
    {SUBMISSION_QUEUED, SUBMISSION_GRADING, SUBMISSION_GRADED, SUBMISSION_DEFERRED}
)


class ExamUnavailable(RuntimeError):
    """The assignment names an exam that cannot be served right now."""


class ExamAttemptTaken(RuntimeError):
    """The student has already used their single attempt."""


class ExamWindow(NamedTuple):
    """The times one student is actually held to.

    ``personal`` is true when a teacher granted a make-up window for this
    student; that row replaces the assignment's two ends completely.
    """

    opens_at: datetime | None
    closes_at: datetime | None
    personal: bool = False


def config_for(assignment: Assignment) -> ExamConfig | None:
    """The exam definition this assignment belongs to, if any."""
    return for_assignment(assignment)


def release_state(assignment: Assignment) -> str:
    """How much of a hand-in the student may see: held, scores or full.

    Marking happens at hand-in, so visibility is a separate decision the
    teacher makes once the class - and every class sitting later - is done.
    """
    return assignment.exam_release_state


def release_label(assignment: Assignment) -> str:
    return RELEASE_LABELS.get(release_state(assignment), RELEASE_LABELS[RELEASE_HELD])


def window_state(
    assignment: Assignment, now=None, window: ExamWindow | None = None
) -> str:
    """Where the exam is relative to its opening and closing time.

    ``window`` is the student's effective window when one is known; without it
    the assignment's class-wide window is used. Every caller that acts for a
    student - the gate, the paper, autosave and the hand-in - passes the same
    effective window so they cannot disagree.
    """
    moment = now or utcnow()
    opens_at, closes_at = (
        (window.opens_at, window.closes_at)
        if window is not None
        else (assignment.opens_at, assignment.closes_at)
    )
    if opens_at is not None and moment < opens_at:
        return BEFORE
    if closes_at is not None and moment > closes_at:
        return CLOSED
    return OPEN


def student_window(
    db: Session, assignment: Assignment, student: User
) -> ExamWindow:
    """The window this student sits in: the personal grant, else the class's."""
    override = db.scalar(
        select(ExamStudentWindow).where(
            ExamStudentWindow.assignment_id == assignment.id,
            ExamStudentWindow.student_id == student.id,
        )
    )
    if override is not None:
        return ExamWindow(override.opens_at, override.closes_at, True)
    return ExamWindow(assignment.opens_at, assignment.closes_at, False)


def student_window_state(
    db: Session, assignment: Assignment, student: User, now=None
) -> str:
    """Where the exam stands for one student, personal window included."""
    return window_state(
        assignment, now, window=student_window(db, assignment, student)
    )


def attempt_taken(submission: Submission | None) -> bool:
    """True once the student has handed in and the hand-in still counts."""
    return submission is not None and submission.status in _ATTEMPT_STATES


def visible_to_student(
    assignment: Assignment, submission: Submission | None
) -> bool:
    """Whether this one student may reach the assignment page at all.

    A published assignment belongs to the whole class. A **draft** exam
    normally belongs to nobody - hiding it is precisely why a teacher
    unpublishes one - but the marks the teacher released must still reach the
    students who already sat it. So a draft exam opens for a student who has a
    hand-in of their own, and stays a plain 404 for a student who has not sat
    it yet: no page, no feed row, not even a hint that the exam exists.

    Only the *door* changes here. What is behind it is still decided by
    :func:`release_state`, so "scores only" keeps the paper and the
    explanations away from everyone, and the hand-in routes keep refusing work
    on a draft.
    """
    if assignment.published:
        return True
    return assignment.exam_results_released and submission is not None


def reopen(db: Session, assignment: Assignment, submission: Submission) -> None:
    """Let one student continue an exam they handed in by accident.

    The single attempt is suspended, not erased. The accidental hand-in stays
    in the history, and the student's structured answers are restored as an
    editable draft so they continue from their own work instead of a blank
    paper. A fresh revision is opened for two reasons: a grading job for the
    old revision no longer matches, so the worker cannot race the reopen and
    re-lock it; and the new revision has no grade yet, so the single-attempt
    rule accepts the continuation hand-in. Handing in again moves the row back
    to ``queued`` and locks it once more. The window still applies - this only
    lets the student continue while their sitting window is open.
    """
    values = worksheets.load_values(db, submission)
    submission.revision += 1
    submission.status = SUBMISSION_REOPENED
    if values:
        drafts.save(
            db,
            assignment.id,
            submission.student_id,
            kind="worksheet",
            values=values,
        )
    logger.info(
        "Exam reopened: assignment %s student %s now revision %s",
        assignment.id,
        submission.student_id,
        submission.revision,
    )


def student_paper(
    db: Session, assignment: Assignment, student: User, config: ExamConfig
) -> Paper:
    """The paper this student sees, drawing and saving their version once."""
    allocation = ensure_allocation(db, config, assignment.id, student.id)
    # The first open is the only write here; committing it immediately means a
    # refresh, a second tab or another device all read the same stored paper.
    db.commit()
    return paper_for_allocation(config, allocation)


def review_paper(
    db: Session, assignment: Assignment, student: User, config: ExamConfig
) -> Paper | None:
    """The paper as it was shown, for the read-only view after hand-in."""
    allocation = allocation_for(db, assignment.id, student.id)
    if allocation is None:
        return None
    return paper_for_allocation(config, allocation)


def allocation_version(
    db: Session, assignment: Assignment, student: User, config: ExamConfig
) -> str:
    allocation: ExamAllocation | None = allocation_for(
        db, assignment.id, student.id
    )
    return allocation.version if allocation is not None else ""


def canonical_paper(config: ExamConfig, version_key: str) -> Paper:
    """One version in paper order, for the teacher's overview."""
    return paper_for(config, config.version(version_key), None)


def mark(
    db: Session,
    assignment: Assignment,
    submission: Submission,
    *,
    student_name: str,
    config: ExamConfig | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> checker.ExamReport:
    """Mark one stored hand-in against the answer key of its version.

    ``progress`` is an optional per-question callback forwarded to the checker
    so a background re-check can report which question it is on; it never
    affects the mark.
    """
    config = config or config_for(assignment)
    if config is None:
        raise ExamUnavailable("this assignment is not a working exam")
    allocation = allocation_for(db, assignment.id, submission.student_id)
    if allocation is None:
        raise ExamUnavailable(
            "the student has no exam version; the paper was never opened"
        )
    version = config.version(allocation.version)
    values: dict[str, Any] = worksheets.load_values(db, submission)
    if not values:
        raise ExamUnavailable("the structured answers for this hand-in are missing")
    # The teacher's calibration for this exam, rendered into bounded blocks keyed
    # by question. Fetched here (the DB-aware layer) and handed to the DB-free
    # checker as plain strings, so the model is calibrated without the checker
    # ever touching the database. A question that has a distilled grading-style
    # profile carries that small paragraph; until one exists it falls back to the
    # last few raw corrections. Both paths are hard-capped, so a large correction
    # history can never grow the request into a 429.
    profiles = exam_calibration.profiles_for_exam(db, config.name)
    examples = exam_feedback.for_exam(db, config.name)
    blocks: dict[str, str] = {}
    for key in set(profiles) | set(examples):
        profile = profiles.get(key, "")
        block = (
            exam_calibration.render_profile_block(profile)
            if profile
            else exam_feedback.render_block(examples.get(key, []))
        )
        if block:
            blocks[key] = block
    return checker.check(config, version, values, feedback=blocks, progress=progress)


__all__ = [
    "BEFORE",
    "CLOSED",
    "OPEN",
    "RELEASE_FULL",
    "RELEASE_HELD",
    "RELEASE_LABELS",
    "RELEASE_SCORES",
    "ExamAttemptTaken",
    "ExamUnavailable",
    "ExamWindow",
    "allocation_version",
    "attempt_taken",
    "canonical_paper",
    "config_for",
    "mark",
    "release_label",
    "release_state",
    "reopen",
    "review_paper",
    "student_paper",
    "student_window",
    "student_window_state",
    "visible_to_student",
    "window_state",
]
