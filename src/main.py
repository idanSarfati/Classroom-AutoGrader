"""Primary CLI: Review & Release grading workflow.

Entry point (from the project root, ``python -m src.main``) offers:

  [1] Evaluate new submissions
      a. Select an active course.
      b. Select a published assignment.
      c. Select an assignment config JSON from ``assignments/``.
      d. Fetch all TURNED_IN submissions.
      e. Extract each Google Doc and evaluate it with Groq.
      f. Show a summary table (Student Name | Score | Tasks Status summary).
      g. Export a Mashov CSV (UTF-8-SIG) and a JSON evaluation report into
         ``exports/<Course>/<Assignment>/`` (folders are created on demand, so
         classes and assignments - including re-submissions - stay apart).
         No Google Classroom state is changed.

  [2] Push grades & feedback from a saved report
      a. List saved reports (``exports/**/*.json``, newest first) and pick one.
      b. Show the student/score summary for review.
      c. On explicit yes/no confirmation, choose the delivery channels, then
         per student: patch the grade, leave ``feedback_hebrew`` as a Drive
         comment on the student's Google Doc, and return the submission.
         Per-student failures never abort the batch, and a refused grade
         (HTTP 403 for coursework this OAuth client did not create) still
         delivers the comment.
      d. A report whose students were all released cleanly is deleted together
         with its CSV; a report with remaining failures is kept so the teacher
         can inspect it and re-run the release.

Transient Google API failures (dropped socket, Windows ``WinError 10053``,
5xx) are retried automatically - see :mod:`src.api_retry`.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Support running as a plain script (python src/main.py).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.api_retry import add_retry_notifier  # noqa: E402
from src.bidi_utils import bidi_print  # noqa: E402
from src.classroom_service import (  # noqa: E402
    ClassroomService,
    ClassroomServiceError,
)
from src.fetch_submissions import (  # noqa: E402 - shared prompt/print helpers
    UNSUPPORTED_ATTACHMENT_MSG,
    _print_course_work,
    _print_courses,
    _prompt_index,
)
from src.late_policy import penalty_for  # noqa: E402
from src.llm_evaluator import (  # noqa: E402
    EVAL_PAUSE_SECONDS,
    LLMEvaluationError,
    evaluate_student_submission,
)
from src.models import (  # noqa: E402
    AssignmentConfig,
    Course,
    CourseWork,
    EvaluationReport,
    GradingResult,
    ReportEntry,
    StudentSubmission,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSIGNMENTS_DIR = PROJECT_ROOT / "assignments"
EXPORTS_DIR = PROJECT_ROOT / "exports"
# Remembers which assignment config JSON belongs to which Google course work,
# so flow [1] skips the config prompt on later runs (key: course_work_id).
ASSIGNMENT_MAPPING_FILE = PROJECT_ROOT / "config" / "assignment_mapping.json"

_STATUS_ORDER = ("CORRECT", "PARTIALLY_CORRECT", "INCORRECT", "MISSING")
_MASHOV_COLUMNS = ("שם התלמיד", "ציון", "משוב מילולי", "סטטוס הגשה")


@dataclass
class GradingRow:
    """One student submission plus its outcome (result or error)."""

    submission: StudentSubmission
    result: Optional[GradingResult] = None
    error: Optional[str] = None


def load_assignment_config(path: Path) -> AssignmentConfig:
    """Load and validate a teacher assignment config JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return AssignmentConfig.model_validate(data)


def list_assignment_configs(directory: Path = ASSIGNMENTS_DIR) -> list[Path]:
    """Return sorted ``*.json`` assignment config files."""
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def load_assignment_mapping(path: Optional[Path] = None) -> dict[str, str]:
    """Return the ``course_work_id -> assignment config`` mapping.

    A missing, unreadable, or malformed mapping file degrades to an empty
    mapping: the user is prompted again and the file is then rewritten.
    """
    if path is None:
        path = ASSIGNMENT_MAPPING_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable assignment mapping %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring malformed assignment mapping %s.", path)
        return {}
    return {
        str(key): str(value)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def resolve_mapped_config(
    course_work_id: str, path: Optional[Path] = None
) -> Optional[Path]:
    """Resolve the remembered config file for ``course_work_id``, if usable.

    Returns ``None`` when nothing is mapped or the mapped file is missing or
    was renamed (explaining why on stderr), so the caller falls back to
    prompting the user.
    """
    configured = load_assignment_mapping(path).get(course_work_id)
    if not configured:
        return None
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    if not candidate.is_file():
        print(
            f"Mapped config '{configured}' for course work {course_work_id} "
            "is missing or was renamed.",
            file=sys.stderr,
        )
        return None
    return candidate


def save_assignment_mapping(
    course_work_id: str,
    config_path: Path,
    path: Optional[Path] = None,
) -> str:
    """Remember ``course_work_id -> config_path``; returns the stored value.

    Paths inside the project are stored relative (portable); other entries
    already in the file are preserved. Raises ``OSError`` when unwritable.
    """
    if path is None:
        path = ASSIGNMENT_MAPPING_FILE
    resolved = config_path.resolve()
    try:
        stored = resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:  # config outside the project -> keep it absolute
        stored = str(resolved)
    mapping = load_assignment_mapping(path)
    mapping[course_work_id] = stored
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return stored


def _safe_filename(name: str, max_len: int = 60) -> str:
    """Make a string safe for Windows file names (keeps Hebrew/letters)."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    return cleaned[:max_len].strip() or "unnamed"


def report_directory(
    course_name: str, assignment_name: str, root: Optional[Path] = None
) -> Path:
    """Folder for one class+assignment: ``exports/<Course>/<Assignment>/``.

    Grouping exports keeps different classes apart and keeps every run of the
    same assignment (original submission, re-submissions, re-grades) together
    instead of mixing them in a flat ``exports/`` listing. ``root`` defaults to
    :data:`EXPORTS_DIR`; the folder itself is created by the writers via
    ``mkdir(parents=True, exist_ok=True)``.
    """
    base = EXPORTS_DIR if root is None else root
    return base / _safe_filename(course_name) / _safe_filename(assignment_name)


def mashov_csv_name(assignment_name: str) -> str:
    """Stable Mashov CSV file name inside one assignment folder.

    The folder already carries the course (and assignment) context, so the
    file itself only needs the assignment name; a stable name means each new
    evaluation overwrites the previous CSV for that assignment.
    """
    return f"{_safe_filename(assignment_name)}.csv"


def _tasks_summary(result: Optional[GradingResult]) -> str:
    """Compact 'CORRECT: 3, MISSING: 1' style summary of task statuses."""
    if result is None:
        return "ERROR"
    counts = {status: 0 for status in _STATUS_ORDER}
    for evaluation in result.task_evaluations:
        counts[evaluation.status] += 1
    parts = [f"{status}: {counts[status]}" for status in _STATUS_ORDER if counts[status]]
    return ", ".join(parts) or "-"


def deductions_summary(result: Optional[GradingResult]) -> str:
    """Compact 'Task 3: -3 (hardcoded string)' summary of the score breakdown.

    Falls back to the raw score/100 gap when the model returned an empty or
    inconsistent breakdown, so the column is never silently blank.
    """
    if result is None:
        return "ERROR"
    if not result.deduction_breakdown:
        return "-" if result.score >= 100 else f"unitemised -{100 - result.score}"
    return "; ".join(
        f"{item.task_name}: -{item.points_deducted} ({item.reason})"
        for item in result.deduction_breakdown
    )


def _print_summary(rows: list[GradingRow]) -> None:
    """Print Student Name | Score | Tasks Status summary."""
    print()
    print(f"{'Student Name':<24} | {'Score':<5} | Tasks Status summary")
    print("-" * 78)
    for row in rows:
        name = bidi_print(row.submission.student_name)
        if row.result is not None:
            print(f"{name:<24} | {row.result.score:<5} | {_tasks_summary(row.result)}")
        else:
            print(f"{name:<24} | {'-':<5} | ERROR: {row.error}")
    for row in rows:
        if row.result is not None and row.result.deduction_breakdown:
            print()
            print(f"{bidi_print(row.submission.student_name)}:")
            for item in row.result.deduction_breakdown:
                print(f"  -{item.points_deducted}  {item.task_name}: {item.reason}")


def write_mashov_csv(
    rows: list[GradingRow],
    course_name: str,
    assignment_name: str,
    directory: Optional[Path] = None,
) -> Path:
    """Write a Mashov-friendly CSV (UTF-8-SIG BOM for Excel/Hebrew).

    Defaults to the class/assignment folder
    ``exports/<Course>/<Assignment>/`` and creates it when missing.
    """
    if directory is None:
        directory = report_directory(course_name, assignment_name)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / mashov_csv_name(assignment_name)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_MASHOV_COLUMNS)
        for row in rows:
            if row.result is not None:
                grade: object = row.result.score
                feedback = row.result.feedback_hebrew
            else:
                grade = ""
                feedback = f"לא ניתן להעריך: {row.error}"
            writer.writerow(
                [
                    row.submission.student_name,
                    grade,
                    feedback,
                    row.submission.state,
                ]
            )
    return target


def _confirm_publish() -> bool:
    """Ask for explicit yes/no consent before touching Classroom (EOF = no)."""
    while True:
        try:
            raw = input(
                "Publish grades & comments and return submissions to students? "
                "(yes/no): "
            ).strip().lower()
        except EOFError:
            return False
        if raw in {"yes", "y"}:
            return True
        if raw in {"no", "n"}:
            return False
        print("  Please answer yes or no.")


def _prompt_yes_no(prompt: str, default: bool = True) -> bool:
    """Generic yes/no prompt; ``default`` is used on a bare Enter (EOF = no)."""
    suffix = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{prompt} ({suffix}): ").strip().lower()
        except EOFError:
            return False
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("  Please answer yes or no.")


def _ask_publish_options() -> dict[str, bool]:
    """Ask which delivery channels to use for this release run.

    Pushing a Classroom grade fails with HTTP 403 when the coursework was not
    created by this project's OAuth client (a very common setup, because
    assignments are usually made in the Classroom UI). Leaving the feedback
    as a comment on the student's document needs only Drive access, so it is
    offered as the fallback that still reaches the student.
    """
    print("\nHow should feedback be delivered?")
    publish_grades = _prompt_yes_no("  Publish grades to Google Classroom?", True)
    post_comment = _prompt_yes_no(
        "  Leave a comment on the student's Google Doc?", True
    )
    return_submissions = _prompt_yes_no("  Return submissions to students?", True)
    if not publish_grades and not post_comment:
        print("  Warning: no feedback channel selected - only returns will happen.")
    return {
        "publish_grades": publish_grades,
        "post_comment": post_comment,
        "return_submissions": return_submissions,
    }


def _list_report_files(directory: Optional[Path] = None) -> list[Path]:
    """Saved evaluation reports anywhere under ``exports/``, newest first.

    Recursive on purpose: reports live in ``exports/<Course>/<Assignment>/``,
    and legacy flat ``exports/*.json`` files are still picked up. ``directory``
    defaults to :data:`EXPORTS_DIR` at call time.
    """
    root = EXPORTS_DIR if directory is None else directory
    if not root.is_dir():
        return []
    files = [path for path in root.rglob("*.json") if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def _display_path(path: Path) -> str:
    """Compact label for menus: relative to ``exports/`` when possible."""
    try:
        inside = path.resolve().relative_to(EXPORTS_DIR.resolve())
    except (OSError, ValueError):
        return str(path)
    return f"exports/{inside.as_posix()}"


def _is_inside_exports(path: Path) -> bool:
    """Safety guard: cleanup may only ever touch files under ``exports/``."""
    try:
        path.resolve().relative_to(EXPORTS_DIR.resolve())
    except (OSError, ValueError):
        return False
    return True


def _report_artifacts(report_path: Path, report: EvaluationReport) -> list[Path]:
    """Files produced by one evaluation run: its JSON plus its Mashov CSV.

    The CSV is resolved from the name recorded in the report, falling back to
    the current layout (``<Assignment>.csv`` beside the report) and to the
    pre-hierarchy flat layout (``<Course>_<Assignment>.csv`` in ``exports/``).
    """
    names: list[str] = []
    recorded = (report.csv_file or "").strip()
    if recorded:
        # Store/treat only the file name: never trust a report with a path.
        names.append(Path(recorded).name)
    names.append(mashov_csv_name(report.course_work_title))
    names.append(
        f"{_safe_filename(report.course_name)}_"
        f"{_safe_filename(report.course_work_title)}.csv"
    )

    artifacts = [report_path]
    for name in names:
        for directory in (report_path.parent, EXPORTS_DIR):
            candidate = directory / name
            if (
                candidate not in artifacts
                and candidate.is_file()
                and _is_inside_exports(candidate)
            ):
                artifacts.append(candidate)
    return artifacts


def _delete_report_artifacts(
    report_path: Path, report: EvaluationReport
) -> list[Path]:
    """Delete a fully released report and its CSV; returns what was removed.

    Every deletion is best effort: an unreadable/locked file is logged and
    skipped rather than aborting the release summary, and anything outside
    ``exports/`` is refused outright. Empty class/assignment folders left
    behind are pruned so the tree does not accumulate dead directories.
    """
    removed: list[Path] = []
    for path in _report_artifacts(report_path, report):
        if not _is_inside_exports(path):
            logger.warning("Refusing to delete %s (outside exports/).", path)
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
            continue
        removed.append(path)
    _prune_empty_directories(report_path.parent)
    return removed


def _prune_empty_directories(directory: Path) -> None:
    """Remove empty folders left after a cleanup, stopping below ``exports/``."""
    root = EXPORTS_DIR.resolve()
    current = directory
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return
        # Never remove ``exports/`` itself, and never leave the export tree.
        if resolved == root or not _is_inside_exports(current):
            return
        try:
            current.rmdir()  # only succeeds while the folder is empty
        except OSError:
            return
        current = current.parent


def main() -> int:
    """Entry point: choose the evaluation or the release workflow."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Mirror the Streamlit retry notices on the console so a silent pause
    # during a long Google API call is visible here too.
    add_retry_notifier(lambda message: print(f"  [retry] {message}"))

    try:
        service = ClassroomService()
    except Exception as exc:  # noqa: BLE001 - entry point reports and exits
        print(f"Failed to initialize Google API clients: {exc}", file=sys.stderr)
        return 1

    _print_menu()
    choice = _prompt_index(2, "option")
    if choice is None:
        print("Cancelled.")
        return 0
    try:
        if choice == 0:
            return _run_evaluation_flow(service)
        return _run_release_flow(service)
    except ClassroomServiceError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1


def _run_evaluation_flow(service: ClassroomService) -> int:
    """Menu [1]: evaluate TURNED_IN submissions; export CSV + JSON report."""
    try:
        # (a) course
        courses = service.list_courses()
        if not courses:
            print("No active courses found.")
            return 0
        _print_courses(courses)
        course_index = _prompt_index(len(courses), "course")
        if course_index is None:
            print("Cancelled.")
            return 0
        course = courses[course_index]

        # (b) assignment
        work_items = service.list_course_work(course.id)
        if not work_items:
            print(
                f"No published assignments found in '{bidi_print(course.name)}'."
            )
            return 0
        _print_course_work(course.name, work_items)
        work_index = _prompt_index(len(work_items), "assignment")
        if work_index is None:
            print("Cancelled.")
            return 0
        assignment = work_items[work_index]

        # (c) assignment config JSON - remembered per course work
        config_path = resolve_mapped_config(assignment.id)
        config: Optional[AssignmentConfig] = None
        if config_path is not None:
            print(
                f"\nUsing mapped config for course work {assignment.id}:"
                f" {bidi_print(config_path.name)}"
            )
            try:
                config = load_assignment_config(config_path)
            except (OSError, ValueError) as exc:
                print(
                    f"Mapped config '{config_path}' is unusable ({exc}); "
                    "please select a config again.",
                    file=sys.stderr,
                )
                config_path = None
        if config is None:
            configs = list_assignment_configs()
            if not configs:
                print(
                    f"No assignment config JSON files found in '{ASSIGNMENTS_DIR}'.",
                    file=sys.stderr,
                )
                return 1
            print("\nAssignment config files:")
            for index, path in enumerate(configs, start=1):
                print(f"  [{index}] {bidi_print(path.name)}")
            config_index = _prompt_index(len(configs), "assignment config")
            if config_index is None:
                print("Cancelled.")
                return 0
            config_path = configs[config_index]
            try:
                config = load_assignment_config(config_path)
            except (OSError, ValueError) as exc:
                print(
                    f"Could not load config '{config_path.name}': {exc}",
                    file=sys.stderr,
                )
                return 1
            try:
                stored = save_assignment_mapping(assignment.id, config_path)
            except OSError as exc:
                print(
                    f"Warning: could not save the assignment mapping: {exc}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Remembered: course work {assignment.id} -> "
                    f"{bidi_print(stored)} (no prompt next time)"
                )
        print(
            f"Using config: {bidi_print(config.assignment_id)} - "
            f"{bidi_print(config.title)} ({len(config.tasks)} tasks)"
        )

        # (d) TURNED_IN submissions
        submissions = service.get_submissions(course.id, assignment.id)
        turned_in = [
            submission
            for submission in submissions
            if submission.state == "TURNED_IN"
        ]
        if not turned_in:
            print("No TURNED_IN submissions to grade.")
            return 0

        # (e) extract + evaluate each
        print(
            f"\nGrading {len(turned_in)} TURNED_IN submission(s) "
            f"with {settings.groq_model}..."
        )
        rows: list[GradingRow] = []
        for position, submission in enumerate(turned_in, start=1):
            prefix = (
                f"  [{position}/{len(turned_in)}] "
                f"{bidi_print(submission.student_name)}"
            )
            if not submission.doc_id:
                rows.append(
                    GradingRow(submission, error=UNSUPPORTED_ATTACHMENT_MSG)
                )
                print(f"{prefix}: skipped ({UNSUPPORTED_ATTACHMENT_MSG})")
                continue
            try:
                doc_text = service.extract_doc_text(submission.doc_id)
            except ClassroomServiceError as exc:
                rows.append(
                    GradingRow(
                        submission, error=f"Document extraction failed: {exc}"
                    )
                )
                print(f"{prefix}: extraction failed")
                continue
            try:
                # Resolve the penalty once: is_late() logs when it has to fall
                # back to timestamps, and re-asking would double that noise.
                late_penalty = penalty_for(
                    submission, assignment, settings.late_penalty_points
                )
                result = evaluate_student_submission(
                    submission.student_name,
                    doc_text,
                    config,
                    late_penalty_points=late_penalty,
                )
                rows.append(GradingRow(submission, result=result))
                late_note = " (LATE)" if late_penalty else ""
                print(f"{prefix}: score {result.score}{late_note}")
            except LLMEvaluationError as exc:
                rows.append(
                    GradingRow(submission, error=f"LLM evaluation failed: {exc}")
                )
                print(f"{prefix}: evaluation failed")
            # Pace sequential Groq completions so back-to-back long outputs do
            # not burst the per-minute output-token (OTPM) budget.
            if position < len(turned_in):
                time.sleep(EVAL_PAUSE_SECONDS)

        # (f) review table
        _print_summary(rows)

        # (g) Mashov CSV (UTF-8-SIG for Excel/Hebrew)
        try:
            csv_path = write_mashov_csv(rows, course.name, assignment.title)
            print(f"\nMashov CSV exported: {bidi_print(_display_path(csv_path))}")
        except (OSError, UnicodeError) as exc:
            print(f"CSV export failed: {exc}", file=sys.stderr)

        # (h) JSON report for the Review & Release workflow (menu option 2)
        try:
            report_path = write_evaluation_report(rows, course, assignment, config)
            print(
                "Evaluation report saved: "
                f"{bidi_print(_display_path(report_path))}"
            )
        except (OSError, UnicodeError) as exc:
            print(f"Report export failed: {exc}", file=sys.stderr)

        print("\nReview the report (edit scores/feedback if needed), then run")
        print("option [2] to release grades, comments, and return submissions.")
        return 0
    except ClassroomServiceError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1


def _print_menu() -> None:
    """Show the top-level workflow choice."""
    print("\nClassroom AutoGrader")
    print("  [1] Evaluate new submissions")
    print("  [2] Push grades & feedback from a saved report")


def write_evaluation_report(
    rows: list[GradingRow],
    course: Course,
    assignment: CourseWork,
    config: AssignmentConfig,
    directory: Optional[Path] = None,
) -> Path:
    """Save graded rows as a timestamped JSON report (Review & Release).

    Written to ``exports/<Course>/<Assignment>/<timestamp>.json`` (created on
    demand) next to the Mashov CSV of the same run; the CSV file name is
    recorded in the report so the release flow can pair - and later clean up -
    both files without guessing.
    """
    if directory is None:
        directory = report_directory(course.name, assignment.title)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = directory / f"{stamp}.json"
    # Two runs inside the same second must not overwrite each other's report.
    suffix = 2
    while target.exists():
        target = directory / f"{stamp}-{suffix}.json"
        suffix += 1
    report = EvaluationReport(
        created_at=datetime.now().astimezone(),
        model=settings.groq_model,
        course_id=course.id,
        course_name=course.name,
        course_work_id=assignment.id,
        course_work_title=assignment.title,
        assignment_config_id=config.assignment_id,
        csv_file=mashov_csv_name(assignment.title),
        entries=[
            ReportEntry(
                student_id=row.submission.student_id,
                student_name=row.submission.student_name,
                submission_id=row.submission.submission_id,
                doc_id=row.submission.doc_id,
                result=row.result,
            )
            for row in rows
            if row.result is not None
        ],
    )
    target.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def _finish_release_cleanup(
    report_path: Path,
    report: EvaluationReport,
    unreleased: list[tuple[str, str]],
) -> None:
    """Delete a fully released report, or keep it when students remain.

    A report is only removed once every entry actually reached the student
    (grade set, comment posted or intentionally skipped, submission returned).
    Any remaining failure keeps the JSON - and its CSV - in ``exports/`` so the
    teacher can inspect the report and re-run option [2].
    """
    if unreleased:
        print(
            "\nReport kept for follow-up: "
            f"{bidi_print(_display_path(report_path))}"
        )
        for student_name, reason in unreleased:
            print(f"  still pending: {bidi_print(student_name)} - {reason}")
        print("Fix the failures above, then run option [2] again.")
        return

    removed = _delete_report_artifacts(report_path, report)
    if not removed:
        print("\nAll students released - no report files left to remove.")
        return
    print("\nAll students released - removed:")
    for path in removed:
        print(f"  {bidi_print(_display_path(path))}")
    print("Run option [1] again if you need the Mashov CSV back.")


def _run_release_flow(service: ClassroomService) -> int:
    """Menu [2]: review a saved report, release grades/comments, then tidy up."""
    reports = _list_report_files()
    if not reports:
        print(
            f"No evaluation reports (*.json) found under '{EXPORTS_DIR}' "
            "(run option [1] first)."
        )
        return 0
    print("\nSaved evaluation reports (most recent first):")
    for index, path in enumerate(reports, start=1):
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        print(
            f"  [{index}] {bidi_print(_display_path(path))}  "
            f"({modified.strftime('%Y-%m-%d %H:%M')})"
        )
    report_index = _prompt_index(len(reports), "report")
    if report_index is None:
        print("Cancelled.")
        return 0
    report_path = reports[report_index]
    try:
        report = EvaluationReport.model_validate(
            json.loads(report_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError) as exc:  # ValueError covers pydantic + JSON
        print(f"Could not load report: {exc}", file=sys.stderr)
        return 1

    print(
        f"\nReport: {bidi_print(report.course_name)} | "
        f"{bidi_print(report.course_work_title)} "
        f"({report.created_at.strftime('%Y-%m-%d %H:%M')})"
    )
    print(f"{'Student Name':<24} | Score | Feedback (preview)")
    print("-" * 78)
    for entry in report.entries:
        preview = " ".join(entry.result.feedback_hebrew.split())[:48]
        print(
            f"{bidi_print(entry.student_name):<24} | "
            f"{entry.result.score:<5} | {bidi_print(preview)}"
        )
    if not report.entries:
        print("Report contains no graded entries.")
        return 0

    if not _confirm_publish():
        print("Nothing was published.")
        return 0

    options = _ask_publish_options()
    publish_grades = options["publish_grades"]
    post_comment = options["post_comment"]
    return_submissions = options["return_submissions"]

    total = len(report.entries)
    grades = 0
    comments = 0
    returns = 0
    comment_skips = 0
    comment_errors = 0
    duplicates_removed = 0
    failures = 0
    # Per-student record of everything that did NOT reach the student, so the
    # report can be kept for a follow-up run instead of being deleted.
    unreleased: list[tuple[str, str]] = []
    print()
    for position, entry in enumerate(report.entries, start=1):
        name = bidi_print(entry.student_name)
        parts: list[str] = []
        feedback = entry.result.feedback_hebrew.strip()
        student_failed = False

        # 1. Classroom grade. May be restricted (HTTP 403) for coursework this
        #    OAuth client did not create - the comment below still runs.
        grade_published = not publish_grades
        if publish_grades:
            try:
                service.update_submission_grade(
                    report.course_id,
                    report.course_work_id,
                    entry.submission_id,
                    float(entry.result.score),
                )
                grades += 1
                grade_published = True
                parts.append("grade ok")
            except ClassroomServiceError as exc:
                failures += 1
                student_failed = True
                unreleased.append((entry.student_name, f"grade failed: {exc}"))
                parts.append(f"GRADE FAILED ({exc})")
        else:
            parts.append("grade skipped")

        # 2. Leave the Hebrew feedback as a comment on the student's document.
        #    Works even when the grade above was refused, and never modifies
        #    the student's own text.
        if not post_comment:
            parts.append("comment skipped (option off)")
        elif entry.doc_id and feedback:
            try:
                outcome = service.publish_feedback_comment(
                    entry.doc_id, feedback, score=entry.result.score
                )
                comments += 1
                removed = int(outcome.get("duplicatesRemoved", 0) or 0)
                duplicates_removed += removed
                label = "comment updated" if outcome.get("action") == "updated" else "comment ok"
                parts.append(label + (f" (+{removed} duplicate removed)" if removed else ""))
            except ClassroomServiceError as exc:
                comment_errors += 1
                student_failed = True
                unreleased.append(
                    (entry.student_name, f"comment failed: {exc}")
                )
                parts.append(f"COMMENT FAILED ({exc})")
        else:
            comment_skips += 1
            parts.append("comment skipped (no doc/feedback)")

        # 3. Return the submission - never for a student left ungraded.
        if not return_submissions:
            parts.append("return skipped (option off)")
        elif not grade_published:
            parts.append("return skipped (grade not published)")
        else:
            try:
                service.return_student_submission(
                    report.course_id, report.course_work_id, entry.submission_id
                )
                returns += 1
                parts.append("returned")
            except ClassroomServiceError as exc:
                failures += 1
                student_failed = True
                unreleased.append((entry.student_name, f"return failed: {exc}"))
                parts.append(f"RETURN FAILED ({exc})")
        print(f"  [{position}/{total}] {name}: " + ", ".join(parts))
        if student_failed:
            logger.warning("Release had failures for %s", entry.student_name)

    print(
        f"\nRelease complete: {grades} grade(s) published, "
        f"{comments} comment(s) left on the Google Docs, "
        f"{returns} submission(s) returned."
    )
    if duplicates_removed:
        print(
            f"  Cleaned up {duplicates_removed} duplicate AutoGrader comment(s) "
            "from earlier runs."
        )
    if comment_skips or comment_errors or failures:
        print(
            f"  Note: {comment_skips} comment(s) skipped, "
            f"{comment_errors} comment error(s), "
            f"{failures} grade/return failure(s)."
        )

    _finish_release_cleanup(report_path, report, unreleased)
    return 0 if failures == 0 else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
