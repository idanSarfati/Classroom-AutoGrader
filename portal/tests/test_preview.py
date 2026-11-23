"""Teacher preview: every class can be seen exactly as a student sees it."""
from __future__ import annotations

from sqlalchemy import select

from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Lesson,
    LessonSlide,
)
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"


def _db(app):
    return app.state.session_factory()


def _seed_teacher(app) -> int:
    return create_user(
        app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורי כהן"
    )


def _login_teacher(client) -> None:
    response = login(client, "mori", TEACHER_PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/teacher"


def _create_class(client, name: str = "כיתה ח׳ 2") -> int:
    token = csrf(client, "/teacher")
    response = client.post(
        "/teacher/classes",
        data={"csrf_token": token, "name": name},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])


def _seed_lesson(app, class_id: int, *, published: bool = True) -> int:
    with _db(app) as db:
        teacher = db.scalar(select(ClassGroup.teacher_id).where(ClassGroup.id == class_id))
        lesson = Lesson(
            teacher_id=teacher,
            class_id=class_id,
            title="שיעור תצוגה",
            summary="תוכן לשיעור התצוגה",
            published=published,
        )
        db.add(lesson)
        db.flush()
        db.add(
            LessonSlide(
                lesson_id=lesson.id,
                position=0,
                title="שקופית ראשונה",
                body="טקסט השקופית",
            )
        )
        db.commit()
        return lesson.id


def _create_assignment(
    client, class_id: int, title: str, *, published: bool
) -> int:
    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/classes/{class_id}/assignments",
        data={
            "csrf_token": token,
            "title": title,
            "instructions": "הוראות המטלה",
            "rubric_path": "assignments/lesson_1.json",
            "published": "on" if published else "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with _db(client.app) as db:
        assignment = db.scalar(
            select(Assignment).where(
                Assignment.class_id == class_id,
                Assignment.title == title,
            )
        )
        assert assignment is not None
        return assignment.id


def test_class_preview_shows_published_content_only(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)
    lesson_id = _seed_lesson(app, class_id, published=True)
    draft_lesson = _seed_lesson(app, class_id, published=False)
    assignment_id = _create_assignment(
        client, class_id, "מטלת תצוגה מפורסמת", published=True
    )
    draft_assignment = _create_assignment(
        client, class_id, "טיוטה מוסתרת", published=False
    )
    # Consume the teacher's own flash messages before entering the preview.
    assert client.get(f"/teacher/classes/{class_id}").status_code == 200

    page = client.get(f"/teacher/preview/classes/{class_id}")
    assert page.status_code == 200
    html = page.text
    assert "תצוגת תלמיד" in html
    assert "כך תלמידי הכיתה רואים את התוכן" in html
    assert "שיעור תצוגה" in html
    assert "מטלת תצוגה מפורסמת" in html
    assert "טיוטה מוסתרת" not in html
    assert f"/teacher/preview/lessons/{lesson_id}" in html
    assert f"/teacher/preview/assignments/{assignment_id}" in html
    assert f"/teacher/preview/lessons/{draft_lesson}" not in html
    assert f"/teacher/preview/assignments/{draft_assignment}" not in html
    assert 'data-preview-class' in html

    # Draft lessons stay hidden, but the owning teacher may open a draft
    # assignment to check its content before publishing it.
    assert client.get(f"/teacher/preview/lessons/{draft_lesson}").status_code == 404
    draft = client.get(f"/teacher/preview/assignments/{draft_assignment}")
    assert draft.status_code == 200
    assert "טיוטה מוסתרת" in draft.text
    assert "כך המטלה תיראה לתלמידים לאחר הפרסום" in draft.text
    assert "כך תלמידי הכיתה רואים את התוכן" not in draft.text


def test_assignment_pages_link_to_the_teacher_preview(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)
    assignment_id = _create_assignment(
        client, class_id, "מטלת תצוגה", published=False
    )

    grading = client.get(f"/teacher/assignments/{assignment_id}/grading")
    assert grading.status_code == 200
    assert f"/teacher/preview/assignments/{assignment_id}" in grading.text
    assert "צפייה במטלה" in grading.text
    assert "המטלה עדיין טיוטה" in grading.text

    class_page = client.get(f"/teacher/classes/{class_id}")
    assert class_page.status_code == 200
    assert f"/teacher/preview/assignments/{assignment_id}" in class_page.text


def test_lesson_and_assignment_preview_are_read_only(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)
    lesson_id = _seed_lesson(app, class_id)
    assignment_id = _create_assignment(
        client, class_id, "מטלת תצוגה מפורסמת", published=True
    )

    lesson = client.get(f"/teacher/preview/lessons/{lesson_id}")
    assert lesson.status_code == 200
    assert "טקסט השקופית" in lesson.text
    assert "תצוגת תלמיד" in lesson.text
    assert "/teacher/classes/" in lesson.text

    assignment = client.get(f"/teacher/preview/assignments/{assignment_id}")
    assert assignment.status_code == 200
    html = assignment.text
    assert "הוראות המטלה" in html
    assert "תצוגת מורה" in html
    assert "data-submit-form" not in html
    assert 'name="code"' not in html
    assert "/submit" not in html
    assert "lesson_1.json" not in html
    with _db(app) as db:
        sha = db.get(Assignment, assignment_id).rubric_sha256
    assert sha not in html


def test_preview_is_limited_to_the_owning_teacher(app, client, new_client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)

    other_id = create_user(
        app, "other", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורה אחר"
    )
    with _db(app) as db:
        other_class = ClassGroup(name="כיתה זרה", code="XYZ234", teacher_id=other_id)
        db.add(other_class)
        db.flush()
        other_lesson = Lesson(
            teacher_id=other_id,
            class_id=other_class.id,
            title="שיעור זר",
            published=True,
        )
        db.add(other_lesson)
        db.commit()
        other_class_id = other_class.id
        other_lesson_id = other_lesson.id

    assert (
        client.get(f"/teacher/preview/classes/{other_class_id}").status_code
        == 404
    )
    assert (
        client.get(f"/teacher/preview/lessons/{other_lesson_id}").status_code
        == 404
    )

    create_user(
        app, "noa", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="נועה"
    )
    login(new_client, "noa", STUDENT_PASSWORD)
    assert (
        new_client.get(f"/teacher/preview/classes/{class_id}").status_code
        == 403
    )