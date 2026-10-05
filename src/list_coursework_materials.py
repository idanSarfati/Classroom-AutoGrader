"""Interactive CLI: list a course's assignments and their Drive materials.

Why this exists
---------------
Building a rubric config (``assignments/*.json``) needs more than what the
Streamlit UI shows. A lesson assignment typically carries its *instructions*
as an attached Google Slides deck or Google Doc, while the tasks the student
must satisfy live in a second file (a worksheet table, a code template).
Knowing which attachments exist - their kind, Drive id, and link - is what
makes it possible to read the right sources before writing the config.

This tool reuses the existing :mod:`src.classroom_service` calls
(``list_courses``, ``list_course_work_items``, ``list_topics``) and adds a
Drive metadata lookup so an attachment is reported as a *Google Doc*, *Slides*
or *Sheet* rather than an opaque id. Nothing is created, modified, or
published: it only reads.

Usage (from the project root)::

    python -m src.list_coursework_materials                     # pick a course
    python -m src.list_coursework_materials --all-courses       # every course
    python -m src.list_coursework_materials --course "ט4"
    python -m src.list_coursework_materials --include-drafts    # drafts too
    python -m src.list_coursework_materials --json dump.json    # machine-readable

``--json`` writes the same listing (with the resolved Drive metadata) to a
file, so a later step can consume it without re-querying the API.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

# Support running as a plain script (python src/list_coursework_materials.py).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.bidi_utils import bidi_print  # noqa: E402
from src.classroom_service import (  # noqa: E402
    ClassroomService,
    ClassroomServiceError,
    _drive_file_id_from_link,  # noqa: E402 - shared Drive-id parsing
)
from src.fetch_submissions import (  # noqa: E402 - shared prompt/print helpers
    _print_courses,
    _prompt_index,
)
from src.migrate_class_assignments import (  # noqa: E402 - shared matcher
    _format_due,
    _matches_query,
)
from src.models import Course  # noqa: E402

logger = logging.getLogger(__name__)

# Material kinds reported by ``courseWork.materials[].materials[]``.
MATERIAL_KIND_DRIVE = "drive_file"
MATERIAL_KIND_LINK = "link"
MATERIAL_KIND_YOUTUBE = "youtube_video"
MATERIAL_KIND_FORM = "form"

# Human labels for the Drive mime types this project actually meets. Anything
# else falls back to the raw mimeType, so an unexpected file type stays
# visible rather than being silently labelled.
MIME_LABELS: dict[str, str] = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.form": "Google Form",
    "application/vnd.google-apps.drawing": "Google Drawing",
    "application/vnd.google-apps.script": "Apps Script",
    "application/pdf": "PDF",
}

_GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

# Long descriptions are truncated to keep the listing readable; the full text
# always remains available through ``--json``.
DESCRIPTION_PREVIEW_CHARS = 300


def describe_mime(mime_type: Optional[str]) -> str:
    """Human label for a Drive mimeType, e.g. ``Google Doc``."""
    if not mime_type:
        return "unknown type"
    return MIME_LABELS.get(mime_type) or (
        "Google Drive file" if mime_type.startswith(_GOOGLE_APPS_PREFIX) else mime_type
    )


@dataclass
class MaterialInfo:
    """One attachment of an assignment, flattened for printing."""

    kind: str
    title: str
    file_id: Optional[str] = None
    url: Optional[str] = None
    # Populated from the Drive API when a lookup is possible.
    mime_type: Optional[str] = None
    drive_name: Optional[str] = None
    web_view_link: Optional[str] = None
    readable: Optional[bool] = None


@dataclass
class AssignmentInfo:
    """One coursework item plus its flattened attachments."""

    id: str
    title: str
    state: str
    work_type: str
    topic: Optional[str] = None
    max_points: Optional[int] = None
    due: str = ""
    description: str = ""
    developer_owned: bool = False
    materials: list[MaterialInfo] = field(default_factory=list)


def _clean(text: Any) -> str:
    """Collapse whitespace of a possibly-missing API string field."""
    return " ".join(str(text or "").split())


def _preview(description: str) -> str:
    """One-line, truncated view of a (possibly HTML) coursework description."""
    text = _clean(re.sub(r"<[^>]+>", " ", description or ""))
    if len(text) > DESCRIPTION_PREVIEW_CHARS:
        return f"{text[:DESCRIPTION_PREVIEW_CHARS].rstrip()}..."
    return text


def _drive_url(file_id: str) -> str:
    """Canonical Drive URL for a file id (no API call needed)."""
    return f"https://drive.google.com/file/d/{file_id}/view"
def _describe_one(
    material: dict[str, Any], group_title: str
) -> Optional[MaterialInfo]:
    """Flatten one ``Material`` into a :class:`MaterialInfo`.

    Returns ``None`` for a material carrying no recognised payload, so an
    unfamiliar attachment type is logged and skipped rather than printed as an
    empty row.
    """
    drive_file = material.get("driveFile")
    if drive_file:
        link = _clean(drive_file.get("alternateLink"))
        file_id = _clean(drive_file.get("id")) or _drive_file_id_from_link(link)
        return MaterialInfo(
            kind=MATERIAL_KIND_DRIVE,
            title=_clean(drive_file.get("title")) or group_title,
            file_id=file_id or None,
            url=link or (_drive_url(file_id) if file_id else None),
        )

    link_material = material.get("link")
    if link_material:
        url = _clean(link_material.get("url"))
        return MaterialInfo(
            kind=MATERIAL_KIND_LINK,
            title=_clean(link_material.get("title")) or group_title or url,
            url=url or None,
        )

    video = material.get("youtubeVideo")
    if video:
        video_id = _clean(video.get("id"))
        title = group_title or (
            f"YouTube video {video_id}" if video_id else "YouTube video"
        )
        return MaterialInfo(
            kind=MATERIAL_KIND_YOUTUBE,
            title=title,
            url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        )

    form = material.get("form")
    if form:
        return MaterialInfo(
            kind=MATERIAL_KIND_FORM,
            title=group_title or "Google Form",
            url=_clean(form.get("formUrl")) or None,
        )

    logger.warning(
        "Unsupported material type under '%s': %s",
        group_title or "untitled material",
        ", ".join(sorted(material)) or "(empty)",
    )
    return None


def describe_materials(item: dict[str, Any]) -> list[MaterialInfo]:
    """Flatten every attachment of one coursework item.

    Classroom nests attachments two deep - ``materials[]`` groups, each with its
    own ``materials[]`` of actual materials - and the useful name is often on
    the *group* ("הוראות", "דף עבודה") rather than on the file itself, so the
    group title is kept as the fallback name.
    """
    results: list[MaterialInfo] = []
    for group in item.get("materials") or []:
        group_title = _clean(group.get("title"))
        for material in group.get("materials") or []:
            described = _describe_one(material, group_title)
            if described is not None:
                results.append(described)
    return results


def describe_assignment(
    item: dict[str, Any], topics: Optional[dict[str, str]] = None
) -> AssignmentInfo:
    """Build the printable view of one raw coursework payload."""
    topic_id = item.get("topicId")
    max_points = item.get("maxPoints")
    return AssignmentInfo(
        id=_clean(item.get("id")),
        title=_clean(item.get("title")) or "Untitled assignment",
        state=_clean(item.get("state")) or "PUBLISHED",
        work_type=_clean(item.get("workType")) or "ASSIGNMENT",
        topic=(topics or {}).get(topic_id, topic_id) if topic_id else None,
        max_points=int(max_points) if isinstance(max_points, (int, float)) else None,
        due=_format_due(item),
        description=_preview(_clean(item.get("description"))),
        developer_owned=bool(item.get("associatedWithDeveloper")),
        materials=describe_materials(item),
    )
def enrich_with_drive_metadata(
    service: ClassroomService, assignments: Sequence[AssignmentInfo]
) -> None:
    """Fill in mimeType / name / link for every Drive attachment, in place.

    Each distinct file is looked up once, even when several assignments share
    it. A file we cannot read simply keeps its reported kind - the listing
    still shows the id and link the teacher can open by hand.
    """
    cache: dict[str, Optional[dict[str, Any]]] = {}
    for assignment in assignments:
        for material in assignment.materials:
            if material.kind != MATERIAL_KIND_DRIVE or not material.file_id:
                continue
            if material.file_id not in cache:
                cache[material.file_id] = service.get_drive_file_info(
                    material.file_id
                )

    for assignment in assignments:
        for material in assignment.materials:
            if material.kind != MATERIAL_KIND_DRIVE or not material.file_id:
                continue
            info = cache.get(material.file_id)
            material.readable = info is not None
            if not info:
                continue
            material.mime_type = info.get("mimeType")
            material.drive_name = info.get("name")
            material.web_view_link = info.get("webViewLink") or material.url


_MATERIAL_LABELS = {
    MATERIAL_KIND_DRIVE: "Drive file",
    MATERIAL_KIND_LINK: "Link",
    MATERIAL_KIND_YOUTUBE: "Video",
    MATERIAL_KIND_FORM: "Form",
}


def _print_material(index: int, material: MaterialInfo) -> None:
    """Print one attachment line (and its Drive detail line, when relevant)."""
    label = _MATERIAL_LABELS.get(material.kind, material.kind)
    title = bidi_print(material.title) if material.title else "(no title)"
    print(f"      ({index}) [{label}] {title}")
    details: list[str] = []
    if material.kind == MATERIAL_KIND_DRIVE:
        details.append(describe_mime(material.mime_type))
        if material.file_id:
            details.append(f"id: {material.file_id}")
        if material.readable is False:
            details.append("NOT readable with the current OAuth grant")
    if material.url:
        details.append(material.url)
    if details:
        print(f"          {' | '.join(details)}")


def _print_assignment(index: int, assignment: AssignmentInfo, total: int) -> None:
    """Print one assignment header and its attachments."""
    print(f"\n  [{index}/{total}] {bidi_print(assignment.title)}")
    print(f"      id: {assignment.id} | {assignment.state} | {assignment.work_type}")
    meta: list[str] = []
    if assignment.topic:
        meta.append(f"topic: {bidi_print(str(assignment.topic))}")
    if assignment.max_points is not None:
        meta.append(f"{assignment.max_points} pts")
    meta.append(f"due: {assignment.due}" if assignment.due else "no due date")
    meta.append(
        "developer-owned" if assignment.developer_owned else "NOT developer-owned"
    )
    print(f"      {' | '.join(meta)}")
    if assignment.description:
        print(f"      description: {bidi_print(assignment.description)}")
    if not assignment.materials:
        print("      materials: (none attached)")
        return
    print(f"      materials ({len(assignment.materials)}):")
    for material_index, material in enumerate(assignment.materials, start=1):
        _print_material(material_index, material)


def list_course_assignments(
    service: ClassroomService,
    course: Course,
    *,
    include_drafts: bool = False,
    drive_lookup: bool = True,
) -> list[AssignmentInfo]:
    """Fetch and flatten every assignment of one course.

    Topic names and Drive metadata are best-effort: a course whose topics (or
    files) cannot be read still gets a full assignment list, with ids.
    """
    states = ("PUBLISHED", "DRAFT") if include_drafts else ("PUBLISHED",)
    raw_items = service.list_course_work_items(course.id, states=states)

    try:
        topics = service.list_topics(course.id)
    except ClassroomServiceError as exc:
        logger.warning("Could not list topics for '%s': %s", course.id, exc)
        topics = {}

    assignments = [describe_assignment(item, topics) for item in raw_items]
    if drive_lookup:
        enrich_with_drive_metadata(service, assignments)
    return assignments


def _resolve_course(
    courses: Sequence[Course], query: Optional[str]
) -> Optional[Course]:
    """Pick a course: exact id first, else a tolerant name/section match."""
    if not query:
        _print_courses(list(courses))
        index = _prompt_index(len(courses), "course")
        return None if index is None else courses[index]

    raw_query = query.strip()
    for course in courses:
        if course.id == raw_query:
            return course

    matches = [
        course
        for course in courses
        if _matches_query(course.name or "", raw_query)
        or _matches_query(course.section or "", raw_query)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(
            f"No active course matches '{query}'. Available courses:",
            file=sys.stderr,
        )
        for course in courses:
            print(f"  - {bidi_print(course.name)} (id: {course.id})", file=sys.stderr)
        return None

    print(f"'{query}' matches {len(matches)} course(s):")
    for index, course in enumerate(matches, start=1):
        section = f" ({bidi_print(course.section)})" if course.section else ""
        print(f"  [{index}] {bidi_print(course.name)}{section}")
    index = _prompt_index(len(matches), "course")
    return None if index is None else matches[index]


def _dump_json(
    path: Path, courses: Sequence[Course], listing: dict[str, list[AssignmentInfo]]
) -> None:
    """Write the listing to ``path`` as UTF-8 JSON."""
    payload = {
        "courses": [
            {
                "id": course.id,
                "name": course.name,
                "section": course.section,
                "assignments": [
                    asdict(assignment) for assignment in listing.get(course.id, [])
                ],
            }
            for course in courses
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {path}")


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the CLI flags."""
    parser = argparse.ArgumentParser(
        prog="python -m src.list_coursework_materials",
        description=(
            "List a course's assignments with the Drive files attached to "
            "each one, so the right instruction sources can be picked before "
            "writing an assignments/*.json config."
        ),
    )
    parser.add_argument(
        "--course",
        help=(
            "Course name, section, or course id - prompted for when omitted "
            '(e.g. --course "ט4").'
        ),
    )
    parser.add_argument(
        "--all-courses",
        action="store_true",
        help="List every active course instead of picking one.",
    )
    parser.add_argument(
        "--include-drafts",
        action="store_true",
        help="Include DRAFT coursework (default: PUBLISHED only).",
    )
    parser.add_argument(
        "--no-drive-lookup",
        action="store_true",
        help=(
            "Skip the Drive metadata calls; attachments are printed with their "
            "id and link only."
        ),
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="PATH",
        help="Also write the listing (with Drive metadata) to a JSON file.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; see the module docstring for usage examples."""
    args = _parse_args(argv)
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
    except ClassroomServiceError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1
    if not courses:
        print("No active courses found.")
        return 0

    if args.all_courses:
        selected = list(courses)
    else:
        course = _resolve_course(courses, args.course)
        if course is None:
            print("Cancelled.")
            return 0
        selected = [course]

    listing: dict[str, list[AssignmentInfo]] = {}
    failures = 0
    for course in selected:
        section = f" ({bidi_print(course.section)})" if course.section else ""
        print(f"\n=== {bidi_print(course.name)}{section}  [id: {course.id}] ===")
        try:
            assignments = list_course_assignments(
                service,
                course,
                include_drafts=args.include_drafts,
                drive_lookup=not args.no_drive_lookup,
            )
        except ClassroomServiceError as exc:
            print(
                f"API error for '{bidi_print(course.name)}': {exc}",
                file=sys.stderr,
            )
            failures += 1
            continue

        if not assignments:
            print("  (no assignments)")
            continue
        total = len(assignments)
        for index, assignment in enumerate(assignments, start=1):
            _print_assignment(index, assignment, total)
        listing[course.id] = assignments

    if args.json:
        _dump_json(args.json, selected, listing)

    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)