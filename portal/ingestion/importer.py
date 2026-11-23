"""Preview and apply Classroom content into portal lessons and assignments.

Design rules enforced here:

* **Drafts only.** Imported lessons and assignments are always created with
  ``published=False``; the teacher reviews and publishes.
* **No rubric, no assignment.** An assignment is created only after the
  teacher confirms a rubric path; the binding is hashed by the existing
  grading adapter.
* **Idempotent.** ``portal_import_items`` and the source columns on
  ``portal_lesson_sources`` / ``portal_assignment_content`` are checked before
  creating anything, and a re-apply never duplicates content.
* **Reuse shares bytes.** Copying a lesson/assignment to another class clones
  database rows and points at the same content-addressed blobs.
* **Fail per item.** One bad document/deck is recorded and the run continues.
"""
from __future__ import annotations

import hashlib
import html as html_module
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from portal.grading import adapter
from portal.ingestion import converter
from portal.ingestion.classroom_source import SourceError, preferred_lesson_rubric
from portal.ingestion.deck import DeckRenderError, render_pdf_pages
from portal.ingestion.google_auth import IngestionAuthError
from portal.ingestion.normalize import (
    DedupeInfo,
    clean_title,
    dedupe_coursework,
    dedupe_drive_items,
    html_to_text,
    title_key,
)
from portal.ingestion.storage import ContentStore, ContentStoreError
from portal.ingestion.types import (
    Attachment,
    CourseSnapshot,
    DriveItem,
    ImportManifest,
    PreviewItem,
)
from portal.models import (
    Assignment,
    AssignmentAttachment,
    AssignmentContent,
    ClassGroup,
    ClassroomLink,
    ImportItem,
    ImportJob,
    Lesson,
    LessonSlide,
    LessonSource,
    SlideContent,
)
from portal.paths import RUBRIC_ROOTS
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

# A rubric suggestion below this score is shown but never auto-selected as
# "assignment": the teacher must explicitly choose it.
RUBRIC_SUGGEST_THRESHOLD = 0.55
MAX_DOCS_PER_LESSON = 4
ACTIONS = ("skip", "assignment", "lesson", "reuse_lesson", "reuse_assignment")

_IMAGE_MIME_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _now_iso() -> str:
    return utcnow().isoformat()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _paragraphs(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return "".join(f"<p>{html_module.escape(line)}</p>" for line in lines)


def _parse_due(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _image_mime(data: bytes) -> str:
    for magic, mime in _IMAGE_MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        raise ContentStoreError("GIF images are not imported.")
    raise ContentStoreError("The downloaded image is not a supported format.")


# ---------------------------------------------------------------------------
# Existing / reusable lookups
# ---------------------------------------------------------------------------


def _class_name(db: Session, class_id: int | None) -> str:
    if class_id is None:
        return ""
    group = db.get(ClassGroup, class_id)
    return group.name if group else ""


def _find_existing(
    db: Session, teacher_id: int, class_id: int, source_kind: str, source_id: str
) -> tuple[str, int, str] | None:
    """Return ``(portal_type, portal_id, source_version)`` in the target class."""
    lesson = db.execute(
        select(Lesson.id, LessonSource.source_version)
        .join(LessonSource, LessonSource.lesson_id == Lesson.id)
        .where(
            Lesson.teacher_id == teacher_id,
            Lesson.class_id == class_id,
            LessonSource.source_kind == source_kind,
            LessonSource.source_id == source_id,
        )
    ).first()
    if lesson is not None:
        return "lesson", int(lesson.id), str(lesson.source_version or "")

    assignment = db.execute(
        select(Assignment.id, AssignmentContent.source_version)
        .join(
            AssignmentContent,
            AssignmentContent.assignment_id == Assignment.id,
        )
        .where(
            Assignment.class_id == class_id,
            AssignmentContent.source_kind == source_kind,
            AssignmentContent.source_id == source_id,
        )
    ).first()
    if assignment is not None:
        return "assignment", int(assignment.id), str(assignment.source_version or "")
    return None


def _find_reusable(
    db: Session, teacher_id: int, class_id: int, source_kind: str, source_id: str
) -> tuple[str, int, str] | None:
    """Find the same Google source already imported into another class."""
    lesson = db.execute(
        select(Lesson.id, ClassGroup.name)
        .join(LessonSource, LessonSource.lesson_id == Lesson.id)
        .join(ClassGroup, ClassGroup.id == Lesson.class_id)
        .where(
            Lesson.teacher_id == teacher_id,
            Lesson.class_id != class_id,
            LessonSource.source_kind == source_kind,
            LessonSource.source_id == source_id,
        )
        .order_by(Lesson.id)
    ).first()
    if lesson is not None:
        return "lesson", int(lesson.id), str(lesson.name or "")

    assignment = db.execute(
        select(Assignment.id, ClassGroup.name)
        .join(
            AssignmentContent,
            AssignmentContent.assignment_id == Assignment.id,
        )
        .join(ClassGroup, ClassGroup.id == Assignment.class_id)
        .where(
            ClassGroup.teacher_id == teacher_id,
            Assignment.class_id != class_id,
            AssignmentContent.source_kind == source_kind,
            AssignmentContent.source_id == source_id,
        )
        .order_by(Assignment.id)
    ).first()
    if assignment is not None:
        return "assignment", int(assignment.id), str(assignment.name or "")
    return None


def source_status(
    db: Session, teacher_id: int, class_id: int, source_kind: str, source_id: str
) -> tuple[tuple[str, int, str] | None, tuple[str, int, str] | None]:
    """Where a Google source already lives.

    Returns ``(existing_in_target_class, reusable_from_another_class)``. The
    web picker uses this to warn before the teacher adds the same item again;
    the importer itself still re-checks at apply time.
    """
    existing = _find_existing(db, teacher_id, class_id, source_kind, source_id)
    reusable = (
        None
        if existing is not None
        else _find_reusable(db, teacher_id, class_id, source_kind, source_id)
    )
    return existing, reusable


def source_status_group(
    db: Session,
    teacher_id: int,
    class_id: int,
    source_kind: str,
    source_ids: list[str],
) -> tuple[tuple[str, int, str] | None, tuple[str, int, str] | None]:
    """Like :func:`source_status`, for a whole duplicate group.

    Classroom keeps several copies of the same material with distinct ids
    (one per course). Checking the group means the visible canonical row still
    says "already in class" when the teacher imported a different copy.
    """
    ids = [value for value in dict.fromkeys(source_ids) if value]
    if not ids:
        return None, None

    lesson = db.execute(
        select(Lesson.id, LessonSource.source_version)
        .join(LessonSource, LessonSource.lesson_id == Lesson.id)
        .where(
            Lesson.teacher_id == teacher_id,
            Lesson.class_id == class_id,
            LessonSource.source_kind == source_kind,
            LessonSource.source_id.in_(ids),
        )
        .order_by(Lesson.id)
    ).first()
    if lesson is not None:
        return (
            "lesson",
            int(lesson.id),
            str(lesson.source_version or ""),
        ), None

    assignment = db.execute(
        select(Assignment.id, AssignmentContent.source_version)
        .join(
            AssignmentContent,
            AssignmentContent.assignment_id == Assignment.id,
        )
        .where(
            Assignment.class_id == class_id,
            AssignmentContent.source_kind == source_kind,
            AssignmentContent.source_id.in_(ids),
        )
        .order_by(Assignment.id)
    ).first()
    if assignment is not None:
        return (
            (
                "assignment",
                int(assignment.id),
                str(assignment.source_version or ""),
            ),
            None,
        )

    for source_id in ids:
        reusable = _find_reusable(db, teacher_id, class_id, source_kind, source_id)
        if reusable is not None:
            return None, reusable
    return None, None


def class_title_index(
    db: Session, teacher_id: int, class_id: int
) -> dict[str, tuple[str, int]]:
    """Titles already imported into the class, keyed by :func:`title_key`.

    Courses hold copies of the same material under different Classroom ids.
    The picker uses this to keep showing "כבר בכיתה" when the teacher browses
    a different course copy - a badge is advisory and never changes what the
    importer creates.
    """
    index: dict[str, tuple[str, int]] = {}

    def remember(title: str, portal_type: str, portal_id: int) -> None:
        key = title_key(title)
        if key:
            index.setdefault(key, (portal_type, portal_id))

    lesson_rows = db.execute(
        select(Lesson.id, Lesson.title, LessonSource.original_title)
        .join(LessonSource, LessonSource.lesson_id == Lesson.id)
        .where(
            Lesson.teacher_id == teacher_id,
            Lesson.class_id == class_id,
        )
        .order_by(Lesson.id)
    ).all()
    for row in lesson_rows:
        remember(row.title, "lesson", int(row.id))
        remember(row.original_title, "lesson", int(row.id))

    assignment_rows = db.execute(
        select(Assignment.id, Assignment.title, AssignmentContent.original_title)
        .join(
            AssignmentContent,
            AssignmentContent.assignment_id == Assignment.id,
        )
        .where(Assignment.class_id == class_id)
        .order_by(Assignment.id)
    ).all()
    for row in assignment_rows:
        remember(row.title, "assignment", int(row.id))
        remember(row.original_title, "assignment", int(row.id))

    return index


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def _link_course(db: Session, teacher_id: int, group: ClassGroup, snapshot) -> None:
    link = db.scalar(
        select(ClassroomLink).where(ClassroomLink.class_id == group.id)
    )
    if link is None:
        link = ClassroomLink(
            teacher_id=teacher_id,
            class_id=group.id,
            course_id=snapshot.course_id,
            course_name=snapshot.course_name,
        )
        db.add(link)
    else:
        link.teacher_id = teacher_id
        link.course_id = snapshot.course_id
        link.course_name = snapshot.course_name
    link.section = snapshot.section
    link.last_synced_at = utcnow()


def _new_job(
    db: Session,
    *,
    teacher_id: int,
    class_id: int | None,
    course_id: str,
    kind: str,
    manifest: ImportManifest,
    counts: dict,
    state: str = "preview",
) -> ImportJob:
    job = ImportJob(
        teacher_id=teacher_id,
        class_id=class_id,
        course_id=course_id,
        kind=kind,
        state=state,
        manifest_json=json.dumps(manifest.as_json(), ensure_ascii=False),
        counts_json=json.dumps(counts, ensure_ascii=False),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _suggested_action(
    item,
    *,
    existing,
    reusable,
    rubric: str,
    rubric_score: float,
) -> str:
    if not item.id:
        return "skip"
    if existing is not None:
        return "skip"
    if reusable is not None:
        return f"reuse_{reusable[0]}"
    if item.presentations:
        return "lesson"
    if rubric and rubric_score >= RUBRIC_SUGGEST_THRESHOLD:
        return "assignment"
    if item.documents or item.downloadable:
        return "lesson"
    return "skip"


def _coursework_preview_item(
    db: Session,
    source,
    *,
    teacher_id: int,
    group: ClassGroup,
    coursework,
    info: DedupeInfo,
    duplicate_of: str = "",
    rubric: str = "",
    rubric_score: float = 0.0,
    rubric_reason: str = "",
    action: str | None = None,
) -> PreviewItem:
    """One preview row, shared by the full course preview and quick selection."""
    existing = _find_existing(
        db, teacher_id, group.id, "coursework", coursework.id
    )
    reusable = (
        None
        if existing is not None
        else _find_reusable(db, teacher_id, group.id, "coursework", coursework.id)
    )
    if action is None:
        action = _suggested_action(
            coursework,
            existing=existing,
            reusable=reusable,
            rubric=rubric,
            rubric_score=rubric_score,
        )
    return PreviewItem(
        source_kind="coursework",
        source_id=coursework.id,
        title=clean_title(coursework.title),
        original_title=coursework.title,
        parent_id="",
        topic_name=coursework.topic_name or "",
        description_text=html_to_text(coursework.description_html)[:4000],
        description_html=coursework.description_html,
        state=coursework.state,
        work_type=coursework.work_type,
        due_at=coursework.due_at.isoformat() if coursework.due_at else "",
        max_points=coursework.max_points,
        source_version=coursework.source_version,
        attachments=[
            attachment.as_teacher_json()
            for attachment in coursework.attachments
        ],
        suggested_action=action,
        suggested_rubric=rubric,
        rubric_score=round(rubric_score, 3),
        rubric_reason=rubric_reason,
        duplicate_of=duplicate_of,
        duplicate_count=info.duplicate_count,
        already_imported=existing is not None,
        existing_portal_id=existing[1] if existing else None,
        existing_class_name=_class_name(db, group.id),
        reusable_from_class=reusable[2] if reusable else "",
    )


def build_course_preview(
    db: Session,
    source,
    *,
    teacher_id: int,
    group: ClassGroup,
    course_id: str,
    include_drafts: bool = False,
) -> ImportJob:
    """Build and store a teacher preview for one Classroom course."""
    snapshot: CourseSnapshot = source.load_course(
        course_id, include_drafts=include_drafts
    )
    items = list(snapshot.coursework)
    _keep, dedupe = dedupe_coursework(items)
    rubric_cache: dict[str, tuple[str, float, str]] = {}

    preview: list[PreviewItem] = []
    counts = {
        "total": len(items),
        "duplicates": 0,
        "assignment": 0,
        "lesson": 0,
        "reuse": 0,
        "skip": 0,
    }
    for coursework in items:
        info = dedupe.get(coursework.id, DedupeInfo())
        rubric, score, reason = "", 0.0, ""
        if not info.duplicate_of:
            if coursework.title not in rubric_cache:
                rubric_cache[coursework.title] = source.suggest_rubric(
                    coursework.title
                )
            rubric, score, reason = rubric_cache[coursework.title]

        item = _coursework_preview_item(
            db,
            source,
            teacher_id=teacher_id,
            group=group,
            coursework=coursework,
            info=info,
            duplicate_of=info.duplicate_of,
            rubric=rubric,
            rubric_score=score,
            rubric_reason=reason,
        )
        action = item.suggested_action
        if info.duplicate_of:
            counts["duplicates"] += 1
            counts["skip"] += 1
        elif action.startswith("reuse_"):
            counts["reuse"] += 1
        else:
            counts[action] = counts.get(action, 0) + 1

        preview.append(item)

    manifest = ImportManifest(
        kind="course",
        course_id=snapshot.course_id,
        course_name=snapshot.course_name,
        class_id=group.id,
        class_name=group.name,
        items=preview,
        generated_at=_now_iso(),
        include_drafts=include_drafts,
    )
    _link_course(db, teacher_id, group, snapshot)
    return _new_job(
        db,
        teacher_id=teacher_id,
        class_id=group.id,
        course_id=course_id,
        kind="course",
        manifest=manifest,
        counts=counts,
    )


def build_deck_preview(
    db: Session,
    source,
    *,
    teacher_id: int,
    group: ClassGroup,
    file_ids: list[str],
    folder_id: str = "",
) -> ImportJob:
    """Preview decks picked directly from Drive (not attached to coursework)."""
    drive_items: list[DriveItem] = []
    for file_id in dict.fromkeys(fid for fid in file_ids if fid):
        info = source.deck_info(file_id)
        drive_items.append(info)

    keep, info_map = dedupe_drive_items(drive_items)
    counts = {
        "total": len(drive_items),
        "duplicates": max(0, len(drive_items) - len(keep)),
        "lesson": 0,
        "skip": 0,
    }
    preview: list[PreviewItem] = []
    for deck in drive_items:
        info = info_map.get(deck.file_id, DedupeInfo())
        existing = _find_existing(
            db, teacher_id, group.id, "drive_deck", deck.file_id
        )
        reusable = (
            None
            if existing is not None
            else _find_reusable(
                db, teacher_id, group.id, "drive_deck", deck.file_id
            )
        )
        if info.duplicate_of or existing is not None:
            action = "skip"
            counts["skip"] += 1
        elif reusable is not None:
            action = f"reuse_{reusable[0]}"
            counts["reuse"] = counts.get("reuse", 0) + 1
        else:
            action = "lesson"
            counts["lesson"] += 1
        preview.append(
            PreviewItem(
                source_kind="drive_deck",
                source_id=deck.file_id,
                title=clean_title(deck.title),
                original_title=deck.title,
                parent_id=folder_id,
                source_version=deck.source_version,
                suggested_action=action,
                duplicate_of=info.duplicate_of,
                duplicate_count=info.duplicate_count,
                already_imported=existing is not None,
                existing_portal_id=existing[1] if existing else None,
                existing_class_name=_class_name(db, group.id),
                reusable_from_class=reusable[2] if reusable else "",
            )
        )

    manifest = ImportManifest(
        kind="decks",
        course_id="",
        course_name="מצגות מ-Drive",
        class_id=group.id,
        class_name=group.name,
        items=preview,
        generated_at=_now_iso(),
    )
    return _new_job(
        db,
        teacher_id=teacher_id,
        class_id=group.id,
        course_id="",
        kind="decks",
        manifest=manifest,
        counts=counts,
    )


def build_selection_preview(
    db: Session,
    source,
    *,
    teacher_id: int,
    group: ClassGroup,
    snapshot: CourseSnapshot | None,
    coursework_ids: list[str] | tuple[str, ...] = (),
    deck_ids: list[str] | tuple[str, ...] = (),
    actions: dict[str, str] | None = None,
    rubrics: dict[str, str] | None = None,
    decks: dict[str, DriveItem] | None = None,
    doc_ids: list[str] | tuple[str, ...] = (),
    docs: dict[str, DriveItem] | None = None,
) -> ImportJob:
    """Build a ready-to-run job from the teacher's one-screen selection.

    The teacher already decided, per item, whether it becomes a lesson
    (material) or an assignment, so this job starts ``queued`` instead of
    waiting for a second confirmation page. Keys in ``actions``/``rubrics``
    are ``coursework:<id>``, ``deck:<file_id>`` or ``doc:<file_id>``.

    ``decks``/``docs`` supply metadata from the local catalog. They are
    optional only so callers without a catalog can still build a preview; the
    picker passes them and therefore makes no Google call before the worker
    starts.
    """
    actions = dict(actions or {})
    rubrics = dict(rubrics or {})
    course_id = snapshot.course_id if snapshot is not None else ""
    course_name = (
        snapshot.course_name if snapshot is not None else "מצגות מ-Drive"
    )
    preview: list[PreviewItem] = []
    counts = {"total": 0, "lesson": 0, "assignment": 0, "skip": 0}

    if snapshot is not None and coursework_ids:
        wanted = set(coursework_ids)
        chosen = [item for item in snapshot.coursework if item.id in wanted]
        _keep, dedupe = dedupe_coursework(chosen)
        rubric_cache: dict[str, tuple[str, float, str]] = {}
        for coursework in chosen:
            key = f"coursework:{coursework.id}"
            action = (actions.get(key) or "lesson").strip()
            if action not in {"lesson", "assignment", "skip"}:
                action = "lesson"
            info = dedupe.get(coursework.id, DedupeInfo())
            duplicate_of = (
                info.duplicate_of if info.duplicate_of in wanted else ""
            )
            rubric = (rubrics.get(key) or "").strip()
            score, reason = 0.0, ""
            if action == "assignment":
                # The x.2 hand-in always belongs to lesson x. Re-apply that
                # hard rule live so a stale catalog suggestion (a fuzzy ".2"
                # -> lesson_2 guess cached before lesson_x.json existed) can
                # never bind the wrong lesson again, even if the picker sent
                # the old pre-filled value.
                preferred = preferred_lesson_rubric(coursework.title)
                if preferred:
                    rubric, score, reason = preferred, 1.0, "lesson number match"
                elif not rubric:
                    if coursework.title not in rubric_cache:
                        rubric_cache[coursework.title] = source.suggest_rubric(
                            coursework.title
                        )
                    rubric, score, reason = rubric_cache[coursework.title]

            preview.append(
                _coursework_preview_item(
                    db,
                    source,
                    teacher_id=teacher_id,
                    group=group,
                    coursework=coursework,
                    info=info,
                    duplicate_of=duplicate_of,
                    rubric=rubric,
                    rubric_score=score,
                    rubric_reason=reason,
                    action=action,
                )
            )
            counts["total"] += 1
            counts["skip" if duplicate_of else action] += 1

    unique_decks = list(dict.fromkeys(fid for fid in deck_ids if fid))
    if unique_decks:
        known_decks = decks or {}
        drive_items = [
            known_decks.get(file_id) or source.deck_info(file_id)
            for file_id in unique_decks
        ]
        _keep, info_map = dedupe_drive_items(drive_items)
        for deck in drive_items:
            info = info_map.get(deck.file_id, DedupeInfo())
            duplicate_of = (
                info.duplicate_of if info.duplicate_of in unique_decks else ""
            )
            existing = _find_existing(
                db, teacher_id, group.id, "drive_deck", deck.file_id
            )
            reusable = (
                None
                if existing is not None
                else _find_reusable(
                    db, teacher_id, group.id, "drive_deck", deck.file_id
                )
            )
            preview.append(
                PreviewItem(
                    source_kind="drive_deck",
                    source_id=deck.file_id,
                    title=clean_title(deck.title),
                    original_title=deck.title,
                    parent_id=deck.folder_id,
                    source_version=deck.source_version,
                    suggested_action="skip" if duplicate_of else "lesson",
                    duplicate_of=duplicate_of,
                    duplicate_count=info.duplicate_count,
                    already_imported=existing is not None,
                    existing_portal_id=existing[1] if existing else None,
                    existing_class_name=_class_name(db, group.id),
                    reusable_from_class=reusable[2] if reusable else "",
                )
            )
            counts["total"] += 1
            counts["skip" if duplicate_of else "lesson"] += 1

    unique_docs = list(dict.fromkeys(fid for fid in doc_ids if fid))
    if unique_docs:
        known_docs = docs or {}
        doc_items = [
            known_docs.get(file_id) or source.deck_info(file_id)
            for file_id in unique_docs
        ]
        _keep, doc_info = dedupe_drive_items(doc_items)
        for document in doc_items:
            info = doc_info.get(document.file_id, DedupeInfo())
            duplicate_of = (
                info.duplicate_of if info.duplicate_of in unique_docs else ""
            )
            existing = _find_existing(
                db, teacher_id, group.id, "drive_doc", document.file_id
            )
            reusable = (
                None
                if existing is not None
                else _find_reusable(
                    db, teacher_id, group.id, "drive_doc", document.file_id
                )
            )
            preview.append(
                PreviewItem(
                    source_kind="drive_doc",
                    source_id=document.file_id,
                    title=clean_title(document.title),
                    original_title=document.title,
                    parent_id=document.folder_id,
                    source_version=document.source_version,
                    suggested_action="skip" if duplicate_of else "lesson",
                    duplicate_of=duplicate_of,
                    duplicate_count=info.duplicate_count,
                    already_imported=existing is not None,
                    existing_portal_id=existing[1] if existing else None,
                    existing_class_name=_class_name(db, group.id),
                    reusable_from_class=reusable[2] if reusable else "",
                )
            )
            counts["total"] += 1
            counts["skip" if duplicate_of else "lesson"] += 1

    manifest = ImportManifest(
        kind="selection",
        course_id=course_id,
        course_name=course_name,
        class_id=group.id,
        class_name=group.name,
        items=preview,
        generated_at=_now_iso(),
    )
    if snapshot is not None:
        _link_course(db, teacher_id, group, snapshot)
    return _new_job(
        db,
        teacher_id=teacher_id,
        class_id=group.id,
        course_id=course_id,
        kind="selection",
        manifest=manifest,
        counts=counts,
        state="queued",
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class ItemOutcome:
    status: str
    message: str = ""
    portal_type: str = ""
    portal_id: int | None = None
    content_hash: str = ""
    duplicate_of: str = ""


def _record_item(
    db: Session,
    job: ImportJob,
    item: PreviewItem,
    outcome: ItemOutcome,
    *,
    decision: str,
) -> None:
    row = db.scalar(
        select(ImportItem).where(
            ImportItem.teacher_id == job.teacher_id,
            ImportItem.class_id == job.class_id,
            ImportItem.source_kind == item.source_kind,
            ImportItem.source_id == item.source_id,
        )
    )
    if row is None:
        row = ImportItem(
            teacher_id=job.teacher_id,
            class_id=job.class_id or 0,
            source_kind=item.source_kind,
            source_id=item.source_id,
        )
        db.add(row)
    row.job_id = job.id
    row.parent_id = item.parent_id
    row.title = item.title[:200]
    row.decision = decision[:20]
    row.status = outcome.status
    row.message = outcome.message
    row.portal_type = outcome.portal_type
    row.portal_id = outcome.portal_id
    row.source_version = item.source_version[:64]
    row.content_hash = outcome.content_hash
    row.duplicate_of = outcome.duplicate_of
    row.payload_json = json.dumps(
        {"title": item.title, "rubric": item.suggested_rubric},
        ensure_ascii=False,
    )


def _image_resolver(db: Session, store: ContentStore, source, assignment_id: int):
    position = [0]

    def resolve(url: str, alt: str) -> str | None:
        try:
            data = source.fetch_image(url)
            mime = _image_mime(data)
            blob = store.save_and_register(db, data, mime)
        except Exception as exc:  # noqa: BLE001 - image is optional
            logger.info("Skipping an unreadable document image: %s", exc)
            return None
        attachment = AssignmentAttachment(
            assignment_id=assignment_id,
            title=(alt or "תמונה מתוך המסמך")[:300],
            mime_type=mime,
            blob_sha=blob.sha256,
            position=position[0],
        )
        db.add(attachment)
        db.flush()
        position[0] += 1
        return f"/student/assignments/{assignment_id}/media/{attachment.id}"

    return resolve


def _convert_description(
    raw_html: str,
    fallback_text: str,
    *,
    resolver=None,
) -> converter.ConvertedDoc:
    converted = converter.convert_doc_html(raw_html, resolve_image=resolver)
    if converted.text:
        return converted
    if fallback_text.strip():
        value = _paragraphs(fallback_text)
        return converter.ConvertedDoc(
            html=value, text=html_to_text(value)
        )
    return converted


def _add_file_attachments(
    db: Session,
    store: ContentStore,
    source,
    *,
    assignment_id: int,
    item: PreviewItem,
    start_position: int = 0,
) -> int:
    """Store downloadable files and deck PDFs as local attachments."""
    position = start_position
    for raw in item.attachments:
        if raw.get("kind") != "drive" or not raw.get("file_id"):
            continue
        mime = (raw.get("mime_type") or "").strip()
        title = (raw.get("title") or "קובץ")[:300]
        if mime == "application/vnd.google-apps.document":
            continue  # used as instructions / slide text
        try:
            if mime == "application/vnd.google-apps.presentation":
                data = source.deck_pdf(raw["file_id"]).pdf
                mime = "application/pdf"
                title = f"{title}.pdf"
            elif mime in {"application/pdf", ""} or mime.startswith(
                "application/vnd.openxmlformats-officedocument"
            ) or mime.startswith("application/msword"):
                data = source.download(raw["file_id"])
            else:
                continue
        except Exception as exc:  # noqa: BLE001 - attachment is optional
            logger.warning("Skipping attachment %s: %s", title, exc)
            continue
        try:
            blob = store.save_and_register(db, data, mime or "application/octet-stream")
        except ContentStoreError as exc:
            logger.info("Skipping oversized attachment %s: %s", title, exc)
            continue
        db.add(
            AssignmentAttachment(
                assignment_id=assignment_id,
                title=title,
                mime_type=blob.mime_type,
                blob_sha=blob.sha256,
                position=position,
            )
        )
        position += 1
    return position


def _assignment_body(
    db: Session,
    store: ContentStore,
    source,
    *,
    assignment_id: int,
    item: PreviewItem,
):
    resolver = _image_resolver(db, store, source, assignment_id)
    docs = [
        raw
        for raw in item.attachments
        if raw.get("mime_type") == "application/vnd.google-apps.document"
        and raw.get("file_id")
    ]
    converted = _convert_description(
        item.description_html, item.description_text, resolver=resolver
    )
    if not converted.text and docs:
        try:
            payload = source.doc_html(str(docs[0]["file_id"]))
            if payload.html:
                converted = converter.convert_doc_html(
                    payload.html, resolve_image=resolver
                )
            elif payload.text:
                converted = _convert_description("", payload.text)
        except Exception as exc:  # noqa: BLE001 - keep the assignment anyway
            converted.warnings.append(f"לא ניתן היה לייבא את מסמך ההוראות: {exc}")
    if not converted.text:
        converted = _convert_description("", item.title)
    return converted, docs


def _content_hash(item: PreviewItem, body_html: str) -> str:
    attachment_bits = "|".join(
        sorted(
            f"{raw.get('title')}:{raw.get('mime_type')}"
            for raw in item.attachments
        )
    )
    return _digest(item.source_version, body_html, attachment_bits)


def _create_assignment(
    db: Session,
    store: ContentStore,
    source,
    job: ImportJob,
    item: PreviewItem,
) -> ItemOutcome:
    rubric = (item.suggested_rubric or "").strip()
    if not rubric:
        return ItemOutcome(
            status="needs_review",
            message="לא נבחר מחוון. בחרו קובץ מחוון לפני ייבוא המטלה.",
        )
    try:
        binding = adapter.load_rubric(rubric, allowed_roots=RUBRIC_ROOTS)
    except adapter.RubricBindingError as exc:
        return ItemOutcome(status="failed", message=f"מחוון לא תקין: {exc}")

    title = clean_title(item.title)
    assignment = Assignment(
        class_id=job.class_id or 0,
        title=title,
        instructions=item.description_text[:20_000],
        rubric_path=str(binding.path),
        rubric_sha256=binding.sha256,
        due_at=_parse_due(item.due_at),
        published=False,
    )
    db.add(assignment)
    db.flush()

    body, docs = _assignment_body(
        db, store, source, assignment_id=assignment.id, item=item
    )
    position = _add_file_attachments(
        db, store, source, assignment_id=assignment.id, item=item
    )
    if len(docs) > 1:
        body.warnings.append("יובא מסמך ההוראות הראשון; מסמכים נוספים לא יובאו.")

    content_hash = _content_hash(item, body.html)
    db.add(
        AssignmentContent(
            assignment_id=assignment.id,
            body_html=body.html,
            body_text=body.text,
            topic_name=item.topic_name[:200],
            source_kind=item.source_kind,
            source_id=item.source_id,
            source_version=item.source_version[:64],
            original_title=item.original_title[:300],
            attachments_json=json.dumps(
                [
                    {
                        "title": raw.get("title"),
                        "mime_type": raw.get("mime_type"),
                    }
                    for raw in item.attachments
                ],
                ensure_ascii=False,
            ),
            content_hash=content_hash,
        )
    )
    message = "המטלה יובאה כטיוטה."
    if body.warnings:
        message += " " + " ".join(body.warnings)
    return ItemOutcome(
        status="imported",
        message=message,
        portal_type="assignment",
        portal_id=assignment.id,
        content_hash=content_hash,
    )


def _delete_lesson_slides(db: Session, lesson_id: int) -> None:
    slides = list(
        db.scalars(select(LessonSlide).where(LessonSlide.lesson_id == lesson_id))
    )
    for slide in slides:
        content = db.get(SlideContent, slide.id)
        if content is not None:
            db.delete(content)
        db.delete(slide)
    db.flush()


def _add_text_slide(
    db: Session,
    lesson: Lesson,
    *,
    position: int,
    title: str,
    body_html: str,
    text: str,
) -> None:
    slide = LessonSlide(
        lesson_id=lesson.id,
        position=position,
        title=title[:160],
        body="",
        code="",
    )
    db.add(slide)
    db.flush()
    db.add(
        SlideContent(
            slide_id=slide.id,
            body_html=body_html,
            text_snapshot=text[:20_000],
            alt_text=title[:300],
        )
    )


def _links_slide(item: PreviewItem) -> tuple[str, str]:
    """Clickable list of external links attached to a Classroom material.

    The Materials tab is mostly H5P/מודל activities rather than files. They
    are not Drive/Docs links, so showing them to students leaks nothing; a
    lesson that ignored them would import as a title-only empty page. Only
    ``http(s)`` URLs are accepted and every label is escaped.
    """
    entries: list[str] = []
    labels: list[str] = []
    for raw in item.attachments:
        if raw.get("kind") != "link":
            continue
        url = str(raw.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        title = str(raw.get("title") or "").strip() or url
        entries.append(
            '<li><a href="{href}" target="_blank" rel="noopener noreferrer">'
            "{label}</a></li>".format(
                href=html_module.escape(url, quote=True),
                label=html_module.escape(title),
            )
        )
        labels.append(title)
    if not entries:
        return "", ""
    return "<ul>" + "".join(entries) + "</ul>", " · ".join(labels)


def _deck_failure_text(exc: BaseException) -> str:
    """A teacher-safe Hebrew reason for one failed deck export/render.

    ``SourceError`` and ``IngestionAuthError`` already carry actionable Hebrew
    (expired Google token, unshared file, empty export). Anything else is
    reduced to its class name so a raw provider URL or stack detail can never
    reach the job screen.
    """
    if isinstance(exc, (SourceError, IngestionAuthError)):
        return str(exc)
    if isinstance(exc, DeckRenderError):
        return f"עיבוד קובץ המצגת נכשל: {exc}"
    return f"שגיאה בייצוא המצגת ({type(exc).__name__})"


def _build_lesson_slides(
    db: Session,
    store: ContentStore,
    source,
    lesson: Lesson,
    item: PreviewItem,
) -> tuple[str, int, str]:
    """Create slides for a lesson and return (render_mode, page_count, hash).

    A lesson whose deck cannot be exported is a **failed item**, not a draft:
    silently falling back to a single title-only slide hides the real problem
    (an expired Google token, an unshared file) behind a lesson that looks
    imported but has no content. The caller records the Hebrew reason on the
    job screen, and the teacher can retry after fixing the cause.
    """
    deck_ids = [
        str(raw["file_id"])
        for raw in item.attachments
        if raw.get("mime_type") == "application/vnd.google-apps.presentation"
        and raw.get("file_id")
    ]
    # Decks picked straight from Drive have no attachment row; the source item
    # itself is the deck to render.
    if item.source_kind == "drive_deck" and item.source_id:
        deck_ids.append(item.source_id)
    deck_ids = list(dict.fromkeys(deck_ids))
    docs = [
        raw
        for raw in item.attachments
        if raw.get("mime_type") == "application/vnd.google-apps.document"
        and raw.get("file_id")
    ]
    # A document picked straight from Drive has no attachment row; the source
    # item itself is the document whose text becomes the lesson.
    if item.source_kind == "drive_doc" and item.source_id:
        docs.append(
            {
                "file_id": item.source_id,
                "title": item.title,
                "mime_type": "application/vnd.google-apps.document",
            }
        )
    hashes: list[str] = []
    page_count = 0
    render_mode = "text"

    if deck_ids:
        render_mode = "images"
        rendered_any = False
        deck_errors: list[str] = []
        for deck_id in deck_ids:
            try:
                pages = render_pdf_pages(source.deck_pdf(deck_id).pdf)
            except Exception as exc:  # noqa: BLE001 - one bad deck must not abort
                logger.warning("Deck import failed for %s: %s", deck_id, exc)
                deck_errors.append(_deck_failure_text(exc))
                continue
            for page in pages:
                blob = store.save_and_register(db, page.webp, "image/webp")
                slide = LessonSlide(
                    lesson_id=lesson.id,
                    position=page_count,
                    title=f"שקופית {page_count + 1}",
                    body="",
                    code="",
                )
                db.add(slide)
                db.flush()
                db.add(
                    SlideContent(
                        slide_id=slide.id,
                        image_blob_sha=blob.sha256,
                        alt_text=f"{lesson.title} – שקופית {page_count + 1}",
                        width=page.width,
                        height=page.height,
                        text_snapshot=page.text,
                    )
                )
                hashes.append(blob.sha256)
                page_count += 1
                rendered_any = True
        if not rendered_any:
            # No deck could be exported/rendered. Fail the item with the real
            # reason instead of creating a title-only "lesson" that hides it.
            raise SourceError(
                "לא ניתן לייבא את המצגת, ולכן השיעור לא נוצר. "
                + " · ".join(dict.fromkeys(deck_errors))
            )
        if deck_errors:
            logger.warning(
                "Lesson %s imported with %s unreadable deck(s): %s",
                lesson.id,
                len(deck_errors),
                "; ".join(deck_errors),
            )

    if render_mode == "text":
        if item.description_text.strip() or item.description_html.strip():
            converted = _convert_description(
                item.description_html, item.description_text
            )
            if converted.text:
                _add_text_slide(
                    db,
                    lesson,
                    position=page_count,
                    title=lesson.title,
                    body_html=converted.html,
                    text=converted.text,
                )
                hashes.append(_digest(converted.html))
                page_count += 1
        for raw in docs[:MAX_DOCS_PER_LESSON]:
            try:
                payload = source.doc_html(str(raw["file_id"]))
            except Exception as exc:  # noqa: BLE001 - one doc must not abort
                logger.info("Doc import failed: %s", exc)
                continue
            if payload.html:
                converted = converter.convert_doc_html(payload.html)
            else:
                converted = _convert_description("", payload.text)
            if not converted.text:
                continue
            _add_text_slide(
                db,
                lesson,
                position=page_count,
                title=clean_title(str(raw.get("title") or lesson.title)),
                body_html=converted.html,
                text=converted.text,
            )
            hashes.append(_digest(converted.html))
            page_count += 1
        links_html, links_text = _links_slide(item)
        if links_html:
            _add_text_slide(
                db,
                lesson,
                position=page_count,
                title="קישורים",
                body_html=links_html,
                text=links_text,
            )
            hashes.append(_digest(links_html))
            page_count += 1

    if page_count == 0:
        # Always leave a readable draft: a title-only lesson is easier for the
        # teacher to fix than a silent failure.
        _add_text_slide(
            db,
            lesson,
            position=0,
            title=lesson.title,
            body_html=_paragraphs(item.description_text or item.title),
            text=item.description_text or item.title,
        )
        page_count = 1

    return render_mode, page_count, _digest(item.source_version, *hashes)


def _create_lesson(
    db: Session,
    store: ContentStore,
    source,
    job: ImportJob,
    item: PreviewItem,
) -> ItemOutcome:
    lesson = Lesson(
        teacher_id=job.teacher_id,
        class_id=job.class_id,
        title=clean_title(item.title),
        summary=item.description_text[:20_000],
        published=False,
    )
    db.add(lesson)
    db.flush()
    render_mode, page_count, content_hash = _build_lesson_slides(
        db, store, source, lesson, item
    )
    db.add(
        LessonSource(
            lesson_id=lesson.id,
            source_kind=item.source_kind,
            source_id=item.source_id,
            source_version=item.source_version[:64],
            topic_name=item.topic_name[:200],
            render_mode=render_mode,
            page_count=page_count,
            original_title=item.original_title[:300],
            content_hash=content_hash,
        )
    )
    return ItemOutcome(
        status="imported",
        message="השיעור יובא כטיוטה.",
        portal_type="lesson",
        portal_id=lesson.id,
        content_hash=content_hash,
    )


def _update_assignment(
    db: Session,
    store: ContentStore,
    source,
    assignment: Assignment,
    content: AssignmentContent,
    item: PreviewItem,
) -> ItemOutcome:
    # Assignments created by the importer are not editable in Phase 2, so an
    # update can safely refresh generated fields. The teacher's publish state
    # is deliberately preserved.
    rubric = (item.suggested_rubric or "").strip()
    if rubric:
        try:
            binding = adapter.load_rubric(rubric, allowed_roots=RUBRIC_ROOTS)
        except adapter.RubricBindingError as exc:
            return ItemOutcome(
                status="needs_review",
                message=f"המחוון שנבחר אינו תקין: {exc}",
                portal_type="assignment",
                portal_id=assignment.id,
            )
        assignment.rubric_path = str(binding.path)
        assignment.rubric_sha256 = binding.sha256

    assignment.title = clean_title(item.title)
    assignment.instructions = item.description_text[:20_000]
    assignment.due_at = _parse_due(item.due_at)

    for attachment in db.scalars(
        select(AssignmentAttachment).where(
            AssignmentAttachment.assignment_id == assignment.id
        )
    ).all():
        db.delete(attachment)
    db.flush()
    body, docs = _assignment_body(
        db, store, source, assignment_id=assignment.id, item=item
    )
    _add_file_attachments(
        db, store, source, assignment_id=assignment.id, item=item
    )
    content.body_html = body.html
    content.body_text = body.text
    content.topic_name = item.topic_name[:200]
    content.source_version = item.source_version[:64]
    content.original_title = item.original_title[:300]
    content.content_hash = _content_hash(item, body.html)
    content.imported_at = utcnow()
    return ItemOutcome(
        status="updated",
        message="המטלה עודכנה מהמקור.",
        portal_type="assignment",
        portal_id=assignment.id,
        content_hash=content.content_hash,
    )


def _update_lesson(
    db: Session,
    store: ContentStore,
    source,
    lesson: Lesson,
    provenance: LessonSource,
    item: PreviewItem,
) -> ItemOutcome:
    if lesson.updated_at > provenance.imported_at:
        return ItemOutcome(
            status="needs_review",
            message="השיעור נערך בפורטל מאז הייבוא; לא דורסים עריכה ידנית.",
            portal_type="lesson",
            portal_id=lesson.id,
        )
    lesson.title = clean_title(item.title)
    lesson.summary = item.description_text[:20_000]
    _delete_lesson_slides(db, lesson.id)
    render_mode, page_count, content_hash = _build_lesson_slides(
        db, store, source, lesson, item
    )
    provenance.source_version = item.source_version[:64]
    provenance.topic_name = item.topic_name[:200]
    provenance.render_mode = render_mode
    provenance.page_count = page_count
    provenance.original_title = item.original_title[:300]
    provenance.content_hash = content_hash
    provenance.imported_at = utcnow()
    return ItemOutcome(
        status="updated",
        message="השיעור עודכן מהמקור.",
        portal_type="lesson",
        portal_id=lesson.id,
        content_hash=content_hash,
    )


def _clone_lesson(
    db: Session, lesson: Lesson, target_class_id: int
) -> Lesson:
    clone = Lesson(
        teacher_id=lesson.teacher_id,
        class_id=target_class_id,
        title=lesson.title,
        summary=lesson.summary,
        published=False,
    )
    db.add(clone)
    db.flush()
    ordered = db.scalars(
        select(LessonSlide)
        .where(LessonSlide.lesson_id == lesson.id)
        .order_by(LessonSlide.position, LessonSlide.id)
    ).all()
    for slide in ordered:
        new_slide = LessonSlide(
            lesson_id=clone.id,
            position=slide.position,
            title=slide.title,
            body=slide.body,
            code=slide.code,
        )
        db.add(new_slide)
        db.flush()
        content = db.get(SlideContent, slide.id)
        if content is not None:
            db.add(
                SlideContent(
                    slide_id=new_slide.id,
                    image_blob_sha=content.image_blob_sha,
                    alt_text=content.alt_text,
                    width=content.width,
                    height=content.height,
                    text_snapshot=content.text_snapshot,
                    body_html=content.body_html,
                )
            )
    provenance = db.get(LessonSource, lesson.id)
    if provenance is not None:
        db.add(
            LessonSource(
                lesson_id=clone.id,
                source_kind=provenance.source_kind,
                source_id=provenance.source_id,
                source_version=provenance.source_version,
                topic_name=provenance.topic_name,
                render_mode=provenance.render_mode,
                page_count=provenance.page_count,
                original_title=provenance.original_title,
                source_blob_sha=provenance.source_blob_sha,
                content_hash=provenance.content_hash,
                reused_from_lesson_id=lesson.id,
            )
        )
    return clone


def _clone_assignment(
    db: Session, assignment: Assignment, target_class_id: int
) -> Assignment:
    clone = Assignment(
        class_id=target_class_id,
        title=assignment.title,
        instructions=assignment.instructions,
        rubric_path=assignment.rubric_path,
        rubric_sha256=assignment.rubric_sha256,
        due_at=assignment.due_at,
        published=False,
    )
    db.add(clone)
    db.flush()
    content = db.get(AssignmentContent, assignment.id)
    if content is not None:
        db.add(
            AssignmentContent(
                assignment_id=clone.id,
                body_html=content.body_html,
                body_text=content.body_text,
                topic_name=content.topic_name,
                source_kind=content.source_kind,
                source_id=content.source_id,
                source_version=content.source_version,
                original_title=content.original_title,
                attachments_json=content.attachments_json,
                content_hash=content.content_hash,
                reused_from_assignment_id=assignment.id,
            )
        )
    for attachment in db.scalars(
        select(AssignmentAttachment)
        .where(AssignmentAttachment.assignment_id == assignment.id)
        .order_by(AssignmentAttachment.position)
    ).all():
        db.add(
            AssignmentAttachment(
                assignment_id=clone.id,
                title=attachment.title,
                mime_type=attachment.mime_type,
                blob_sha=attachment.blob_sha,
                position=attachment.position,
            )
        )
    return clone


class ReuseError(RuntimeError):
    """A lesson/assignment cannot be reused in the requested class."""


def reuse_lesson(
    db: Session, lesson: Lesson, target_group: ClassGroup
) -> Lesson:
    """Copy an existing lesson into another class as a draft."""
    if lesson.teacher_id != target_group.teacher_id:
        raise ReuseError("השיעור אינו שייך למורה הזה.")
    if lesson.class_id == target_group.id:
        raise ReuseError("השיעור כבר נמצא בכיתה הזו.")
    existing = db.scalar(
        select(Lesson)
        .join(LessonSource, LessonSource.lesson_id == Lesson.id)
        .where(
            Lesson.class_id == target_group.id,
            LessonSource.reused_from_lesson_id == lesson.id,
        )
    )
    if existing is not None:
        raise ReuseError(f"השיעור כבר נמצא בכיתה '{target_group.name}'.")
    return _clone_lesson(db, lesson, target_group.id)


def reuse_assignment(
    db: Session, assignment: Assignment, target_group: ClassGroup
) -> Assignment:
    """Copy an existing assignment into another class as a draft."""
    source_class = db.get(ClassGroup, assignment.class_id)
    if source_class is None or source_class.teacher_id != target_group.teacher_id:
        raise ReuseError("המטלה אינה שייכת למורה הזה.")
    if assignment.class_id == target_group.id:
        raise ReuseError("המטלה כבר נמצאת בכיתה הזו.")
    existing = db.scalar(
        select(Assignment)
        .join(
            AssignmentContent,
            AssignmentContent.assignment_id == Assignment.id,
        )
        .where(
            Assignment.class_id == target_group.id,
            AssignmentContent.reused_from_assignment_id == assignment.id,
        )
    )
    if existing is not None:
        raise ReuseError(f"המטלה כבר נמצאת בכיתה '{target_group.name}'.")
    return _clone_assignment(db, assignment, target_group.id)


def _apply_item(
    db: Session,
    store: ContentStore,
    source,
    job: ImportJob,
    item: PreviewItem,
) -> ItemOutcome:
    decision = (item.suggested_action or "skip").strip()
    if decision not in ACTIONS:
        decision = "skip"

    if item.duplicate_of:
        return ItemOutcome(
            status="skipped",
            message="כפילות זהה של פריט אחר באותה כיתה.",
            duplicate_of=item.duplicate_of,
        )
    if decision == "skip":
        return ItemOutcome(status="skipped", message="דולג לפי בחירת המורה.")

    if job.class_id is None:
        return ItemOutcome(
            status="failed", message="לא נבחרה כיתת יעד לייבוא."
        )

    existing = _find_existing(
        db, job.teacher_id, job.class_id, item.source_kind, item.source_id
    )

    if decision.startswith("reuse_"):
        reusable = _find_reusable(
            db, job.teacher_id, job.class_id, item.source_kind, item.source_id
        )
        if existing is not None:
            return ItemOutcome(
                status="skipped",
                message="הפריט כבר קיים בכיתה הזו.",
                portal_type=existing[0],
                portal_id=existing[1],
            )
        if reusable is not None:
            if reusable[0] == "lesson":
                origin = db.get(Lesson, reusable[1])
                if origin is not None:
                    clone = _clone_lesson(db, origin, job.class_id)
                    return ItemOutcome(
                        status="imported",
                        message=f"השיעור הועתק מהכיתה '{reusable[2]}' כטיוטה.",
                        portal_type="lesson",
                        portal_id=clone.id,
                    )
            else:
                origin = db.get(Assignment, reusable[1])
                if origin is not None:
                    clone = _clone_assignment(db, origin, job.class_id)
                    return ItemOutcome(
                        status="imported",
                        message=f"המטלה הועתקה מהכיתה '{reusable[2]}' כטיוטה.",
                        portal_type="assignment",
                        portal_id=clone.id,
                    )
        # Fall through to a fresh import if the other class disappeared.

    if decision == "assignment":
        if existing is not None and existing[0] == "assignment":
            assignment = db.get(Assignment, existing[1])
            content = db.get(AssignmentContent, existing[1])
            if assignment is None or content is None:
                existing = None
            elif existing[2] == item.source_version:
                return ItemOutcome(
                    status="skipped",
                    message="המטלה כבר יובאה ואינה השתנתה במקור.",
                    portal_type="assignment",
                    portal_id=assignment.id,
                )
            else:
                return _update_assignment(
                    db, store, source, assignment, content, item
                )
        if existing is not None and existing[0] == "lesson":
            return ItemOutcome(
                status="needs_review",
                message="הפריט קיים כשיעור. מחקו אותו כדי לייבא כמטלה.",
                portal_type="lesson",
                portal_id=existing[1],
            )
        return _create_assignment(db, store, source, job, item)

    # decision == "lesson"
    if existing is not None and existing[0] == "lesson":
        lesson = db.get(Lesson, existing[1])
        provenance = db.get(LessonSource, existing[1])
        if lesson is None or provenance is None:
            existing = None
        elif existing[2] == item.source_version:
            return ItemOutcome(
                status="skipped",
                message="השיעור כבר יובא ולא השתנה במקור.",
                portal_type="lesson",
                portal_id=lesson.id,
            )
        else:
            return _update_lesson(
                db, store, source, lesson, provenance, item
            )
    if existing is not None and existing[0] == "assignment":
        return ItemOutcome(
            status="needs_review",
            message="הפריט קיים כמטלה. מחקו אותו כדי לייבא כשיעור.",
            portal_type="assignment",
            portal_id=existing[1],
        )
    return _create_lesson(db, store, source, job, item)


def apply_job(
    db: Session,
    store: ContentStore,
    source,
    job: ImportJob,
    *,
    limit: int | None = None,
) -> dict:
    """Apply the teacher's decisions stored on the job's manifest.

    Commits per item so a killed process leaves a resumable job: rows already
    imported are recognised on the next run.
    """
    manifest = ImportManifest.from_json(json.loads(job.manifest_json or "{}"))
    job.state = "running"
    job.started_at = utcnow()
    job.error = ""
    db.commit()

    counts = {
        "total": 0,
        "imported": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "needs_review": 0,
    }
    processed = 0
    for item in manifest.items:
        if limit is not None and processed >= limit:
            break
        processed += 1
        counts["total"] += 1
        decision = (item.suggested_action or "skip").strip()
        try:
            outcome = _apply_item(db, store, source, job, item)
            _record_item(db, job, item, outcome, decision=decision)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - continue with the next item
            logger.exception("Import item failed: %s", item.source_id)
            db.rollback()
            job = db.get(ImportJob, job.id) or job
            # Source/auth errors carry a teacher-safe Hebrew reason (expired
            # Google token, unreadable deck); anything else stays a class name
            # so raw provider detail never reaches the job screen.
            detail = (
                str(exc)
                if isinstance(exc, (SourceError, IngestionAuthError))
                else type(exc).__name__
            )
            try:
                _record_item(
                    db,
                    job,
                    item,
                    ItemOutcome(
                        status="failed",
                        message=f"הייבוא נכשל: {detail}",
                    ),
                    decision=decision,
                )
                db.commit()
            except Exception:  # noqa: BLE001 - never abort the whole run
                db.rollback()
            counts["failed"] += 1
            continue
        counts[outcome.status] = counts.get(outcome.status, 0) + 1

    job = db.get(ImportJob, job.id) or job
    remaining = len(manifest.items) - processed
    if counts["failed"] and (counts["imported"] or counts["updated"]):
        job.state = "partial"
    elif counts["failed"]:
        job.state = "failed"
    elif remaining:
        job.state = "queued"
    else:
        job.state = "completed"
    job.counts_json = json.dumps(counts, ensure_ascii=False)
    job.finished_at = utcnow() if not remaining else None
    job.manifest_json = json.dumps(manifest.as_json(), ensure_ascii=False)
    db.commit()
    return counts


def reset_job_for_retry(db: Session, job: ImportJob) -> None:
    """Put a stopped job back in the queue so the worker can resume it."""
    job.state = "queued"
    job.error = ""
    db.commit()
