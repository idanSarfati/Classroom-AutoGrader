"""Topic-grouped lesson/assignment feed for the student class view.

The teacher's Classroom folders group material into numbered topics such as
``3 | פקודת קלט``. The portal stores only titles, so this module derives the
unit number from the leading lesson number (``3.2`` -> unit 3) and takes the
readable topic name from the cached Classroom snapshot. When no snapshot is
available the feed falls back to ``יחידה N`` headings; the page still works.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from portal.ingestion import catalog as catalog_store
from portal.models import (
    SUBMISSION_DEFERRED,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    SUBMISSION_GRADING,
    SUBMISSION_QUEUED,
    Assignment,
    ClassGroup,
    Lesson,
)

_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?!\d)")
_PRACTICE_RE = re.compile(r"תרגול יחידות\s*(\d+)\s*[-–]\s*(\d+)")
_TOPIC_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\s*\|")


def _squash(value: str) -> str:
    return re.sub(r"[^0-9a-zא-ת]", "", (value or "").lower())


def item_number(title: str) -> tuple[int, int] | None:
    """Lesson number used for sorting and topic grouping.

    ``3.2`` becomes ``(3, 2)``. The unnumbered "תרגול יחידות 1-2" revision is
    placed at the end of unit 2 where the teacher's plan has it.
    """
    match = _NUMBER_RE.match(title or "")
    if match is not None:
        return int(match.group(1)), int(match.group(2))
    practice = _PRACTICE_RE.search(title or "")
    if practice is not None:
        return int(practice.group(2)), 99
    return None


def _topic_labels(snapshot) -> dict[int, str]:
    labels: dict[int, str] = {}
    if snapshot is None:
        return labels
    # Coursework carries the real topic of each numbered lesson (0.3 -> היכרות),
    # which is more reliable than the topics list alone.
    for item in snapshot.coursework:
        number = item_number(item.title)
        if number is not None and item.topic_name:
            labels.setdefault(number[0], item.topic_name.strip())
    for name in snapshot.topics.values():
        match = _TOPIC_NUMBER_RE.match(name or "")
        if match is not None:
            labels.setdefault(int(match.group(1)), name.strip())
    return labels


def load_catalog(db: Session, teacher_id: int):
    try:
        return catalog_store.load(db, teacher_id)
    except Exception:  # noqa: BLE001 - the feed must render without a cache
        return None


def _course_snapshot(catalog, group: ClassGroup):
    if catalog is None:
        return None
    target = _squash(group.name)
    if not target:
        return None
    for course in catalog.courses:
        if _squash(course.name).endswith(target):
            return catalog.snapshots.get(course.id)
    return None


@dataclass
class FeedItem:
    kind: str  # "lesson" | "assignment"
    title: str
    href: str
    number: tuple[int, int] | None
    object_id: int = 0
    published: bool = True
    created_at: datetime | None = None
    summary: str = ""
    slide_count: int = 0
    due_at: datetime | None = None
    status_label: str = ""
    status_badge: str = ""
    grade_only: bool = False

    @property
    def kind_label(self) -> str:
        if self.kind == "lesson":
            return "שיעור"
        return "אירוע ציון" if self.grade_only else "מטלה"

    @property
    def kind_badge(self) -> str:
        if self.kind == "lesson":
            return "badge-lesson"
        return "badge-exam" if self.grade_only else "badge-assignment"

    @property
    def action_label(self) -> str:
        if self.kind == "lesson":
            return "למידה"
        return "צפייה" if self.grade_only else "פתיחה"


@dataclass
class FeedTopic:
    label: str
    items: list[FeedItem] = field(default_factory=list)


def is_grade_only(assignment: Assignment) -> bool:
    """An exam/quiz event: the teacher entered a score, there is no rubric.

    Such an assignment has nothing to hand in, so the feed must not tell a
    student "לא הוגש" - there was never a submission to make.
    """
    return not (assignment.rubric_path or "").strip()


def status_badge(state: dict | None) -> tuple[str, str]:
    if not state:
        return "לא הוגש", "badge-missing"
    status = state.get("status")
    score = state.get("score")
    if status == SUBMISSION_GRADED and score is not None:
        return f"ציון {score:g}", "badge-published"
    if status in (SUBMISSION_QUEUED, SUBMISSION_DEFERRED):
        return "ממתין לציון", "badge-draft"
    if status == SUBMISSION_GRADING:
        return "בבדיקה", "badge-type"
    if status == SUBMISSION_FAILED:
        return "הבדיקה נכשלה", "badge-danger"
    return state.get("label") or "ממתין לציון", state.get("badge") or "badge-draft"


def _feed_sort_key(item: FeedItem) -> tuple:
    """Highest unit first, newest item on top; unnumbered material last.

    A class page reads like a feed: unit 3 above unit 2 above unit 1, and
    inside a unit the later item (``3.2``) above the earlier one (``3.1``).
    The "תרגול יחידות" revision closes its unit instead of opening it.
    ``created_at`` breaks ties so a re-imported item still lands by recency.
    """
    stamp = item.created_at.timestamp() if item.created_at else 0.0
    if item.number is None:
        return (1, 0, 0, 0, -stamp, -item.object_id)
    practice = _PRACTICE_RE.search(item.title) is not None
    return (
        0,
        -item.number[0],
        1 if practice else 0,
        -item.number[1],
        -stamp,
        -item.object_id,
    )


def build_class_feed(
    db: Session,
    group: ClassGroup,
    lessons: list[Lesson],
    assignments: list[Assignment],
    slide_counts: dict[int, int],
    submission_status: dict[int, dict],
    base: str,
    *,
    catalog=None,
    show_status: bool = True,
) -> list[FeedTopic]:
    snapshot = _course_snapshot(catalog, group)
    labels = _topic_labels(snapshot)

    items: list[FeedItem] = []
    for lesson in lessons:
        items.append(
            FeedItem(
                kind="lesson",
                title=lesson.title,
                href=f"{base}/lessons/{lesson.id}",
                number=item_number(lesson.title),
                object_id=lesson.id,
                published=lesson.published,
                created_at=lesson.created_at,
                summary=lesson.summary or "",
                slide_count=slide_counts.get(lesson.id, 0),
            )
        )
    for assignment in assignments:
        grade_only = is_grade_only(assignment)
        if show_status:
            state = submission_status.get(assignment.id)
            if state is None and grade_only:
                label, badge = "לא הוזן ציון", "badge-missing"
            else:
                label, badge = status_badge(state)
        else:
            label, badge = "", ""
        items.append(
            FeedItem(
                kind="assignment",
                title=assignment.title,
                href=f"{base}/assignments/{assignment.id}",
                number=item_number(assignment.title),
                object_id=assignment.id,
                published=assignment.published,
                created_at=assignment.created_at,
                due_at=assignment.due_at,
                status_label=label,
                status_badge=badge,
                grade_only=grade_only,
            )
        )

    items.sort(key=_feed_sort_key)

    topics: list[FeedTopic] = []
    by_unit: dict[int | None, FeedTopic] = {}
    for item in items:
        unit = item.number[0] if item.number is not None else None
        topic = by_unit.get(unit)
        if topic is None:
            if unit is None:
                label = "כללי"
            else:
                label = labels.get(unit) or f"יחידה {unit}"
            topic = FeedTopic(label=label)
            by_unit[unit] = topic
            topics.append(topic)
        topic.items.append(item)
    return topics