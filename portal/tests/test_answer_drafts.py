"""Server-side drafts: work a student typed but has not submitted yet.

A draft must never become a submission, must survive a reload, must lose to a
newer submission and must be removed once the student actually hands in.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from portal import drafts
from portal.grading import adapter
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    AnswerDraft,
    Assignment,
    ClassGroup,
    Enrollment,
    Submission,
)
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"


def _db(app):
    return app.state.session_factory()


class RecordingWorker:
    def __init__(self) -> None:
        self.jobs: list[int] = []

    def enqueue(self, job_id: int | None = None) -> None:
        if job_id is not None:
            self.jobs.append(int(job_id))

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 5.0) -> None:
        pass


# ------------------------------------------------------------------- fixtures


def _seed(app) -> dict:
    teacher_id = create_user(app, "dr-mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    student_id = create_user(app, "dr-noa", STUDENT_PASSWORD, role=ROLE_STUDENT)
    outsider_id = create_user(app, "dr-dan", STUDENT_PASSWORD, role=ROLE_STUDENT)
    lesson1 = adapter.load_rubric("assignments/lesson_1.json")
    lesson2 = adapter.load_rubric("assignments/lesson_2_worksheet.json")
    lesson3 = adapter.load_rubric("assignments/lesson_3_worksheet.json")
    with _db(app) as db:
        group = ClassGroup(name="כיתת טיוטות", code="DR01", teacher_id=teacher_id)
        db.add(group)
        db.flush()
        db.add(Enrollment(class_id=group.id, student_id=student_id))
        questions = Assignment(
            class_id=group.id,
            title="1.2 | תרגול",
            instructions="ענו על השאלות.",
            rubric_path=str(lesson1.path),
            rubric_sha256=lesson1.sha256,
            published=True,
        )
        worksheet2 = Assignment(
            class_id=group.id,
            title="2.3 | דף עבודה",
            instructions="ענו על דף העבודה.",
            rubric_path=str(lesson2.path),
            rubric_sha256=lesson2.sha256,
            published=True,
        )
        worksheet3 = Assignment(
            class_id=group.id,
            title="3.3 | דף עבודה",
            instructions="ענו על דף העבודה.",
            rubric_path=str(lesson3.path),
            rubric_sha256=lesson3.sha256,
            published=True,
        )
        db.add_all([questions, worksheet2, worksheet3])
        db.commit()
        return {
            "student": student_id,
            "outsider": outsider_id,
            "questions": questions.id,
            "worksheet2": worksheet2.id,
            "worksheet3": worksheet3.id,
        }


@pytest.fixture
def seeded(app, client):
    data = _seed(app)
    app.state.grading_worker.stop()
    app.state.grading_worker = RecordingWorker()
    return data


def _login(client, username: str) -> None:
    response = login(client, username, STUDENT_PASSWORD)
    assert response.status_code == 303, response.text


def _page(client, assignment_id: int) -> str:
    response = client.get(f"/student/assignments/{assignment_id}")
    assert response.status_code == 200, response.text[:500]
    return response.text


def _draft(client, assignment_id: int, fields: dict[str, str]):
    token = csrf(client, f"/student/assignments/{assignment_id}")
    return client.post(
        f"/student/assignments/{assignment_id}/draft",
        data={"csrf_token": token, **fields},
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )


def _submit(client, assignment_id: int, fields: dict[str, str]):
    token = csrf(client, f"/student/assignments/{assignment_id}")
    return client.post(
        f"/student/assignments/{assignment_id}/submit",
        data={"csrf_token": token, **fields},
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )


def _draft_row(app, assignment_id: int, student_id: int) -> AnswerDraft | None:
    with _db(app) as db:
        return db.scalar(
            select(AnswerDraft).where(
                AnswerDraft.assignment_id == assignment_id,
                AnswerDraft.student_id == student_id,
            )
        )


def _submissions(app, assignment_id: int, student_id: int) -> list[Submission]:
    with _db(app) as db:
        return list(
            db.scalars(
                select(Submission).where(
                    Submission.assignment_id == assignment_id,
                    Submission.student_id == student_id,
                )
            ).all()
        )


# -------------------------------------------------------------- worksheet drafts


def test_worksheet_draft_is_saved_and_restored(app, client, seeded) -> None:
    _login(client, "dr-noa")

    response = _draft(
        client,
        seeded["worksheet3"],
        {"answer_ex1__trace__r0__output": "DRAFT-TRACE-41", "answer_ex4__blanks__s0": "int"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["saved"] is True
    # Saving a draft is not handing in: no submission, no job, no grade.
    assert _submissions(app, seeded["worksheet3"], seeded["student"]) == []
    row = _draft_row(app, seeded["worksheet3"], seeded["student"])
    assert row is not None

    html = _page(client, seeded["worksheet3"])
    assert 'value="DRAFT-TRACE-41"' in html
    assert 'value="int"' in html
    assert "שחזרנו את הטיוטה ששמרת" in html
    assert "נענו 2 מתוך" in html


def test_submission_clears_the_draft(app, client, seeded) -> None:
    _login(client, "dr-noa")
    _draft(
        client,
        seeded["worksheet2"],
        {"answer_ex2__outputs__r0__output": "DRAFT-ONLY"},
    )

    response = _submit(
        client,
        seeded["worksheet2"],
        {"answer_ex2__outputs__r0__output": "FINAL-ANSWER"},
    )

    assert response.status_code == 202, response.text
    assert _draft_row(app, seeded["worksheet2"], seeded["student"]) is None
    html = _page(client, seeded["worksheet2"])
    assert "FINAL-ANSWER" in html
    assert "DRAFT-ONLY" not in html
    assert "שחזרנו את הטיוטה ששמרת" not in html


def test_newer_draft_wins_over_an_older_submission(app, client, seeded) -> None:
    _login(client, "dr-noa")
    response = _submit(
        client,
        seeded["worksheet3"],
        {"answer_ex1__trace__r0__output": "SUBMITTED-OLD"},
    )
    assert response.status_code == 202

    _draft(
        client,
        seeded["worksheet3"],
        {"answer_ex1__trace__r0__output": "DRAFT-NEWER"},
    )

    html = _page(client, seeded["worksheet3"])
    assert 'value="DRAFT-NEWER"' in html
    assert "SUBMITTED-OLD" not in html
    assert "שחזרנו את הטיוטה ששמרת" in html


def test_draft_older_than_the_submission_is_ignored(app, client, seeded) -> None:
    _login(client, "dr-noa")
    response = _submit(
        client,
        seeded["worksheet3"],
        {"answer_ex1__trace__r0__output": "SUBMITTED-WINS"},
    )
    assert response.status_code == 202
    submission = _submissions(app, seeded["worksheet3"], seeded["student"])[0]

    _draft(
        client,
        seeded["worksheet3"],
        {"answer_ex1__trace__r0__output": "STALE-DRAFT"},
    )
    with _db(app) as db:
        row = db.scalar(
            select(AnswerDraft).where(
                AnswerDraft.assignment_id == seeded["worksheet3"],
                AnswerDraft.student_id == seeded["student"],
            )
        )
        row.updated_at = submission.submitted_at - timedelta(minutes=5)
        db.commit()

    html = _page(client, seeded["worksheet3"])
    assert 'value="SUBMITTED-WINS"' in html
    assert "STALE-DRAFT" not in html
    assert "שחזרנו את הטיוטה ששמרת" not in html


def test_empty_form_clears_the_stored_draft(app, client, seeded) -> None:
    _login(client, "dr-noa")
    _draft(
        client,
        seeded["worksheet2"],
        {"answer_ex2__outputs__r0__output": "WILL-BE-CLEARED"},
    )

    response = _draft(client, seeded["worksheet2"], {})

    assert response.status_code == 200
    assert response.json() == {"saved": False, "cleared": True}
    assert _draft_row(app, seeded["worksheet2"], seeded["student"]) is None


# ------------------------------------------------------------- question drafts


def test_question_draft_round_trip(app, client, seeded) -> None:
    _login(client, "dr-noa")

    response = _draft(client, seeded["questions"], {"answer_1": "QB-DRAFT-7"})

    assert response.status_code == 200, response.text
    assert response.json()["saved"] is True
    html = _page(client, seeded["questions"])
    assert "QB-DRAFT-7" in html
    assert "שחזרנו את הטיוטה ששמרת" in html
    assert _submissions(app, seeded["questions"], seeded["student"]) == []

    submitted = _submit(client, seeded["questions"], {"answer_1": "QB-FINAL"})
    assert submitted.status_code == 202, submitted.text
    assert _draft_row(app, seeded["questions"], seeded["student"]) is None
    html = _page(client, seeded["questions"])
    assert "QB-FINAL" in html
    assert "QB-DRAFT-7" not in html


# ------------------------------------------------------------------- guardrails


def test_draft_requires_a_csrf_token(app, client, seeded) -> None:
    _login(client, "dr-noa")

    response = client.post(
        f"/student/assignments/{seeded['worksheet2']}/draft",
        data={"answer_ex2__outputs__r0__output": "NO-TOKEN"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code in (400, 403)
    assert _draft_row(app, seeded["worksheet2"], seeded["student"]) is None


def test_draft_is_limited_to_the_enrolled_student(app, client, seeded) -> None:
    _login(client, "dr-dan")

    token = csrf(client, "/student")
    response = client.post(
        f"/student/assignments/{seeded['worksheet2']}/draft",
        data={"csrf_token": token, "answer_ex2__outputs__r0__output": "OUTSIDER"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 404
    assert _draft_row(app, seeded["worksheet2"], seeded["outsider"]) is None


def test_teacher_cannot_create_a_student_draft(app, client, seeded) -> None:
    response = login(client, "dr-mori", TEACHER_PASSWORD)
    assert response.status_code == 303, response.text

    token = csrf(client, "/teacher")
    response = client.post(
        f"/student/assignments/{seeded['worksheet2']}/draft",
        data={"csrf_token": token, "answer_ex2__outputs__r0__output": "TEACHER"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 404


def test_oversized_draft_is_rejected(app, client, seeded, monkeypatch) -> None:
    _login(client, "dr-noa")
    monkeypatch.setattr(drafts, "MAX_DRAFT_BYTES", 40)

    response = _draft(
        client,
        seeded["worksheet2"],
        {"answer_ex3__code": "print('a very long answer' * 20)"},
    )

    assert response.status_code == 413
    assert "error" in response.json()
    assert _draft_row(app, seeded["worksheet2"], seeded["student"]) is None


# ----------------------------------------------------------------- unit level


def test_draft_store_round_trip_and_corrupt_payload(app, seeded) -> None:
    student_id = seeded["student"]
    assignment_id = seeded["worksheet2"]

    with _db(app) as db:
        saved = drafts.save(
            db, assignment_id, student_id, kind="code", values={"code": "x = 1"}
        )
        db.commit()
        assert saved.kind == "code"
        loaded = drafts.load(db, assignment_id, student_id)
        assert loaded is not None and loaded.values == {"code": "x = 1"}

        row = db.scalar(
            select(AnswerDraft).where(AnswerDraft.student_id == student_id)
        )
        row.payload = "{not json"
        db.commit()
        assert drafts.load(db, assignment_id, student_id) is None

        assert drafts.clear(db, assignment_id, student_id) is True
        db.commit()
        assert drafts.load(db, assignment_id, student_id) is None
        assert drafts.clear(db, assignment_id, student_id) is False
