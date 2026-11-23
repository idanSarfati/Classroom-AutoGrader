"""Read-only boundary between the portal and Google Classroom/Drive/Docs.

This is the only ingestion module that imports ``src.*``. It intentionally
does **not** import Streamlit and never starts an OAuth flow: credentials come
from :mod:`portal.ingestion.google_auth`, which only loads/refreshes a token.

Everything returned from here is a normalized snapshot. Raw API payloads,
Drive ids and Google URLs stay inside this module / teacher-only preview data.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from src.classroom_service import (
    ClassroomService,
    ClassroomServiceError,
    _classroom_due_instant,
)

from portal.ingestion.google_auth import load_credentials
from portal.ingestion.normalize import clean_title, leading_number, strip_control_chars
from portal.ingestion.types import (
    Attachment,
    CourseSnapshot,
    CourseworkItem,
    DeckPayload,
    DocPayload,
    DriveItem,
)
from portal.paths import PROJECT_ROOT, RUBRIC_ROOTS

logger = logging.getLogger(__name__)

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"

_MAX_FILE_BYTES = 40 * 1024 * 1024

# "1.2 | הגשת משימות" is the table of exercises from the 1.1 slides.
_LESSON_SUBMISSION_RE = re.compile(r"^\s*(\d+)\.2(?![\d.])")


def preferred_lesson_rubric(title: str) -> str:
    """Pick ``assignments/lesson_<n>.json`` for an ``n.2`` coursework.

    The generic similarity matcher favors an exam-practice spec for titles
    such as "1.2 | הגשת משימות" (both mention 1 and 2), which imports the wrong
    questions. The x.2 assignment always belongs to lesson ``x``.
    """
    match = _LESSON_SUBMISSION_RE.match(title or "")
    if not match:
        return ""
    candidate = PROJECT_ROOT / "assignments" / f"lesson_{match.group(1)}.json"
    if not candidate.is_file():
        return ""
    return candidate.relative_to(PROJECT_ROOT).as_posix()


class SourceError(RuntimeError):
    """Google could not be read; the message is teacher-safe."""


@dataclass(frozen=True)
class CourseInfo:
    id: str
    name: str
    section: str | None = None


def _naive_utc(value: datetime | None) -> datetime | None:
    """Portal convention: naive UTC timestamps in the database."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def drive_file_from_material(material: dict) -> dict | None:
    """Normalize both attachment shapes Classroom returns.

    ``SharedDriveFile`` is nested as
    ``{"driveFile": {"driveFile": {...}, "shareMode": ...}}``; a plain
    ``DriveFile`` is ``{"driveFile": {"id": ...}}``. Reading only the flat
    shape (as the existing helper does) silently loses every attachment on
    this account.
    """
    node = material.get("driveFile")
    if not isinstance(node, dict):
        return None
    inner = node.get("driveFile")
    file = inner if isinstance(inner, dict) else node
    file_id = str(file.get("id") or "").strip()
    if not file_id:
        return None
    return {
        "id": file_id,
        "title": str(file.get("title") or "").strip(),
        "link": str(file.get("alternateLink") or "").strip(),
    }


def link_material(material: dict) -> Attachment | None:
    """A non-Drive attachment (link, form, video) kept as teacher metadata."""
    for key in ("link", "form", "youTubeVideo"):
        node = material.get(key)
        if isinstance(node, dict):
            title = str(node.get("title") or "").strip()
            url = str(node.get("url") or node.get("alternateLink") or "").strip()
            if title or url:
                return Attachment(
                    file_id="",
                    title=title or "קישור",
                    mime_type=None,
                    url=url,
                    kind=key,
                )
    return None


class ClassroomSource:
    """A thin, cached adapter over the existing read-only service."""

    def __init__(self, service: ClassroomService | None = None) -> None:
        self._service = service
        self._mime_cache: dict[str, str | None] = {}
        self._rubric_cache: dict[str, tuple[str, float, str]] = {}
        self._folder_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ setup
    @property
    def service(self) -> ClassroomService:
        if self._service is None:
            credentials = load_credentials(interactive=False)
            self._service = ClassroomService(credentials=credentials)
        return self._service

    def _mime_type(self, file_id: str) -> str | None:
        if file_id in self._mime_cache:
            return self._mime_cache[file_id]
        try:
            info = self.service.get_drive_file_info(file_id)
        except ClassroomServiceError:
            info = None
        mime = str((info or {}).get("mimeType") or "") or None
        self._mime_cache[file_id] = mime
        return mime

    def seed_mime_cache(self, known: dict[str, str]) -> None:
        """Reuse mime types from a previous scan instead of one API call each."""
        for file_id, mime_type in known.items():
            if file_id and mime_type:
                self._mime_cache.setdefault(file_id, mime_type)

    # ---------------------------------------------------------------- courses
    def list_courses(self) -> list[CourseInfo]:
        try:
            return [
                CourseInfo(id=course.id, name=course.name, section=course.section)
                for course in self.service.list_courses()
            ]
        except ClassroomServiceError as exc:
            raise SourceError(
                f"לא ניתן לקרוא את רשימת הקורסים מ-Google Classroom: {exc}"
            ) from exc

    def course_info(self, course_id: str) -> CourseInfo | None:
        return next(
            (course for course in self.list_courses() if course.id == course_id),
            None,
        )

    # ------------------------------------------------------------- coursework
    def load_course(
        self,
        course_id: str,
        *,
        include_drafts: bool = False,
    ) -> CourseSnapshot:
        states = ("PUBLISHED", "DRAFT") if include_drafts else ("PUBLISHED",)
        try:
            raw_items = self.service.list_course_work_items(
                course_id, states=states
            )
        except ClassroomServiceError as exc:
            raise SourceError(
                f"לא ניתן לקרוא את המטלות של הקורס: {exc}"
            ) from exc

        try:
            topics = self.service.list_topics(course_id)
        except ClassroomServiceError:
            # Topics are grouping sugar; losing them must not abort a preview.
            topics = {}

        info = self.course_info(course_id)
        coursework: list[CourseworkItem] = []
        for raw in raw_items:
            coursework.append(
                CourseworkItem(
                    id=str(raw.get("id") or ""),
                    title=str(raw.get("title") or "").strip(),
                    description_html=str(raw.get("description") or ""),
                    state=str(raw.get("state") or "PUBLISHED"),
                    work_type=str(raw.get("workType") or "ASSIGNMENT"),
                    topic_id=raw.get("topicId"),
                    topic_name=topics.get(str(raw.get("topicId") or "")),
                    due_at=_naive_utc(_classroom_due_instant(raw)[1]),
                    max_points=raw.get("maxPoints"),
                    source_version=str(
                        raw.get("updateTime")
                        or raw.get("creationTime")
                        or raw.get("id")
                        or ""
                    ),
                    attachments=self._normalize_materials(
                        raw.get("materials") or []
                    ),
                )
            )

        # Classroom stores the "Materials" tab separately from Coursework.
        # Its items (worksheets, links, videos) are lesson material, so they
        # join the same snapshot with a distinct work type and a prefixed id.
        for raw in self.list_course_materials(course_id):
            coursework.append(
                CourseworkItem(
                    id=f"material:{raw.get('id') or ''}",
                    title=str(raw.get("title") or "").strip(),
                    description_html=str(raw.get("description") or ""),
                    state=str(raw.get("state") or "PUBLISHED"),
                    work_type="COURSE_MATERIAL",
                    topic_id=raw.get("topicId"),
                    topic_name=topics.get(str(raw.get("topicId") or "")),
                    due_at=None,
                    max_points=None,
                    source_version=str(
                        raw.get("updateTime")
                        or raw.get("creationTime")
                        or raw.get("id")
                        or ""
                    ),
                    attachments=self._normalize_materials(
                        raw.get("materials") or []
                    ),
                )
            )

        return CourseSnapshot(
            course_id=course_id,
            course_name=info.name if info else course_id,
            section=info.section if info else None,
            topics=topics,
            coursework=tuple(coursework),
        )

    def list_course_materials(self, course_id: str) -> list[dict]:
        """Every item on the course's **Materials** tab, all pages.

        This is a different Classroom resource from coursework and needs the
        ``classroom.courseworkmaterials.readonly`` scope; the grading engine's
        coursework scopes do not cover it. Google returns an explicit 403 when
        the grant is missing, so the caller can tell the teacher to re-run the
        one-time consent.

        The API returns only ``PUBLISHED`` items unless asked otherwise. Most
        of this teacher's worksheets and summaries are drafts that are visible
        in Classroom itself, so both states are requested - exactly like the
        coursework listing.
        """
        materials: list[dict] = []
        page_token = ""
        while True:
            request = (
                self.service.classroom.courses()
                .courseWorkMaterials()
                .list(
                    courseId=course_id,
                    pageSize=100,
                    courseWorkMaterialStates=["PUBLISHED", "DRAFT"],
                    **({"pageToken": page_token} if page_token else {}),
                )
            )
            try:
                response = self.service._execute(  # noqa: SLF001
                    request, "list course materials"
                )
            except ClassroomServiceError as exc:
                raise SourceError(
                    "לא ניתן לקרוא את לשונית החומרים של הקורס. הריצו פעם אחת "
                    "python -m portal.scripts.classroom_import auth "
                    "כדי להוסיף את הרשאת החומרים, ואז סרקו שוב. "
                    f"({exc})"
                ) from exc
            materials.extend(
                item
                for item in response.get("courseWorkMaterial") or []
                if isinstance(item, dict)
            )
            page_token = str(response.get("nextPageToken") or "")
            if not page_token:
                return materials

    def _normalize_materials(self, materials: list[dict]) -> tuple[Attachment, ...]:
        seen: set[str] = set()
        attachments: list[Attachment] = []
        for material in materials or []:
            if not isinstance(material, dict):
                continue
            drive = drive_file_from_material(material)
            if drive is not None:
                file_id = drive["id"]
                if file_id in seen:
                    continue
                seen.add(file_id)
                attachments.append(
                    Attachment(
                        file_id=file_id,
                        title=drive["title"] or "קובץ",
                        mime_type=self._mime_type(file_id),
                        url=drive["link"],
                        kind="drive",
                    )
                )
                continue
            link = link_material(material)
            if link is not None:
                attachments.append(link)
        return tuple(attachments)

    # ------------------------------------------------------------------- docs
    def doc_html(self, file_id: str) -> DocPayload:
        """Export a Google Doc as HTML, falling back to plain text."""
        try:
            raw = self.service._execute(  # noqa: SLF001 - read-only boundary
                self.service.drive.files().export(
                    fileId=file_id, mimeType="text/html"
                ),
                f"export document '{file_id}'",
            )
        except ClassroomServiceError as exc:
            logger.warning("HTML export failed for a document: %s", exc)
            raw = None

        if raw:
            if isinstance(raw, bytes):
                html = raw.decode("utf-8", "replace")
            else:
                html = str(raw)
            return DocPayload(html=html, text="")

        try:
            text = self.service.extract_doc_text(file_id)
        except ClassroomServiceError as exc:
            raise SourceError(f"לא ניתן לקרוא את המסמך: {exc}") from exc
        return DocPayload(html="", text=text)

    # ------------------------------------------------------------------ decks
    def deck_pdf(self, file_id: str) -> DeckPayload:
        try:
            raw = self.service._execute(  # noqa: SLF001 - read-only boundary
                self.service.drive.files().export(
                    fileId=file_id, mimeType="application/pdf"
                ),
                f"export presentation '{file_id}'",
            )
        except ClassroomServiceError as exc:
            raise SourceError(
                "לא ניתן לייצא את המצגת ל-PDF. ודאו שהקובץ משותף "
                f"לחשבון המחובר. ({exc})"
            ) from exc
        if isinstance(raw, str):
            payload = raw.encode("latin-1", "replace")
        else:
            payload = bytes(raw or b"")
        if not payload:
            raise SourceError("הייצוא של המצגת חזר ריק.")
        return DeckPayload(pdf=payload)

    # -------------------------------------------------------------- raw files
    def download(self, file_id: str) -> bytes:
        try:
            raw = self.service._execute(  # noqa: SLF001 - read-only boundary
                self.service.drive.files().get_media(fileId=file_id),
                f"download file '{file_id}'",
            )
        except ClassroomServiceError as exc:
            raise SourceError(f"לא ניתן להוריד את הקובץ: {exc}") from exc
        data = raw.encode("latin-1", "replace") if isinstance(raw, str) else bytes(raw or b"")
        if not data:
            raise SourceError("הקובץ שהורד ריק.")
        if len(data) > _MAX_FILE_BYTES:
            raise SourceError("הקובץ גדול מדי לייבוא.")
        return data

    def fetch_image(self, url: str) -> bytes:
        """Fetch a Docs image server-side with the same OAuth grant."""
        if not url.lower().startswith(("https://", "http://")):
            raise SourceError("כתובת התמונה אינה חוקית.")
        from google.auth.transport.requests import AuthorizedSession

        session = AuthorizedSession(self.service.credentials)
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - requests raises several types
            raise SourceError(f"לא ניתן להוריד תמונה מהמסמך: {exc}") from exc
        finally:
            session.close()
        data = response.content or b""
        if len(data) > _MAX_FILE_BYTES:
            raise SourceError("התמונה גדולה מדי.")
        return data

    # ------------------------------------------------------------ drive decks
    def _folder_name(self, folder_id: str) -> str:
        """Parent folder name, cached; Drive names contain bidi control marks."""
        if not folder_id:
            return ""
        if folder_id not in self._folder_cache:
            try:
                response = self.service._execute(  # noqa: SLF001
                    self.service.drive.files().get(
                        fileId=folder_id,
                        fields="id,name",
                        supportsAllDrives=True,
                    ),
                    "inspect Drive folder",
                )
                name = str(response.get("name") or "")
            except ClassroomServiceError:
                name = ""
            self._folder_cache[folder_id] = strip_control_chars(name)
        return self._folder_cache[folder_id]

    def _list_files(self, query: str, action: str) -> list[dict]:
        """Walk ``nextPageToken`` until a Drive query is exhausted.

        A single ``files.list`` response can be split into pages; stopping
        after the first one silently hides materials from the picker.
        """
        files: list[dict] = []
        page_token = ""
        while True:
            request = self.service.drive.files().list(
                q=query,
                pageSize=1000,
                orderBy="name_natural",
                fields=(
                    "nextPageToken,"
                    "files(id,name,mimeType,modifiedTime,parents)"
                ),
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                **({"pageToken": page_token} if page_token else {}),
            )
            try:
                response = self.service._execute(  # noqa: SLF001
                    request, action
                )
            except ClassroomServiceError as exc:
                raise SourceError(f"{action}: {exc}") from exc
            files.extend(
                item
                for item in response.get("files", [])
                if isinstance(item, dict)
            )
            page_token = str(response.get("nextPageToken") or "")
            if not page_token:
                return files

    def list_drive_decks(self, folder_id: str = "") -> list[DriveItem]:
        """Every presentation in Drive (or in one folder), all pages."""
        query = [
            "trashed = false",
            f"mimeType = '{GOOGLE_SLIDES_MIME}'",
        ]
        if folder_id:
            escaped = folder_id.replace("'", "\\'")
            query.append(f"'{escaped}' in parents")
        files = self._list_files(
            " and ".join(query), "לא ניתן לקרוא את תיקיית המצגות"
        )
        decks: list[DriveItem] = []
        for raw in files:
            if not raw.get("id"):
                continue
            parents = [
                str(parent) for parent in raw.get("parents") or [] if parent
            ]
            parent_id = parents[0] if parents else folder_id
            folder_name = self._folder_name(parent_id)
            # When scanning the whole Drive, a folder named "3.2 | הגשת
            # משימות" is an assignment/submission folder: the slides inside it
            # are students' uploads, not teaching decks.
            if (
                not folder_id
                and folder_name
                and leading_number(folder_name) is not None
            ):
                continue
            decks.append(
                DriveItem(
                    file_id=str(raw.get("id") or ""),
                    title=str(raw.get("name") or "").strip() or "מצגת",
                    mime_type=str(raw.get("mimeType") or GOOGLE_SLIDES_MIME),
                    modified_time=str(raw.get("modifiedTime") or ""),
                    folder_id=parent_id,
                    folder_name=folder_name,
                )
            )
        return decks

    def list_drive_docs(self, folder_ids: Iterable[str]) -> list[DriveItem]:
        """Lesson documents living in the teacher's material folders.

        Only folders that already hold teaching decks are inspected, and only
        docs whose cleaned title starts with a lesson number ("4.5 | ...") are
        kept: that excludes student hand-ins ("Dana Yarom - 3.2 ...") and
        untitled scratch documents. Drive's "[Template]" marker sits in front
        of many worksheets, so the title is normalized before the number is
        read ("[Template] 11.3 | ..." is lesson 11.3).
        """
        docs: list[DriveItem] = []
        seen: set[str] = set()
        for folder_id in dict.fromkeys(
            value for value in folder_ids if value
        ):
            escaped = folder_id.replace("'", "\\'")
            query = " and ".join(
                (
                    "trashed = false",
                    f"mimeType = '{GOOGLE_DOC_MIME}'",
                    f"'{escaped}' in parents",
                )
            )
            files = self._list_files(
                query, "לא ניתן לקרוא את מסמכי ההוראה"
            )
            folder_name = self._folder_name(folder_id)
            for raw in files:
                file_id = str(raw.get("id") or "")
                if not file_id or file_id in seen:
                    continue
                title = clean_title(
                    strip_control_chars(str(raw.get("name") or ""))
                )
                if leading_number(title) is None:
                    continue
                seen.add(file_id)
                docs.append(
                    DriveItem(
                        file_id=file_id,
                        title=title or "מסמך",
                        mime_type=str(
                            raw.get("mimeType") or GOOGLE_DOC_MIME
                        ),
                        modified_time=str(raw.get("modifiedTime") or ""),
                        folder_id=folder_id,
                        folder_name=folder_name,
                    )
                )
        return docs

    def deck_info(self, file_id: str) -> DriveItem:
        try:
            response = self.service._execute(  # noqa: SLF001
                self.service.drive.files().get(
                    fileId=file_id,
                    fields="id,name,mimeType,modifiedTime,parents",
                    supportsAllDrives=True,
                ),
                "inspect Drive presentation",
            )
        except ClassroomServiceError as exc:
            raise SourceError(f"לא ניתן לקרוא את המצגת: {exc}") from exc
        parents = [str(parent) for parent in response.get("parents") or [] if parent]
        parent_id = parents[0] if parents else ""
        return DriveItem(
            file_id=str(response.get("id") or file_id),
            title=str(response.get("name") or "מצגת").strip(),
            mime_type=str(response.get("mimeType") or GOOGLE_SLIDES_MIME),
            modified_time=str(response.get("modifiedTime") or ""),
            folder_id=parent_id,
            folder_name=self._folder_name(parent_id),
        )

    # ---------------------------------------------------------------- rubrics
    def suggest_rubric(self, title: str) -> tuple[str, float, str]:
        """Suggest one rubric file under ``assignments/`` or ``configs/``.

        The suggestion is advisory: the teacher confirms it in the preview,
        and a low score is displayed instead of being silently accepted.
        Repeated titles during one scan reuse the first result.
        """
        if title in self._rubric_cache:
            return self._rubric_cache[title]
        preferred = preferred_lesson_rubric(title)
        if preferred:
            result = (preferred, 1.0, "lesson number match")
            self._rubric_cache[title] = result
            return result
        from src.config_match import list_config_files, suggest_config_for_assignment

        roots = [root.resolve() for root in RUBRIC_ROOTS]
        candidates = []
        for path in list_config_files():
            resolved = path.resolve()
            if any(resolved == root or root in resolved.parents for root in roots):
                candidates.append(path)
        if not candidates:
            return "", 0.0, ""
        best, score, reason = suggest_config_for_assignment(title, candidates)
        if best is None:
            result = ("", 0.0, "")
        else:
            try:
                relative = best.resolve().relative_to(PROJECT_ROOT).as_posix()
            except ValueError:
                relative = ""
            result = (
                (relative, float(score), reason)
                if relative
                else ("", 0.0, "")
            )
        self._rubric_cache[title] = result
        return result
