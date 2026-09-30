"""Pilot utility: clone a class's assignments as Developer-owned drafts.

Why this exists
---------------
Google Classroom only lets a Developer Console project modify the course work -
and the student submissions - that the *same* OAuth client created (see
``courses.courseWork.create`` and
``courses.courseWork.studentSubmissions.patch``: "PERMISSION_DENIED if the
requesting developer project did not create the corresponding course work").
Assignments typed by hand in the Classroom UI therefore cannot be graded and
returned by :mod:`src.main`, even though reading their submissions works fine.

This one-off pilot tool re-creates one course's assignments *through* our
Developer Console project as DRAFTs, so the clones can later receive grades,
comments and returns. It never modifies the originals, never publishes
anything, and defaults to a dry run that only prints the plan.

Usage (from the project root)::

    python -m src.migrate_class_assignments                     # dry run, pick course
    python -m src.migrate_class_assignments --course "2ט"       # match name/section/id
    python -m src.migrate_class_assignments --list-courses
    python -m src.migrate_class_assignments --course "2ט" --apply

After a successful ``--apply``: open the new drafts in the Classroom UI, adjust
the due date (it is copied from the original), publish them, and grade them as
usual. The originals can be archived once the clones are in use.

Safety notes
------------
* Only ``ASSIGNMENT`` course work is cloned; quiz questions are ignored.
  Learning materials (``courses.courseWorkMaterials``) and topics are separate
  Classroom resources and are never created or modified here.
* Clones are always created in ``DRAFT`` state - publishing stays a deliberate
  action by the teacher.
* The tool is idempotent: an existing managed copy with the same base title is
  detected (management tag and/or ``associatedWithDeveloper`` ownership) and
  skipped unless ``--force`` is passed.
* This script is a pilot scaffold; delete it once every class is migrated.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

# Support running as a plain script (python src/migrate_class_assignments.py).
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
    _prompt_index,
)
from src.models import Course  # noqa: E402

# Suffix appended to every cloned title, so managed copies are easy to spot in
# the Classroom UI and easy to detect on a later run (pass --tag '' to keep the
# original titles).
DEFAULT_TAG = "[Auto]"

# Tags used by earlier manual cloning experiments: titles ending with one of
# these also count as "already managed", so the pilot never clones twice.
DEFAULT_LEGACY_TAGS: tuple[str, ...] = ("[Managed Test]",)

# The only work type this pilot clones: real submission assignments. Quiz
# questions (SHORT_ANSWER_QUESTION / MULTIPLE_CHOICE_QUESTION) are skipped.
CLONEABLE_WORK_TYPE = "ASSIGNMENT"

# Classwork fields copied verbatim from the source item. Everything else is
# either read-only/server-generated (id, courseId, creationTime, updateTime,
# creatorUserId, alternateLink, associatedWithDeveloper, studentWorkFolder) or
# deliberately excluded: ``title``/``state`` are ours to set, and
# ``scheduledTime`` cannot be combined with a DRAFT.
COPYABLE_FIELDS: tuple[str, ...] = (
    "description",
    "materials",
    "maxPoints",
    "workType",
    "dueDate",
    "dueTime",
    "topicId",
    "assigneeMode",
    "individualStudentsOptions",
    "gradeCategory",
    "submissionModificationMode",
)

# A title such as "1.2 - הגשת משימות [Auto]" -> its trailing management tag.
_TRAILING_TAG = re.compile(r"\s*\[(?P<tag>[^\[\]]{1,40})\]\s*$")


def _normalized_title(title: str) -> str:
    """Title without a trailing management tag, casefolded for comparison."""
    return " ".join(_TRAILING_TAG.sub("", title or "").split()).casefold()


def _trailing_tag(title: str) -> str:
    """The trailing ``[...]`` tag of a title, or '' when there is none."""
    match = _TRAILING_TAG.search(title or "")
    return match.group("tag").strip() if match else ""


def is_developer_owned(item: dict[str, Any]) -> bool:
    """True when this coursework belongs to our Developer Console project.

    Classroom sets ``associatedWithDeveloper`` on items that the Developer
    Console project of the requesting OAuth client created; only those items
    can be graded, commented on, and returned by this tool.
    """
    return bool(item.get("associatedWithDeveloper"))


def _format_due(item: dict[str, Any]) -> str:
    """Render ``dueDate``/``dueTime`` of a coursework item, or '' if unset."""
    due = item.get("dueDate") or {}
    if not (due.get("year") and due.get("month") and due.get("day")):
        return ""
    stamp = f"{due['year']:04d}-{due['month']:02d}-{due['day']:02d}"
    due_time = item.get("dueTime") or {}
    if "hours" in due_time:
        hours = due_time.get("hours", 0)
        minutes = due_time.get("minutes", 0)
        return f"{stamp} {hours:02d}:{minutes:02d}"
    return stamp


def find_managed_equivalent(
    source: dict[str, Any],
    items: Sequence[dict[str, Any]],
    tags: Sequence[str],
) -> Optional[dict[str, Any]]:
    """Return the already-managed copy of ``source``, when one exists.

    An item counts as the managed copy when it is a *different* coursework
    item with the same base title (its own trailing tag ignored) and either

    * ends with one of the known management ``tags`` (``[Auto]`` or a legacy
      tag from an earlier manual cloning experiment), or
    * is associated with our Developer Console project
      (``associatedWithDeveloper``), i.e. a previous migration created it.

    The source item itself never matches, so an untagged original is still
    cloned even when several items happen to share a title.
    """
    base = _normalized_title(source.get("title", ""))
    if not base:
        return None
    wanted = {tag.casefold() for tag in tags if tag and tag.strip()}
    for item in items:
        if item.get("id") and item.get("id") == source.get("id"):
            continue
        if _normalized_title(item.get("title", "")) != base:
            continue
        tag = _trailing_tag(item.get("title", ""))
        if (tag and tag.casefold() in wanted) or is_developer_owned(item):
            return item
    return None


def build_draft_body(
    source: dict[str, Any], target_title: str
) -> dict[str, Any]:
    """Build the ``courseWork.create`` body cloning ``source`` as a draft.

    Only writable classwork fields are forwarded (:data:`COPYABLE_FIELDS`);
    read-only and server-generated values are dropped, ``scheduledTime`` is
    never copied (a DRAFT cannot be scheduled), and ``state`` is left to
    :meth:`ClassroomService.create_course_work_draft`, which forces DRAFT.
    """
    body: dict[str, Any] = {"title": target_title}
    for name in COPYABLE_FIELDS:
        if name in source:
            body[name] = source[name]
    # ``individualStudentsOptions`` is only meaningful with INDIVIDUAL_STUDENTS;
    # sending it otherwise would restrict the clone unexpectedly.
    if body.get("assigneeMode") != "INDIVIDUAL_STUDENTS":
        body.pop("individualStudentsOptions", None)
    return body


@dataclass
class MigrationPlan:
    """Decision taken for one coursework item of the pilot course."""

    source: dict[str, Any]
    action: Literal["CLONE", "SKIP"]
    target_title: str
    reason: str = ""
    existing: Optional[dict[str, Any]] = None

    @property
    def source_title(self) -> str:
        """Title of the original item (as shown in Classroom)."""
        return self.source.get("title", "").strip() or "Untitled assignment"


def plan_migration(
    items: Sequence[dict[str, Any]],
    *,
    tag: str = DEFAULT_TAG,
    legacy_tags: Sequence[str] = DEFAULT_LEGACY_TAGS,
    force: bool = False,
) -> tuple[list[MigrationPlan], int]:
    """Decide CLONE/SKIP for every ASSIGNMENT in ``items``.

    Returns the plans (in API order) plus the number of non-assignment items
    that were ignored. ``force`` clones even when a managed equivalent exists.
    """
    tags = [value for value in (tag, *legacy_tags) if value and value.strip()]
    plans: list[MigrationPlan] = []
    ignored = 0
    for item in items:
        if item.get("workType", CLONEABLE_WORK_TYPE) != CLONEABLE_WORK_TYPE:
            ignored += 1
            continue
        # Never re-clone items that are already managed drafts created by developer
        if is_developer_owned(item) and not force:
            continue
        source_title = item.get("title", "").strip() or "Untitled assignment"
        target_title = (
            f"{source_title} {tag}".strip() if tag.strip() else source_title
        )
        existing = find_managed_equivalent(item, items, tags)
        if existing is not None and not force:
            plans.append(
                MigrationPlan(
                    source=item,
                    action="SKIP",
                    target_title=target_title,
                    reason="managed equivalent already exists",
                    existing=existing,
                )
            )
            continue
        plans.append(
            MigrationPlan(
                source=item,
                action="CLONE",
                target_title=target_title,
                reason=(
                    "forced re-clone; a managed equivalent already exists"
                    if existing is not None
                    else ""
                ),
                existing=existing,
            )
        )
    return plans, ignored


def _describe_source(item: dict[str, Any], topics: dict[str, str]) -> str:
    """One-line description of a source item (state, topic, points, due...)."""
    bits = [item.get("state", "?")]
    topic_id = item.get("topicId")
    bits.append(
        f"topic: {topics.get(topic_id, topic_id)}" if topic_id else "no topic"
    )
    if "maxPoints" in item:
        bits.append(f"{item['maxPoints']} pts")
    due = _format_due(item)
    if due:
        bits.append(f"due {due}")
    materials = item.get("materials") or []
    if materials:
        bits.append(f"{len(materials)} material(s)")
    bits.append(
        "owned by this project"
        if is_developer_owned(item)
        else "NOT owned by this project"
    )
    return " | ".join(bits)


def print_plan(
    course: Course,
    plans: Sequence[MigrationPlan],
    ignored: int,
    topics: dict[str, str],
    tag: str,
) -> None:
    """Print the dry-run report: what would be cloned and what is skipped."""
    clones = [plan for plan in plans if plan.action == "CLONE"]
    skips = [plan for plan in plans if plan.action == "SKIP"]

    print(f"\nPilot course: {bidi_print(course.name)}  (id: {course.id})")
    if course.section:
        print(f"  section: {bidi_print(course.section)}")
    print(
        f"Coursework: {len(plans) + ignored} item(s) -> {len(plans)} "
        f"ASSIGNMENT(s), {ignored} non-assignment item(s) ignored"
    )
    print(
        "  (learning materials and topics are separate Classroom resources "
        "and are never cloned)"
    )
    if tag.strip():
        print(f"  clone title suffix: {tag!r}")
    else:
        print("  clone title suffix: none (clones keep the original title)")

    for index, plan in enumerate(plans, start=1):
        print(f"\n  [{index}] {plan.action}  {bidi_print(plan.source_title)}")
        print(f"       source : {_describe_source(plan.source, topics)}")
        if plan.action == "CLONE":
            print(f"       target : {bidi_print(plan.target_title)}  (DRAFT)")
            if plan.source.get("scheduledTime"):
                print(
                    "       note   : scheduled publish time is not copied - "
                    "publish the draft by hand"
                )
            if plan.reason:
                print(f"       note   : {plan.reason}")
            continue
        existing = plan.existing or {}
        owned = (
            "developer-owned"
            if is_developer_owned(existing)
            else "NOT developer-owned"
        )
        print(
            f"       skipped: {plan.reason} -> "
            f"{bidi_print(existing.get('title', ''))} "
            f"({existing.get('state', '?')}, id {existing.get('id', '?')}, "
            f"{owned})"
        )
        if not is_developer_owned(existing):
            print(
                "       warn   : grades cannot be pushed to that copy; "
                "re-run with --force to create an owned clone"
            )

    print(
        f"\nSummary: {len(clones)} draft(s) to create, "
        f"{len(skips)} already managed, {ignored} non-assignment ignored."
    )


def _confirm_apply(count: int, course_name: str) -> bool:
    """Ask before creating anything (EOF counts as 'no')."""
    while True:
        try:
            raw = input(
                f"Create {count} draft(s) in '{bidi_print(course_name)}'? "
                "(yes/no): "
            ).strip().lower()
        except EOFError:
            return False
        if raw in {"yes", "y"}:
            return True
        if raw in {"no", "n"}:
            return False
        print("  Please answer yes or no.")


def apply_migration(
    service: ClassroomService,
    course: Course,
    plans: Sequence[MigrationPlan],
) -> int:
    """Create the planned draft clones and verify Developer ownership.

    Returns 0 when every planned draft was created, 1 otherwise. Failures are
    reported per item and never abort the batch, so a partially migrated class
    can simply be re-run (the smart skip makes that idempotent).
    """
    clones = [plan for plan in plans if plan.action == "CLONE"]
    if not clones:
        print("\nNothing to create: every assignment is already managed.")
        return 0

    created = 0
    owned = 0
    failed = 0
    print(
        f"\nCreating {len(clones)} draft(s) in '{bidi_print(course.name)}' "
        "(state=DRAFT; originals are not touched)..."
    )
    for index, plan in enumerate(clones, start=1):
        body = build_draft_body(plan.source, plan.target_title)
        try:
            item = service.create_course_work_draft(course.id, body)
        except ClassroomServiceError as exc:
            failed += 1
            print(
                f"  [{index}/{len(clones)}] "
                f"{bidi_print(plan.target_title)}: FAILED - {exc}"
            )
            continue
        created += 1
        if is_developer_owned(item):
            owned += 1
            ownership = "developer-owned"
        else:
            ownership = "NOT developer-owned - pushing grades may still fail"
        print(
            f"  [{index}/{len(clones)}] "
            f"{bidi_print(item.get('title', plan.target_title))}: "
            f"created id {item.get('id', '?')} "
            f"({item.get('state', '?')}, {ownership})"
        )

    print(
        f"\nResult: {created} draft(s) created "
        f"({owned} owned by this Developer Console project), {failed} failed."
    )
    if failed:
        print("Re-run the same command to retry (already managed items are skipped).")
        return 1
    print(
        "Next: open the drafts in Classroom, adjust the due date, publish them, "
        "then grade them as usual."
    )
    return 0


def _strip_bidi_controls(text: str) -> str:
    """Strip Unicode bidirectional formatting characters (RLO, LRE, etc.)."""
    return re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", text or "")


def _normalize_search_tokens(text: str) -> set[str]:
    """Extract alphanumeric/Hebrew word tokens for flexible matching."""
    cleaned = _strip_bidi_controls(text).replace("'", "").replace('"', "").replace("-", " ")
    return set(cleaned.casefold().split())


def _matches_query(target: str, query: str) -> bool:
    """Match query against target string, tolerant of punctuation, tokens, and RTL flipping."""
    clean_target = _strip_bidi_controls(target).casefold()
    clean_query = _strip_bidi_controls(query).casefold()
    if clean_query in clean_target or clean_query[::-1] in clean_target:
        return True

    # Token-based match: check if all query words appear in target
    query_tokens = _normalize_search_tokens(query)
    target_tokens = _normalize_search_tokens(target)
    if query_tokens and query_tokens.issubset(target_tokens):
        return True

    return False


def _resolve_course(
    courses: Sequence[Course], query: Optional[str]
) -> Optional[Course]:
    """Pick the pilot course: exact id, else name/section substring match."""
    if not query:
        _print_courses(list(courses))
        index = _prompt_index(len(courses), "pilot course")
        if index is None:
            print("Cancelled.")
            return None
        return courses[index]

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
            print(
                f"  - {bidi_print(course.name)} (id: {course.id})",
                file=sys.stderr,
            )
        return None

    print(f"'{query}' matches {len(matches)} course(s):")
    for index, course in enumerate(matches, start=1):
        section = f" ({bidi_print(course.section)})" if course.section else ""
        print(f"  [{index}] {bidi_print(course.name)}{section}")
    index = _prompt_index(len(matches), "pilot course")
    if index is None:
        print("Cancelled.")
        return None
    return matches[index]


def _fetch_topics(service: ClassroomService, course: Course) -> dict[str, str]:
    """Topic id -> name, degrading to an empty mapping when unavailable."""
    try:
        return service.list_topics(course.id)
    except ClassroomServiceError as exc:
        print(
            f"Note: topic names unavailable ({exc}); showing topic ids.",
            file=sys.stderr,
        )
        return {}


def run_migration_for_course(
    service: ClassroomService,
    course: Course,
    *,
    tag: str = DEFAULT_TAG,
    legacy_tags: Sequence[str] = DEFAULT_LEGACY_TAGS,
    apply: bool = False,
    force: bool = False,
    assume_yes: bool = False,
) -> int:
    """Plan and optionally apply migration for a single Course."""
    try:
        items = service.list_course_work_items(course.id)
    except ClassroomServiceError as exc:
        print(f"API error for '{bidi_print(course.name)}': {exc}", file=sys.stderr)
        return 1
    if not items:
        print(f"No coursework found in '{bidi_print(course.name)}'.")
        return 0

    topics = _fetch_topics(service, course)
    plans, ignored = plan_migration(
        items, tag=tag, legacy_tags=legacy_tags, force=force
    )
    print_plan(course, plans, ignored, topics, tag)

    clones = [plan for plan in plans if plan.action == "CLONE"]
    if not apply:
        if clones:
            print(
                "\nDRY RUN - nothing was created. Re-run with --apply to "
                f"create {len(clones)} draft(s)."
            )
        return 0
    if not clones:
        print("\nNothing to create: every assignment is already managed.")
        return 0
    if not assume_yes and not _confirm_apply(len(clones), course.name):
        print("Nothing was created.")
        return 0
    return apply_migration(service, course, plans)


def run_migration(
    service: ClassroomService,
    course_query: Optional[str] = None,
    *,
    all_courses: bool = False,
    tag: str = DEFAULT_TAG,
    legacy_tags: Sequence[str] = DEFAULT_LEGACY_TAGS,
    apply: bool = False,
    force: bool = False,
    assume_yes: bool = False,
) -> int:
    """Plan - and optionally apply - migration for one or all courses.

    Always prints the plan first (dry run), so the result can be verified
    before anything is created. Returns 0 on success (dry run included) and
    1 when an API call failed.
    """
    try:
        courses = service.list_courses()
    except ClassroomServiceError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1
    if not courses:
        print("No active courses found.")
        return 0

    if all_courses:
        print(f"\nFound {len(courses)} active course(s). Running migration across all classes...")
        overall_exit = 0
        total_created = 0
        for idx, course in enumerate(courses, start=1):
            print(f"\n{'=' * 60}")
            print(f"[{idx}/{len(courses)}] Course: {bidi_print(course.name)} (ID: {course.id})")
            print(f"{'=' * 60}")
            ret = run_migration_for_course(
                service,
                course,
                tag=tag,
                legacy_tags=legacy_tags,
                apply=apply,
                force=force,
                assume_yes=assume_yes,
            )
            if ret != 0:
                overall_exit = 1
        return overall_exit

    course = _resolve_course(courses, course_query)
    if course is None:
        return 0

    return run_migration_for_course(
        service,
        course,
        tag=tag,
        legacy_tags=legacy_tags,
        apply=apply,
        force=force,
        assume_yes=assume_yes,
    )


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the pilot CLI flags."""
    parser = argparse.ArgumentParser(
        prog="python -m src.migrate_class_assignments",
        description=(
            "Pilot: re-create one class's ASSIGNMENT coursework as drafts "
            "owned by this Developer Console project, so the auto-grader can "
            "push grades, comments and returns for them."
        ),
    )
    parser.add_argument(
        "--course",
        help=(
            "Pilot course name, section, or course id - prompted for when "
            "omitted (e.g. --course \"2ט\")."
        ),
    )
    parser.add_argument(
        "--tag",
        default=DEFAULT_TAG,
        help=(
            "Suffix appended to cloned titles so managed copies are easy to "
            f"spot (default: {DEFAULT_TAG!r}; pass '' to keep titles as-is)."
        ),
    )
    parser.add_argument(
        "--legacy-tag",
        action="append",
        dest="legacy_tags",
        metavar="TAG",
        help=(
            "Extra title suffix that marks an existing item as already "
            "managed, on top of the built-in "
            f"{', '.join(DEFAULT_LEGACY_TAGS)} (repeatable)."
        ),
    )
    parser.add_argument(
        "--all-courses",
        action="store_true",
        help="Run the migration across every active course.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the drafts for real (default: dry run only).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clone even when a managed equivalent already exists.",
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; see the module docstring for the usage examples."""
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

    if args.list_courses:
        try:
            _print_courses(service.list_courses())
        except ClassroomServiceError as exc:
            print(f"API error: {exc}", file=sys.stderr)
            return 1
        return 0

    legacy_tags = tuple(
        dict.fromkeys((*DEFAULT_LEGACY_TAGS, *(args.legacy_tags or [])))
    )
    return run_migration(
        service,
        args.course,
        all_courses=args.all_courses,
        tag=args.tag,
        legacy_tags=legacy_tags,
        apply=args.apply,
        force=args.force,
        assume_yes=args.yes,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
