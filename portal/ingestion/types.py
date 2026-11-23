"""Plain data structures exchanged by the ingestion pipeline.

Nothing here imports ``src/`` or the web layer. The source adapter produces
these snapshots, the deduplicator and preview operate on them, and the
importer consumes them. Keeping the boundary data-only makes the importer
testable with fake sources and impossible to accidentally hand a raw Google
payload to a template.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Attachment:
    """One classroom attachment, normalized for both API shapes."""

    file_id: str
    title: str
    mime_type: str | None = None
    # Teacher-only. Never copied into a student template or JSON response.
    url: str = ""
    kind: str = "drive"
    modified_time: str = ""

    @property
    def is_drive(self) -> bool:
        return self.kind == "drive" and bool(self.file_id)

    @property
    def is_document(self) -> bool:
        return self.mime_type == "application/vnd.google-apps.document"

    @property
    def is_presentation(self) -> bool:
        return self.mime_type == "application/vnd.google-apps.presentation"

    @property
    def is_pdf(self) -> bool:
        return self.mime_type == "application/pdf"

    def as_teacher_json(self) -> dict[str, Any]:
        """Metadata safe for the teacher-only preview, never for students."""
        return {
            "file_id": self.file_id,
            "title": self.title,
            "mime_type": self.mime_type,
            "kind": self.kind,
            "url": self.url,
        }


@dataclass(frozen=True)
class CourseworkItem:
    """Normalized coursework resource.

    ``description_html`` is the raw Classroom HTML (untrusted). The converter
    sanitizes it before it is stored or rendered.
    """

    id: str
    title: str
    description_html: str = ""
    state: str = "PUBLISHED"
    work_type: str = "ASSIGNMENT"
    topic_id: str | None = None
    topic_name: str | None = None
    due_at: datetime | None = None
    max_points: int | None = None
    source_version: str = ""
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)

    @property
    def documents(self) -> tuple[Attachment, ...]:
        return tuple(a for a in self.attachments if a.is_document)

    @property
    def presentations(self) -> tuple[Attachment, ...]:
        return tuple(a for a in self.attachments if a.is_presentation)

    @property
    def downloadable(self) -> tuple[Attachment, ...]:
        """Attachments that are neither Docs nor decks, but real files."""
        return tuple(
            a
            for a in self.attachments
            if a.is_drive and not a.is_document and not a.is_presentation
        )


@dataclass(frozen=True)
class CourseSnapshot:
    course_id: str
    course_name: str
    section: str | None = None
    topics: dict[str, str] = field(default_factory=dict)
    coursework: tuple[CourseworkItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DriveItem:
    """A deck (or other file) selected directly from Drive."""

    file_id: str
    title: str
    mime_type: str
    modified_time: str = ""
    folder_id: str = ""
    # Teacher-only provenance used to keep unused courses (פייתון ב') out of
    # the picker; sanitized of RTL control characters.
    folder_name: str = ""

    @property
    def is_presentation(self) -> bool:
        return self.mime_type == "application/vnd.google-apps.presentation"

    @property
    def is_document(self) -> bool:
        return self.mime_type == "application/vnd.google-apps.document"

    @property
    def source_version(self) -> str:
        return self.modified_time or self.file_id


@dataclass(frozen=True)
class DocPayload:
    html: str
    text: str
    title: str = ""
    modified_time: str = ""


@dataclass(frozen=True)
class DeckPayload:
    pdf: bytes
    title: str = ""
    modified_time: str = ""


@dataclass
class PreviewItem:
    """One row the teacher sees in the import preview."""

    source_kind: str
    source_id: str
    title: str
    original_title: str = ""
    parent_id: str = ""
    topic_name: str = ""
    description_text: str = ""
    # Raw Classroom HTML, stored only in the teacher-only import manifest.
    description_html: str = ""
    state: str = "PUBLISHED"
    work_type: str = "ASSIGNMENT"
    due_at: str = ""
    max_points: int | None = None
    source_version: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    suggested_action: str = "skip"
    suggested_rubric: str = ""
    rubric_score: float = 0.0
    rubric_reason: str = ""
    duplicate_of: str = ""
    duplicate_count: int = 0
    already_imported: bool = False
    existing_portal_id: int | None = None
    existing_class_name: str = ""
    reusable_from_class: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "title": self.title,
            "original_title": self.original_title,
            "parent_id": self.parent_id,
            "topic_name": self.topic_name,
            "description_text": self.description_text,
            "description_html": self.description_html,
            "state": self.state,
            "work_type": self.work_type,
            "due_at": self.due_at,
            "max_points": self.max_points,
            "source_version": self.source_version,
            "attachments": list(self.attachments),
            "suggested_action": self.suggested_action,
            "suggested_rubric": self.suggested_rubric,
            "rubric_score": self.rubric_score,
            "rubric_reason": self.rubric_reason,
            "duplicate_of": self.duplicate_of,
            "duplicate_count": self.duplicate_count,
            "already_imported": self.already_imported,
            "existing_portal_id": self.existing_portal_id,
            "existing_class_name": self.existing_class_name,
            "reusable_from_class": self.reusable_from_class,
        }


@dataclass
class ImportManifest:
    """Serializable preview stored on ``ImportJob.manifest_json``."""

    kind: str
    course_id: str
    course_name: str
    class_id: int
    class_name: str
    items: list[PreviewItem]
    generated_at: str
    include_drafts: bool = False
    version: int = 1

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "course_id": self.course_id,
            "course_name": self.course_name,
            "class_id": self.class_id,
            "class_name": self.class_name,
            "generated_at": self.generated_at,
            "include_drafts": self.include_drafts,
            "items": [item.as_json() for item in self.items],
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ImportManifest":
        items = [
            PreviewItem(**item) for item in payload.get("items") or []
        ]
        return cls(
            kind=str(payload.get("kind") or "course"),
            course_id=str(payload.get("course_id") or ""),
            course_name=str(payload.get("course_name") or ""),
            class_id=int(payload.get("class_id") or 0),
            class_name=str(payload.get("class_name") or ""),
            items=items,
            generated_at=str(payload.get("generated_at") or ""),
            include_drafts=bool(payload.get("include_drafts")),
            version=int(payload.get("version") or 1),
        )
