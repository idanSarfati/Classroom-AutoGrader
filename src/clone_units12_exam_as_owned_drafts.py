"""Clone units 1-2 quarterly exams (בוחן רבעוני) in course ח4 as autograder-owned drafts.

Google Classroom only allows grade/return writes for course work created by the
same Developer Console project (``associatedWithDeveloper``). The 3 exams
``1 [Auto]``/``2 [Auto]``/``3 [Auto]`` under course ח4, topic ``בוחן רבעוני`` are
foreign-owned, so the autograder cannot grade them. This script creates new
owned DRAFTs (nothing is published) attaching the teacher's .docx files.

Idempotent: re-runs skip titles that already exist as owned drafts.

Usage::
    python -m src.clone_units12_exam_as_owned_drafts        # dry run
    python -m src.clone_units12_exam_as_owned_drafts --apply --yes
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.bidi_utils import bidi_print  # noqa: E402
from src.classroom_service import ClassroomService, ClassroomServiceError  # noqa: E402
from src.fetch_submissions import _print_courses  # noqa: E402
from src.models import Course  # noqa: E402

logger = logging.getLogger(__name__)
DOWNLOADS_DIR = Path.home() / "Downloads"

EXAM_FILES: dict[str, Path] = {
    "1 [Auto]": DOWNLOADS_DIR / "בוחן רבעוני בפייתון - יחידות 1-2 גרסה א.docx",
    "2 [Auto]": DOWNLOADS_DIR / "בוחן רבעוני בפייתון - יחידות 1-2 - גרסה ב.docx",
    "3 [Auto]": DOWNLOADS_DIR / "בוחן רבעוני בפייתון - יחידות 1-2 - גרסה ג.docx",
}
COURSE_ID = "876789046831"  # פייתון א' - ח4
TOPIC_NAME = "בוחן רבעוני"
DESCRIPTION = "Auto-grader owned draft cloned from a foreign quarterly exam. Publish in Classroom UI when ready."

service: Optional[ClassroomService] = None


def _resolve_course(service: ClassroomService) -> Course:
    courses = service.list_courses()
    for c in courses:
        if c.id == COURSE_ID:
            return c
    for c in courses:
        if c.name and ("ח4" in c.name or "ח 4" in c.name):
            return c
    raise ClassroomServiceError(f"Course '{COURSE_ID}' (ח4) not found among active courses.")


def _ensure_topic(service: ClassroomService, course_id: str) -> str:
    topics = service.list_topics(course_id)
    names = {v: k for k, v in topics.items()}
    if TOPIC_NAME in names:
        topic_id = names[TOPIC_NAME]
        print(f"  Topic '{TOPIC_NAME}' already exists (id={topic_id})")
        return topic_id
    print(f"  Creating topic '{TOPIC_NAME}' ...")
    topic = service.create_topic(course_id, TOPIC_NAME)
    return topic["topicId"] if "topicId" in topic else topic["id"]


def _upload_exam_files() -> dict[str, dict[str, Any]]:
    assert service is not None
    files: dict[str, dict[str, Any]] = {}
    print(f"\nUploading {len(EXAM_FILES)} exam file(s) to Google Drive...")
    for title, path in EXAM_FILES.items():
        if not path.is_file():
            raise FileNotFoundError(f"Exam file not found: {path}")
        info = service.upload_drive_file(path, path.name)
        files[title] = info
        print(f"  Uploaded: {info['name']} (id={info['id']})")
    return files


def _owned_draft_exists(service: ClassroomService, course_id: str, title: str) -> bool:
    items = service.list_course_work_items(course_id, states=("PUBLISHED", "DRAFT"))
    return any(
        it.get("title") == title and bool(it.get("associatedWithDeveloper"))
        for it in items
    )


def _create_owned_drafts(
    service: ClassroomService,
    course: Course,
    topic_id: str,
    files: dict[str, dict[str, Any]],
    apply: bool,
) -> tuple[int, int, int]:
    created = skipped = failed = 0
    for title, file_info in files.items():
        if _owned_draft_exists(service, course.id, title):
            print(f"  Skip (owned draft already exists): {title!r}")
            skipped += 1
            continue
        body = {
            "title": title,
            "description": DESCRIPTION,
            "workType": "assignment",
            "state": "DRAFT",
            "topicId": topic_id,
            "materials": [{"driveFile": {"driveFile": {"id": file_info["id"]}, "shareMode": "STUDENT_COPY"}}],
        }
        if apply:
            try:
                cw = service.create_course_work_draft(course.id, body)
                created += 1
                print(
                    f"  Created owned draft: {bidi_print(cw.get('title',''))} "
                    f"(id={cw.get('id')}) in {bidi_print(course.name)}"
                )
            except ClassroomServiceError as exc:
                failed += 1
                print(f"  FAILED {title!r}: {exc}")
        else:
            print(f"  [plan] would create owned draft: {title!r} (file={file_info['id']})")
    return created, skipped, failed


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone units 1-2 quarterly exams as autograder-owned drafts in Classroom ח4."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the drafts for real (default: dry run only).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt when using --apply.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")

    global service
    try:
        service = ClassroomService()
    except Exception as exc:
        print(f"Failed to initialize Google API clients: {exc}", file=sys.stderr)
        return 1

    try:
        course = _resolve_course(service)
    except ClassroomServiceError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1

    print(f"Target course: {bidi_print(course.name)} (id={course.id})")

    try:
        topic_id = _ensure_topic(service, course.id)
        files = _upload_exam_files()
        created, skipped, failed = _create_owned_drafts(service, course, topic_id, files, args.apply)
    except (FileNotFoundError, ClassroomServiceError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1

    mode = "created" if args.apply else "planned"
    print(
        f"\nDone: {created} draft {mode} "
        f"({skipped} already existed, {failed} failed)."
    )
    if args.apply and failed:
        print("Re-run to retry (already-created drafts are skipped).")
        return 1
    if not args.apply:
        print("This was a dry run. Pass --apply to create the drafts for real.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)

