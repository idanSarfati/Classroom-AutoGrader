"""Interactive test CLI: preview TURNED_IN Google Doc submissions.

Usage (from the project root):

    python -m src.fetch_submissions
    python src/fetch_submissions.py   # also works (adds the root to sys.path)

Flow: pick an active course -> pick a published assignment -> fetch
TURNED_IN submissions -> extract each attached Google Doc -> print a
``Student Name | Submission Status | First 3 lines of code`` preview.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

# Support running as a plain script (python src/fetch_submissions.py) in
# addition to `python -m src.fetch_submissions`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.bidi_utils import bidi_print  # noqa: E402
from src.classroom_service import ClassroomService, ClassroomServiceError  # noqa: E402
from src.models import Course, CourseWork, StudentSubmission  # noqa: E402

UNSUPPORTED_ATTACHMENT_MSG = "Unsupported attachment type"
PREVIEW_LINE_COUNT = 3
_NAME_WIDTH = 24
_STATE_WIDTH = 12


def _prompt_index(count: int, label: str) -> Optional[int]:
    """Ask for a 1-based index; re-prompt until valid. None = user quit."""
    while True:
        try:
            raw = input(f"Select {label} [1-{count}] (q to quit): ").strip()
        except EOFError:
            return None
        if raw.lower() in {"q", "quit", "exit"}:
            return None
        try:
            choice = int(raw)
        except ValueError:
            print("  Please enter a number.")
            continue
        if 1 <= choice <= count:
            return choice - 1
        print(f"  Number out of range 1-{count}.")


def _print_courses(courses: list[Course]) -> None:
    print("\nActive courses:")
    for index, course in enumerate(courses, start=1):
        section = f" ({bidi_print(course.section)})" if course.section else ""
        print(f"  [{index}] {bidi_print(course.name)}{section}")


def _print_course_work(course_name: str, items: list[CourseWork]) -> None:
    print(f"\nPublished assignments in '{bidi_print(course_name)}':")
    for index, item in enumerate(items, start=1):
        due = (
            f" - due {item.due_date.isoformat()}"
            if item.due_date
            else " - no due date"
        )
        print(f"  [{index}] {bidi_print(item.title)}{due}")


def _print_preview(
    service: ClassroomService, submissions: list[StudentSubmission]
) -> None:
    """Print Name | Status | first-3-code-lines rows, flagging unsupported work."""
    extracted = 0
    unsupported = 0
    errors = 0

    print()
    print(
        f"{'Student Name':<{_NAME_WIDTH}} | "
        f"{'Status':<{_STATE_WIDTH}} | First {PREVIEW_LINE_COUNT} lines of extracted code"
    )
    print("-" * (_NAME_WIDTH + _STATE_WIDTH + 44))

    for submission in submissions:
        if not submission.doc_id:
            unsupported += 1
            print(
                f"{bidi_print(submission.student_name):<{_NAME_WIDTH}} | "
                f"{submission.state:<{_STATE_WIDTH}} | {UNSUPPORTED_ATTACHMENT_MSG}"
            )
            continue

        try:
            submission.raw_code = service.extract_doc_text(submission.doc_id)
        except ClassroomServiceError as exc:
            errors += 1
            print(
                f"{bidi_print(submission.student_name):<{_NAME_WIDTH}} | "
                f"{submission.state:<{_STATE_WIDTH}} | Error reading document: {exc}"
            )
            continue

        extracted += 1
        lines = submission.raw_code.splitlines()[:PREVIEW_LINE_COUNT]
        if not lines:
            print(
                f"{bidi_print(submission.student_name):<{_NAME_WIDTH}} | "
                f"{submission.state:<{_STATE_WIDTH}} | (document is empty)"
            )
            continue
        # First line on the main row; remaining preview lines hang-indent
        # under the code column to keep the table aligned.
        print(
            f"{bidi_print(submission.student_name):<{_NAME_WIDTH}} | "
            f"{submission.state:<{_STATE_WIDTH}} | {bidi_print(lines[0])}"
        )
        for line in lines[1:]:
            print(
                f"{'':<{_NAME_WIDTH}} | {'':<{_STATE_WIDTH}} | {bidi_print(line)}"
            )

    print(
        f"\nPreviewed {len(submissions)} submission(s): "
        f"{extracted} extracted, {unsupported} {UNSUPPORTED_ATTACHMENT_MSG}, "
        f"{errors} error(s)."
    )


def main() -> int:
    """Run the interactive course -> assignment -> submission preview."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        service = ClassroomService()
    except Exception as exc:  # noqa: BLE001 - entry point reports and exits
        print(f"Failed to initialize Google API clients: {exc}", file=sys.stderr)
        return 1

    try:
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

        work_items = service.list_course_work(course.id)
        if not work_items:
            print(f"No published assignments found in '{bidi_print(course.name)}'.")
            return 0
        _print_course_work(course.name, work_items)
        work_index = _prompt_index(len(work_items), "assignment")
        if work_index is None:
            print("Cancelled.")
            return 0
        assignment = work_items[work_index]

        submissions = service.get_submissions(course.id, assignment.id)
        turned_in = [
            submission
            for submission in submissions
            if submission.state == "TURNED_IN"
        ]
        print(
            f"\n{len(turned_in)} TURNED_IN submission(s) "
            f"(of {len(submissions)} total) for '{bidi_print(assignment.title)}'."
        )
        if not turned_in:
            print("Nothing to preview yet.")
            return 0

        _print_preview(service, turned_in)
        return 0
    except ClassroomServiceError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
