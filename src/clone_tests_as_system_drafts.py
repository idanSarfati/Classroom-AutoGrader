"""Clone the 3 Python test versions as Developer-owned drafts under a new
bochean rovaqnui topic in the next courses (z3, z4, t2, ch4).

Why this exists
----------------
Google Classroom only lets a Developer Console project modify course work -
and later push grades / return submissions - that the *same* OAuth client
created. So the 3 test versions must be (re)created *through* this project as
DRAFTs, with each .docx uploaded to Drive and attached as a material, all
filed under a brand-new bochean rovaqnui topic.

Nothing is published: everything lands as DRAFT and the teacher opens the
Classroom UI to set a due date and publish when ready.

Usage (from the project root)::

    python -m src.clone_tests_as_system_drafts          # plan only (dry run)
    python -m src.clone_tests_as_system_drafts --apply   # create for real
    python -m src.clone_tests_as_system_drafts --list-courses
    python -m src.clone_tests_as_system_drafts --course "z3" --apply

The drafts are idempotent: a re-run skips any draft whose title already exists
in the course, so running twice never duplicates work.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

# Support running as a plain script (python src/clone_tests_as_system_drafts.py).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.bidi_utils import bidi_print  # noqa: E402
from src.classroom_service import (  # noqa: E402
    ClassroomService,
    ClassroomServiceError,
)
from src.fetch_submissions import (  # noqa: E402 - shared prompt/print helpers
    _print_courses,
)
from src.migrate_class_assignments import (  # noqa: E402 - shared matcher
    _matches_query,
)
from src.models import Course  # noqa: E402

logger = logging.getLogger(__name__)

# The three test versions the teacher prepared locally.
DOWNLOADS_DIR = Path.home() / "Downloads"
TEST_FILES: tuple[Path, ...] = (
    DOWNLOADS_DIR / "Unit 3 Input Command Version A.docx",
    DOWNLOADS_DIR / "Unit 3 Input Command Version B.docx",
    DOWNLOADS_DIR / "Unit 3 Input Command Version C.docx",
)

# The new Classroom topic that groups all of these drafts.
TOPIC_NAME = "בוחן רבעוני"

# Courses that should receive the drafts, matched against course name/section.
TARGET_QUERIES: tuple[str, ...] = ("ח4", "ט3", "ח3", "ט4")

# Suffix appended to each cloned title so a re-run spots drafts we already
# created and skips them (idempotency).
MANAGED_SUFFIX = " [system]"

DESCRIPTION = (
    "Automatically created by the system as a draft under the project ownership, "
    "under the 'bochean rovaqnui' topic. Set a due date and publish in the class "
    "screen as needed."
)
def _draft_title(stem: str) -> str:
    return f"{stem}{MANAGED_SUFFIX}"


def _base_title(title: str) -> str:
    return title[: -len(MANAGED_SUFFIX)] if title.endswith(MANAGED_SUFFIX) else title


def _upload_test_files(service: ClassroomService) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    print(f"\nUploading {len(TEST_FILES)} test file(s) to Google Drive...")
    for path in TEST_FILES:
        if not path.is_file():
            raise FileNotFoundError(f"Test file not found: {path}")
        info = service.upload_drive_file(path, path.name)
        files.append(info)
        print(f"  Uploaded: {info['name']} (id={info['id']})")
    return files


def _ensure_topic(service: ClassroomService, course_id: str) -> str:
    topics = service.list_topics(course_id)
    if TOPIC_NAME in topics:
        topic_id = list(topics.keys())[list(topics.values()).index(TOPIC_NAME)]
        print(f"  Topic '{TOPIC_NAME}' already exists in course {course_id} (id={topic_id})")
        return topic_id
    print(f"  Creating topic '{TOPIC_NAME}' in course {course_id}...")
    topic = service.create_topic(course_id, TOPIC_NAME)
    print(f"  Created topic '{TOPIC_NAME}' (id={topic.get('topicId', topic.get('id', 'unknown'))})")
    return topic["topicId"] if "topicId" in topic else topic["id"]


def _resolve_targets(courses, queries=None):
    import re
    query_list = queries if queries else TARGET_QUERIES
    targets = []
    for query in query_list:
        for course in courses:
            if _matches_query(course.name, query) or _matches_query(course.section, query):
                targets.append((query, course))
                break
    return targets


def _create_drafts(service, course, topic_id, files, apply):
    created = skipped = failed = 0
    for file_info in files:
        title = _draft_title(file_info["name"])
        existing = service.list_course_work_items(course.id, states=("PUBLISHED", "DRAFT"))
        existing = [cw for cw in existing if cw.get("title") == title]
        if existing and not apply:
            skipped += 1
            continue
        body = {
            "title": title,
            "description": DESCRIPTION,
            "workType": "assignment",
            "state": "draft",
            "associatedWithDeveloper": True,
            "topicId": topic_id,
            "materials": [{"driveFile": {"fileId": file_info["id"]}}],
        }
        if apply:
            created_cw = service.courses_coursework_create(course.id, body)
            created += 1
            print(f"  Created draft: {bidi_print(created_cw.title)} in {bidi_print(course.name)}")
        else:
            skipped += 1
    return created, skipped, failed


def _parse_args(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Clone test versions as System-owned drafts in Classroom.")
    parser.add_argument(
        "--course",
        help="Course name, section, or id to target (overrides the default z3 / z4 / t2 / ch4 list; repeatable).",
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
    parser.add_argument(
        "--list-courses",
        action="store_true",
        help="List the active courses and exit.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")

    try:
        service = ClassroomService()
    except Exception as exc:
        print(f"Failed to initialize Google API clients: {exc}", file=sys.stderr)
        return 1

    try:
        courses = service.list_courses()
    except ClassroomServiceError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1

    if args.list_courses:
        _print_courses(list(courses))
        return 0

    if not courses:
        print("No active courses found.")
        return 0

    queries = tuple(args.course) if args.course else TARGET_QUERIES
    targets = _resolve_targets(courses, queries)
    if not targets:
        print("No target courses could be resolved - nothing to do.")
        return 0

    try:
        files = _upload_test_files(service)
    except (FileNotFoundError, ClassroomServiceError) as exc:
        print(f"Failed to upload test files: {exc}", file=sys.stderr)
        return 1

    total_created = total_skipped = total_failed = 0
    for query, course in targets:
        created = skipped = failed = 0
        try:
            topic_id = _ensure_topic(service, course.id)
            created, skipped, failed = _create_drafts(service, course, topic_id, files, args.apply)
        except ClassroomServiceError as exc:
            print(f"\nAPI error for '{bidi_print(course.name)}': {exc}")
            failed += len(TEST_FILES)
            continue
        total_created += created
        total_skipped += skipped
        total_failed += failed

    mode = "created" if args.apply else "planned"
    print(
        f"\nDone: {total_created} draft {mode} "
        f"({total_skipped} already existed, {total_failed} failed)."
    )
    if args.apply and total_failed:
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