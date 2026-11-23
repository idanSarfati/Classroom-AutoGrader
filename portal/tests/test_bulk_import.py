"""The bulk import script: drafts into every linked class, nothing else.

The teacher asked to import all Classroom material into all classes now (as
drafts) and publish manually each week, so the weekly workflow needs no
Google token. These tests pin the script's contract: only linked classes are
touched, everything stays unpublished, a second run adds nothing, and
``--preview-only`` / ``--class-id`` behave as documented.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from portal.ingestion.types import (
    Attachment,
    CourseSnapshot,
    CourseworkItem,
    DeckPayload,
)
from portal.models import (
    ClassGroup,
    ClassroomLink,
    ImportJob,
    Lesson,
    LessonSlide,
    ROLE_TEACHER,
)
from portal.scripts import bulk_import
from portal.tests.helpers import create_user


def _pdf(pages: int = 2) -> bytes:
    images = [Image.new("RGB", (320, 180), "white") for _ in range(pages)]
    buffer = BytesIO()
    images[0].save(
        buffer, format="PDF", save_all=True, append_images=images[1:]
    )
    return buffer.getvalue()


class FakeSource:
    """One deck item per course; no network, no rubric suggestions."""

    def load_course(self, course_id, *, include_drafts=False):
        item = CourseworkItem(
            id=f"cw-{course_id}",
            title="4.1 | למידה תכל'ס - פעולה חלק א",
            state="PUBLISHED",
            source_version="v1",
            attachments=(
                Attachment(
                    file_id=f"deck-{course_id}",
                    title="4.1 | מצגת",
                    mime_type="application/vnd.google-apps.presentation",
                ),
            ),
        )
        return CourseSnapshot(
            course_id=course_id, course_name="פייתון א'", coursework=(item,)
        )

    def suggest_rubric(self, title):
        return "", 0.0, ""

    def deck_pdf(self, file_id):
        return DeckPayload(pdf=_pdf())


@pytest.fixture
def wired(app, portal_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(bulk_import, "load_settings", lambda: portal_settings)
    monkeypatch.setattr(bulk_import, "ClassroomSource", FakeSource)
    monkeypatch.setattr(bulk_import, "CONTENT_DIR", tmp_path / "content")
    teacher_id = create_user(
        app, "morbulk", "TeacherPass1", role=ROLE_TEACHER, display_name="מורה"
    )
    with app.state.session_factory() as db:
        linked = ClassGroup(name="ט'3", code="TET3BB", teacher_id=teacher_id)
        unlinked = ClassGroup(name="ח'6", code="HET6BB", teacher_id=teacher_id)
        db.add_all([linked, unlinked])
        db.commit()
        db.add(
            ClassroomLink(
                teacher_id=teacher_id,
                class_id=linked.id,
                course_id="course-1",
            )
        )
        db.commit()
        return teacher_id, linked.id, unlinked.id


def _lessons(app, class_id: int):
    with app.state.session_factory() as db:
        return list(
            db.scalars(select(Lesson).where(Lesson.class_id == class_id))
        )


def test_bulk_import_creates_drafts_only_in_linked_classes(app, wired, capsys):
    _teacher_id, linked_id, unlinked_id = wired
    assert bulk_import.main([]) == 0
    lessons = _lessons(app, linked_id)
    assert len(lessons) == 1
    assert lessons[0].published is False
    with app.state.session_factory() as db:
        slides = list(
            db.scalars(
                select(LessonSlide).where(LessonSlide.lesson_id == lessons[0].id)
            )
        )
    assert len(slides) == 2
    assert _lessons(app, unlinked_id) == []
    assert "no Classroom link" in capsys.readouterr().out


def test_second_run_adds_nothing(app, wired):
    _teacher_id, linked_id, _unlinked_id = wired
    assert bulk_import.main([]) == 0
    assert bulk_import.main([]) == 0
    assert len(_lessons(app, linked_id)) == 1


def test_preview_only_creates_no_content(app, wired):
    _teacher_id, linked_id, _unlinked_id = wired
    assert bulk_import.main(["--preview-only"]) == 0
    assert _lessons(app, linked_id) == []
    with app.state.session_factory() as db:
        jobs = list(db.scalars(select(ImportJob)))
    assert len(jobs) == 1
    assert jobs[0].state == "preview"


def test_class_id_filter_limits_the_run(app, wired):
    _teacher_id, linked_id, unlinked_id = wired
    assert bulk_import.main(["--class-id", str(unlinked_id)]) == 0
    assert _lessons(app, linked_id) == []
    with app.state.session_factory() as db:
        assert db.scalar(select(ImportJob)) is None