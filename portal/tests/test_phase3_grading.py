"""Phase 3 tests: submissions, the grading queue, persistence and access rules.

The engine is never called. Worker tests inject a fake ``grade`` callable and
route tests replace the app's worker with a recorder, so the suite stays fast,
offline and deterministic while still exercising the real adapter contract
(``grade_submission`` keyword arguments, ``GradeArtifact.to_record``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from portal.grading import adapter, service
from portal.grading.worker import GradingWorker, reset_stale
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    GradingJob,
    Submission,
    User,
)
from portal.tests.helpers import create_user, csrf, login
from src.grade_audit import AuditFinding, AuditVerdict
from src.models import (
    DeductionItem,
    GradingResult,
    TaskEvaluation,
)

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"

RAW_TEXT = "x = 5\nprint(x)  # keep this spacing  \n"


def _db(app):
    return app.state.session_factory()


# --------------------------------------------------------------------- setup


def _seed(app, *, class_name: str = "כיתה ח׳ 2"):
    teacher_id = create_user(
        app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורי כהן"
    )
    student_id = create_user(
        app, "noa", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="נועה כהן"
    )
    binding = adapter.load_rubric("assignments/lesson_1.json")
    due = datetime(2026, 11, 1, 12, 0)
    with _db(app) as db:
        group = ClassGroup(name=class_name, code="ABC123", teacher_id=teacher_id)
        db.add(group)
        db.flush()
        db.add(Enrollment(class_id=group.id, student_id=student_id))
        assignment = Assignment(
            class_id=group.id,
            title="מטלה 1 – משתנים",
            instructions="כתבו קוד שקורא קלט ומדפיס.",
            rubric_path=str(binding.path),
            rubric_sha256=binding.sha256,
            due_at=due,
            published=True,
        )
        db.add(assignment)
        db.commit()
        return {
            "teacher": teacher_id,
            "student": student_id,
            "class": group.id,
            "assignment": assignment.id,
            "rubric_sha256": binding.sha256,
            "due": due,
        }


def _make_artifact(
    *,
    score: int = 88,
    student_name: str = "נועה כהן",
    needs_review: bool = False,
) -> adapter.GradeArtifact:
    findings = (
        [AuditFinding(flag="score_math", message="12 != 100 - 88", severity="warning")]
        if needs_review
        else []
    )
    audit = AuditVerdict(
        ok=not needs_review,
        autofixed=False,
        flags=[item.flag for item in findings],
        findings=findings,
        original_score=score,
        final_score=score,
        deductions_total=12,
    )
    result = GradingResult(
        student_name=student_name,
        score=score,
        feedback_hebrew="עבודה טובה! הקוד עובד, אבל שם המשתנה לא ברור.",
        deduction_breakdown=[
            DeductionItem(
                task_name="Task 1",
                points_deducted=12,
                reason="Used a cryptic variable name.",
            )
        ],
        task_evaluations=[
            TaskEvaluation(
                task_name="Task 1",
                student_answer_found="x = 5",
                status="PARTIALLY_CORRECT",
                notes="Works, but the variable name is unclear.",
            )
        ],
    )
    return adapter.GradeArtifact(
        result=result,
        audit=audit,
        rubric_sha256="deadbeef" * 8,
        rubric_title="Lesson 1",
        late_penalty_points=0,
        configured_model="fake-model",
        graded_at=datetime.now(timezone.utc),
    )


class RecordingWorker:
    """Stand-in for ``app.state.grading_worker`` in route tests."""

    def __init__(self) -> None:
        self.jobs: list[int] = []
        self.started = False

    def enqueue(self, job_id: int | None = None) -> None:
        if job_id is not None:
            self.jobs.append(int(job_id))

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        self.started = False


@pytest.fixture
def seeded(app, client):
    data = _seed(app)
    # The lifespan starts the real worker; stop it before any job exists so a
    # test can never reach Groq. RecordingWorker takes its place.
    app.state.grading_worker.stop()
    recorder = RecordingWorker()
    app.state.grading_worker = recorder
    data["worker"] = recorder
    return data


def _login_student(client, username: str = "noa") -> None:
    response = login(client, username, STUDENT_PASSWORD)
    assert response.status_code == 303, response.text


def _login_teacher(client, username: str = "mori") -> None:
    response = login(client, username, TEACHER_PASSWORD)
    assert response.status_code == 303, response.text


def _submit(
    client,
    assignment_id: int,
    text: str,
    *,
    json_response: bool = True,
):
    token = csrf(client, f"/student/assignments/{assignment_id}")
    headers = {"Accept": "application/json"} if json_response else {}
    return client.post(
        f"/student/assignments/{assignment_id}/submit",
        data={"csrf_token": token, "code": text},
        headers=headers,
        follow_redirects=False,
    )


def _current(app, submission_id: int) -> dict:
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        job = db.scalar(
            select(GradingJob)
            .where(GradingJob.submission_id == submission_id)
            .order_by(GradingJob.id.desc())
        )
        grade = db.scalar(
            select(Grade).where(Grade.submission_id == submission_id)
        )
        return {"submission": submission, "job": job, "grade": grade}


# ------------------------------------------------------------------ student


def test_student_submission_is_saved_verbatim_and_answered_202(
    app, seeded
) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        response = _submit(client, seeded["assignment"], RAW_TEXT)
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["accepted"] is True
        assert payload["queued"] is True
        assert payload["revision"] == 1
        assert payload["submission_id"]
        assert payload["job_id"]

        state = _current(app, payload["submission_id"])
        assert state["submission"].text == RAW_TEXT
        assert state["submission"].revision == 1
        assert state["submission"].status == "queued"
        assert state["submission"].first_submitted_at is not None
        assert state["job"].state == "queued"
        assert seeded["worker"].jobs == [payload["job_id"]]
    finally:
        client.close()


def test_submission_stays_open_after_the_due_date_with_ten_point_penalty(
    app, seeded
) -> None:
    """A missed deadline never closes the form; it only costs the late penalty."""
    client = TestClient(app)
    try:
        _login_student(client)
        page = client.get(f"/student/assignments/{seeded['assignment']}")
        assert page.status_code == 200
        assert f'action="/student/assignments/{seeded["assignment"]}/submit"' in page.text
        assert "אפשר להגיש גם אחרי תאריך היעד" in page.text
        assert "10 נקודות" in page.text

        response = _submit(client, seeded["assignment"], RAW_TEXT)
        assert response.status_code == 202, response.text
        submission_id = response.json()["submission_id"]

        with _db(app) as db:
            submission = db.get(Submission, submission_id)
            assert submission is not None
            # The seeded deadline is in the past, so this hand-in is late.
            assert submission.first_submitted_at > seeded["due"]
            points = adapter.late_penalty_points_for(
                student_id=str(seeded["student"]),
                student_name="נועה כהן",
                submission_id=f"portal-{submission.id}",
                due_at=seeded["due"],
                first_submitted_at=submission.first_submitted_at,
                submitted_at=submission.submitted_at,
            )
        assert points == 10
    finally:
        client.close()


def test_resubmission_keeps_first_submitted_at_and_creates_new_revision(
    app, seeded
) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        first = _submit(client, seeded["assignment"], RAW_TEXT).json()
        first_state = _current(app, first["submission_id"])
        first_time = first_state["submission"].first_submitted_at

        second_text = "x = 7\nprint(x)\n"
        second = _submit(client, seeded["assignment"], second_text).json()
        assert second["revision"] == 2
        assert second["queued"] is True
        assert second["job_id"] != first["job_id"]

        state = _current(app, second["submission_id"])
        assert state["submission"].text == second_text
        assert state["submission"].first_submitted_at == first_time
        assert state["submission"].submitted_at >= first_time
        assert state["submission"].status == "queued"
    finally:
        client.close()


def test_identical_resubmission_is_idempotent(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        first = _submit(client, seeded["assignment"], RAW_TEXT).json()
        again = _submit(client, seeded["assignment"], RAW_TEXT).json()
        assert again["revision"] == 1
        assert again["job_id"] == first["job_id"]
        with _db(app) as db:
            jobs = db.scalars(
                select(GradingJob).where(
                    GradingJob.submission_id == first["submission_id"]
                )
            ).all()
            assert len(jobs) == 1
    finally:
        client.close()


def test_submission_rejects_empty_and_oversized_text(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        empty = _submit(client, seeded["assignment"], "   \n")
        assert empty.status_code == 400
        assert "לא הוגש קוד" in empty.json()["error"]

        oversized = _submit(
            client, seeded["assignment"], "x" * (service.MAX_SUBMISSION_CHARS + 1)
        )
        assert oversized.status_code == 413
    finally:
        client.close()


def test_submission_requires_csrf(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        response = client.post(
            f"/student/assignments/{seeded['assignment']}/submit",
            data={"code": RAW_TEXT},
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 400
    finally:
        client.close()


def test_assignment_page_offers_the_editor_and_pending_state(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        page = client.get(f"/student/assignments/{seeded['assignment']}")
        assert page.status_code == 200
        assert 'name="answer_1"' in page.text
        assert 'name="answer_4"' in page.text
        assert 'data-answer-block' in page.text
        assert "טרם הוגש" in page.text

        submitted = _submit(client, seeded["assignment"], RAW_TEXT).json()
        page = client.get(f"/student/assignments/{seeded['assignment']}")
        assert 'data-grading-pending' in page.text
        assert "ממתין לבדיקה" in page.text
        assert RAW_TEXT.strip().splitlines()[0] in page.text

        result = client.get(f"/student/submissions/{submitted['submission_id']}")
        assert result.status_code == 200
        assert "ההגשה בבדיקה" in result.text
    finally:
        client.close()


def test_worksheet_answers_are_composed_and_prefilled(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        token = csrf(client, f"/student/assignments/{seeded['assignment']}")
        response = client.post(
            f"/student/assignments/{seeded['assignment']}/submit",
            data={
                "csrf_token": token,
                "answer_1": "print('a')",
                "answer_2": "print('b')",
                "answer_3": "   ",
                "answer_4": "print('d')",
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 202, response.text

        with _db(app) as db:
            submission = db.get(Submission, response.json()["submission_id"])
            text = submission.text

        # Every rubric item gets a heading, including the unanswered one, so
        # the grader can report it as missing instead of guessing.
        assert "## שאלה 1: 1 - פקודת הדפסה" in text
        assert "print('a')" in text
        assert "## שאלה 3: 3 - פקודת טאב" in text
        assert "## שאלה 4: 4 - משימה מסכמת" in text

        page = client.get(f"/student/assignments/{seeded['assignment']}")
        assert page.status_code == 200
        assert "print(&#39;a&#39;)" in page.text
        assert "print(&#39;d&#39;)" in page.text
        assert "נענו 3 מתוך 4" in page.text

        empty = client.post(
            f"/student/assignments/{seeded['assignment']}/submit",
            data={
                "csrf_token": csrf(
                    client, f"/student/assignments/{seeded['assignment']}"
                ),
                "answer_1": "  ",
                "answer_2": "",
                "answer_3": "\n",
                "answer_4": "",
            },
            headers={"Accept": "application/json"},
        )
        assert empty.status_code == 400
    finally:
        client.close()


def test_a_student_cannot_read_another_students_submission(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        mine = _submit(client, seeded["assignment"], RAW_TEXT).json()

        other_id = create_user(
            app, "dan", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="דן"
        )
        with _db(app) as db:
            db.add(Enrollment(class_id=seeded["class"], student_id=other_id))
            db.commit()
    finally:
        client.close()

    other = TestClient(app)
    try:
        _login_student(other, "dan")
        assert other.get(f"/student/submissions/{mine['submission_id']}").status_code == 404
        assert other.get(f"/student/assignments/{seeded['assignment']}").status_code == 200
    finally:
        other.close()


def test_result_page_shows_grade_feedback_and_tasks_without_rubric_data(
    app, seeded
) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        submission_id = _submit(client, seeded["assignment"], RAW_TEXT).json()[
            "submission_id"
        ]
        with _db(app) as db:
            job = db.scalar(select(GradingJob))
            worker = GradingWorker(
                app.state.session_factory,
                grade=lambda **kwargs: _make_artifact(),
                pause=lambda: None,
            )
            worker.process(job.id)

        page = client.get(f"/student/submissions/{submission_id}")
        assert page.status_code == 200
        assert "88" in page.text
        assert "עבודה טובה" in page.text
        assert "Task 1" in page.text
        assert "נכון חלקית" in page.text
        assert seeded["rubric_sha256"] not in page.text
        assert "rubric" not in page.text.lower()

        assignment_page = client.get(f"/student/assignments/{seeded['assignment']}")
        assert "88" in assignment_page.text
        assert seeded["rubric_sha256"] not in assignment_page.text
    finally:
        client.close()


# ------------------------------------------------------ grading-status probes
# The pending pages poll these tiny JSON endpoints instead of reloading the
# whole page on a timer (a blind reload jumps to the top and wipes code the
# student is typing). The contract: {"status", "pending", "revision"}, and the
# same visibility rules as the pages themselves.


def test_grading_status_probe_tracks_the_submission_lifecycle(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        assignment_id = seeded["assignment"]

        # Before any hand-in the probe answers "none", not an error.
        before = client.get(f"/student/assignments/{assignment_id}/grading-status")
        assert before.status_code == 200
        assert before.json() == {"status": "none", "pending": False, "revision": 0}

        submitted = _submit(client, assignment_id, RAW_TEXT).json()
        submission_id = submitted["submission_id"]

        queued = client.get(f"/student/assignments/{assignment_id}/grading-status")
        assert queued.status_code == 200
        assert queued.json() == {"status": "queued", "pending": True, "revision": 1}

        result_probe = client.get(
            f"/student/submissions/{submission_id}/grading-status"
        )
        assert result_probe.status_code == 200
        assert result_probe.json()["pending"] is True

        with _db(app) as db:
            job = db.scalar(select(GradingJob))
            worker = GradingWorker(
                app.state.session_factory,
                grade=lambda **kwargs: _make_artifact(),
                pause=lambda: None,
            )
            worker.process(job.id)

        done = client.get(f"/student/assignments/{assignment_id}/grading-status")
        assert done.json() == {"status": "graded", "pending": False, "revision": 1}
        done_result = client.get(
            f"/student/submissions/{submission_id}/grading-status"
        )
        assert done_result.json() == {
            "status": "graded",
            "pending": False,
            "revision": 1,
        }
    finally:
        client.close()


def test_grading_status_probes_enforce_ownership(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        mine = _submit(client, seeded["assignment"], RAW_TEXT).json()
        # A student never sees the teacher probe.
        assert (
            client.get(
                f"/teacher/submissions/{mine['submission_id']}/grading-status"
            ).status_code
            == 403
        )

        other_id = create_user(
            app, "dan", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="דן"
        )
        with _db(app) as db:
            db.add(Enrollment(class_id=seeded["class"], student_id=other_id))
            db.commit()
    finally:
        client.close()

    other = TestClient(app)
    try:
        _login_student(other, "dan")
        assert (
            other.get(
                f"/student/submissions/{mine['submission_id']}/grading-status"
            ).status_code
            == 404
        )
        # Same class, so the assignment probe works - but it reports dan's own
        # (missing) submission, never noa's.
        probe = other.get(
            f"/student/assignments/{seeded['assignment']}/grading-status"
        )
        assert probe.status_code == 200
        assert probe.json()["status"] == "none"
    finally:
        other.close()

    anonymous = TestClient(app)
    try:
        response = anonymous.get(
            f"/student/assignments/{seeded['assignment']}/grading-status",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "/login" in response.headers["location"]
    finally:
        anonymous.close()

    teacher = TestClient(app)
    try:
        _login_teacher(teacher)
        assert (
            teacher.get(
                f"/student/submissions/{mine['submission_id']}/grading-status"
            ).status_code
            == 404
        )
    finally:
        teacher.close()


def test_teacher_grading_status_probe(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        submission_id = _submit(client, seeded["assignment"], RAW_TEXT).json()[
            "submission_id"
        ]
    finally:
        client.close()

    teacher = TestClient(app)
    try:
        _login_teacher(teacher)
        probe = teacher.get(f"/teacher/submissions/{submission_id}/grading-status")
        assert probe.status_code == 200
        assert probe.json() == {"status": "queued", "pending": True, "revision": 1}
    finally:
        teacher.close()

    # A teacher who does not own the class cannot probe the hand-in.
    create_user(
        app,
        "other.teacher",
        TEACHER_PASSWORD,
        role=ROLE_TEACHER,
        display_name="מורה אחר",
    )
    stranger = TestClient(app)
    try:
        _login_teacher(stranger, "other.teacher")
        assert (
            stranger.get(
                f"/teacher/submissions/{submission_id}/grading-status"
            ).status_code
            == 404
        )
    finally:
        stranger.close()


def test_pending_pages_carry_the_probe_url(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        submission_id = _submit(client, seeded["assignment"], RAW_TEXT).json()[
            "submission_id"
        ]

        page = client.get(f"/student/assignments/{seeded['assignment']}")
        assert (
            'data-status-url="/student/assignments/'
            f'{seeded["assignment"]}/grading-status"' in page.text
        )
        result = client.get(f"/student/submissions/{submission_id}")
        assert (
            f'data-status-url="/student/submissions/{submission_id}/grading-status"'
            in result.text
        )
    finally:
        client.close()

    teacher = TestClient(app)
    try:
        _login_teacher(teacher)
        review = teacher.get(f"/teacher/submissions/{submission_id}")
        assert review.status_code == 200
        assert (
            f'data-status-url="/teacher/submissions/{submission_id}/grading-status"'
            in review.text
        )
    finally:
        teacher.close()


# ------------------------------------------------------------------- worker


def _seed_submission(app, data, text: str = RAW_TEXT) -> tuple[int, int]:
    with _db(app) as db:
        user = db.get(User, data["student"])
        assignment = db.get(Assignment, data["assignment"])
        outcome = service.submit_work(db, assignment, user, text)
        db.commit()
        return outcome.submission.id, outcome.job.id


def test_worker_persists_full_result_score_feedback_and_audit(app) -> None:
    data = _seed(app)
    submission_id, job_id = _seed_submission(app, data)
    calls: list[dict] = []

    def fake_grade(**kwargs):
        calls.append(kwargs)
        return _make_artifact(needs_review=True)

    worker = GradingWorker(
        app.state.session_factory, grade=fake_grade, pause=lambda: None
    )
    record = worker.process(job_id)

    assert record is not None
    assert calls and calls[0]["raw_doc_text"] == RAW_TEXT
    assert calls[0]["student_name"] == "נועה כהן"
    assert calls[0]["due_at"] == data["due"]

    state = _current(app, submission_id)
    grade = state["grade"]
    assert state["job"].state == "graded"
    assert state["submission"].status == "graded"
    assert grade is not None
    assert grade.score == 88
    assert grade.feedback_hebrew.startswith("עבודה טובה")
    assert grade.audit_needs_review is True
    assert grade.audit_summary
    assert "score_math" in grade.audit_json
    assert '"task_evaluations"' in grade.result_json
    assert "Task 1" in grade.deduction_json
    assert grade.rubric_sha256
    assert grade.configured_model == "fake-model"


def test_worker_paces_between_queued_jobs_only(app) -> None:
    data = _seed(app)
    _seed_submission(app, data, "a = 1\n")
    _seed_submission(app, data, "b = 2\n")
    pauses: list[int] = []
    worker = GradingWorker(
        app.state.session_factory,
        grade=lambda **kwargs: _make_artifact(),
        pause=lambda: pauses.append(1),
    )

    assert worker.process_next() is True
    assert pauses == [1]
    assert worker.process_next() is True
    assert pauses == [1]
    assert worker.process_next() is False


def test_daily_quota_defers_and_cools_down_without_failing(app) -> None:
    data = _seed(app)
    submission_id, job_id = _seed_submission(app, data)
    now = datetime(2026, 11, 8, 10, 0)
    calls: list[int] = []

    def spent(**kwargs):
        calls.append(1)
        raise adapter.DailyQuotaExhausted("every key is out for today")

    worker = GradingWorker(
        app.state.session_factory,
        grade=spent,
        pause=lambda: None,
        quota_defer_seconds=3600,
        clock=lambda: now,
    )
    worker.process(job_id)

    state = _current(app, submission_id)
    assert state["job"].state == "deferred"
    assert state["job"].next_attempt_at == now + timedelta(seconds=3600)
    assert state["submission"].status == "deferred"
    assert "מכסת המודל" in state["job"].error

    # While cooled down, even a queued job must not burn another request.
    assert worker.process_next() is False
    assert calls == [1]


def test_deferred_job_is_retried_after_the_cooldown(app) -> None:
    data = _seed(app)
    submission_id, job_id = _seed_submission(app, data)
    clock = {"now": datetime(2026, 11, 8, 10, 0)}
    calls: list[int] = []

    def flaky(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise adapter.DailyQuotaExhausted("spent")
        return _make_artifact()

    worker = GradingWorker(
        app.state.session_factory,
        grade=flaky,
        pause=lambda: None,
        quota_defer_seconds=60,
        clock=lambda: clock["now"],
    )
    worker.process(job_id)
    assert _current(app, submission_id)["job"].state == "deferred"

    clock["now"] += timedelta(seconds=61)
    assert worker.process_next() is True
    state = _current(app, submission_id)
    assert state["job"].state == "graded"
    assert state["submission"].status == "graded"
    assert len(calls) == 2


def test_persistent_429_is_deferred_not_failed(app) -> None:
    data = _seed(app)
    submission_id, job_id = _seed_submission(app, data)

    def rate_limited(**kwargs):
        raise adapter.LLMAPIError("too many requests", status_code=429)

    worker = GradingWorker(
        app.state.session_factory,
        grade=rate_limited,
        pause=lambda: None,
        rate_limit_defer_seconds=900,
    )
    worker.process(job_id)

    state = _current(app, submission_id)
    assert state["job"].state == "deferred"
    assert state["submission"].status == "deferred"
    assert "עמוס" in state["job"].error


def test_evaluation_failure_marks_the_job_and_submission_failed(app) -> None:
    data = _seed(app)
    submission_id, job_id = _seed_submission(app, data)

    def broken(**kwargs):
        raise adapter.LLMEvaluationError("no usable grading result")

    worker = GradingWorker(
        app.state.session_factory, grade=broken, pause=lambda: None
    )
    worker.process(job_id)

    state = _current(app, submission_id)
    assert state["job"].state == "failed"
    assert state["submission"].status == "failed"
    assert "הבדיקה נכשלה" in state["job"].error
    assert state["grade"] is None


def test_startup_resets_running_jobs_to_queued(app) -> None:
    data = _seed(app)
    submission_id, job_id = _seed_submission(app, data)
    with _db(app) as db:
        job = db.get(GradingJob, job_id)
        submission = db.get(Submission, submission_id)
        job.state = "grading"
        submission.status = "grading"
        db.commit()

    assert reset_stale(app.state.session_factory) == 1
    state = _current(app, submission_id)
    assert state["job"].state == "queued"
    assert state["submission"].status == "queued"


def test_worker_does_not_grade_a_superseded_revision(app) -> None:
    data = _seed(app)
    submission_id, first_job = _seed_submission(app, data)
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        user = db.get(User, data["student"])
        assignment = db.get(Assignment, data["assignment"])
        service.submit_work(db, assignment, user, "newer code\n")
        db.commit()
        assert submission.revision == 2

    calls: list[int] = []
    worker = GradingWorker(
        app.state.session_factory,
        grade=lambda **kwargs: calls.append(1) or _make_artifact(),
        pause=lambda: None,
    )
    worker.process(first_job)
    assert calls == []
    with _db(app) as db:
        stale = db.get(GradingJob, first_job)
        assert stale.state == "superseded"
        latest = db.scalars(
            select(GradingJob)
            .where(GradingJob.submission_id == submission_id)
            .order_by(GradingJob.id.desc())
        ).first()
        assert latest.state == "queued"
        assert latest.revision == 2


# ----------------------------------------------------- teacher overview/review


def _grade_one(app, submission_id: int, job_id: int, *, needs_review=False) -> None:
    worker = GradingWorker(
        app.state.session_factory,
        grade=lambda **kwargs: _make_artifact(needs_review=needs_review),
        pause=lambda: None,
    )
    worker.process(job_id)


def test_teacher_overview_and_assignment_roster(app, seeded) -> None:
    data = seeded
    client = TestClient(app)
    try:
        _login_student(client)
        submission_id = _submit(client, data["assignment"], RAW_TEXT).json()[
            "submission_id"
        ]
        with _db(app) as db:
            job = db.scalar(select(GradingJob))
        _grade_one(app, submission_id, job.id, needs_review=True)
    finally:
        client.close()

    teacher = TestClient(app)
    try:
        _login_teacher(teacher)
        overview = teacher.get("/teacher/grading")
        assert overview.status_code == 200
        assert "מרכז הבדיקות" in overview.text
        assert "מטלה 1 – משתנים" in overview.text
        assert "לסקירה 1" in overview.text

        class_overview = teacher.get(f"/teacher/classes/{data['class']}/grading")
        assert class_overview.status_code == 200

        roster = teacher.get(f"/teacher/assignments/{data['assignment']}/grading")
        assert roster.status_code == 200
        assert "נועה כהן" in roster.text
        assert "ציון 88" in roster.text
        assert "לסקירה" in roster.text
    finally:
        teacher.close()


def test_teacher_review_page_and_marking_reviewed(app, seeded) -> None:
    data = seeded
    student = TestClient(app)
    try:
        _login_student(student)
        submission_id = _submit(student, data["assignment"], RAW_TEXT).json()[
            "submission_id"
        ]
        with _db(app) as db:
            job = db.scalar(select(GradingJob))
        _grade_one(app, submission_id, job.id, needs_review=True)
    finally:
        student.close()

    teacher = TestClient(app)
    try:
        _login_teacher(teacher)
        page = teacher.get(f"/teacher/submissions/{submission_id}")
        assert page.status_code == 200
        assert RAW_TEXT.strip().splitlines()[0] in page.text
        assert "עבודה טובה" in page.text
        assert "score_math" in page.text
        assert "דורש סקירה" in page.text

        token = csrf(teacher, f"/teacher/submissions/{submission_id}")
        response = teacher.post(
            f"/teacher/submissions/{submission_id}/review",
            data={"csrf_token": token, "note": "נבדק, הציון תקין."},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with _db(app) as db:
            grade = db.scalar(select(Grade))
            assert grade.reviewed_at is not None
            assert grade.reviewed_by == data["teacher"]
            assert grade.review_note == "נבדק, הציון תקין."

        page = teacher.get(f"/teacher/submissions/{submission_id}")
        assert "נסקר" in page.text
    finally:
        teacher.close()


def test_regrade_queues_another_job_for_the_same_revision(app, seeded) -> None:
    data = seeded
    student = TestClient(app)
    try:
        _login_student(student)
        submission_id = _submit(student, data["assignment"], RAW_TEXT).json()[
            "submission_id"
        ]
        with _db(app) as db:
            job = db.scalar(select(GradingJob))
        _grade_one(app, submission_id, job.id)
    finally:
        student.close()

    teacher = TestClient(app)
    try:
        _login_teacher(teacher)
        token = csrf(teacher, f"/teacher/submissions/{submission_id}")
        response = teacher.post(
            f"/teacher/submissions/{submission_id}/regrade",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303

        with _db(app) as db:
            jobs = db.scalars(
                select(GradingJob)
                .where(GradingJob.submission_id == submission_id)
                .order_by(GradingJob.id)
            ).all()
            assert len(jobs) == 2
            assert jobs[1].revision == jobs[0].revision
            assert jobs[1].state == "queued"
        assert len(data["worker"].jobs) == 2
    finally:
        teacher.close()


def test_teacher_cannot_open_another_teachers_submission(app, seeded) -> None:
    data = seeded
    student = TestClient(app)
    try:
        _login_student(student)
        submission_id = _submit(student, data["assignment"], RAW_TEXT).json()[
            "submission_id"
        ]
    finally:
        student.close()

    create_user(
        app, "other", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורה אחר"
    )
    teacher = TestClient(app)
    try:
        _login_teacher(teacher, "other")
        assert teacher.get(f"/teacher/submissions/{submission_id}").status_code == 404
        assert teacher.get(f"/teacher/assignments/{data['assignment']}/grading").status_code == 404
    finally:
        teacher.close()


def test_student_cannot_open_teacher_grading_pages(app, seeded) -> None:
    client = TestClient(app)
    try:
        _login_student(client)
        assert client.get("/teacher/grading").status_code == 403
        assert (
            client.get(f"/teacher/classes/{seeded['class']}/grading").status_code
            == 403
        )
    finally:
        client.close()
