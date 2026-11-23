"""Content-addressed storage for imported media.

Imported bytes are immutable and named by their SHA-256, so the same Doc image
or deck page is stored once no matter how many classes reuse it. The database
stores a relative path; resolving a path is restricted to the configured
content root to make traversal impossible even if a malformed row existed.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.models import ContentBlob

# A single imported file larger than this is almost certainly not course
# material; refuse it rather than filling the school server's disk.
MAX_BLOB_BYTES = 40 * 1024 * 1024

_EXTENSIONS = {
    "image/webp": ".webp",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
}


def extension_for(mime_type: str) -> str:
    return _EXTENSIONS.get((mime_type or "").split(";")[0].strip().lower(), ".bin")


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    rel_path: str
    mime_type: str
    byte_size: int


class ContentStoreError(Exception):
    """A write could not be completed safely."""


class ContentStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, rel_path: str) -> Path:
        candidate = (self.root / rel_path).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ContentStoreError("Refusing a path outside the content root.")
        return candidate

    def save(self, data: bytes, mime_type: str) -> StoredBlob:
        if not data:
            raise ContentStoreError("Refusing to store an empty file.")
        if len(data) > MAX_BLOB_BYTES:
            raise ContentStoreError(
                f"Imported file is too large ({len(data)} bytes)."
            )
        digest = hashlib.sha256(data).hexdigest()
        rel_path = f"blobs/{digest[:2]}/{digest}{extension_for(mime_type)}"
        target = self._resolve(rel_path)
        if target.is_file() and target.stat().st_size == len(data):
            return StoredBlob(digest, rel_path, mime_type, len(data))

        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=".import_", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, target)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
        return StoredBlob(digest, rel_path, mime_type, len(data))

    def save_and_register(
        self, db: Session, data: bytes, mime_type: str
    ) -> StoredBlob:
        blob = self.save(data, mime_type)
        existing = db.get(ContentBlob, blob.sha256)
        if existing is None:
            db.add(
                ContentBlob(
                    sha256=blob.sha256,
                    rel_path=blob.rel_path,
                    mime_type=blob.mime_type,
                    byte_size=blob.byte_size,
                )
            )
        return blob

    def path_for(self, rel_path: str) -> Path:
        target = self._resolve(rel_path)
        if not target.is_file():
            raise ContentStoreError("Content file is missing.")
        return target

    def path_for_sha(self, db: Session, sha256: str) -> tuple[Path, ContentBlob]:
        record = db.get(ContentBlob, (sha256 or "").strip())
        if record is None:
            raise ContentStoreError("Content metadata is missing.")
        return self.path_for(record.rel_path), record

    def has_sha(self, db: Session, sha256: str) -> bool:
        return db.scalar(
            select(ContentBlob.sha256).where(ContentBlob.sha256 == sha256)
        ) is not None

    def delete_unreferenced(self, db: Session, sha256: str) -> None:
        """Remove a blob only when no extension row still points at it."""
        from portal.models import AssignmentAttachment, SlideContent

        in_slides = db.scalar(
            select(SlideContent.slide_id).where(
                SlideContent.image_blob_sha == sha256
            )
        )
        in_attachments = db.scalar(
            select(AssignmentAttachment.id).where(
                AssignmentAttachment.blob_sha == sha256
            )
        )
        if in_slides or in_attachments:
            return
        record = db.get(ContentBlob, sha256)
        if record is None:
            return
        try:
            self.path_for(record.rel_path).unlink(missing_ok=True)
        except ContentStoreError:
            pass
        db.delete(record)
