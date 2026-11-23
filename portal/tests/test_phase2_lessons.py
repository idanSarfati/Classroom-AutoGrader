"""Phase 2 tests: lesson catalog, slide CRUD, visibility and mobile viewer."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    ClassGroup,
    Enrollment,
    Lesson,
    LessonSlide,
)
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"


def _db(app):
    return app.state.session_factory()


def _seed_teacher(app, username: str = "mori", display_name: str = "מורי כהן") -> int:
    return create_user(
        app, username, TEACHER_PASSWORD, role=ROLE_TEACHER, display_name=display_name
    )


def _login_teacher(client, username: str = "mori") -> None:
    response = login(client, username, TEACHER_PASSWORD)
    assert response.status_code == 303, response.text
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


def _create_student(
    app, username: str, *, class_id: int | None = None
) -> int:
    student_id = create_user(
        app,
        username,
        STUDENT_PASSWORD,
        role=ROLE_STUDENT,
        display_name=f"תלמיד {username}",
    )
    if class_id is not None:
        with _db(app) as db:
            db.add(Enrollment(class_id=class_id, student_id=student_id))
            db.commit()
    return student_id


def _create_lesson(
    client,
    *,
    title: str,
    summary: str = "",
    class_id: int | None = None,
    published: bool = True,
) -> int:
    token = csrf(client, "/teacher/lessons")
    data = {
        "csrf_token": token,
        "title": title,
        "summary": summary,
        "class_id": "" if class_id is None else str(class_id),
    }
    if published:
        data["published"] = "on"
    response = client.post(
        "/teacher/lessons", data=data, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])


def _add_slide(
    client,
    lesson_id: int,
    *,
    title: str = "",
    body: str = "",
    code: str = "",
) -> int:
    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    response = client.post(
        f"/teacher/lessons/{lesson_id}/slides",
        data={
            "csrf_token": token,
            "title": title,
            "body": body,
            "code": code,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert "#slide-" in location
    return int(location.rsplit("#slide-", 1)[1])


def _slides(app, lesson_id: int) -> list[LessonSlide]:
    with _db(app) as db:
        return list(
            db.scalars(
                select(LessonSlide)
                .where(LessonSlide.lesson_id == lesson_id)
                .order_by(LessonSlide.position, LessonSlide.id)
            ).all()
        )


def test_teacher_creates_global_lesson_with_slides(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)

    lesson_id = _create_lesson(
        client,
        title="שיעור מבוא",
        summary="מה לומדים בשיעור הראשון.",
        published=True,
    )
    first = _add_slide(
        client,
        lesson_id,
        title="מה זה פייתון?",
        body="שפת תכנות קריאה ופשוטה.",
        code='print("Hello")',
    )
    second = _add_slide(
        client,
        lesson_id,
        title="תרגיל",
        body="כתבו פקודת print ראשונה.",
    )

    with _db(app) as db:
        lesson = db.get(Lesson, lesson_id)
        assert lesson is not None
        assert lesson.class_id is None
        assert lesson.published is True
        assert lesson.title == "שיעור מבוא"
        assert lesson.teacher_id is not None
        slides = db.scalars(
            select(LessonSlide)
            .where(LessonSlide.lesson_id == lesson_id)
            .order_by(LessonSlide.position)
        ).all()
        assert [slide.id for slide in slides] == [first, second]
        assert [slide.position for slide in slides] == [0, 1]

    page = client.get(f"/teacher/lessons/{lesson_id}")
    assert page.status_code == 200
    assert "מה זה פייתון?" in page.text
    assert "הוספת שקופית" in page.text
    assert "מפורסם" in page.text


def test_teacher_updates_reorders_and_deletes_slides(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    lesson_id = _create_lesson(client, title="שיעור עם שקופיות", published=False)

    a = _add_slide(client, lesson_id, title="A", body="ראשונה")
    b = _add_slide(client, lesson_id, title="B", body="שנייה")
    c = _add_slide(client, lesson_id, title="C", body="שלישית")
    assert [slide.title for slide in _slides(app, lesson_id)] == ["A", "B", "C"]

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    response = client.post(
        f"/teacher/lessons/{lesson_id}/slides/{c}/move",
        data={"csrf_token": token, "direction": "up"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert [slide.title for slide in _slides(app, lesson_id)] == ["A", "C", "B"]

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    client.post(
        f"/teacher/lessons/{lesson_id}/slides/{a}/move",
        data={"csrf_token": token, "direction": "down"},
    )
    assert [slide.title for slide in _slides(app, lesson_id)] == ["C", "A", "B"]

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    client.post(
        f"/teacher/lessons/{lesson_id}/slides/{a}/update",
        data={
            "csrf_token": token,
            "title": "A חדש",
            "body": "תוכן מעודכן",
            "code": "x = 1",
        },
    )
    updated = {slide.id: slide for slide in _slides(app, lesson_id)}[a]
    assert updated.title == "A חדש"
    assert updated.body == "תוכן מעודכן"
    assert updated.code == "x = 1"

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    client.post(
        f"/teacher/lessons/{lesson_id}/slides/{a}/delete",
        data={"csrf_token": token},
    )
    remaining = _slides(app, lesson_id)
    assert [slide.id for slide in remaining] == [c, b]
    assert [slide.position for slide in remaining] == [0, 1]


def test_unpublished_lesson_is_hidden_until_published(
    app, client, new_client
) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    lesson_id = _create_lesson(client, title="טיוטה סודית", published=False)
    _add_slide(client, lesson_id, title="פנימי", body="לא לפרסום")
    _create_student(app, "noa")

    login(new_client, "noa", STUDENT_PASSWORD)
    assert new_client.get(f"/student/lessons/{lesson_id}").status_code == 404
    assert "טיוטה סודית" not in new_client.get("/student/lessons").text
    assert "טיוטה סודית" not in new_client.get("/student").text

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    response = client.post(
        f"/teacher/lessons/{lesson_id}/update",
        data={
            "csrf_token": token,
            "title": "טיוטה סודית",
            "summary": "",
            "class_id": "",
            "published": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = new_client.get(f"/student/lessons/{lesson_id}")
    assert page.status_code == 200
    assert "טיוטה סודית" in page.text
    assert "פנימי" in page.text


def test_global_lesson_is_visible_to_every_student(
    app, client, new_client
) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    lesson_id = _create_lesson(client, title="שיעור כללי", published=True)
    _add_slide(client, lesson_id, title="פתיחה", body="לכולם")

    _create_student(app, "noa")
    _create_student(app, "dana")
    login(new_client, "noa", STUDENT_PASSWORD)
    assert "שיעור כללי" in new_client.get("/student/lessons").text
    assert "שיעור כללי" in new_client.get("/student").text
    assert new_client.get(f"/student/lessons/{lesson_id}").status_code == 200

    other_client = TestClient(app)
    try:
        login(other_client, "dana", STUDENT_PASSWORD)
        assert "שיעור כללי" in other_client.get("/student/lessons").text
        assert other_client.get(f"/student/lessons/{lesson_id}").status_code == 200
    finally:
        other_client.close()


def test_class_lesson_requires_enrollment(app, client, new_client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client, "כיתה ט׳")
    lesson_id = _create_lesson(
        client, title="שיעור לכיתה ט׳", class_id=class_id, published=True
    )
    _add_slide(client, lesson_id, title="רק לתלמידי הכיתה", body="תוכן")

    _create_student(app, "inside", class_id=class_id)
    _create_student(app, "outside")
    login(new_client, "inside", STUDENT_PASSWORD)
    assert new_client.get(f"/student/lessons/{lesson_id}").status_code == 200
    assert "שיעור לכיתה ט׳" in new_client.get("/student").text

    outside = TestClient(app)
    try:
        login(outside, "outside", STUDENT_PASSWORD)
        assert outside.get(f"/student/lessons/{lesson_id}").status_code == 404
        assert "שיעור לכיתה ט׳" not in outside.get("/student/lessons").text
    finally:
        outside.close()

    class_page = client.get(f"/teacher/classes/{class_id}")
    assert class_page.status_code == 200
    assert "שיעור לכיתה ט׳" in class_page.text
    assert f"/teacher/classes/{class_id}/new" in class_page.text


def test_other_teacher_cannot_manage_lesson(app, client, new_client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    lesson_id = _create_lesson(client, title="שיעור פרטי", published=True)
    slide_id = _add_slide(client, lesson_id, title="שקופית", body="תוכן")

    _seed_teacher(app, "other", "מורה אחר")
    _login_teacher(new_client, "other")

    assert new_client.get(f"/teacher/lessons/{lesson_id}").status_code == 404
    token = csrf(new_client, "/teacher/lessons")
    for path, data in (
        (
            f"/teacher/lessons/{lesson_id}/update",
            {
                "csrf_token": token,
                "title": "נחטף",
                "summary": "",
                "class_id": "",
                "published": "on",
            },
        ),
        (
            f"/teacher/lessons/{lesson_id}/slides",
            {
                "csrf_token": token,
                "title": "זרה",
                "body": "תוכן",
                "code": "",
            },
        ),
        (
            f"/teacher/lessons/{lesson_id}/slides/{slide_id}/update",
            {
                "csrf_token": token,
                "title": "זרה",
                "body": "תוכן",
                "code": "",
            },
        ),
        (
            f"/teacher/lessons/{lesson_id}/slides/{slide_id}/move",
            {"csrf_token": token, "direction": "down"},
        ),
        (
            f"/teacher/lessons/{lesson_id}/slides/{slide_id}/delete",
            {"csrf_token": token},
        ),
        (f"/teacher/lessons/{lesson_id}/delete", {"csrf_token": token}),
    ):
        response = new_client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 404, (path, response.status_code)

    with _db(app) as db:
        lesson = db.get(Lesson, lesson_id)
        assert lesson is not None and lesson.title == "שיעור פרטי"
        slide = db.get(LessonSlide, slide_id)
        assert slide is not None and slide.title == "שקופית"


def test_lesson_content_is_html_escaped(app, client, new_client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    lesson_id = _create_lesson(
        client, title="<script>alert(1)</script>", published=True
    )
    _add_slide(
        client,
        lesson_id,
        title="<b>כותרת</b>",
        body="<img src=x onerror=alert(1)>",
        code="<script>alert('code')</script>",
    )
    _create_student(app, "noa")

    login(new_client, "noa", STUDENT_PASSWORD)
    page = new_client.get(f"/student/lessons/{lesson_id}")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<img src=x" not in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text
    assert "<script>alert('code')</script>" not in page.text
    assert "&lt;script&gt;alert(&#39;code&#39;)&lt;/script&gt;" in page.text


def test_slide_navigation_clamps_out_of_range(app, client, new_client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    lesson_id = _create_lesson(client, title="שיעור עם ניווט", published=True)
    _add_slide(client, lesson_id, title="ראשונה", body="תוכן ראשון")
    _add_slide(client, lesson_id, title="שנייה", body="תוכן שני")
    _create_student(app, "noa")

    login(new_client, "noa", STUDENT_PASSWORD)
    last = new_client.get(f"/student/lessons/{lesson_id}?slide=99")
    assert last.status_code == 200
    assert "שקופית 2 מתוך 2" in last.text
    assert "תוכן שני" in last.text

    first = new_client.get(f"/student/lessons/{lesson_id}?slide=-5")
    assert first.status_code == 200
    assert "שקופית 1 מתוך 2" in first.text
    assert "תוכן ראשון" in first.text
    assert "?slide=1" in first.text


def test_slide_moves_replace_history_instead_of_pushing(app) -> None:
    # Moving between slides must not add browser history entries: Back should
    # leave the viewer for the page the student came from (a class tab), not
    # walk backwards through slides. app.js enforces this with replaceState.
    source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    slide_part = source.split("Lecture slides:", 1)[1]
    assert "window.history.replaceState" in slide_part
    assert "window.history.pushState" not in slide_part
    assert 'addEventListener("popstate"' not in slide_part


def test_deleting_lesson_or_class_cascades_slides(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)

    class_id = _create_class(client)
    class_lesson = _create_lesson(
        client, title="שיעור כיתתי", class_id=class_id, published=True
    )
    _add_slide(client, class_lesson, title="כיתתי", body="תוכן")

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/classes/{class_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _db(app) as db:
        assert db.get(Lesson, class_lesson) is None
        assert (
            db.scalar(
                select(LessonSlide).where(LessonSlide.lesson_id == class_lesson)
            )
            is None
        )

    global_lesson = _create_lesson(client, title="שיעור כללי", published=True)
    _add_slide(client, global_lesson, title="כללי", body="תוכן")
    token = csrf(client, f"/teacher/lessons/{global_lesson}")
    response = client.post(
        f"/teacher/lessons/{global_lesson}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _db(app) as db:
        assert db.get(Lesson, global_lesson) is None
        assert (
            db.scalar(
                select(LessonSlide).where(LessonSlide.lesson_id == global_lesson)
            )
            is None
        )


def test_lesson_routes_enforce_csrf_and_roles(app, client, new_client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    response = client.post(
        "/teacher/lessons",
        data={"title": "ללא אסימון"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    with _db(app) as db:
        assert db.scalar(select(Lesson)) is None

    _create_student(app, "noa")
    login(new_client, "noa", STUDENT_PASSWORD)
    assert new_client.get("/teacher/lessons").status_code == 403
    assert new_client.get("/student/lessons").status_code == 200


def test_teacher_lessons_list_and_empty_state(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    page = client.get("/teacher/lessons")
    assert page.status_code == 200
    assert "השיעורים שלי" in page.text
    assert "עוד אין שיעורים" in page.text

    class_id = _create_class(client, "כיתה י׳")
    page = client.get(f"/teacher/lessons?class_id={class_id}")
    assert page.status_code == 200
    assert f'value="{class_id}"' in page.text
    assert "selected" in page.text


def test_lesson_timestamps_are_stored(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    lesson_id = _create_lesson(client, title="שיעור עם חותמת", published=True)
    with _db(app) as db:
        lesson = db.get(Lesson, lesson_id)
        assert isinstance(lesson.created_at, datetime)
        assert isinstance(lesson.updated_at, datetime)
        assert lesson.created_at is not None
