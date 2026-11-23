"""Teacher actions added after Phase 7: password reset and live deadlines.

Both actions exist so a teacher can fix a forgotten password or move a
deadline from the class screen, without a shell, a database edit or
unpublishing a task in the middle of a lesson.
"""
from __future__ import annotations

import re
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from portal.auth.security import verify_password
from portal.models import ROLE_STUDENT, ROLE_TEACHER, Assignment, User
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"
_REVEAL_RE = re.compile(r'class="password-reveal" dir="ltr">([^<]+)<')


def _db(app):
    return app.state.session_factory()


def _login_teacher(client, username: str = "mori") -> None:
    response = login(client, username, TEACHER_PASSWORD)
    assert response.status_code == 303, response.text


def _create_class(client, name: str = "כיתה ז׳ 3") -> int:
    token = csrf(client, "/teacher")
    response = client.post(
        "/teacher/classes",
        data={"csrf_token": token, "name": name},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])


def _add_student(client, class_id: int, username: str = "noa") -> int:
    token = csrf(client, f"/teacher/classes/{class_id}?tab=students")
    response = client.post(
        f"/teacher/classes/{class_id}/students",
        data={
            "csrf_token": token,
            "display_name": "נועה כהן",
            "username": username,
            "password": STUDENT_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with _db(client.app) as db:
        return db.scalar(select(User.id).where(User.username == username))


def _create_assignment(client, class_id: int, *, published: bool = True) -> int:
    token = csrf(client, f"/teacher/classes/{class_id}")
    data = {
        "csrf_token": token,
        "title": "מטלה 3.3 – קליטת קלט",
        "instructions": "הגישו את דף העבודה.",
        "rubric_path": "assignments/lesson_1.json",
        "due_at": "2026-12-01",
    }
    if published:
        data["published"] = "on"
    response = client.post(
        f"/teacher/classes/{class_id}/assignments",
        data=data,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with _db(client.app) as db:
        return db.scalar(
            select(Assignment.id).where(Assignment.class_id == class_id)
        )


def _seed(app, client):
    create_user(
        app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורי כהן"
    )
    _login_teacher(client)
    class_id = _create_class(client)
    student_id = _add_student(client, class_id)
    return class_id, student_id


def _reset(client, student_id: int, *, token: str | None = None):
    if token is None:
        token = csrf(client, "/teacher")
    return client.post(
        f"/teacher/students/{student_id}/reset-password",
        data={"csrf_token": token},
        follow_redirects=False,
    )


# --------------------------------------------------------------- passwords


def test_reset_reveals_once_and_replaces_the_old_password(app, client, new_client):
    class_id, student_id = _seed(app, client)

    response = _reset(client, student_id)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    match = _REVEAL_RE.search(response.text)
    assert match is not None, "the new password was not revealed"
    revealed = match.group(1)
    assert len(revealed) >= 8
    assert revealed != STUDENT_PASSWORD
    assert "נועה כהן" in response.text

    with _db(app) as db:
        student = db.get(User, student_id)
        assert student.password_hash.startswith("$argon2id$")
        assert verify_password(student.password_hash, revealed)
        assert not verify_password(student.password_hash, STUDENT_PASSWORD)
        # The plaintext must not be stored anywhere in the row.
        assert revealed not in str(student.__dict__)

    # The old password no longer signs in; the revealed one does.
    assert login(new_client, "noa", STUDENT_PASSWORD).status_code == 200
    assert login(new_client, "noa", revealed).status_code == 303

    # The reveal is one-shot: it is not flashed into the session cookie, so
    # the next page the teacher loads must not contain it again.
    page = client.get(f"/teacher/classes/{class_id}?tab=students")
    assert page.status_code == 200
    assert revealed not in page.text


def test_reset_clears_an_existing_login_lockout(app, client, new_client):
    _, student_id = _seed(app, client)

    for _ in range(5):
        assert login(new_client, "noa", "wrong-password").status_code == 200
    locked = login(new_client, "noa", STUDENT_PASSWORD)
    assert locked.status_code == 429

    response = _reset(client, student_id)
    revealed = _REVEAL_RE.search(response.text).group(1)
    # Same browser, same address: the lock must be gone, not merely expired.
    assert login(new_client, "noa", revealed).status_code == 303


def test_reset_requires_csrf(app, client):
    _, student_id = _seed(app, client)
    with _db(app) as db:
        before = db.get(User, student_id).password_hash

    response = client.post(
        f"/teacher/students/{student_id}/reset-password",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 400
    with _db(app) as db:
        assert db.get(User, student_id).password_hash == before


def test_reset_is_teacher_only(app, client, new_client):
    _, student_id = _seed(app, client)

    anonymous = client.post(
        f"/teacher/students/{student_id}/reset-password",
        data={},
        follow_redirects=False,
    )
    # Anonymous is redirected to login before CSRF is even considered.
    assert anonymous.status_code in {303, 400}

    create_user(app, "noa2", STUDENT_PASSWORD, role=ROLE_STUDENT)
    login(new_client, "noa2", STUDENT_PASSWORD)
    token = csrf(new_client, "/student")
    response = new_client.post(
        f"/teacher/students/{student_id}/reset-password",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_reset_only_targets_students_of_this_teacher(app, client, new_client):
    _, student_id = _seed(app, client)

    create_user(app, "mori2", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(new_client, "mori2")
    other_class = _create_class(new_client, "כיתה ז׳ 4")
    other_student = _add_student(new_client, other_class, "dan")

    # Another teacher's student, a teacher account and a missing id are all 404.
    assert _reset(client, other_student).status_code == 404
    teacher_id = create_user(app, "mori3", TEACHER_PASSWORD, role=ROLE_TEACHER)
    assert _reset(client, teacher_id).status_code == 404
    assert _reset(client, 999999).status_code == 404

    with _db(app) as db:
        assert verify_password(db.get(User, other_student).password_hash, STUDENT_PASSWORD)
        assert verify_password(db.get(User, student_id).password_hash, STUDENT_PASSWORD)


def test_roster_and_profile_offer_the_reset_action(app, client):
    class_id, student_id = _seed(app, client)
    action = f"/teacher/students/{student_id}/reset-password"

    roster = client.get(f"/teacher/classes/{class_id}?tab=students")
    assert roster.status_code == 200
    assert action in roster.text
    assert "איפוס סיסמה" in roster.text

    profile = client.get(f"/teacher/students/{student_id}/profile")
    assert profile.status_code == 200
    assert action in profile.text


# --------------------------------------------------------------- deadlines


def test_published_deadline_can_be_changed_and_cleared(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    class_id = _create_class(client)
    assignment_id = _create_assignment(client, class_id)

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/assignments/{assignment_id}/due",
        data={"csrf_token": token, "due_at": "2026-11-16"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/teacher/classes/{class_id}"
    with _db(app) as db:
        assignment = db.get(Assignment, assignment_id)
        # 23:59 Asia/Jerusalem in November (UTC+2) is 21:59 UTC.
        assert assignment.due_at == datetime(2026, 11, 16, 21, 59)
        assert assignment.published is True

    page = client.get(f"/teacher/classes/{class_id}")
    assert "תאריך היעד של" in page.text and "עודכן" in page.text

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/assignments/{assignment_id}/due",
        data={"csrf_token": token, "due_at": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _db(app) as db:
        assert db.get(Assignment, assignment_id).due_at is None
    page = client.get(f"/teacher/classes/{class_id}")
    assert "הוסר" in page.text


def test_invalid_deadline_keeps_the_previous_value(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    class_id = _create_class(client)
    assignment_id = _create_assignment(client, class_id)

    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/assignments/{assignment_id}/due",
        data={"csrf_token": token, "due_at": "16/11/2026"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _db(app) as db:
        assert db.get(Assignment, assignment_id).due_at == datetime(2026, 12, 1, 21, 59)
    page = client.get(f"/teacher/classes/{class_id}")
    assert "תאריך היעד אינו תקין" in page.text


def test_deadline_route_guards(app, client, new_client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    class_id = _create_class(client)
    assignment_id = _create_assignment(client, class_id)
    student_id = _add_student(client, class_id)

    missing_csrf = client.post(
        f"/teacher/assignments/{assignment_id}/due",
        data={"due_at": "2026-11-16"},
        follow_redirects=False,
    )
    assert missing_csrf.status_code == 400

    login(new_client, "noa", STUDENT_PASSWORD)
    token = csrf(new_client, "/student")
    as_student = new_client.post(
        f"/teacher/assignments/{assignment_id}/due",
        data={"csrf_token": token, "due_at": "2026-11-16"},
        follow_redirects=False,
    )
    assert as_student.status_code == 403

    create_user(app, "mori2", TEACHER_PASSWORD, role=ROLE_TEACHER)
    other = TestClient(app)
    try:
        _login_teacher(other, "mori2")
        token = csrf(other, "/teacher")
        response = other.post(
            f"/teacher/assignments/{assignment_id}/due",
            data={"csrf_token": token, "due_at": "2026-11-16"},
            follow_redirects=False,
        )
        assert response.status_code == 404
    finally:
        other.close()

    with _db(app) as db:
        assert db.get(Assignment, assignment_id).due_at == datetime(2026, 12, 1, 21, 59)
        assert db.get(User, student_id).role == ROLE_STUDENT


def test_deadline_editor_appears_only_for_published_assignments(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    class_id = _create_class(client)
    draft_id = _create_assignment(client, class_id, published=False)

    page = client.get(f"/teacher/classes/{class_id}")
    assert f"/teacher/assignments/{draft_id}/due" not in page.text

    token = csrf(client, f"/teacher/classes/{class_id}")
    client.post(
        f"/teacher/assignments/{draft_id}/toggle",
        data={"csrf_token": token, "due_at": "2026-11-16"},
        follow_redirects=False,
    )
    page = client.get(f"/teacher/classes/{class_id}")
    assert f"/teacher/assignments/{draft_id}/due" in page.text
    assert "שמירת תאריך היעד" in page.text