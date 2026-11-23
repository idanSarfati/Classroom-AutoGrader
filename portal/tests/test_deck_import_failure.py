"""A deck that cannot be exported must fail the item, not fake a lesson.

Regression for the 4.1 import: the Google token expired, the deck export
failed, and the importer silently fell back to a single title-only slide while
reporting "imported". The teacher saw a lesson with one empty slide and no
error anywhere. These tests pin the new behaviour: the item fails with the
real Hebrew reason on the job screen, no lesson row is left behind, and a
later successful retry imports every slide.
"""
from __future__ import annotations

import json
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import func, select

from portal.ingestion import importer
from portal.ingestion.classroom_source import SourceError
from portal.ingestion.google_auth import IngestionAuthError
from portal.ingestion.storage import ContentStore
from portal.ingestion.types import (
    Attachment,
    CourseSnapshot,
    CourseworkItem,
    DeckPayload,
    ImportManifest,
)
from portal.models import (
    ClassGroup,
    ImportItem,
    ImportJob,
    Lesson,
    LessonSlide,
    ROLE_TEACHER,
    SlideContent,
)
from portal.tests.helpers import create_user


def _pdf(pages: int = 1) -> bytes:
    images = [Image.new("RGB", (320, 180), "white") for _ in range(pages)]
    buffer = BytesIO()
    images[0].save(
        buffer, format="PDF", save_all=True, append_images=images[1:]
    )
    return buffer.getvalue()


def _snapshot(version: str = "v1") -> CourseSnapshot:
    deck = CourseworkItem(
        id="cw-41",
        title="4.1 | למידה תכל'ס - פעולה חלק א",
        state="PUBLISHED",
        source_version=version,
        attachments=(
            Attachment(
                file_id="deck-41",
                title="4.1 | פייתון א | פעולה חלק א",
                mime_type="application/vnd.google-apps.presentation",
            ),
        ),
    )
    return CourseSnapshot(
        course_id="course-1", course_name="פייתון א' - ט3", coursework=(deck,)
    )


class DeckSource:
    """Minimal source: one deck whose export either works or raises."""

    def __init__(self, *, pdf: bytes | None = None, error: Exception | None = None):
        self.snapshot = _snapshot()
        self._pdf = pdf if pdf is not None else _pdf(2)
        self._error = error
        self.calls = 0

    def load_course(self, course_id, *, include_drafts=False):
        return self.snapshot

    def suggest_rubric(self, title):
        return "", 0.0, ""

    def deck_pdf(self, file_id):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return DeckPayload(pdf=self._pdf)


@pytest.fixture
def store(app, tmp_path):
    value = ContentStore(tmp_path / "content")
    app.state.content_store = value
    return value


def _setup(app):
    teacher_id = create_user(
        app, "mor41", "TeacherPass1", role=ROLE_TEACHER, display_name="מורה"
    )
    with app.state.session_factory() as db:
        group = ClassGroup(name="ט'3", code="TET3AA", teacher_id=teacher_id)
        db.add(group)
        db.commit()
        return teacher_id, group.id


def _queued_job(app, teacher_id: int, class_id: int, source) -> ImportJob:
    with app.state.session_factory() as db:
        group = db.get(ClassGroup, class_id)
        return importer.build_selection_preview(
            db,
            source,
            teacher_id=teacher_id,
            group=group,
            snapshot=source.snapshot,
            coursework_ids=["cw-41"],
            actions={"coursework:cw-41": "lesson"},
        )


def _apply(app, store, source, job):
    with app.state.session_factory() as db:
        job = db.get(ImportJob, job.id)
        counts = importer.apply_job(db, store, source, job)
        db.refresh(job)
        item = db.scalar(select(ImportItem).where(ImportItem.job_id == job.id))
        return counts, job.state, item.status, item.message


def test_failed_deck_export_fails_the_item_and_creates_no_lesson(app, store):
    teacher_id, class_id = _setup(app)
    source = DeckSource(error=SourceError("לא ניתן לייצא את המצגת ל-PDF."))
    job = _queued_job(app, teacher_id, class_id, source)

    counts, state, status, message = _apply(app, store, source, job)

    assert counts["failed"] == 1 and counts["imported"] == 0
    assert state == "failed"
    assert status == "failed"
    assert "לא ניתן לייבא את המצגת" in message
    assert "לא ניתן לייצא את המצגת ל-PDF." in message
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Lesson.id))) == 0
        assert db.scalar(select(func.count(LessonSlide.id))) == 0


def test_expired_google_token_reason_reaches_the_job_screen(app, store):
    teacher_id, class_id = _setup(app)
    source = DeckSource(
        error=IngestionAuthError(
            "פג תוקף אסימון Google ולא ניתן לחדש אותו. הריצו מחדש את "
            "תהליך ההרשאה: python -m portal.scripts.classroom_import auth."
        )
    )
    job = _queued_job(app, teacher_id, class_id, source)

    counts, state, status, message = _apply(app, store, source, job)

    assert counts["failed"] == 1 and state == "failed"
    assert "פג תוקף אסימון Google" in message
    assert "classroom_import auth" in message
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Lesson.id))) == 0


def test_unexpected_deck_error_never_leaks_raw_detail(app, store):
    teacher_id, class_id = _setup(app)
    secret = "https://provider.example/v1?key=SUPERSECRET"
    source = DeckSource(error=RuntimeError(secret))
    job = _queued_job(app, teacher_id, class_id, source)

    _, _, status, message = _apply(app, store, source, job)

    assert status == "failed"
    assert "SUPERSECRET" not in message and "https" not in message
    assert "RuntimeError" in message


def test_corrupt_pdf_is_reported_as_a_render_failure(app, store):
    teacher_id, class_id = _setup(app)
    source = DeckSource(pdf=b"this is not a pdf")
    job = _queued_job(app, teacher_id, class_id, source)

    _, state, status, message = _apply(app, store, source, job)

    assert state == "failed" and status == "failed"
    assert "עיבוד קובץ המצגת נכשל" in message
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Lesson.id))) == 0


def test_a_working_deck_still_imports_every_slide(app, store):
    teacher_id, class_id = _setup(app)
    source = DeckSource(pdf=_pdf(3))
    job = _queued_job(app, teacher_id, class_id, source)

    counts, state, status, _ = _apply(app, store, source, job)

    assert counts["imported"] == 1 and state == "completed"
    with app.state.session_factory() as db:
        lesson = db.scalar(select(Lesson))
        assert lesson is not None and lesson.published is False
        slides = db.scalars(
            select(LessonSlide).where(LessonSlide.lesson_id == lesson.id)
        ).all()
        assert len(slides) == 3
        for slide in slides:
            content = db.get(SlideContent, slide.id)
            assert content is not None and content.image_blob_sha


def test_retry_after_fixing_the_cause_imports_the_deck(app, store):
    teacher_id, class_id = _setup(app)
    broken = DeckSource(error=IngestionAuthError("פג תוקף אסימון Google."))
    job = _queued_job(app, teacher_id, class_id, broken)
    counts, state, _, _ = _apply(app, store, broken, job)
    assert counts["failed"] == 1 and state == "failed"

    # The teacher re-authorizes; the same job is retried with a working source.
    fixed = DeckSource(pdf=_pdf(2))
    with app.state.session_factory() as db:
        row = db.get(ImportJob, job.id)
        importer.reset_job_for_retry(db, row)
    counts, state, status, _ = _apply(app, store, fixed, job)
    assert counts["imported"] == 1 and state == "completed"
    with app.state.session_factory() as db:
        lesson = db.scalar(select(Lesson))
        assert lesson is not None
        assert db.scalar(
            select(func.count(LessonSlide.id)).where(
                LessonSlide.lesson_id == lesson.id
            )
        ) == 2


def test_failed_update_keeps_the_existing_slides(app, store):
    teacher_id, class_id = _setup(app)
    working = DeckSource(pdf=_pdf(2))
    job = _queued_job(app, teacher_id, class_id, working)
    counts, _, _, _ = _apply(app, store, working, job)
    assert counts["imported"] == 1
    with app.state.session_factory() as db:
        lesson = db.scalar(select(Lesson))
        lesson_id = lesson.id
        original_title = lesson.title

    # The source changed, but the token died before the update could export.
    broken = DeckSource(error=IngestionAuthError("פג תוקף אסימון Google."))
    broken.snapshot = _snapshot(version="v2")
    with app.state.session_factory() as db:
        row = db.get(ImportJob, job.id)
        manifest = ImportManifest.from_json(json.loads(row.manifest_json))
        manifest.items[0].source_version = "v2"
        row.manifest_json = json.dumps(manifest.as_json(), ensure_ascii=False)
        importer.reset_job_for_retry(db, row)
    counts, state, status, message = _apply(app, store, broken, job)

    assert counts["failed"] == 1 and state == "failed"
    assert "פג תוקף אסימון Google" in message
    with app.state.session_factory() as db:
        lesson = db.get(Lesson, lesson_id)
        assert lesson is not None and lesson.title == original_title
        assert db.scalar(
            select(func.count(LessonSlide.id)).where(
                LessonSlide.lesson_id == lesson_id
            )
        ) == 2


def test_text_only_material_still_gets_a_readable_draft(app, store):
    teacher_id, class_id = _setup(app)

    class TextSource(DeckSource):
        def __init__(self):
            super().__init__()
            reading = CourseworkItem(
                id="cw-read",
                title="דף קריאה",
                state="PUBLISHED",
                source_version="read-v1",
                description_html="<p>טקסט לקריאה</p>",
            )
            self.snapshot = CourseSnapshot(
                course_id="course-1",
                course_name="פייתון א' - ט3",
                coursework=(reading,),
            )

    source = TextSource()
    with app.state.session_factory() as db:
        group = db.get(ClassGroup, class_id)
        job = importer.build_selection_preview(
            db,
            source,
            teacher_id=teacher_id,
            group=group,
            snapshot=source.snapshot,
            coursework_ids=["cw-read"],
            actions={"coursework:cw-read": "lesson"},
        )
    counts, state, _, _ = _apply(app, store, source, job)
    assert counts["imported"] == 1 and state == "completed"
    assert source.calls == 0  # no deck was requested, none was exported
    with app.state.session_factory() as db:
        lesson = db.scalar(select(Lesson))
        assert lesson is not None
        assert db.scalar(
            select(func.count(LessonSlide.id)).where(
                LessonSlide.lesson_id == lesson.id
            )
        ) == 1