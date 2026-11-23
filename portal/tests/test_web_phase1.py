"""Phase 1 web tests: auth, classes, provisioning, assignments, RTL shell."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from portal.auth.security import hash_password, verify_password
from portal.grading import adapter
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
    User,
)
from portal.scripts.bootstrap_teacher import main as bootstrap_teacher
from portal.tests.helpers import (
    create_user,
    csrf,
    csrf_from,
    login,
    logout,
)
from portal.timeutil import parse_local_due_date

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"


def _db(app):
    return app.state.session_factory()


def _login_teacher(client) -> None:
    response = login(client, "mori", TEACHER_PASSWORD)
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


def _seed_teacher(app) -> int:
    return create_user(
        app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורי כהן"
    )


def test_healthz(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "portal"


def test_due_date_is_the_last_minute_of_the_selected_day() -> None:
    # Winter (UTC+2) and summer (UTC+3) both resolve to 23:59 local time.
    assert parse_local_due_date("2026-12-01", "Asia/Jerusalem") == datetime(
        2026, 12, 1, 21, 59
    )
    assert parse_local_due_date("2026-07-01", "Asia/Jerusalem") == datetime(
        2026, 7, 1, 20, 59
    )
    assert parse_local_due_date("", "Asia/Jerusalem") is None
    # Older callers that still send a full datetime keep their exact time.
    assert parse_local_due_date("2026-12-01T20:00", "Asia/Jerusalem") == datetime(
        2026, 12, 1, 18, 0
    )


def test_root_redirects_anonymous_to_login(client) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_anonymous_teacher_page_redirects_to_login(client) -> None:
    response = client.get("/teacher", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert response.headers["location"].endswith("/teacher")


def test_login_page_is_mobile_hebrew_rtl(client) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    html = response.text
    assert 'lang="he"' in html
    assert 'dir="rtl"' in html
    assert 'name="viewport"' in html
    assert "התחברות לפורטל" in html
    assert csrf_from(html)


def test_login_password_can_be_shown_while_typing(client) -> None:
    """The eye button reveals the same field; nothing is posted or prefilled."""
    page = client.get("/login").text
    assert 'type="password"' in page
    assert "data-password-toggle" in page
    assert 'aria-controls="password"' in page
    assert 'aria-pressed="false"' in page
    assert "הצגת הסיסמה" in page
    # The behaviour lives in the shared script, not in an inline handler, so a
    # strict CSP stays possible.
    script = client.get("/static/app.js").text
    assert "data-password-toggle" in script
    assert 'field.type = field.type === "password" ? "text" : "password"' in script


def test_password_hashing_uses_argon2id() -> None:
    stored = hash_password(TEACHER_PASSWORD)
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, TEACHER_PASSWORD)
    assert not verify_password(stored, "wrong-password")
    assert not verify_password("not-a-hash", TEACHER_PASSWORD)


def test_teacher_login_and_logout(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)

    page = client.get("/teacher")
    assert page.status_code == 200
    assert "הכיתות שלי" in page.text
    assert "מורי כהן" in page.text

    logout(client)
    assert client.get("/teacher", follow_redirects=False).status_code == 303


def test_teacher_creates_class_and_sees_code(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client, "כיתה ט׳ 1")

    with _db(app) as db:
        group = db.get(ClassGroup, class_id)
        assert group is not None
        assert group.name == "כיתה ט׳ 1"
        assert len(group.code) == 6
        assert group.code.isalnum()

    page = client.get(f"/teacher/classes/{class_id}")
    assert page.status_code == 200
    assert "כיתה ט׳ 1" in page.text
    assert group.code in page.text
    # The roster lives on its own tab; the content tab only shows content.
    assert "הוספת תלמיד/ה" not in page.text
    assert "תוכן הכיתה" in page.text
    # The gradebook link belongs in the class header only; the content card
    # must not repeat it.
    assert page.text.count(f"/teacher/classes/{class_id}/gradebook") == 1

    students_page = client.get(f"/teacher/classes/{class_id}?tab=students")
    assert students_page.status_code == 200
    assert "הוספת תלמיד/ה" in students_page.text
    assert "תוכן הכיתה" not in students_page.text


def test_student_tab_keeps_roster_actions_in_place(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/classes/{class_id}/students",
        data={
            "csrf_token": token,
            "display_name": "נועה כהן",
            "username": "noa.cohen",
            "password": STUDENT_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?tab=students")

    students_page = client.get(f"/teacher/classes/{class_id}?tab=students")
    assert "נועה כהן" in students_page.text
    content_page = client.get(f"/teacher/classes/{class_id}")
    assert "נועה כהן" not in content_page.text

    with _db(app) as db:
        student_id = db.scalar(select(User.id).where(User.username == "noa.cohen"))

    token = csrf(client, f"/teacher/classes/{class_id}?tab=students")
    response = client.post(
        f"/teacher/classes/{class_id}/students/{student_id}/remove",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?tab=students")


def test_teacher_provisions_student_and_student_logs_in(
    app, client, new_client
) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/classes/{class_id}/students",
        data={
            "csrf_token": token,
            "display_name": "נועה כהן",
            "username": "Noa.Cohen",
            "password": STUDENT_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with _db(app) as db:
        student = db.scalar(select(User).where(User.username == "noa.cohen"))
        assert student is not None
        assert student.role == ROLE_STUDENT
        assert student.display_name == "נועה כהן"
        assert student.password_hash.startswith("$argon2id$")
        enrollment = db.scalar(
            select(Enrollment).where(
                Enrollment.class_id == class_id,
                Enrollment.student_id == student.id,
            )
        )
        assert enrollment is not None

    # A separate browser session: the student cannot reach the teacher side.
    student_login = login(new_client, "noa.cohen", STUDENT_PASSWORD)
    assert student_login.status_code == 303
    assert student_login.headers["location"] == "/student"
    assert new_client.get("/teacher").status_code == 403

    dashboard = new_client.get("/student")
    assert dashboard.status_code == 200
    assert "נועה כהן" in dashboard.text


def test_assignment_listing_does_not_leak_rubric_to_student(
    app, client, new_client
) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)
    token = csrf(client, f"/teacher/classes/{class_id}")

    response = client.post(
        f"/teacher/classes/{class_id}/students",
        data={
            "csrf_token": token,
            "display_name": "נועה כהן",
            "username": "noa",
            "password": STUDENT_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/classes/{class_id}/assignments",
        data={
            "csrf_token": token,
            "title": "מטלה 3 – לולאות",
            "instructions": "הגישו קוד שמדפיס את המספרים מ־1 עד 10.",
            "rubric_path": "assignments/lesson_1.json",
            "due_at": "2026-12-01",
            "published": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with _db(app) as db:
        assignment = db.scalar(
            select(Assignment).where(Assignment.class_id == class_id)
        )
        assert assignment is not None
        assert assignment.published is True
        assert assignment.rubric_sha256
        assert len(assignment.rubric_sha256) == 64
        # A date-only deadline means the last minute of that day: 23:59
        # Asia/Jerusalem in December is 21:59 UTC.
        assert assignment.due_at == datetime(2026, 12, 1, 21, 59)
        sha = assignment.rubric_sha256
        assignment_id = assignment.id

    login(new_client, "noa", STUDENT_PASSWORD)
    dashboard = new_client.get("/student")
    assert dashboard.status_code == 200
    assert "מטלה 3 – לולאות" in dashboard.text
    assert "lesson_1.json" not in dashboard.text
    assert sha not in dashboard.text

    page = new_client.get(f"/student/assignments/{assignment_id}")
    assert page.status_code == 200
    assert "הגישו קוד שמדפיס את המספרים" in page.text
    assert "lesson_1.json" not in page.text
    assert sha not in page.text


def test_unpublished_assignment_is_hidden(app, client, new_client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)
    token = csrf(client, f"/teacher/classes/{class_id}")

    client.post(
        f"/teacher/classes/{class_id}/students",
        data={
            "csrf_token": token,
            "display_name": "נועה",
            "username": "noa",
            "password": STUDENT_PASSWORD,
        },
    )
    token = csrf(client, f"/teacher/classes/{class_id}")
    client.post(
        f"/teacher/classes/{class_id}/assignments",
        data={
            "csrf_token": token,
            "title": "טיוטה סודית",
            "instructions": "",
            "rubric_path": "assignments/lesson_1.json",
            "published": "",
        },
    )

    with _db(app) as db:
        assignment = db.scalar(select(Assignment))
        assert assignment is not None and assignment.published is False
        assignment_id = assignment.id

    login(new_client, "noa", STUDENT_PASSWORD)
    assert "טיוטה סודית" not in new_client.get("/student").text
    assert new_client.get(f"/student/assignments/{assignment_id}").status_code == 404


def test_student_joins_class_by_code(app, client, new_client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client, "כיתה י׳")

    create_user(
        app, "dana", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="דנה לוי"
    )
    with _db(app) as db:
        code = db.get(ClassGroup, class_id).code

    login(new_client, "dana", STUDENT_PASSWORD)
    token = csrf(new_client, "/student")
    response = new_client.post(
        "/student/join",
        data={"csrf_token": token, "code": f"  {code.lower()}  "},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/student/classes/{class_id}"

    with _db(app) as db:
        student = db.scalar(select(User).where(User.username == "dana"))
        assert db.scalar(
            select(Enrollment).where(
                Enrollment.class_id == class_id,
                Enrollment.student_id == student.id,
            )
        )

    page = new_client.get("/student")
    assert "כיתה י׳" in page.text


def test_wrong_class_code_shows_error_without_enrolling(
    app, client, new_client
) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    _create_class(client)
    create_user(app, "dana", STUDENT_PASSWORD, role=ROLE_STUDENT)

    login(new_client, "dana", STUDENT_PASSWORD)
    token = csrf(new_client, "/student")
    response = new_client.post(
        "/student/join",
        data={"csrf_token": token, "code": "ZZZZZZ"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "קוד הכיתה לא נמצא" in response.text

    with _db(app) as db:
        assert db.scalar(select(Enrollment)) is None


def test_csrf_is_required_for_state_changes(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    response = client.post(
        "/teacher/classes",
        data={"name": "ללא אסימון"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    with _db(app) as db:
        assert db.scalar(select(ClassGroup)) is None


def test_teacher_cannot_open_another_teachers_class(app, client) -> None:
    _seed_teacher(app)
    other_id = create_user(
        app, "other", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורה אחר"
    )
    with _db(app) as db:
        group = ClassGroup(name="כיתה זרה", code="ABC234", teacher_id=other_id)
        db.add(group)
        db.commit()
        foreign_id = group.id

    _login_teacher(client)
    assert client.get(f"/teacher/classes/{foreign_id}").status_code == 404


def test_rubric_path_outside_allowlist_is_rejected(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)
    token = csrf(client, f"/teacher/classes/{class_id}")

    response = client.post(
        f"/teacher/classes/{class_id}/assignments",
        data={
            "csrf_token": token,
            "title": "ניסיון חציית גבול",
            "instructions": "",
            "rubric_path": ".env",
            "published": "on",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "לא ניתן לחבר את קובץ המחוון" in response.text
    with _db(app) as db:
        assert db.scalar(select(Assignment)) is None


def test_login_throttle_locks_after_repeated_failures(app, client) -> None:
    _seed_teacher(app)
    for _ in range(5):
        response = login(client, "mori", "wrong-password")
        assert response.status_code == 200
    locked = login(client, "mori", "wrong-password")
    assert locked.status_code == 429
    assert "יותר מדי ניסיונות" in locked.text


def test_teacher_renames_and_deletes_class_with_cascade(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client, "שם ישן")

    token = csrf(client, f"/teacher/classes/{class_id}")
    client.post(
        f"/teacher/classes/{class_id}/rename",
        data={"csrf_token": token, "name": "שם חדש"},
    )
    with _db(app) as db:
        assert db.get(ClassGroup, class_id).name == "שם חדש"

    token = csrf(client, f"/teacher/classes/{class_id}")
    client.post(
        f"/teacher/classes/{class_id}/students",
        data={
            "csrf_token": token,
            "display_name": "נועה",
            "username": "noa",
            "password": STUDENT_PASSWORD,
        },
    )
    token = csrf(client, f"/teacher/classes/{class_id}")
    client.post(
        f"/teacher/classes/{class_id}/assignments",
        data={
            "csrf_token": token,
            "title": "מטלה",
            "instructions": "",
            "rubric_path": "assignments/lesson_1.json",
        },
    )

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/classes/{class_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/teacher"

    with _db(app) as db:
        assert db.get(ClassGroup, class_id) is None
        assert db.scalar(select(Enrollment)) is None
        assert db.scalar(select(Assignment)) is None
        # The account itself survives; only the enrollment is cascaded.
        assert db.scalar(select(User).where(User.username == "noa")) is not None


def test_teacher_publish_rechecks_rubric_hash(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)

    with _db(app) as db:
        assignment = Assignment(
            class_id=class_id,
            title="מחוון שהשתנה",
            instructions="",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="0" * 64,
            published=False,
        )
        db.add(assignment)
        db.commit()
        assignment_id = assignment.id

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/assignments/{assignment_id}/toggle",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "קובץ המחוון השתנה או חסר" in response.text
    with _db(app) as db:
        assert db.get(Assignment, assignment_id).published is False


def test_publishing_a_draft_accepts_a_due_date(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)

    token = csrf(client, f"/teacher/classes/{class_id}")
    client.post(
        f"/teacher/classes/{class_id}/assignments",
        data={
            "csrf_token": token,
            "title": "מטלה שנשמרה כטיוטה",
            "instructions": "",
            "rubric_path": "assignments/lesson_1.json",
            "published": "",
        },
    )
    with _db(app) as db:
        assignment = db.scalar(select(Assignment))
        assert assignment is not None
        assert assignment.published is False
        assert assignment.due_at is None
        assignment_id = assignment.id

    page = client.get(f"/teacher/classes/{class_id}")
    assert page.status_code == 200
    # The draft row offers the deadline inside the publish panel, not only a
    # bare button that flips the flag.
    assert f'id="publish-due-{assignment_id}"' in page.text
    assert 'id="publish-due-' + str(assignment_id) + '" name="due_at"' in page.text
    assert 'type="date"' in page.text
    assert "פרסום המטלה" in page.text
    assert "תאריך היעד הוא סוף היום (23:59)" in page.text
    assert "איחור גורר ניכוי של" in page.text

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/assignments/{assignment_id}/toggle",
        data={"csrf_token": token, "due_at": "2026-12-01"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "פורסמה" in response.text
    with _db(app) as db:
        assignment = db.get(Assignment, assignment_id)
        assert assignment.published is True
        # A date-only deadline means the last minute of that day: 23:59
        # Asia/Jerusalem in December is 21:59 UTC.
        assert assignment.due_at == datetime(2026, 12, 1, 21, 59)

    # A published row no longer offers the publish panel.
    published_page = client.get(f"/teacher/classes/{class_id}")
    assert f'id="publish-due-{assignment_id}"' not in published_page.text
    assert "ביטול פרסום" in published_page.text


def test_publishing_rejects_an_invalid_due_date(app, client) -> None:
    _seed_teacher(app)
    _login_teacher(client)
    class_id = _create_class(client)

    with _db(app) as db:
        assignment = Assignment(
            class_id=class_id,
            title="טיוטה עם תאריך שגוי",
            instructions="",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="",
            published=False,
        )
        # Bind the real rubric so the publish check passes and only the date
        # can fail this request.
        binding = adapter.load_rubric("assignments/lesson_1.json")
        assignment.rubric_sha256 = binding.sha256
        db.add(assignment)
        db.commit()
        assignment_id = assignment.id

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/assignments/{assignment_id}/toggle",
        data={"csrf_token": token, "due_at": "not-a-date"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "תאריך היעד אינו תקין" in response.text
    with _db(app) as db:
        assignment = db.get(Assignment, assignment_id)
        assert assignment.published is False
        assert assignment.due_at is None


def test_bootstrap_teacher_script(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{(tmp_path / 'boot.db').as_posix()}"
    monkeypatch.setenv("PORTAL_DATABASE_URL", url)
    monkeypatch.setenv("PORTAL_SECRET_KEY", "test-secret")
    monkeypatch.setenv("PORTAL_TEACHER_PASSWORD", TEACHER_PASSWORD)

    assert bootstrap_teacher(["--username", "mori", "--name", "מורי כהן"]) == 0

    from portal.db import build_engine, build_session_factory

    engine = build_engine(url)
    try:
        with build_session_factory(engine)() as db:
            user = db.scalar(select(User).where(User.username == "mori"))
            assert user is not None
            assert user.role == ROLE_TEACHER
            assert verify_password(user.password_hash, TEACHER_PASSWORD)
    finally:
        engine.dispose()
