"""Local cache of the teacher's Classroom courses and Drive decks.

Opening the picker used to call Google Classroom, Drive and the rubric matcher
on every page view, so a class with 100+ items could take a long time before
anything rendered. The catalog moves that work into one explicit *scan*:

* ``refresh`` reads Google once and stores a normalized snapshot in
  ``portal_classroom_catalog``;
* the picker and the "add to class" route read only that snapshot;
* a later scan compares source ids/versions with the previous snapshot, so
  newly published materials can be highlighted as ``new_ids``.

The cache is teacher-only metadata (titles, topics, attachment names, rubric
suggestions). Media bytes are still downloaded by the import worker when the
teacher actually adds an item.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.ingestion.classroom_source import CourseInfo, SourceError
from portal.ingestion.normalize import (
    clean_title,
    is_python_b_material,
    is_results_sheet,
)
from portal.ingestion.types import (
    Attachment,
    CourseSnapshot,
    CourseworkItem,
    DriveItem,
)
from portal.models import ClassroomCatalog
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

CATALOG_VERSION = 2

_Suggestion = tuple[str, float, str]


@dataclass
class Catalog:
    """Everything the picker needs, loaded in one query."""

    courses: list[CourseInfo] = field(default_factory=list)
    snapshots: dict[str, CourseSnapshot] = field(default_factory=dict)
    # course id -> coursework id -> (rubric path, score, reason)
    suggestions: dict[str, dict[str, _Suggestion]] = field(default_factory=dict)
    decks: list[DriveItem] = field(default_factory=list)
    # Lesson documents from the teaching folders (worksheets, summaries).
    docs: list[DriveItem] = field(default_factory=list)
    new_ids: set[str] = field(default_factory=set)
    status: str = "empty"
    error: str = ""
    synced_at: datetime | None = None

    @property
    def has_data(self) -> bool:
        return self.synced_at is not None

    @property
    def is_refreshing(self) -> bool:
        return self.status == "refreshing"


# ------------------------------------------------------------ serialization


def _attachment_json(attachment: Attachment) -> dict[str, Any]:
    return {
        "file_id": attachment.file_id,
        "title": attachment.title,
        "mime_type": attachment.mime_type,
        "url": attachment.url,
        "kind": attachment.kind,
        "modified_time": attachment.modified_time,
    }


def _attachment_from_json(payload: dict[str, Any]) -> Attachment:
    return Attachment(
        file_id=str(payload.get("file_id") or ""),
        title=str(payload.get("title") or ""),
        mime_type=payload.get("mime_type") or None,
        url=str(payload.get("url") or ""),
        kind=str(payload.get("kind") or "drive"),
        modified_time=str(payload.get("modified_time") or ""),
    )


def _coursework_json(item: CourseworkItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "description_html": item.description_html,
        "state": item.state,
        "work_type": item.work_type,
        "topic_id": item.topic_id,
        "topic_name": item.topic_name,
        "due_at": item.due_at.isoformat() if item.due_at else "",
        "max_points": item.max_points,
        "source_version": item.source_version,
        "attachments": [_attachment_json(a) for a in item.attachments],
    }


def _coursework_from_json(payload: dict[str, Any]) -> CourseworkItem:
    due = str(payload.get("due_at") or "")
    return CourseworkItem(
        id=str(payload.get("id") or ""),
        title=str(payload.get("title") or ""),
        description_html=str(payload.get("description_html") or ""),
        state=str(payload.get("state") or "PUBLISHED"),
        work_type=str(payload.get("work_type") or "ASSIGNMENT"),
        topic_id=payload.get("topic_id") or None,
        topic_name=payload.get("topic_name") or None,
        due_at=datetime.fromisoformat(due) if due else None,
        max_points=payload.get("max_points"),
        source_version=str(payload.get("source_version") or ""),
        attachments=tuple(
            _attachment_from_json(a)
            for a in payload.get("attachments") or []
            if isinstance(a, dict)
        ),
    )


def _snapshot_json(snapshot: CourseSnapshot) -> dict[str, Any]:
    return {
        "course_id": snapshot.course_id,
        "course_name": snapshot.course_name,
        "section": snapshot.section,
        "topics": dict(snapshot.topics),
        "coursework": [_coursework_json(item) for item in snapshot.coursework],
    }


def _snapshot_from_json(payload: dict[str, Any]) -> CourseSnapshot:
    return CourseSnapshot(
        course_id=str(payload.get("course_id") or ""),
        course_name=str(payload.get("course_name") or ""),
        section=payload.get("section") or None,
        topics={
            str(key): str(value)
            for key, value in (payload.get("topics") or {}).items()
        },
        coursework=tuple(
            _coursework_from_json(item)
            for item in payload.get("coursework") or []
            if isinstance(item, dict)
        ),
    )


def _deck_json(deck: DriveItem) -> dict[str, Any]:
    return {
        "file_id": deck.file_id,
        "title": deck.title,
        "mime_type": deck.mime_type,
        "modified_time": deck.modified_time,
        "folder_id": deck.folder_id,
        "folder_name": deck.folder_name,
    }


def _deck_from_json(payload: dict[str, Any]) -> DriveItem:
    return DriveItem(
        file_id=str(payload.get("file_id") or ""),
        title=str(payload.get("title") or "מצגת"),
        mime_type=str(
            payload.get("mime_type")
            or "application/vnd.google-apps.presentation"
        ),
        modified_time=str(payload.get("modified_time") or ""),
        folder_id=str(payload.get("folder_id") or ""),
        folder_name=str(payload.get("folder_name") or ""),
    )


def visible_docs(docs: Iterable[DriveItem]) -> list[DriveItem]:
    """Same course filter as decks, applied on read and on scan.

    Personal exam-results sheets are dropped here too: they are grade reports
    about pupils, not teaching material, so they never reach the picker or the
    course-content tree.
    """
    return [
        doc
        for doc in docs
        if not is_python_b_material(doc.title, doc.folder_name)
        and not is_results_sheet(doc.title)
    ]


def visible_decks(decks: Iterable[DriveItem]) -> list[DriveItem]:
    """Drop materials of courses the teacher does not use (פייתון ב').

    Filtering here - on read as well as on scan - means an old catalog hides
    those decks immediately, without waiting for the next Google scan.
    """
    return [
        deck
        for deck in decks
        if not is_python_b_material(deck.title, deck.folder_name)
    ]


def serialize(
    courses: Iterable[CourseInfo],
    snapshots: dict[str, CourseSnapshot],
    suggestions: dict[str, dict[str, _Suggestion]],
    decks: Iterable[DriveItem],
    docs: Iterable[DriveItem] = (),
) -> str:
    payload = {
        "version": CATALOG_VERSION,
        "courses": [
            {"id": course.id, "name": course.name, "section": course.section}
            for course in courses
        ],
        "snapshots": {
            course_id: _snapshot_json(snapshot)
            for course_id, snapshot in snapshots.items()
        },
        "suggestions": {
            course_id: {
                item_id: {
                    "rubric": value[0],
                    "score": value[1],
                    "reason": value[2],
                }
                for item_id, value in items.items()
            }
            for course_id, items in suggestions.items()
        },
        "decks": [_deck_json(deck) for deck in decks],
        "docs": [_deck_json(doc) for doc in docs],
    }
    return json.dumps(payload, ensure_ascii=False)


def deserialize(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        logger.warning("Classroom catalog JSON is corrupt; treating it as empty")
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


# --------------------------------------------------------------- read/store


def _row(db: Session, teacher_id: int) -> ClassroomCatalog | None:
    return db.scalar(
        select(ClassroomCatalog).where(
            ClassroomCatalog.teacher_id == teacher_id
        )
    )


def load(db: Session, teacher_id: int) -> Catalog:
    """Read the cached catalog; never talks to Google."""
    row = _row(db, teacher_id)
    if row is None:
        return Catalog()
    payload = deserialize(row.catalog_json)
    courses = [
        CourseInfo(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            section=item.get("section") or None,
        )
        for item in payload.get("courses") or []
        if isinstance(item, dict) and item.get("id")
    ]
    snapshots = {
        str(course_id): _snapshot_from_json(item)
        for course_id, item in (payload.get("snapshots") or {}).items()
        if isinstance(item, dict)
    }
    suggestions: dict[str, dict[str, _Suggestion]] = {}
    for course_id, items in (payload.get("suggestions") or {}).items():
        if not isinstance(items, dict):
            continue
        suggestions[str(course_id)] = {
            str(item_id): (
                str(value.get("rubric") or ""),
                float(value.get("score") or 0.0),
                str(value.get("reason") or ""),
            )
            for item_id, value in items.items()
            if isinstance(value, dict)
        }
    decks = visible_decks(
        _deck_from_json(item)
        for item in payload.get("decks") or []
        if isinstance(item, dict) and item.get("file_id")
    )
    docs = visible_docs(
        _deck_from_json(item)
        for item in payload.get("docs") or []
        if isinstance(item, dict) and item.get("file_id")
    )
    try:
        new_ids = set(json.loads(row.new_ids_json or "[]"))
    except (TypeError, ValueError):
        new_ids = set()
    return Catalog(
        courses=courses,
        snapshots=snapshots,
        suggestions=suggestions,
        decks=decks,
        docs=docs,
        new_ids={str(value) for value in new_ids},
        status=row.status or "empty",
        error=row.error or "",
        synced_at=row.synced_at,
    )


def mark_refreshing(db: Session, teacher_id: int) -> None:
    """Flag a scan as running so the picker can show progress immediately."""
    row = _row(db, teacher_id)
    if row is None:
        row = ClassroomCatalog(teacher_id=teacher_id)
        db.add(row)
    row.status = "refreshing"
    row.error = ""
    row.updated_at = utcnow()
    db.commit()


def record_failure(db: Session, teacher_id: int, message: str) -> None:
    row = _row(db, teacher_id)
    if row is None:
        row = ClassroomCatalog(teacher_id=teacher_id)
        db.add(row)
    row.status = "failed"
    row.error = message
    row.updated_at = utcnow()
    db.commit()


def _suggestion(source, title: str) -> _Suggestion:
    cleaned = clean_title(title).strip()
    # Numeric or one-letter titles carry no signal; a "95% match" here would
    # silently preselect the wrong rubric.
    if len(cleaned) < 3 or cleaned.isdigit():
        return ("", 0.0, "")
    try:
        return source.suggest_rubric(title)
    except Exception:  # noqa: BLE001 - a suggestion is optional
        return ("", 0.0, "")


def _versions(
    snapshots: dict[str, CourseSnapshot],
    decks: Iterable[DriveItem],
    docs: Iterable[DriveItem] = (),
) -> dict[str, str]:
    versions: dict[str, str] = {}
    for snapshot in snapshots.values():
        for item in snapshot.coursework:
            versions[f"coursework:{item.id}"] = item.source_version
    for deck in decks:
        versions[f"deck:{deck.file_id}"] = deck.source_version
    for doc in docs:
        versions[f"doc:{doc.file_id}"] = doc.source_version
    return versions


def refresh(db: Session, source, teacher_id: int, *, course_id: str = "") -> dict[str, int]:
    """Read Google once and update the stored catalog.

    ``course_id`` limits the (expensive) coursework scan to one course - the
    one linked to the class - while the course list and Drive decks are always
    refreshed. Other courses from an earlier scan stay in the catalog, so
    switching the course selector remains instant. Without ``course_id`` all
    courses are scanned (the initial CLI/test path).

    Raises on a Google error; the caller records the failure and the previous
    catalog stays intact. The first successful scan of a course is a baseline
    (no items are marked "new"); later scans report items that were not there
    before.
    """
    previous = load(db, teacher_id)
    seed = getattr(source, "seed_mime_cache", None)
    if seed is not None:
        seed(
            {
                attachment.file_id: attachment.mime_type
                for snapshot in previous.snapshots.values()
                for item in snapshot.coursework
                for attachment in item.attachments
                if attachment.file_id and attachment.mime_type
            }
        )

    courses = list(source.list_courses())
    known = {course.id for course in courses}
    if course_id and course_id not in known:
        raise SourceError(
            "הקורס המקושר לא נמצא בחשבון Google Classroom. "
            "קשרו את הכיתה לקורס קיים ונסו שוב."
        )
    targets = [course_id] if course_id else [course.id for course in courses]

    snapshots: dict[str, CourseSnapshot] = dict(previous.snapshots)
    suggestions: dict[str, dict[str, _Suggestion]] = {
        key: dict(value) for key, value in previous.suggestions.items()
    }
    for target in targets:
        snapshot = source.load_course(target, include_drafts=True)
        snapshots[target] = snapshot
        suggestions[target] = {
            item.id: _suggestion(source, item.title)
            for item in snapshot.coursework
        }

    decks = visible_decks(source.list_drive_decks(""))
    # Lesson docs live beside the decks; scanning their folders keeps student
    # hand-ins (stored in sub-folders) out of the picker.
    list_docs = getattr(source, "list_drive_docs", None)
    docs = (
        visible_docs(
            list_docs(
                sorted({deck.folder_id for deck in decks if deck.folder_id})
            )
        )
        if list_docs is not None
        else []
    )
    current = _versions(
        {target: snapshots[target] for target in targets}, decks, docs
    )
    old = _versions(
        {
            target: previous.snapshots[target]
            for target in targets
            if target in previous.snapshots
        },
        previous.decks,
        previous.docs,
    )
    new_ids = set(current) - set(old) if previous.has_data else set()
    updated = (
        sum(1 for key in set(current) & set(old) if current[key] != old[key])
        if previous.has_data
        else 0
    )

    row = _row(db, teacher_id)
    if row is None:
        row = ClassroomCatalog(teacher_id=teacher_id)
        db.add(row)
    row.catalog_json = serialize(courses, snapshots, suggestions, decks, docs)
    row.new_ids_json = json.dumps(sorted(new_ids), ensure_ascii=False)
    row.status = "ready"
    row.error = ""
    row.synced_at = utcnow()
    row.updated_at = utcnow()
    db.commit()

    return {
        "courses": len(targets),
        "coursework": sum(
            len(snapshots[target].coursework) for target in targets
        ),
        "decks": len(decks),
        "docs": len(docs),
        "new": len(new_ids),
        "updated": updated,
    }


def reset_stale(db: Session) -> int:
    """Close scans interrupted by a restart.

    Catalog scans are not resumable (unlike import jobs), so a ``running`` job
    left behind by a stopped process would keep the picker in "סורק…" forever
    and block a fresh scan. Call this once at application startup.
    """
    from portal.models import ImportJob

    stale = db.scalars(
        select(ImportJob).where(
            ImportJob.kind == "catalog_sync",
            ImportJob.state.in_(("queued", "running")),
        )
    ).all()
    for job in stale:
        job.state = "failed"
        job.error = "הסריקה נקטעה. הפעילו סריקה חדשה."
        job.finished_at = utcnow()
    rows = db.scalars(
        select(ClassroomCatalog).where(ClassroomCatalog.status == "refreshing")
    ).all()
    for row in rows:
        row.status = "failed" if row.synced_at is None else "ready"
        row.error = (
            "הסריקה נקטעה לפני שהסתיימה. הפעילו סריקה חדשה."
            if row.synced_at is None
            else "הסריקה האחרונה נקטעה; מוצגים החומרים מהסריקה הקודמת."
        )
        row.updated_at = utcnow()
    if stale or rows:
        db.commit()
    return len(stale)

