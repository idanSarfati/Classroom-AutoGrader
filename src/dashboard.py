"""Pending-submission dashboard data and the automated ``DRY_RUN`` pipeline.

Two responsibilities, both deliberately free of any Streamlit import so they
can be unit-tested headlessly (``app.py`` owns the widgets):

1. **Pending detection** - scan the active courses for submissions that are
   ``TURNED_IN`` (handed in) but not yet ``RETURNED`` (graded and handed back),
   grouped by assignment so the teacher sees at a glance what is waiting for
   review.
2. **Automated dry-run evaluation** - run those submissions through the Groq
   evaluator and write the usual report + Mashov CSV **locally** under
   ``exports/``, purely so the predicted scores and feedback can be reviewed on
   screen.

Safety
------
The pipeline runs with :data:`DRY_RUN = True` and wraps the service in
:class:`ReadOnlyClassroomService`, which raises :class:`DryRunWriteViolation`
*before* any mutating method (grade, comment, return, coursework creation) can
be reached. It is therefore structurally impossible for this module to push
grades or feedback back to Google Classroom - publishing stays a deliberate
human act in the Release tab.

Run from the project root::

    python -m pytest tests/test_dashboard_dry_run.py -q
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Optional

from config.settings import settings
from src.classroom_service import ClassroomServiceError
from src.fetch_submissions import UNSUPPORTED_ATTACHMENT_MSG
from src.late_policy import penalty_for
from src.llm_evaluator import LLMEvaluationError, evaluate_student_submission
from src.main import (
    GradingRow,
    _tasks_summary,
    deductions_summary,
    write_evaluation_report,
    write_mashov_csv,
)
from src.models import (
    AssignmentConfig,
    Course,
    CourseWork,
    GradingResult,
    StudentSubmission,
)

logger = logging.getLogger(__name__)

# The pipeline is *always* a dry run. This is not read from the environment on
# purpose: ``settings.dry_run`` is a free-form app-wide preference, whereas the
# dashboard's whole contract is "compute and show, never publish". Making it a
# constant means no configuration can ever turn a dry-run into a write.
DRY_RUN: Final[bool] = True

# Classroom's own verdict: handed in by the student, not yet graded/returned.
# ``RETURNED`` is the graded-and-handed-back state, so it is *not* pending.
PENDING_STATE: Final[str] = "TURNED_IN"

# Every ``ClassroomService`` method that mutates Google Classroom or Drive.
# Listed explicitly (rather than guessed from a naming convention) so adding a
# new write method to the service is a deliberate act that must also be added
# here - see
# ``tests.test_dashboard_dry_run.test_every_classroom_write_method_is_blocked``.
CLASSROOM_WRITE_METHODS: Final[frozenset[str]] = frozenset(
    {
        # Coursework creation (migration pilot).
        "create_course_work_draft",
        # Grade publishing.
        "update_submission_grade",
        # Handing the submission back to the student.
        "return_student_submission",
        # Feedback left as a Drive comment on the student's document.
        "publish_feedback_comment",
        # ...and the comment primitives it is built from.
        "_create_comment",
        "_create_anchored_comment",
        "_delete_comment_quietly",
    }
)


class DryRunWriteViolation(RuntimeError):
    """Raised when dry-run code tries to mutate Google Classroom.

    Raised *before* the underlying call is made, so a violation can never
    reach the API even once.
    """


class ReadOnlyClassroomService:
    """Read-only facade over :class:`ClassroomService` for ``DRY_RUN`` runs.

    Attribute lookup falls through to the wrapped service for every method
    except the ones in :data:`CLASSROOM_WRITE_METHODS`, which raise
    :class:`DryRunWriteViolation`. This is the enforcement mechanism behind
    "never push grades or comments back automatically": the guarantee does not
    rest on remembering not to call something.
    """

    def __init__(self, service: Any) -> None:
        # Idempotent, so wrapping an already-wrapped service is harmless.
        if isinstance(service, ReadOnlyClassroomService):
            service = service._service
        self._service = service

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_service"):
            # ``self._service`` itself is missing - report it normally instead
            # of recursing through this method forever.
            raise AttributeError(name)
        if name in CLASSROOM_WRITE_METHODS:
            raise DryRunWriteViolation(
                f"Refusing to call '{name}': the dry-run pipeline is read-only "
                "and never pushes grades, comments, or returns to Google "
                "Classroom. Publish from the Release tab instead."
            )
        return getattr(self._service, name)

    def __repr__(self) -> str:
        return f"ReadOnlyClassroomService({self._service!r})"


# --------------------------------------------------------------------------- #
# Pending submissions
# --------------------------------------------------------------------------- #


def is_pending_submission(submission: StudentSubmission) -> bool:
    """True when the submission was handed in but not yet graded/returned.

    Classroom reports ``TURNED_IN`` for work a student submitted that has not
    been returned yet, and ``RETURNED`` once it has been graded and handed
    back - so ``TURNED_IN`` alone is exactly "waiting for review". ``NEW`` /
    ``RECLAIMED_BY_STUDENT`` mean the student has not (or no longer) submitted
    and must not show up as pending.
    """
    return (submission.state or "").strip().upper() == PENDING_STATE


def is_late_submission(submission: StudentSubmission) -> bool:
    """True when Classroom reported the submission as late (never guesses)."""
    if submission.is_late is True:
        return True
    return (submission.late_state or "").strip().upper() == "LATE"


@dataclass(frozen=True)
class PendingGroup:
    """One assignment's submissions that are waiting for review."""

    course: Course
    assignment: CourseWork
    submissions: tuple[StudentSubmission, ...]

    @property
    def count(self) -> int:
        return len(self.submissions)

    @property
    def heading(self) -> str:
        """``<course> - <assignment>`` label used by the dashboard."""
        return f"{self.course.name} - {self.assignment.title}"


def collect_pending_submissions(
    service: Any,
    course_ids: Optional[Sequence[str]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> list[PendingGroup]:
    """Scan courses for turned-in-but-unreturned submissions, by assignment.

    ``course_ids`` narrows the scan (``None`` scans every active course). Only
    read methods are used, and the service is wrapped in
    :class:`ReadOnlyClassroomService` first, so this function cannot write even
    if it wanted to.

    Assignments with nothing pending are omitted entirely - an empty section
    per assignment would bury the ones that actually need attention.
    """
    reader = ReadOnlyClassroomService(service)
    courses = reader.list_courses()
    if course_ids is not None:
        wanted = set(course_ids)
        courses = [course for course in courses if course.id in wanted]

    groups: list[PendingGroup] = []
    for course in courses:
        if on_progress:
            on_progress(f"Scanning {course.name} for pending submissions...")
        for assignment in reader.list_course_work(course.id):
            submissions = reader.get_submissions(course.id, assignment.id)
            pending = tuple(
                submission
                for submission in submissions
                if is_pending_submission(submission)
            )
            if not pending:
                continue
            if on_progress:
                on_progress(
                    f"{course.name} / {assignment.title}: "
                    f"{len(pending)} awaiting review"
                )
            groups.append(
                PendingGroup(
                    course=course,
                    assignment=assignment,
                    submissions=pending,
                )
            )
    return groups


@dataclass(frozen=True)
class PendingSummary:
    """Aggregate counters behind the dashboard's metric tiles."""

    course_count: int = 0
    assignment_count: int = 0
    submission_count: int = 0
    late_count: int = 0


def summarize_pending(groups: Sequence[PendingGroup]) -> PendingSummary:
    """Count distinct courses, assignments and submissions across ``groups``."""
    courses = {group.course.id for group in groups}
    submissions = [
        submission for group in groups for submission in group.submissions
    ]
    return PendingSummary(
        course_count=len(courses),
        assignment_count=len(groups),
        submission_count=len(submissions),
        late_count=sum(
            1 for submission in submissions if is_late_submission(submission)
        ),
    )


def pending_rows(group: PendingGroup) -> list[dict[str, Any]]:
    """Display rows for one assignment's pending submissions.

    Returns plain dicts so ``app.py`` only has to hand them to
    ``st.dataframe`` and so the shape can be pinned by a test. ``doc_id`` is
    carried through for the UI to turn into a link.
    """
    rows: list[dict[str, Any]] = []
    for submission in group.submissions:
        submitted = (
            submission.submitted_at.strftime("%Y-%m-%d %H:%M")
            if submission.submitted_at
            else "-"
        )
        rows.append(
            {
                "Student": submission.student_name,
                "Submitted": submitted,
                "Late": (
                    "⚠️ Late" if is_late_submission(submission) else "On time"
                ),
                "Attachment": (
                    "Google Doc" if submission.doc_id else "No extractable doc"
                ),
                "doc_id": submission.doc_id,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Automated dry-run evaluation
# --------------------------------------------------------------------------- #


@dataclass
class DryRunAssignmentResult:
    """Outcome of dry-running one assignment (local files only, never pushed)."""

    course: Course
    assignment: CourseWork
    rows: list[GradingRow] = field(default_factory=list)
    config_path: Optional[Path] = None
    report_path: Optional[Path] = None
    csv_path: Optional[Path] = None
    skipped_reason: Optional[str] = None
    save_error: Optional[str] = None

    @property
    def evaluated(self) -> int:
        return sum(1 for row in self.rows if row.result is not None)

    @property
    def failed(self) -> int:
        return sum(1 for row in self.rows if row.result is None)


def dry_run_rows(result: DryRunAssignmentResult) -> list[dict[str, Any]]:
    """Display rows for one dry-run outcome: predicted score + feedback."""
    rows: list[dict[str, Any]] = []
    for row in result.rows:
        grading: Optional[GradingResult] = row.result
        if grading is not None:
            score: Any = grading.score
            status = _tasks_summary(grading)
            deductions = deductions_summary(grading)
            feedback = grading.feedback_hebrew
            error = ""
        else:
            score = "-"
            status = "ERROR"
            deductions = "-"
            feedback = ""
            error = row.error or "Evaluation failed"
        rows.append(
            {
                "Student": row.submission.student_name,
                "Score": score,
                "Tasks": status,
                "Deductions": deductions,
                "Feedback (Hebrew)": feedback,
                "Error": error,
                "doc_id": row.submission.doc_id,
            }
        )
    return rows


ConfigResolver = Callable[[CourseWork], Optional[tuple[Path, AssignmentConfig]]]
Evaluator = Callable[..., GradingResult]


def run_dry_run_evaluation(
    service: Any,
    groups: Sequence[PendingGroup],
    resolve_config: ConfigResolver,
    *,
    directory: Optional[Path] = None,
    evaluate: Optional[Evaluator] = None,
    on_status: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    pause: Optional[Callable[[], None]] = None,
) -> list[DryRunAssignmentResult]:
    """Evaluate every pending submission and save the reports **locally**.

    This is the automated dry-run: it fetches the pending work, runs it through
    the Groq evaluator, and writes the report JSON + Mashov CSV beside each
    other under ``exports/<Course>/<Assignment>/`` (or ``directory`` when the
    caller supplies one - tests point it at a temp dir).

    What it never does:

    * never sets a grade, never posts a comment, never returns a submission -
      :class:`DryRunWriteViolation` guarantees that at call time;
    * never reaches any other mutating API: only ``list_courses``,
      ``list_course_work``, ``get_submissions`` and ``extract_doc_text`` are
      reachable through :class:`ReadOnlyClassroomService`.

    Arguments
    ----------
    resolve_config:
        ``(assignment) -> (config_path, config) | None``. Returning ``None``
        skips that assignment with a visible reason instead of guessing a
        rubric.
    directory:
        Where to write the artifacts. Defaults to the usual
        ``exports/<Course>/<Assignment>/`` layout.
    evaluate:
        Grading callable; injectable so tests never touch the network.
    pause:
        Called between students (Groq meters output tokens per minute), never
        after the last one.

    ``DRY_RUN`` is asserted at entry so a future refactor cannot silently
    turn this into a publishing path.
    """
    if not DRY_RUN:
        raise RuntimeError(
            "run_dry_run_evaluation must never run with DRY_RUN=False."
        )
    # Resolved here rather than in the signature so tests (and any future
    # caller) can swap the evaluator by patching this module attribute.
    grade_one = evaluate if evaluate is not None else evaluate_student_submission

    reader = ReadOnlyClassroomService(service)
    total = sum(len(group.submissions) for group in groups)
    done = 0
    results: list[DryRunAssignmentResult] = []

    for group in groups:
        resolved = resolve_config(group.assignment)
        if resolved is None:
            if on_status:
                on_status(
                    f"Skipping {group.assignment.title}: no rubric matched."
                )
            results.append(
                DryRunAssignmentResult(
                    course=group.course,
                    assignment=group.assignment,
                    skipped_reason=(
                        "No rubric JSON matched this assignment - pick one in "
                        "the Evaluate tab first."
                    ),
                )
            )
            continue

        config_path, config = resolved
        outcome = DryRunAssignmentResult(
            course=group.course,
            assignment=group.assignment,
            config_path=config_path,
        )

        for submission in group.submissions:
            done += 1
            if on_status:
                on_status(
                    f"[{done}/{total}] {group.assignment.title}: "
                    f"{submission.student_name}..."
                )

            if not submission.doc_id:
                outcome.rows.append(
                    GradingRow(submission, error=UNSUPPORTED_ATTACHMENT_MSG)
                )
            else:
                try:
                    doc_text = reader.extract_doc_text(submission.doc_id)
                except ClassroomServiceError as exc:
                    outcome.rows.append(
                        GradingRow(
                            submission,
                            error=f"Document extraction failed: {exc}",
                        )
                    )
                    _report_progress(on_progress, done, total)
                    continue

                try:
                    result = grade_one(
                        submission.student_name,
                        doc_text,
                        config,
                        late_penalty_points=penalty_for(
                            submission,
                            group.assignment,
                            settings.late_penalty_points,
                        ),
                    )
                    outcome.rows.append(GradingRow(submission, result=result))
                except LLMEvaluationError as exc:
                    outcome.rows.append(
                        GradingRow(
                            submission,
                            error=f"LLM evaluation failed: {exc}",
                        )
                    )

            _report_progress(on_progress, done, total)
            # Groq meters output tokens per minute: pause between students so
            # sequential completions do not burst the OTPM budget (HTTP 429).
            if pause is not None and done < total:
                pause()

        _save_dry_run_artifacts(outcome, config, directory)
        results.append(outcome)

    if on_progress:
        on_progress(1.0)
    return results


def _report_progress(
    on_progress: Optional[Callable[[float], None]], done: int, total: int
) -> None:
    if on_progress and total:
        on_progress(min(done / total, 1.0))


def _save_dry_run_artifacts(
    outcome: DryRunAssignmentResult,
    config: AssignmentConfig,
    directory: Optional[Path],
) -> None:
    """Write the Mashov CSV (always) and the report JSON (when it has entries).

    Both land on the local filesystem only - this is what "generate the
    evaluation reports/scores locally" means. A report with no successful
    evaluation would be an empty husk in the Release tab, so the JSON is skipped
    in that case while the CSV still records what went wrong per student.
    """
    try:
        outcome.csv_path = write_mashov_csv(
            outcome.rows,
            outcome.course.name,
            outcome.assignment.title,
            directory=directory,
        )
        if any(row.result is not None for row in outcome.rows):
            outcome.report_path = write_evaluation_report(
                outcome.rows,
                outcome.course,
                outcome.assignment,
                config,
                directory=directory,
            )
    except Exception as exc:  # noqa: BLE001 - a save failure must not abort the run
        logger.warning(
            "Dry-run could not save artifacts for %s: %s",
            outcome.assignment.title,
            exc,
        )
        outcome.save_error = str(exc)




