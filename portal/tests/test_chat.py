"""Student↔teacher chat: service logic, authorization, context tagging, unread.

A conversation is the (teacher, student) pair. A student may only message a
teacher of a class they are enrolled in; a teacher may only message their own
students. A message sent from a task bubble carries the assignment and a
context label, and a forged assignment id can never redirect it to another
teacher or leak another class's task title.
"""
from __future__ import annotations

import pytest

from portal import chat
from portal.grading import adapter
from portal.timeutil import utcnow
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
    Message,
    Submission,
)
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "ChatTeacher123"
STUDENT_PASSWORD = "ChatStudent123"


def _db(app):
    return app.state.session_factory()


@pytest.fixture
def env(app):
    teacher = create_user(
        app, "chat-mori", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="המורה"
    )
    other = create_user(
        app, "chat-other", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורה אחר"
    )
    noa = create_user(
        app, "chat-noa", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="נועה"
    )
    dan = create_user(
        app, "chat-dan", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="דן"
    )
    outsider = create_user(
        app, "chat-outsider", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="חוץ"
    )
    lesson1 = adapter.load_rubric("assignments/lesson_1.json")
    with _db(app) as db:
        group = ClassGroup(name="כיתת צ'אט", code="CH01", teacher_id=teacher)
        other_group = ClassGroup(name="כיתה אחרת", code="CH02", teacher_id=other)
        db.add_all([group, other_group])
        db.flush()
        db.add_all(
            [
                Enrollment(class_id=group.id, student_id=noa),
                Enrollment(class_id=group.id, student_id=dan),
                Enrollment(class_id=other_group.id, student_id=outsider),
            ]
        )
        task = Assignment(
            class_id=group.id,
            title="1.2 | תרגול",
            instructions="ענו על השאלות.",
            rubric_path=str(lesson1.path),
            rubric_sha256=lesson1.sha256,
            published=True,
        )
        # Never rendered: only its id/title are used to test a forged context.
        foreign_task = Assignment(
            class_id=other_group.id,
            title="משימה של כיתה אחרת",
            rubric_path="",
            rubric_sha256="",
            published=True,
        )
        db.add_all([task, foreign_task])
        db.commit()
        return {
            "teacher": teacher,
            "other": other,
            "noa": noa,
            "dan": dan,
            "outsider": outsider,
            "class": group.id,
            "task": task.id,
            "task_title": task.title,
            "foreign_task": foreign_task.id,
        }


def _login(client, username, password):
    response = login(client, username, password)
    assert response.status_code == 303, response.text


# ------------------------------------------------------------------ service
def test_student_teachers_and_can_message(app, env):
    with _db(app) as db:
        teachers = chat.student_teachers(db, env["noa"])
        assert [t.id for t in teachers] == [env["teacher"]]
        assert chat.can_message(db, env["noa"], env["teacher"]) is True
        assert chat.can_message(db, env["noa"], env["other"]) is False
        assert chat.can_message(db, env["outsider"], env["teacher"]) is False


def test_teacher_students(app, env):
    with _db(app) as db:
        students = chat.teacher_students(db, env["teacher"])
        assert {s.id for s in students} == {env["noa"], env["dan"]}


def test_send_and_thread_order(app, env):
    with _db(app) as db:
        chat.send(
            db, teacher_id=env["teacher"], student_id=env["noa"],
            sender_role=ROLE_STUDENT, body="שאלה ראשונה",
        )
        chat.send(
            db, teacher_id=env["teacher"], student_id=env["noa"],
            sender_role=ROLE_TEACHER, body="תשובה",
        )
        chat.send(
            db, teacher_id=env["teacher"], student_id=env["noa"],
            sender_role=ROLE_STUDENT, body="שאלה שנייה",
        )
        db.commit()
        rows = chat.thread(db, env["teacher"], env["noa"])
        assert [m.body for m in rows] == ["שאלה ראשונה", "תשובה", "שאלה שנייה"]


def test_send_validation(app, env):
    with _db(app) as db:
        with pytest.raises(chat.ChatError):
            chat.send(
                db, teacher_id=env["teacher"], student_id=env["noa"],
                sender_role=ROLE_STUDENT, body="   ",
            )
        with pytest.raises(chat.ChatError):
            chat.send(
                db, teacher_id=env["teacher"], student_id=env["noa"],
                sender_role=ROLE_STUDENT, body="x" * (chat.MAX_MESSAGE_CHARS + 1),
            )
        with pytest.raises(chat.ChatError):
            chat.send(
                db, teacher_id=env["teacher"], student_id=env["noa"],
                sender_role="admin", body="היי",
            )


def test_assignment_context(app, env):
    with _db(app) as db:
        ctx = chat.assignment_context(db, env["task"], env["noa"])
        assert ctx == {
            "teacher_id": env["teacher"],
            "assignment_id": env["task"],
            "label": env["task_title"],
        }
        # Not enrolled in that class -> no context (no leak, no redirect).
        assert chat.assignment_context(db, env["foreign_task"], env["noa"]) is None
        assert chat.assignment_context(db, 999999, env["noa"]) is None


def test_unread_and_mark_read(app, env):
    with _db(app) as db:
        for _ in range(2):
            chat.send(
                db, teacher_id=env["teacher"], student_id=env["noa"],
                sender_role=ROLE_STUDENT, body="היי",
            )
        db.commit()
        assert chat.unread_count(
            db, env["teacher"], env["noa"], reader_role=ROLE_TEACHER
        ) == 2
        assert chat.total_unread(db, env["teacher"], ROLE_TEACHER) == 2
        # The student's own messages are not unread for the student.
        assert chat.total_unread(db, env["noa"], ROLE_STUDENT) == 0
        marked = chat.mark_read(
            db, env["teacher"], env["noa"], reader_role=ROLE_TEACHER
        )
        db.commit()
        assert marked == 2
        assert chat.total_unread(db, env["teacher"], ROLE_TEACHER) == 0


def test_conversations(app, env):
    with _db(app) as db:
        # Before any message the student still sees their teacher; the teacher
        # inbox is empty until a student writes.
        student_rows = chat.conversations_for_student(db, env["noa"])
        assert [r["teacher_id"] for r in student_rows] == [env["teacher"]]
        assert student_rows[0]["has_messages"] is False
        assert chat.conversations_for_teacher(db, env["teacher"]) == []
        chat.send(
            db, teacher_id=env["teacher"], student_id=env["noa"],
            sender_role=ROLE_STUDENT, body="שלום המורה",
        )
        db.commit()
        inbox = chat.conversations_for_teacher(db, env["teacher"])
        assert [r["student_id"] for r in inbox] == [env["noa"]]
        assert inbox[0]["last_body"] == "שלום המורה"
        assert inbox[0]["unread"] == 1


def test_message_view_projection(app, env):
    with _db(app) as db:
        message = chat.send(
            db, teacher_id=env["teacher"], student_id=env["noa"],
            sender_role=ROLE_STUDENT, body="היי",
            assignment_id=env["task"], context_label=env["task_title"],
        )
        db.commit()
        student_view = chat.message_view(message, viewer_role=ROLE_STUDENT)
        teacher_view = chat.message_view(message, viewer_role=ROLE_TEACHER)
        assert student_view["mine"] is True
        assert teacher_view["mine"] is False
        assert student_view["context_label"] == env["task_title"]
        assert student_view["assignment_id"] == env["task"]
        assert student_view["created_at"]  # ISO instant for the browser
        assert student_view["created_local"]  # formatted for first render


# -------------------------------------------------------------------- web
def test_chat_requires_login(client):
    response = client.get("/student/chat", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_student_send_teacher_sees(app, client, new_client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    token = csrf(client, "/student/chat")
    response = client.post(
        "/student/chat/send",
        data={"csrf_token": token, "teacher_id": env["teacher"], "body": "שלום המורה"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True

    _login(new_client, "chat-mori", TEACHER_PASSWORD)
    page = new_client.get("/teacher/chat")
    assert page.status_code == 200
    assert "שלום המורה" in page.text
    assert "נועה" in page.text


def test_student_cannot_message_other_teacher(app, client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    token = csrf(client, "/student/chat")
    response = client.post(
        "/student/chat/send",
        data={"csrf_token": token, "teacher_id": env["other"], "body": "היי"},
    )
    assert response.status_code == 403
    assert response.json()["ok"] is False


def test_teacher_cannot_message_non_student(app, new_client, env):
    _login(new_client, "chat-mori", TEACHER_PASSWORD)
    token = csrf(new_client, "/teacher/chat")
    response = new_client.post(
        "/teacher/chat/send",
        data={"csrf_token": token, "student_id": env["outsider"], "body": "היי"},
    )
    assert response.status_code == 403
    assert response.json()["ok"] is False


def test_send_requires_csrf(app, client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    response = client.post(
        "/student/chat/send",
        data={"teacher_id": env["teacher"], "body": "בלי אסימון"},
    )
    # verify_csrf rejects a missing token with 400 (not 403).
    assert response.status_code == 400


def test_empty_message_refused(app, client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    token = csrf(client, "/student/chat")
    response = client.post(
        "/student/chat/send",
        data={"csrf_token": token, "teacher_id": env["teacher"], "body": "  "},
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_assignment_context_tagging(app, client, new_client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    token = csrf(client, "/student/chat")
    response = client.post(
        "/student/chat/send",
        data={
            "csrf_token": token,
            "teacher_id": env["teacher"],
            "assignment_id": str(env["task"]),
            "body": "שאלה על המשימה",
        },
    )
    assert response.status_code == 200, response.text
    message = response.json()["message"]
    assert message["assignment_id"] == env["task"]
    assert message["context_label"] == env["task_title"]

    _login(new_client, "chat-mori", TEACHER_PASSWORD)
    thread = new_client.get(f"/teacher/chat/thread?student={env['noa']}")
    assert thread.status_code == 200
    bodies = thread.json()["messages"]
    assert bodies[-1]["context_label"] == env["task_title"]


def test_forged_assignment_refused(app, client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    token = csrf(client, "/student/chat")
    response = client.post(
        "/student/chat/send",
        data={
            "csrf_token": token,
            "teacher_id": env["teacher"],
            "assignment_id": str(env["foreign_task"]),
            "body": "ניסיון זיוף",
        },
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False
    with _db(app) as db:
        assert db.query(Message).count() == 0


def test_thread_json_marks_read(app, client, new_client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    token = csrf(client, "/student/chat")
    client.post(
        "/student/chat/send",
        data={"csrf_token": token, "teacher_id": env["teacher"], "body": "היי"},
    )
    _login(new_client, "chat-mori", TEACHER_PASSWORD)
    before = new_client.get("/teacher/chat/unread").json()["unread"]
    assert before == 1
    thread = new_client.get(f"/teacher/chat/thread?student={env['noa']}")
    assert thread.status_code == 200
    payload = thread.json()
    assert payload["ok"] is True
    assert payload["messages"][-1]["body"] == "היי"
    assert payload["unread"] == 0
    assert new_client.get("/teacher/chat/unread").json()["unread"] == 0


def test_teacher_reply_reaches_student(app, client, new_client, env):
    _login(new_client, "chat-mori", TEACHER_PASSWORD)
    token = csrf(new_client, "/teacher/chat")
    response = new_client.post(
        "/teacher/chat/send",
        data={"csrf_token": token, "student_id": env["noa"], "body": "בהצלחה!"},
    )
    assert response.status_code == 200, response.text
    _login(client, "chat-noa", STUDENT_PASSWORD)
    thread = client.get(f"/student/chat/thread?teacher={env['teacher']}")
    assert thread.status_code == 200
    messages = thread.json()["messages"]
    assert messages[-1]["body"] == "בהצלחה!"
    assert messages[-1]["mine"] is False


def test_thread_forbidden_for_other_teacher(app, client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    response = client.get(f"/student/chat/thread?teacher={env['other']}")
    assert response.status_code == 403


def test_nav_badge_present(app, client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    page = client.get("/student")
    assert page.status_code == 200
    assert "data-chat-badge" in page.text
    assert "/student/chat" in page.text


def test_bubble_on_task_page(app, client, env):
    _login(client, "chat-noa", STUDENT_PASSWORD)
    page = client.get(f"/student/assignments/{env['task']}")
    assert page.status_code == 200, page.text[:500]
    assert "data-chat-bubble" in page.text
    assert f'data-assignment="{env["task"]}"' in page.text
    assert f'data-teacher="{env["teacher"]}"' in page.text


def test_assignment_context_hidden_exam(app, env):
    """A chat bubble on a hidden exam must not leak its title.

    The assignment page 404s for a student who never sat a hidden exam, but
    ``assignment_context`` only checked enrollment - so an enrolled student
    could POST the hidden exam's id and get the teacher plus the exam title
    back as a context label, learning that the exam exists. The context must
    follow the same door: no context until the marks are released *and* the
    student handed it in, then a context for exactly that child.
    """
    with _db(app) as db:
        exam = Assignment(
            class_id=env["class"],
            title="3.9 | מבחן מסכם סודי",
            instructions="",
            rubric_path="",
            rubric_sha256="",
            exam_config="units_1_3",
            published=False,
            exam_release="held",
        )
        db.add(exam)
        db.flush()
        exam_id = exam.id
        db.add(
            Submission(
                assignment_id=exam_id,
                student_id=env["noa"],
                text="print('hi')",
                revision=1,
                status="graded",
                first_submitted_at=utcnow(),
                submitted_at=utcnow(),
            )
        )
        db.commit()

    with _db(app) as db:
        # Hidden and held: nobody gets a context for it.
        assert chat.assignment_context(db, exam_id, env["noa"]) is None
        assert chat.assignment_context(db, exam_id, env["dan"]) is None
        # A published assignment is untouched.
        assert chat.assignment_context(db, env["task"], env["noa"]) is not None

        # Marks released: exactly the child who sat it gets a context.
        db.get(Assignment, exam_id).exam_release = "scores"
        db.commit()
    with _db(app) as db:
        ctx = chat.assignment_context(db, exam_id, env["noa"])
        assert ctx == {
            "teacher_id": env["teacher"],
            "assignment_id": exam_id,
            "label": "3.9 | מבחן מסכם סודי",
        }
        # Dan never sat it: still no context, no title.
        assert chat.assignment_context(db, exam_id, env["dan"]) is None


def test_chat_send_hidden_exam_context_refused(app, client, env):
    """The web door rejects a chat bubble on a hidden exam nobody sat."""
    with _db(app) as db:
        exam = Assignment(
            class_id=env["class"],
            title="3.9 | מבחן מסכם סודי",
            instructions="",
            rubric_path="",
            rubric_sha256="",
            exam_config="units_1_3",
            published=False,
            exam_release="held",
        )
        db.add(exam)
        db.commit()
        exam_id = exam.id

    _login(client, "chat-dan", STUDENT_PASSWORD)
    token = csrf(client, "/student/chat")
    response = client.post(
        "/student/chat/send",
        data={
            "csrf_token": token,
            "teacher_id": env["teacher"],
            "assignment_id": str(exam_id),
            "body": "מה זה המבחן הזה?",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["ok"] is False
    with _db(app) as db:
        assert db.query(Message).count() == 0