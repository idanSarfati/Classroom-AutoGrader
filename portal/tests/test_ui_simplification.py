"""UI simplification: the account-wide course-content tree and the removed
teacher-screen clutter.

These tests never touch Google. A fake catalog (the same JSON the real scanner
stores) seeds one course with numbered topics - including an empty topic and a
draft-only topic - plus Drive decks/docs and portal provenance, so the tree is
checked end to end through the real ``/teacher/lessons`` route. The removal
tests walk the actual dashboard, class, picker and grading screens.
"""
from __future__ import annotations

import html
from datetime import datetime

from sqlalchemy import select

from portal.ingestion import catalog
from portal.ingestion.classroom_source import CourseInfo
from portal.ingestion.normalize import is_results_sheet
from portal.ingestion.types import (
    Attachment,
    CourseSnapshot,
    CourseworkItem,
    DriveItem,
)
from portal.models import (
    Assignment,
    AssignmentContent,
    ClassGroup,
    ClassroomCatalog,
    ClassroomLink,
    Lesson,
    LessonSource,
)
from portal.tests.helpers import create_user, login

TEACHER_PASSWORD = "TeacherPass1!"
COURSE_ID = "c-z3"
COURSE_NAME = "פייתון א' - ז3"

PRESENTATION = "application/vnd.google-apps.presentation"
DOCUMENT = "application/vnd.google-apps.document"


# ---------------------------------------------------------------------------
# Fixture helpers (no Google; the catalog JSON is written straight to the DB)
# ---------------------------------------------------------------------------


def _coursework(
    item_id: str,
    title: str,
    state: str,
    topic: str,
    *,
    work_type: str = "ASSIGNMENT",
    attachment: Attachment | None = None,
) -> CourseworkItem:
    return CourseworkItem(
        id=item_id,
        title=title,
        state=state,
        work_type=work_type,
        topic_name=topic,
        attachments=(attachment,) if attachment is not None else (),
    )


def _rich_catalog():
    """One course, four topics (one empty, one draft-only), a duplicate clone,
    a material whose deck is also in Drive, a standalone lecture and worksheet,
    and a personal results sheet that must never surface."""
    topics = {
        "t1": "1 | מבוא",
        "t2": "2 | משתנים",
        "t3": "3 | פקודת קלט",
        "t4": "4 | לולאות",  # empty: no item points here
    }
    coursework = (
        _coursework("cw-12", "1.2 | הגשת משימות", "PUBLISHED", "1 | מבוא"),
        # A clone of the same assignment: the tree must show it once.
        _coursework("cw-12b", "1.2 | הגשת משימות", "PUBLISHED", "1 | מבוא"),
        _coursework("cw-22", "2.2 | הגשת משימות", "DRAFT", "2 | משתנים"),
        _coursework("cw-32", "3.2 | הגשת משימות", "DRAFT", "3 | פקודת קלט"),
        _coursework(
            "cw-11",
            "1.1 | מצגת השיעור",
            "PUBLISHED",
            "1 | מבוא",
            work_type="COURSE_MATERIAL",
            attachment=Attachment(
                file_id="deck-11",
                title="1.1 מצגת השיעור",
                mime_type=PRESENTATION,
            ),
        ),
    )
    snapshot = CourseSnapshot(
        course_id=COURSE_ID,
        course_name=COURSE_NAME,
        topics=topics,
        coursework=coursework,
    )
    courses = [CourseInfo(id=COURSE_ID, name=COURSE_NAME)]
    decks = [
        # Attached to coursework above -> shown once as the material, not again.
        DriveItem(file_id="deck-11", title="1.1 מצגת השיעור", mime_type=PRESENTATION),
        # A standalone lecture -> its own "הרצאה" row under topic 2.
        DriveItem(file_id="deck-21", title="2.1 | הרצאה עצמאית", mime_type=PRESENTATION),
    ]
    docs = [
        DriveItem(file_id="doc-23", title="2.3 | דף עבודה", mime_type=DOCUMENT),
        DriveItem(
            file_id="doc-results",
            title="אופק פרץ - תוצאות בוחן רבעוני",
            mime_type=DOCUMENT,
        ),
    ]
    return courses, {COURSE_ID: snapshot}, decks, docs


def _write_catalog(app, teacher_id, courses, snapshots, decks=(), docs=()) -> None:
    payload = catalog.serialize(courses, snapshots, {}, decks, docs)
    with app.state.session_factory() as db:
        row = db.scalar(
            select(ClassroomCatalog).where(
                ClassroomCatalog.teacher_id == teacher_id
            )
        )
        if row is None:
            row = ClassroomCatalog(teacher_id=teacher_id)
            db.add(row)
        row.status = "ready"
        row.error = ""
        row.catalog_json = payload
        row.new_ids_json = "[]"
        row.synced_at = datetime(2026, 11, 8, 15, 48)
        db.commit()


def _seed_teacher(app, username: str = "mori") -> int:
    return create_user(
        app, username, TEACHER_PASSWORD, display_name="מורי כהן"
    )


def _seed_class(app, teacher_id, name, code, course_id=COURSE_ID) -> int:
    with app.state.session_factory() as db:
        group = ClassGroup(name=name, code=code, teacher_id=teacher_id)
        db.add(group)
        db.flush()
        db.add(
            ClassroomLink(
                teacher_id=teacher_id,
                class_id=group.id,
                course_id=course_id,
                course_name=COURSE_NAME,
            )
        )
        db.commit()
        return group.id


def _import_assignment(app, class_id, source_id, title, *, published, topic="") -> int:
    with app.state.session_factory() as db:
        assignment = Assignment(
            class_id=class_id,
            title=title,
            instructions="",
            rubric_path="rubrics/test.xlsx",
            rubric_sha256="b" * 64,
            published=published,
        )
        db.add(assignment)
        db.flush()
        db.add(
            AssignmentContent(
                assignment_id=assignment.id,
                body_html="<p>מטלה</p>",
                body_text="מטלה",
                topic_name=topic,
                source_kind="coursework",
                source_id=source_id,
                original_title=title,
                content_hash="c" * 64,
            )
        )
        db.commit()
        return assignment.id


def _import_lesson(app, teacher_id, class_id, source_id, title, *, published) -> int:
    with app.state.session_factory() as db:
        lesson = Lesson(
            teacher_id=teacher_id,
            class_id=class_id,
            title=title,
            summary="",
            published=published,
            position=1,
        )
        db.add(lesson)
        db.flush()
        db.add(
            LessonSource(
                lesson_id=lesson.id,
                source_kind="coursework",
                source_id=source_id,
                original_title=title,
                content_hash="a" * 64,
            )
        )
        db.commit()
        return lesson.id


def _manual_lesson(app, teacher_id, class_id, title, *, published) -> int:
    with app.state.session_factory() as db:
        lesson = Lesson(
            teacher_id=teacher_id,
            class_id=class_id,
            title=title,
            summary="",
            published=published,
            position=99,
        )
        db.add(lesson)
        db.commit()
        return lesson.id


def _seed_rich_world(app):
    """Teacher + one linked class + the rich catalog + portal provenance."""
    teacher_id = _seed_teacher(app)
    class_id = _seed_class(app, teacher_id, "ז'3", "AAA111")
    courses, snapshots, decks, docs = _rich_catalog()
    _write_catalog(app, teacher_id, courses, snapshots, decks, docs)
    _import_assignment(
        app, class_id, "cw-12", "1.2 | הגשת משימות", published=True, topic="1 | מבוא"
    )
    _import_assignment(
        app, class_id, "cw-22", "2.2 | הגשת משימות", published=False, topic="2 | משתנים"
    )
    _import_lesson(
        app, teacher_id, class_id, "cw-11", "1.1 | מצגת השיעור", published=True
    )
    _manual_lesson(app, teacher_id, class_id, "שיעור ידני", published=False)
    return teacher_id, class_id


def _lessons_text(client) -> str:
    page = client.get("/teacher/lessons")
    assert page.status_code == 200, page.text[:300]
    return html.unescape(page.text)


# ---------------------------------------------------------------------------
# Course-content tree
# ---------------------------------------------------------------------------


def test_tree_lists_every_topic_including_empty_and_draft(app, client) -> None:
    _seed_rich_world(app)
    login(client, "mori", TEACHER_PASSWORD)
    text = _lessons_text(client)

    assert "השיעורים שלי" in text
    assert COURSE_NAME in text
    # All four defined topics appear, even the empty one and the draft-only one.
    for topic in ("1 | מבוא", "2 | משתנים", "3 | פקודת קלט", "4 | לולאות"):
        assert topic in text, topic
    # The empty topic is shown with a placeholder, not dropped.
    assert "אין פריטים בנושא זה עדיין" in text
    # The toolbar (filter + open/close all) is wired into the page.
    assert "data-picker-filter" in text
    assert "data-course-open-all" in text
    assert "data-course-close-all" in text


def test_tree_dedupes_and_shows_portal_state(app, client) -> None:
    _seed_rich_world(app)
    login(client, "mori", TEACHER_PASSWORD)
    text = _lessons_text(client)

    # One row per item even though the course holds a clone of 1.2.
    assert text.count("1.2 | הגשת משימות") == 1
    # Portal state badges: published, draft-in-portal, and not-yet-imported.
    assert "מפורסם" in text
    assert "טיוטה בפורטל" in text
    assert "טרם יובא" in text
    # Classroom draft state is surfaced too.
    assert "טיוטה בקורס" in text
    # Kind labels for the standalone lecture and worksheet.
    assert "הרצאה" in text
    assert "מסמך" in text
    assert "2.1 | הרצאה עצמאית" in text
    assert "2.3 | דף עבודה" in text
    # A manual portal lesson is not lost.
    assert "שיעור ידני" in text


def test_tree_hides_attached_drive_copy_and_results_sheet(app, client) -> None:
    _seed_rich_world(app)
    login(client, "mori", TEACHER_PASSWORD)
    text = _lessons_text(client)

    # Personal quarterly-exam results are grade data, never course content.
    assert "תוצאות בוחן רבעוני" not in text
    assert "אופק פרץ" not in text
    # The deck attached to coursework is shown once as the material; its own
    # Drive title (no pipe) must not appear as a separate lecture row.
    assert "1.1 מצגת השיעור" not in text
    assert "1.1 | מצגת השיעור" in text
    # Exactly one standalone lecture remains.
    assert text.count("הרצאה עצמאית") == 1


def test_tree_is_account_wide_not_per_class(app, client) -> None:
    teacher_id = _seed_teacher(app)
    _seed_class(app, teacher_id, "ז'3", "AAA111")
    class2 = _seed_class(app, teacher_id, "ז'4", "BBB222")
    courses, snapshots, decks, docs = _rich_catalog()
    _write_catalog(app, teacher_id, courses, snapshots, decks, docs)
    # Import 1.2 into the SECOND class only. The tree is account-wide, so it
    # must still see it as published, and show the syllabus once - not per class.
    _import_assignment(
        app, class2, "cw-12", "1.2 | הגשת משימות", published=True, topic="1 | מבוא"
    )
    login(client, "mori", TEACHER_PASSWORD)
    text = _lessons_text(client)

    assert text.count("1.2 | הגשת משימות") == 1
    assert "מפורסם" in text


def test_tree_empty_state_without_catalog(app, client) -> None:
    _seed_teacher(app)
    login(client, "mori", TEACHER_PASSWORD)
    text = _lessons_text(client)
    assert "עוד אין שיעורים" in text


# ---------------------------------------------------------------------------
# Results-sheet filter (unit level)
# ---------------------------------------------------------------------------


def test_is_results_sheet_matches_personal_grade_reports() -> None:
    assert is_results_sheet("אופק פרץ - תוצאות בוחן רבעוני")
    assert is_results_sheet("תוצאות בוחן רבעוני")
    # Ordinary teaching material is not a results sheet.
    assert not is_results_sheet("2.3 | דף עבודה")
    assert not is_results_sheet("3.2 | הגשת משימות")
    assert not is_results_sheet("")


def test_visible_docs_drops_results_sheet() -> None:
    docs = [
        DriveItem(file_id="d1", title="2.3 | דף עבודה", mime_type=DOCUMENT),
        DriveItem(
            file_id="d2",
            title="אופק פרץ - תוצאות בוחן רבעוני",
            mime_type=DOCUMENT,
        ),
    ]
    kept = catalog.visible_docs(docs)
    assert [doc.file_id for doc in kept] == ["d1"]


# ---------------------------------------------------------------------------
# Removed teacher-screen clutter
# ---------------------------------------------------------------------------


def test_dashboard_drops_create_class_and_expand(app, client) -> None:
    _seed_teacher(app)
    login(client, "mori", TEACHER_PASSWORD)
    page = client.get("/teacher")
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert "הכיתות שלי" in text
    assert "פתיחת כיתה חדשה" not in text
    assert "הרחבת כיתות" not in text
    assert "/teacher/sync-classes" not in text
    # The lessons button stays.
    assert "/teacher/lessons" in text


def test_class_screen_drops_rename_add_and_reuse(app, client) -> None:
    teacher_id = _seed_teacher(app)
    class_id = _seed_class(app, teacher_id, "ז'3", "AAA111")
    login(client, "mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/classes/{class_id}")
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert "שינוי שם" not in text
    assert "הוספה מהקורס" not in text
    assert "הוספת מטלה" not in text
    assert "שימוש חוזר מכיתה אחרת" not in text
    # The kept navigation still works.
    assert f"/teacher/classes/{class_id}/new" in text
    assert f"/teacher/classes/{class_id}/grading" in text
    assert f"/teacher/classes/{class_id}/gradebook" in text


def test_picker_drops_course_selection_card(app, client) -> None:
    teacher_id = _seed_teacher(app)
    class_id = _seed_class(app, teacher_id, "ז'3", "AAA111")
    courses, snapshots, decks, docs = _rich_catalog()
    _write_catalog(app, teacher_id, courses, snapshots, decks, docs)
    login(client, "mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/classes/{class_id}/new")
    assert page.status_code == 200
    text = html.unescape(page.text)
    # No course-selection card; the course is chosen automatically and named.
    assert "בחירת קורס" not in text
    assert COURSE_NAME in text
    # The personal results sheet never reaches the picker either.
    assert "תוצאות בוחן רבעוני" not in text


def test_grading_screen_drops_published_badge(app, client) -> None:
    teacher_id = _seed_teacher(app)
    class_id = _seed_class(app, teacher_id, "ז'3", "AAA111")
    _import_assignment(
        app, class_id, "cw-12", "1.2 | הגשת משימות", published=True
    )
    _import_assignment(
        app, class_id, "cw-22", "2.2 | הגשת משימות", published=False
    )
    login(client, "mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/classes/{class_id}/grading")
    assert page.status_code == 200
    text = html.unescape(page.text)
    # A published assignment no longer carries the redundant "מפורסמת" tag.
    assert "מפורסמת" not in text
    # A draft is still flagged so the teacher knows it is not live.
    assert "טיוטה" in text
    assert "1.2 | הגשת משימות" in text
    assert "2.2 | הגשת משימות" in text