"""The class grade matrix: enrolled students × published assignments.

The matrix is a **projection**, not a new record. For every cell it reads the
student's single :class:`~portal.models.Submission` and the grade for that
submission's *current* revision. That has two consequences worth stating:

* a resubmission automatically hides the old grade - the cell becomes
  ``queued`` until the new revision is graded, so a teacher never averages
  stale work into the class;
* only published assignments are columns - plus the one draft whose marks a
  teacher really did publish: an exam released while its paper stayed hidden.
  Any other draft is invisible to students, so showing it in the gradebook
  would imply work they were never given.

Statuses are normalised to four values because that is what a teacher acts on:

``graded``
    The current revision has a stored grade (score is available).
``queued``
    Handed in, waiting for the worker. ``grading`` and ``deferred`` are folded
    in here - from the teacher's point of view both mean "not yet".
``failed``
    The worker could not grade the current revision; the teacher must look.
``missing``
    No hand-in at all.

Lateness is resolved from the locked ``first_submitted_at`` against the
assignment deadline, matching :mod:`src.late_policy`; a positive
``late_penalty_points`` on the stored grade also marks the cell late so a
deadline edited after grading cannot erase a penalty the score already
includes. Imported Classroom grades carry no hand-in time, so they are never
marked late.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.models import (
    GRADE_SOURCE_CLASSROOM,
    ROLE_STUDENT,
    SUBMISSION_FAILED,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    Submission,
    User,
)
from portal.timeutil import as_utc

CELL_GRADED = "graded"
CELL_QUEUED = "queued"
CELL_FAILED = "failed"
CELL_MISSING = "missing"

CELL_LABELS = {
    CELL_GRADED: "נבדק",
    CELL_QUEUED: "ממתין לבדיקה",
    CELL_FAILED: "הבדיקה נכשלה",
    CELL_MISSING: "לא הוגש",
}

CELL_BADGES = {
    CELL_GRADED: "badge-published",
    CELL_QUEUED: "badge-draft",
    CELL_FAILED: "badge-danger",
    CELL_MISSING: "badge-missing",
}


@dataclass(frozen=True)
class GradebookCell:
    """One student's result for one assignment (current revision only)."""

    assignment_id: int
    status: str
    label: str
    badge: str
    score: int | None = None
    late: bool = False
    needs_review: bool = False
    reviewed: bool = False
    review_note: str = ""
    feedback: str = ""
    late_penalty_points: int = 0
    submission_id: int | None = None
    revision: int | None = None
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    historical: bool = False


@dataclass(frozen=True)
class StudentRow:
    """One student across every published assignment in the class."""

    student: User
    cells: tuple[GradebookCell, ...]
    average: float | None
    graded_count: int
    submitted_count: int
    missing_count: int
    failed_count: int
    queued_count: int
    late_count: int
    needs_review_count: int


@dataclass(frozen=True)
class AssignmentStats:
    """Column totals for one assignment."""

    assignment: Assignment
    graded: int
    queued: int
    failed: int
    missing: int
    submitted: int
    late: int
    needs_review: int
    average: float | None


@dataclass(frozen=True)
class ClassStatistics:
    """Whole-matrix totals shown above and below the gradebook table."""

    students: int
    assignments: int
    graded: int
    queued: int
    failed: int
    missing: int
    submitted: int
    late: int
    needs_review: int
    average: float | None
    possible_submissions: int

    @property
    def completion_percent(self) -> float:
        """Share of all student/assignment cells that have a hand-in."""
        if self.possible_submissions <= 0:
            return 0.0
        return round(self.submitted * 100 / self.possible_submissions, 1)


@dataclass(frozen=True)
class Gradebook:
    group: ClassGroup
    assignments: tuple[Assignment, ...]
    rows: tuple[StudentRow, ...]
    assignment_stats: tuple[AssignmentStats, ...]
    stats: ClassStatistics


@dataclass(frozen=True)
class ProfileEntry:
    """One assignment on a student's cross-class profile."""

    assignment: Assignment
    cell: GradebookCell


@dataclass(frozen=True)
class ProfileHistory:
    """One graded point on the student's recent timeline."""

    class_name: str
    assignment_title: str
    score: int
    graded_at: datetime | None
    historical: bool


@dataclass(frozen=True)
class ProfileClass:
    """One class on the student profile, with that class's slice of work."""

    group: ClassGroup
    entries: tuple[ProfileEntry, ...]
    average: float | None
    graded: int
    submitted: int
    missing: int
    failed: int
    queued: int
    late: int
    needs_review: int
    historical: int = 0


@dataclass(frozen=True)
class StudentProfile:
    student: User
    classes: tuple[ProfileClass, ...]
    average: float | None
    graded: int
    submitted: int
    missing: int
    failed: int
    queued: int
    late: int
    needs_review: int
    historical: int = 0
    history: tuple[ProfileHistory, ...] = ()


def average(values: list[int]) -> float | None:
    """Mean rounded to one decimal, or ``None`` when nothing was graded."""
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def gradebook_assignments(db: Session, class_id: int) -> tuple[Assignment, ...]:
    """The assignments that become columns, in deadline order (undated last).

    Normally that is the published ones: a draft is invisible to students, so a
    column for it would show marks for work nobody was given. One case breaks
    the rule, and it is a mark the teacher *did* publish - an exam whose results
    are released while its paper stays a draft, so a class that has not sat yet
    cannot see it. Hiding that column from the teacher would punish exactly the
    decision they made, so the release, not ``published``, decides the column.
    """
    return tuple(
        item
        for item in db.scalars(
            select(Assignment)
            .where(Assignment.class_id == class_id)
            .order_by(
                Assignment.due_at.is_(None),
                Assignment.due_at,
                Assignment.created_at,
                Assignment.id,
            )
        ).all()
        if item.published or item.exam_results_released
    )


def _roster(db: Session, class_id: int) -> tuple[User, ...]:
    return tuple(
        db.scalars(
            select(User)
            .join(Enrollment, Enrollment.student_id == User.id)
            .where(Enrollment.class_id == class_id)
            .order_by(User.display_name, User.id)
        ).all()
    )


def _load_records(
    db: Session, assignment_ids: list[int]
) -> tuple[dict[tuple[int, int], Submission], dict[int, Grade]]:
    """Load submissions and their current-revision grades in two queries."""
    if not assignment_ids:
        return {}, {}
    submissions = db.scalars(
        select(Submission).where(Submission.assignment_id.in_(assignment_ids))
    ).all()
    by_key = {
        (submission.assignment_id, submission.student_id): submission
        for submission in submissions
    }
    by_id = {submission.id: submission for submission in submissions}
    grades: dict[int, Grade] = {}
    if submissions:
        rows = db.scalars(
            select(Grade).where(
                Grade.submission_id.in_(
                    [submission.id for submission in submissions]
                )
            )
        ).all()
        for grade in rows:
            submission = by_id.get(grade.submission_id)
            if submission is not None and grade.revision == submission.revision:
                grades[submission.id] = grade
    return by_key, grades


def _is_late(
    submission: Submission, assignment: Assignment, grade: Grade | None
) -> bool:
    if grade is not None and grade.configured_model == GRADE_SOURCE_CLASSROOM:
        # A Classroom export has a score but no hand-in time, so the portal
        # cannot decide lateness for imported work.
        return False
    if grade is not None and int(grade.late_penalty_points or 0) > 0:
        return True
    due = as_utc(assignment.due_at)
    first = as_utc(submission.first_submitted_at)
    return bool(due is not None and first is not None and first > due)


def _cell(
    assignment: Assignment,
    submission: Submission | None,
    grade: Grade | None,
) -> GradebookCell:
    if submission is None:
        return GradebookCell(
            assignment_id=assignment.id,
            status=CELL_MISSING,
            label=CELL_LABELS[CELL_MISSING],
            badge=CELL_BADGES[CELL_MISSING],
        )

    if grade is not None and grade.revision != submission.revision:
        # Defensive: ``current_grade`` semantics must never leak an old grade.
        grade = None
    if submission.status == SUBMISSION_FAILED:
        status = CELL_FAILED
    elif grade is not None:
        status = CELL_GRADED
    else:
        status = CELL_QUEUED

    return GradebookCell(
        assignment_id=assignment.id,
        status=status,
        label=CELL_LABELS[status],
        badge=CELL_BADGES[status],
        score=grade.score if grade is not None else None,
        late=_is_late(submission, assignment, grade),
        needs_review=bool(grade is not None and grade.audit_needs_review),
        reviewed=bool(grade is not None and grade.reviewed_at is not None),
        review_note=(grade.review_note or "") if grade is not None else "",
        feedback=(grade.feedback_hebrew or "") if grade is not None else "",
        late_penalty_points=(
            int(grade.late_penalty_points or 0) if grade is not None else 0
        ),
        submission_id=submission.id,
        revision=submission.revision,
        submitted_at=submission.first_submitted_at,
        graded_at=grade.graded_at if grade is not None else None,
        historical=bool(
            grade is not None
            and grade.configured_model == GRADE_SOURCE_CLASSROOM
        ),
    )


def _student_row(
    student: User, cells: tuple[GradebookCell, ...]
) -> StudentRow:
    scores = [
        cell.score
        for cell in cells
        if cell.status == CELL_GRADED and cell.score is not None
    ]
    return StudentRow(
        student=student,
        cells=cells,
        average=average(scores),
        graded_count=len(scores),
        submitted_count=sum(1 for cell in cells if cell.status != CELL_MISSING),
        missing_count=sum(1 for cell in cells if cell.status == CELL_MISSING),
        failed_count=sum(1 for cell in cells if cell.status == CELL_FAILED),
        queued_count=sum(1 for cell in cells if cell.status == CELL_QUEUED),
        late_count=sum(1 for cell in cells if cell.late),
        needs_review_count=sum(1 for cell in cells if cell.needs_review),
    )


def build_gradebook(db: Session, group: ClassGroup) -> Gradebook:
    """Build the complete matrix for one class.

    The result is immutable so a route cannot accidentally mutate what the
    exporter sees; both are computed from the same snapshot.
    """
    assignments = gradebook_assignments(db, group.id)
    students = _roster(db, group.id)
    submissions, grades = _load_records(db, [a.id for a in assignments])

    rows: list[StudentRow] = []
    for student in students:
        cells: list[GradebookCell] = []
        for assignment in assignments:
            submission = submissions.get((assignment.id, student.id))
            grade = grades.get(submission.id) if submission is not None else None
            cells.append(_cell(assignment, submission, grade))
        rows.append(_student_row(student, tuple(cells)))

    assignment_stats: list[AssignmentStats] = []
    for index, assignment in enumerate(assignments):
        column = [row.cells[index] for row in rows]
        scores = [
            cell.score
            for cell in column
            if cell.status == CELL_GRADED and cell.score is not None
        ]
        assignment_stats.append(
            AssignmentStats(
                assignment=assignment,
                graded=len(scores),
                queued=sum(1 for cell in column if cell.status == CELL_QUEUED),
                failed=sum(1 for cell in column if cell.status == CELL_FAILED),
                missing=sum(1 for cell in column if cell.status == CELL_MISSING),
                submitted=sum(
                    1 for cell in column if cell.status != CELL_MISSING
                ),
                late=sum(1 for cell in column if cell.late),
                needs_review=sum(1 for cell in column if cell.needs_review),
                average=average(scores),
            )
        )

    all_cells = [cell for row in rows for cell in row.cells]
    all_scores = [
        cell.score
        for cell in all_cells
        if cell.status == CELL_GRADED and cell.score is not None
    ]
    stats = ClassStatistics(
        students=len(students),
        assignments=len(assignments),
        graded=len(all_scores),
        queued=sum(1 for cell in all_cells if cell.status == CELL_QUEUED),
        failed=sum(1 for cell in all_cells if cell.status == CELL_FAILED),
        missing=sum(1 for cell in all_cells if cell.status == CELL_MISSING),
        submitted=sum(1 for cell in all_cells if cell.status != CELL_MISSING),
        late=sum(1 for cell in all_cells if cell.late),
        needs_review=sum(1 for cell in all_cells if cell.needs_review),
        average=average(all_scores),
        possible_submissions=len(students) * len(assignments),
    )
    return Gradebook(
        group=group,
        assignments=assignments,
        rows=tuple(rows),
        assignment_stats=tuple(assignment_stats),
        stats=stats,
    )


def build_student_profile(
    db: Session, teacher: User, student: User
) -> StudentProfile | None:
    """Cross-class performance for one student.

    Only classes owned by ``teacher`` are included; a student enrolled solely
    in another teacher's class yields ``None`` (the route turns that into a
    404, never a hint that the account exists).
    """
    if student.role != ROLE_STUDENT:
        return None
    groups = tuple(
        db.scalars(
            select(ClassGroup)
            .join(Enrollment, Enrollment.class_id == ClassGroup.id)
            .where(
                Enrollment.student_id == student.id,
                ClassGroup.teacher_id == teacher.id,
            )
            .order_by(ClassGroup.name, ClassGroup.id)
        ).all()
    )
    if not groups:
        return None

    classes: list[ProfileClass] = []
    all_scores: list[int] = []
    history: list[ProfileHistory] = []
    for group in groups:
        book = build_gradebook(db, group)
        row = next(
            (item for item in book.rows if item.student.id == student.id), None
        )
        if row is None:  # pragma: no cover - enrollment and roster agree
            continue
        entries = tuple(
            ProfileEntry(assignment=assignment, cell=cell)
            for assignment, cell in zip(book.assignments, row.cells)
        )
        scores = [
            cell.score
            for cell in row.cells
            if cell.status == CELL_GRADED and cell.score is not None
        ]
        all_scores.extend(scores)
        historical = sum(1 for cell in row.cells if cell.historical)
        for assignment, cell in zip(book.assignments, row.cells):
            if cell.status == CELL_GRADED and cell.score is not None:
                history.append(
                    ProfileHistory(
                        class_name=group.name,
                        assignment_title=assignment.title,
                        score=cell.score,
                        graded_at=cell.graded_at,
                        historical=cell.historical,
                    )
                )
        classes.append(
            ProfileClass(
                group=group,
                entries=entries,
                average=average(scores),
                graded=row.graded_count,
                submitted=row.submitted_count,
                missing=row.missing_count,
                failed=row.failed_count,
                queued=row.queued_count,
                late=row.late_count,
                needs_review=row.needs_review_count,
                historical=historical,
            )
        )

    history.sort(
        key=lambda item: item.graded_at or datetime.min,
        reverse=True,
    )
    return StudentProfile(
        student=student,
        classes=tuple(classes),
        average=average(all_scores),
        graded=sum(item.graded for item in classes),
        submitted=sum(item.submitted for item in classes),
        missing=sum(item.missing for item in classes),
        failed=sum(item.failed for item in classes),
        queued=sum(item.queued for item in classes),
        late=sum(item.late for item in classes),
        needs_review=sum(item.needs_review for item in classes),
        historical=sum(item.historical for item in classes),
        history=tuple(history[:12]),
    )


__all__ = [
    "CELL_BADGES",
    "CELL_FAILED",
    "CELL_GRADED",
    "CELL_LABELS",
    "CELL_MISSING",
    "CELL_QUEUED",
    "AssignmentStats",
    "ClassStatistics",
    "Gradebook",
    "GradebookCell",
    "ProfileClass",
    "ProfileEntry",
    "ProfileHistory",
    "StudentProfile",
    "StudentRow",
    "average",
    "build_gradebook",
    "build_student_profile",
    "gradebook_assignments",
]
