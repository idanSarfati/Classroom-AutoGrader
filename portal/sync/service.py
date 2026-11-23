"""Plan and apply an idempotent expansion of the reference class.

The reference class (ז'3) already holds the imported lessons and assignments.
An expansion run does three things, in this order:

1. match each portal class to its own Google Classroom course;
2. copy only the items the target class does not have yet;
3. set every copy's publication flag independently, using the target course's
   own published/draft state by default.

The planner never writes. The applier re-checks every item before creating it,
so running the same command twice creates nothing the second time and never
duplicates content. Publication changes are applied on every run, which is how
a class moves forward: after the teacher publishes a lesson in Classroom, the
next run publishes it in the portal too.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.ingestion import catalog as catalog_cache
from portal.ingestion import importer
from portal.ingestion.normalize import title_key
from portal.ingestion.types import CourseSnapshot
from portal.models import (
    Assignment,
    AssignmentContent,
    ClassGroup,
    ClassroomLink,
    Lesson,
    LessonSource,
)
from portal.sync.matching import (
    EXCLUDED_CLASSES,
    TARGET_CLASSES,
    TARGET_CLASS_DISPLAY,
    class_key,
    find_course_matches,
    normalize_class_name,
)
from portal.timeutil import utcnow

# Publication modes accepted by the CLI and the web screen.
MODE_CLASSROOM = "classroom"
MODE_DRAFT = "draft"
MODE_PUBLISHED = "published"
MODE_SOURCE = "source"
MODES: tuple[str, ...] = (
    MODE_CLASSROOM,
    MODE_DRAFT,
    MODE_PUBLISHED,
    MODE_SOURCE,
)
MODE_LABELS: dict[str, str] = {
    MODE_CLASSROOM: "לפי מצב הפרסום בקורס Classroom",
    MODE_DRAFT: "הכול כטיוטה",
    MODE_PUBLISHED: "הכול מפורסם",
    MODE_SOURCE: "כמו בתוכן הבסיס",
}

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_KIND_LESSON = "lesson"
_KIND_ASSIGNMENT = "assignment"


class SyncError(RuntimeError):
    """The expansion cannot be planned or applied safely."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceItem:
    """One lesson or assignment in the reference class."""

    kind: str
    id: int
    title: str
    original_title: str = ""
    topic_name: str = ""
    published: bool = False
    position: int = 0


@dataclass
class ItemPlan:
    """What will happen to one source item in one target class."""

    kind: str
    source_id: int
    title: str
    action: str  # create | publish | unpublish | keep
    desired_published: bool
    portal_id: int | None = None
    current_published: bool | None = None
    classroom_state: str = ""  # PUBLISHED | DRAFT | "" (not found in Classroom)
    match: str = ""  # provenance | title | ""
    note: str = ""

    @property
    def is_new(self) -> bool:
        return self.portal_id is None

    @property
    def action_label(self) -> str:
        if self.action == "create":
            return "ייווצר"
        if self.action == "publish":
            return "יפורסם"
        if self.action == "unpublish":
            return "יחזור לטיוטה"
        return "ללא שינוי"

    @property
    def state_label(self) -> str:
        return "מפורסם" if self.desired_published else "טיוטה"


@dataclass
class ClassPlan:
    """One target class in an expansion run."""

    token: str
    display_name: str
    class_id: int | None = None
    class_exists: bool = False
    is_source: bool = False
    course_id: str = ""
    course_name: str = ""
    classroom_name: str = ""
    section: str | None = None
    link_exists: bool = False
    link_will_create: bool = False
    link_will_update: bool = False
    snapshot_available: bool = False
    items: list[ItemPlan] = field(default_factory=list)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def lesson_count(self) -> int:
        return sum(1 for item in self.items if item.kind == _KIND_LESSON)

    @property
    def assignment_count(self) -> int:
        return sum(1 for item in self.items if item.kind == _KIND_ASSIGNMENT)

    @property
    def new_lessons(self) -> int:
        return sum(
            1
            for item in self.items
            if item.kind == _KIND_LESSON and item.action == "create"
        )

    @property
    def new_assignments(self) -> int:
        return sum(
            1
            for item in self.items
            if item.kind == _KIND_ASSIGNMENT and item.action == "create"
        )

    @property
    def publish_count(self) -> int:
        return sum(1 for item in self.items if item.desired_published)

    @property
    def draft_count(self) -> int:
        return sum(1 for item in self.items if not item.desired_published)

    @property
    def change_count(self) -> int:
        return sum(1 for item in self.items if item.action != "keep")


@dataclass
class SyncPlan:
    teacher_id: int
    source_name: str
    source_class_id: int
    mode: str
    classes: list[ClassPlan] = field(default_factory=list)
    source_lessons: int = 0
    source_assignments: int = 0
    catalog_status: str = ""
    catalog_error: str = ""
    catalog_synced_at: datetime | None = None

    @property
    def targets(self) -> list[ClassPlan]:
        return [item for item in self.classes if not item.is_source]

    @property
    def source_class(self) -> ClassPlan | None:
        return next(
            (item for item in self.classes if item.is_source), None
        )

    @property
    def classes_to_create(self) -> int:
        return sum(
            1
            for item in self.classes
            if not item.error and not item.class_exists
        )

    @property
    def links_to_create(self) -> int:
        return sum(1 for item in self.classes if item.link_will_create)

    @property
    def links_to_update(self) -> int:
        return sum(1 for item in self.classes if item.link_will_update)

    @property
    def items_to_create(self) -> int:
        return sum(
            1
            for item in self.classes
            for planned in item.items
            if planned.action == "create"
        )

    @property
    def items_to_publish(self) -> int:
        return sum(
            1
            for item in self.classes
            for planned in item.items
            if planned.action in ("create", "publish")
            and planned.desired_published
        )

    @property
    def items_to_keep_draft(self) -> int:
        return sum(
            1
            for item in self.classes
            for planned in item.items
            if planned.desired_published is False
        )

    @property
    def errors(self) -> list[str]:
        return [
            f"{item.display_name}: {item.error}"
            for item in self.classes
            if item.error
        ]


@dataclass
class SyncOutcome:
    classes_created: int = 0
    links_created: int = 0
    links_updated: int = 0
    lessons_created: int = 0
    assignments_created: int = 0
    published: int = 0
    unpublished: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def created_total(self) -> int:
        return self.lessons_created + self.assignments_created

    def summary_he(self) -> str:
        parts = [
            f"נוצרו {self.classes_created} כיתות",
            f"נוספו {self.lessons_created} שיעורים ו-{self.assignments_created} מטלות",
            f"פורסמו {self.published} פריטים",
            f"נשארו בטיוטה {self.unpublished}",
        ]
        if self.unchanged:
            parts.append(f"{self.unchanged} פריטים ללא שינוי")
        text = ", ".join(parts) + "."
        if self.errors:
            text += " שגיאות: " + " | ".join(self.errors)
        return text


# ---------------------------------------------------------------------------
# Reading the reference class and existing target content
# ---------------------------------------------------------------------------


def _load_source_items(
    db: Session, teacher_id: int, source_group: ClassGroup
) -> list[SourceItem]:
    items: list[SourceItem] = []
    lesson_rows = db.execute(
        select(Lesson, LessonSource)
        .outerjoin(LessonSource, LessonSource.lesson_id == Lesson.id)
        .where(
            Lesson.teacher_id == teacher_id,
            Lesson.class_id == source_group.id,
        )
        .order_by(Lesson.position, Lesson.id)
    ).all()
    for lesson, provenance in lesson_rows:
        items.append(
            SourceItem(
                kind=_KIND_LESSON,
                id=lesson.id,
                title=lesson.title,
                original_title=provenance.original_title if provenance else "",
                topic_name=provenance.topic_name if provenance else "",
                published=bool(lesson.published),
                position=int(getattr(lesson, "position", 0) or 0),
            )
        )

    assignment_rows = db.execute(
        select(Assignment, AssignmentContent)
        .outerjoin(
            AssignmentContent,
            AssignmentContent.assignment_id == Assignment.id,
        )
        .where(Assignment.class_id == source_group.id)
        .order_by(Assignment.id)
    ).all()
    for assignment, content in assignment_rows:
        items.append(
            SourceItem(
                kind=_KIND_ASSIGNMENT,
                id=assignment.id,
                title=assignment.title,
                original_title=content.original_title if content else "",
                topic_name=content.topic_name if content else "",
                published=bool(assignment.published),
            )
        )
    return items


@dataclass
class _ExistingIndex:
    lesson_by_reuse: dict[int, int] = field(default_factory=dict)
    lesson_by_key: dict[str, int] = field(default_factory=dict)
    assignment_by_reuse: dict[int, int] = field(default_factory=dict)
    assignment_by_key: dict[str, int] = field(default_factory=dict)

    def lookup(self, item: SourceItem) -> tuple[int | None, str]:
        keys = _keys(item)
        if item.kind == _KIND_LESSON:
            found = self.lesson_by_reuse.get(item.id)
            if found is not None:
                return found, "provenance"
            for key in keys:
                found = self.lesson_by_key.get(key)
                if found is not None:
                    return found, "title"
            return None, ""
        found = self.assignment_by_reuse.get(item.id)
        if found is not None:
            return found, "provenance"
        for key in keys:
            found = self.assignment_by_key.get(key)
            if found is not None:
                return found, "title"
        return None, ""

    def remember(self, item: SourceItem, portal_id: int) -> None:
        if item.kind == _KIND_LESSON:
            self.lesson_by_reuse[item.id] = portal_id
            for key in _keys(item):
                self.lesson_by_key.setdefault(key, portal_id)
        else:
            self.assignment_by_reuse[item.id] = portal_id
            for key in _keys(item):
                self.assignment_by_key.setdefault(key, portal_id)


def _keys(item: SourceItem) -> set[str]:
    keys = {title_key(item.title), title_key(item.original_title)}
    return {key for key in keys if key}


def _build_existing_index(db: Session, class_id: int) -> _ExistingIndex:
    index = _ExistingIndex()
    lesson_rows = db.execute(
        select(Lesson, LessonSource)
        .outerjoin(LessonSource, LessonSource.lesson_id == Lesson.id)
        .where(Lesson.class_id == class_id)
    ).all()
    for lesson, provenance in lesson_rows:
        if provenance is not None and provenance.reused_from_lesson_id:
            index.lesson_by_reuse[int(provenance.reused_from_lesson_id)] = lesson.id
        for title in (
            lesson.title,
            provenance.original_title if provenance else "",
        ):
            key = title_key(title)
            if key:
                index.lesson_by_key.setdefault(key, lesson.id)

    assignment_rows = db.execute(
        select(Assignment, AssignmentContent)
        .outerjoin(
            AssignmentContent,
            AssignmentContent.assignment_id == Assignment.id,
        )
        .where(Assignment.class_id == class_id)
    ).all()
    for assignment, content in assignment_rows:
        if content is not None and content.reused_from_assignment_id:
            index.assignment_by_reuse[
                int(content.reused_from_assignment_id)
            ] = assignment.id
        for title in (
            assignment.title,
            content.original_title if content else "",
        ):
            key = title_key(title)
            if key:
                index.assignment_by_key.setdefault(key, assignment.id)
    return index


# ---------------------------------------------------------------------------
# Publication resolution
# ---------------------------------------------------------------------------


def _matched_states(
    snapshot: CourseSnapshot | None, item: SourceItem
) -> list[str]:
    """The states of the target-course items that carry this source item.

    Titles are matched after :func:`title_key` normalization, which removes
    ``[Auto]`` tags, copy prefixes and punctuation. Lessons also match on their
    attachment titles: a deck lesson is stored in Classroom as a material whose
    attachment is the deck itself.
    """
    if snapshot is None:
        return []
    keys = _keys(item)
    if not keys:
        return []
    states: list[str] = []
    if item.kind == _KIND_ASSIGNMENT:
        for candidate in snapshot.coursework:
            if candidate.work_type != "ASSIGNMENT":
                continue
            if title_key(candidate.title) in keys:
                states.append((candidate.state or "").upper())
        return states

    for candidate in snapshot.coursework:
        if candidate.work_type == "ASSIGNMENT":
            continue
        if title_key(candidate.title) in keys or any(
            title_key(attachment.title) in keys
            for attachment in candidate.attachments
        ):
            states.append((candidate.state or "").upper())
    return states


def _classroom_state(snapshot: CourseSnapshot | None, item: SourceItem) -> str:
    states = _matched_states(snapshot, item)
    if "PUBLISHED" in states:
        # Classroom often keeps an [Auto] draft next to the published copy;
        # the published one wins because that is what students see.
        return "PUBLISHED"
    if states:
        return "DRAFT"
    return ""


def _pattern_keys(patterns: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        key for key in (title_key(value) for value in patterns) if key
    )


def _desired_publication(
    mode: str,
    item: SourceItem,
    classroom_state: str,
    force_publish: tuple[str, ...],
    force_draft: tuple[str, ...],
) -> bool:
    if mode == MODE_DRAFT:
        desired = False
    elif mode == MODE_PUBLISHED:
        desired = True
    elif mode == MODE_SOURCE:
        desired = item.published
    else:
        desired = classroom_state == "PUBLISHED"

    keys = _keys(item)
    if any(pattern in key for pattern in force_draft for key in keys):
        return False
    if any(pattern in key for pattern in force_publish for key in keys):
        return True
    return desired


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _groups_by_key(db: Session, teacher_id: int) -> dict[str, ClassGroup]:
    groups = db.scalars(
        select(ClassGroup)
        .where(ClassGroup.teacher_id == teacher_id)
        .order_by(ClassGroup.id)
    ).all()
    index: dict[str, ClassGroup] = {}
    for group in groups:
        key = class_key(group.name)
        if not key:
            continue
        if key in index:
            raise SyncError(
                "נמצאו שתי כיתות בעלות שם זהה לאחר נרמול: "
                f"'{index[key].name}' ו-'{group.name}'."
            )
        index[key] = group
    return index


def _validate_targets(targets: Iterable[str]) -> tuple[str, ...]:
    wanted = tuple(dict.fromkeys(str(value).strip() for value in targets))
    excluded = {class_key(value) for value in EXCLUDED_CLASSES}
    known = {class_key(value) for value in TARGET_CLASSES}
    for value in wanted:
        key = class_key(value)
        if key in excluded:
            raise SyncError(
                f"הכיתה '{value}' מוחרגת מהרחבת הכיתות ואינה נוצרת."
            )
        if key not in known:
            raise SyncError(
                f"הכיתה '{value}' אינה ברשימת כיתות היעד. "
                f"הכיתות הנתמכות: {', '.join(TARGET_CLASSES)}."
            )
    if not wanted:
        raise SyncError("לא נבחרו כיתות יעד.")
    return wanted


def build_plan(
    db: Session,
    teacher_id: int,
    *,
    source_name: str = "ז3",
    targets: Iterable[str] | None = None,
    mode: str = MODE_CLASSROOM,
    class_modes: dict[str, str] | None = None,
    force_publish: Iterable[str] = (),
    force_draft: Iterable[str] = (),
    catalog_data: object | None = None,
) -> SyncPlan:
    """Read everything and return a plan. This function never writes."""
    if mode not in MODES:
        raise SyncError(f"מצב פרסום לא מוכר: '{mode}'.")
    class_modes = dict(class_modes or {})
    for value in class_modes.values():
        if value not in MODES:
            raise SyncError(f"מצב פרסום לא מוכר: '{value}'.")

    wanted = _validate_targets(targets if targets is not None else TARGET_CLASSES)
    groups = _groups_by_key(db, teacher_id)
    source_key = class_key(source_name)
    source_group = groups.get(source_key)
    if source_group is None:
        available = ", ".join(group.name for group in groups.values())
        raise SyncError(
            f"לא נמצאה הכיתה '{source_name}' עם התוכן להעתקה."
            + (f" הכיתות הקיימות: {available}." if available else "")
        )

    cache = catalog_data if catalog_data is not None else catalog_cache.load(
        db, teacher_id
    )
    source_items = _load_source_items(db, teacher_id, source_group)
    plan = SyncPlan(
        teacher_id=teacher_id,
        source_name=source_group.name,
        source_class_id=source_group.id,
        mode=mode,
        source_lessons=sum(
            1 for item in source_items if item.kind == _KIND_LESSON
        ),
        source_assignments=sum(
            1 for item in source_items if item.kind == _KIND_ASSIGNMENT
        ),
        catalog_status=str(getattr(cache, "status", "") or ""),
        catalog_error=str(getattr(cache, "error", "") or ""),
        catalog_synced_at=getattr(cache, "synced_at", None),
    )
    courses = list(getattr(cache, "courses", ()) or ())
    snapshots: dict[str, CourseSnapshot] = dict(
        getattr(cache, "snapshots", {}) or {}
    )
    force_publish_keys = _pattern_keys(force_publish)
    force_draft_keys = _pattern_keys(force_draft)

    normalised_modes = {
        class_key(key): value for key, value in class_modes.items()
    }

    for value in wanted:
        token = class_key(value)
        group = groups.get(token)
        class_plan = ClassPlan(
            token=token,
            display_name=(
                group.name
                if group is not None
                else TARGET_CLASS_DISPLAY.get(token, normalize_class_name(value))
            ),
            class_id=group.id if group is not None else None,
            class_exists=group is not None,
            is_source=(token == source_key),
        )
        plan.classes.append(class_plan)
        _plan_course(
            db,
            group,
            class_plan,
            courses,
        )
        if class_plan.error:
            continue

        if class_plan.is_source:
            continue

        snapshot = snapshots.get(class_plan.course_id)
        class_plan.snapshot_available = snapshot is not None
        class_mode = normalised_modes.get(token, mode)
        if class_mode == MODE_CLASSROOM and snapshot is None:
            class_plan.warnings.append(
                "אין סריקת Classroom עדכנית לקורס הזה, לכן כל התוכן "
                "ייווצר כטיוטה. הריצו סריקה ובצעו שוב כדי להתאים את הפרסום."
            )

        index = _build_existing_index(db, group.id) if group is not None else _ExistingIndex()
        for item in source_items:
            state = _classroom_state(snapshot, item)
            desired = _desired_publication(
                class_mode,
                item,
                state,
                force_publish_keys,
                force_draft_keys,
            )
            portal_id, match = index.lookup(item)
            current = None
            if portal_id is not None:
                model = Lesson if item.kind == _KIND_LESSON else Assignment
                existing = db.get(model, portal_id)
                if existing is not None:
                    current = bool(existing.published)
            if portal_id is None:
                action = "create"
            elif current == desired:
                action = "keep"
            elif desired:
                action = "publish"
            else:
                action = "unpublish"
            class_plan.items.append(
                ItemPlan(
                    kind=item.kind,
                    source_id=item.id,
                    title=item.title,
                    action=action,
                    desired_published=desired,
                    portal_id=portal_id,
                    current_published=current,
                    classroom_state=state,
                    match=match,
                )
            )
            if portal_id is not None:
                # A source class may contain two items with the same cleaned
                # title; remember the first so the second is not planned as a
                # second copy.
                index.remember(item, portal_id)

    return plan


def _plan_course(
    db: Session,
    group: ClassGroup | None,
    class_plan: ClassPlan,
    courses: list[object],
) -> None:
    link = (
        db.scalar(
            select(ClassroomLink).where(ClassroomLink.class_id == group.id)
        )
        if group is not None
        else None
    )
    matches = find_course_matches(courses, class_plan.token)
    if len(matches) > 1:
        names = ", ".join(
            normalize_class_name(str(getattr(course, "name", "")))
            for course in matches
        )
        class_plan.error = (
            "נמצאו כמה קורסי Classroom מתאימים ולכן לא ניתן לקשר אוטומטית: "
            f"{names}. קשרו את הכיתה ידנית ובצעו שוב."
        )
        return
    if len(matches) == 1:
        course = matches[0]
        class_plan.course_id = str(getattr(course, "id", "") or "")
        class_plan.classroom_name = str(getattr(course, "name", "") or "")
        class_plan.course_name = normalize_class_name(class_plan.classroom_name)
        class_plan.section = getattr(course, "section", None)
        if link is None:
            class_plan.link_will_create = group is not None
        elif link.course_id != class_plan.course_id:
            class_plan.link_exists = True
            class_plan.link_will_update = True
        else:
            class_plan.link_exists = True
        if group is None:
            class_plan.link_will_create = True
        return

    if link is not None:
        # Existing link but the course list is stale/empty: keep the link and
        # let the publication mode fall back to drafts.
        class_plan.course_id = link.course_id
        class_plan.course_name = normalize_class_name(link.course_name)
        class_plan.classroom_name = link.course_name
        class_plan.section = link.section
        class_plan.link_exists = True
        class_plan.warnings.append(
            "הקורס המקושר לא הופיע בסריקה האחרונה; הקישור הקיים נשמר."
        )
        return

    class_plan.error = (
        f"לא נמצא קורס Google Classroom עבור הכיתה '{class_plan.display_name}'. "
        "ודאו שקורס בשם זה קיים וסרקו את Classroom שוב."
    )
    if class_plan.is_source:
        # The class that supplies the content already holds it; without a
        # course it can still be the base of an expansion run.
        class_plan.error = ""
        class_plan.warnings.append(
            f"הכיתה '{class_plan.display_name}' אינה מקושרת לקורס "
            "Classroom; הקישור שלה לא יעודכן."
        )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _new_class_code(db: Session) -> str:
    for _ in range(30):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        exists = db.scalar(select(ClassGroup.id).where(ClassGroup.code == code))
        if exists is None:
            return code
    raise SyncError("לא ניתן להקצות קוד כיתה ייחודי.")


def _ensure_group(
    db: Session,
    plan: SyncPlan,
    class_plan: ClassPlan,
    outcome: SyncOutcome,
) -> ClassGroup:
    if class_plan.class_id is not None:
        group = db.get(ClassGroup, class_plan.class_id)
        if group is not None and group.teacher_id == plan.teacher_id:
            return group
    found = db.scalar(
        select(ClassGroup).where(
            ClassGroup.teacher_id == plan.teacher_id,
            ClassGroup.name == class_plan.display_name,
        )
    )
    if found is not None:
        class_plan.class_id = found.id
        class_plan.class_exists = True
        return found
    # Normalized match against existing names ("ז'3" vs "ז3").
    for candidate in db.scalars(
        select(ClassGroup).where(ClassGroup.teacher_id == plan.teacher_id)
    ).all():
        if class_key(candidate.name) == class_plan.token:
            class_plan.class_id = candidate.id
            class_plan.class_exists = True
            return candidate

    group = ClassGroup(
        name=class_plan.display_name,
        code=_new_class_code(db),
        teacher_id=plan.teacher_id,
    )
    db.add(group)
    db.flush()
    class_plan.class_id = group.id
    class_plan.class_exists = True
    outcome.classes_created += 1
    return group


def _ensure_link(
    db: Session,
    plan: SyncPlan,
    class_plan: ClassPlan,
    group: ClassGroup,
    outcome: SyncOutcome,
) -> None:
    link = db.scalar(
        select(ClassroomLink).where(ClassroomLink.class_id == group.id)
    )
    if link is None:
        link = ClassroomLink(
            teacher_id=plan.teacher_id,
            class_id=group.id,
            course_id=class_plan.course_id,
            course_name=class_plan.course_name,
        )
        db.add(link)
        outcome.links_created += 1
    else:
        changed = (
            link.course_id != class_plan.course_id
            or link.course_name != class_plan.course_name
            or link.section != class_plan.section
        )
        link.teacher_id = plan.teacher_id
        link.course_id = class_plan.course_id
        link.course_name = class_plan.course_name
        link.section = class_plan.section
        if changed:
            outcome.links_updated += 1
    link.last_synced_at = utcnow()


def _source_item(db: Session, planned: ItemPlan) -> Lesson | Assignment | None:
    if planned.kind == _KIND_LESSON:
        return db.get(Lesson, planned.source_id)
    return db.get(Assignment, planned.source_id)


def _apply_item(
    db: Session,
    class_plan: ClassPlan,
    group: ClassGroup,
    planned: ItemPlan,
    index: _ExistingIndex,
    outcome: SyncOutcome,
) -> None:
    model = Lesson if planned.kind == _KIND_LESSON else Assignment
    portal_id = planned.portal_id
    if portal_id is not None and db.get(model, portal_id) is None:
        portal_id = None
    if portal_id is None:
        lookup, _match = index.lookup(
            SourceItem(
                kind=planned.kind,
                id=planned.source_id,
                title=planned.title,
            )
        )
        portal_id = lookup

    existing = db.get(model, portal_id) if portal_id is not None else None
    if existing is not None:
        before = bool(existing.published)
        existing.published = planned.desired_published
        planned.portal_id = existing.id
        planned.current_published = before
        if before != planned.desired_published:
            planned.action = "publish" if planned.desired_published else "unpublish"
            if planned.desired_published:
                outcome.published += 1
            else:
                outcome.unpublished += 1
        else:
            planned.action = "keep"
            outcome.unchanged += 1
        return

    source = _source_item(db, planned)
    if source is None:
        raise SyncError(
            f"פריט המקור '{planned.title}' (#{planned.source_id}) לא נמצא."
        )
    if planned.kind == _KIND_LESSON:
        clone = importer._clone_lesson(db, source, group.id)
        outcome.lessons_created += 1
    else:
        clone = importer._clone_assignment(db, source, group.id)
        outcome.assignments_created += 1
    clone.published = planned.desired_published
    planned.portal_id = clone.id
    planned.current_published = None
    planned.action = "create"
    if planned.desired_published:
        outcome.published += 1
    else:
        outcome.unpublished += 1
    index.remember(
        SourceItem(
            kind=planned.kind,
            id=planned.source_id,
            title=planned.title,
        ),
        clone.id,
    )


def apply_plan(db: Session, plan: SyncPlan) -> SyncOutcome:
    """Write the plan. The caller owns the transaction and must commit."""
    outcome = SyncOutcome()
    # Planning read the database in this same session, which leaves a
    # deferred read transaction open. If the web worker (or any other
    # process) committed in the meantime, SQLite refuses to upgrade that
    # stale snapshot and raises "database is locked" immediately, before
    # ``busy_timeout`` can help. Ending the read snapshot first makes the
    # first class write start a fresh transaction.
    if not (db.new or db.dirty or db.deleted):
        db.rollback()
    for class_plan in plan.classes:
        if class_plan.error:
            outcome.errors.append(
                f"{class_plan.display_name}: {class_plan.error}"
            )
            continue
        if not class_plan.course_id:
            outcome.errors.append(
                f"{class_plan.display_name}: לא נמצא קורס Classroom מקושר."
            )
            continue
        try:
            with db.begin_nested():
                group = _ensure_group(db, plan, class_plan, outcome)
                _ensure_link(db, plan, class_plan, group, outcome)
                if class_plan.is_source:
                    continue
                index = _build_existing_index(db, group.id)
                for planned in class_plan.items:
                    _apply_item(
                        db,
                        class_plan,
                        group,
                        planned,
                        index,
                        outcome,
                    )
                db.flush()
        except Exception as exc:  # keep other classes usable
            outcome.errors.append(
                f"{class_plan.display_name}: {exc}"
            )
    db.flush()
    return outcome
