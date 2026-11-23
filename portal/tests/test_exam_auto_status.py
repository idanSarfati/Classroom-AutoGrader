"""Visibility of the automatic exam pass: run log, reasons and the activity screen.

Three layers are covered:

* the append-only run log (:class:`portal.models.ExamAutoCheckRun`) - one row
  per tick, per-paper details, pruning to ``RUN_KEEP``, idle runs included;
* the failure journal in :mod:`portal.exams.llm_review` - sanitised reasons
  (status + provider only, never a key or a URL) and their storage on a
  deferred paper;
* the teacher's read-only screen ``/teacher/exam-auto-check`` - access control,
  the pending/legacy/busy buckets, the dashboard badge, and the guarantee that
  looking at the screen changes nothing.

Like test_exam_auto_check, the model is faked (``exam_service.mark``) and forced
on, so no key, network or sandbox is involved.
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from portal.exams import auto_check, auto_status, checker, llm_review
from portal.exams import manual as exam_manual
from portal.exams import service as exam_service
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    Assignment,
    ClassGroup,
    Enrollment,
    ExamAutoCheckRun,
    ExamRecheckPreview,
    Grade,
    GradingJob,
    Submission,
    User,
)
from portal.tests.helpers import create_user, login
from portal.timeutil import utcnow

EXAM = "units_1_3"
PASSWORD = "AutoStatus123"
SCREEN = "/teacher/exam-auto-check"


# --------------------------------------------------------------------- reports
def _report(score: int, state: str) -> checker.ExamReport:
    question = checker.QuestionResult(
        key="c1",
        title="ג · שאלה פתוחה",
        section="ג",
        points=100,
        earned=score,
        status=(
            checker.CORRECT
            if score >= 100
            else checker.PARTIALLY_CORRECT
            if score > 0
            else checker.INCORRECT
        ),
        notes="נבדק במודל." if state == auto_check.REVIEWED else "נבדק אוטומטית.",
        given="תשובת התלמיד",
        expected="",
        llm_eligible=(state != auto_check.OFF),
        llm_reviewed=(state == auto_check.REVIEWED),
        llm_quota_blocked=(state == auto_check.WAITING),
        parts=[
            {
                "part": "explain",
                "label": "ההסבר",
                "points": 100,
                "earned": score,
                "note": "נבדק במודל." if state == auto_check.REVIEWED else "נבדק אוטומטית.",
                "needs_review": False,
            }
        ],
    )
    return checker.ExamReport(
        exam=EXAM,
        version="a",
        version_label="גרסה א'",
        score=score,
        maximum=100,
        questions=[question],
    )


def _fake_mark(report: checker.ExamReport):
    def _mark(db, assignment, submission, *, student_name, config=None, progress=None):
        return report

    return _mark


@pytest.fixture(autouse=True)
def model_on_and_clean_journal(monkeypatch):
    """Force the model on and isolate the failure journal per test."""
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    llm_review.clear_failures()
    yield
    llm_review.clear_failures()


# ---------------------------------------------------------------------- seeds
_SEED_TAG = 0


def _next_tag() -> int:
    global _SEED_TAG
    _SEED_TAG += 1
    return _SEED_TAG


def _assignment(db, teacher_id: int, tag: int, *, title: str) -> Assignment:
    group = ClassGroup(
        name=f"כיתת סטטוס {tag}", code=f"STAT{tag:02d}", teacher_id=teacher_id
    )
    db.add(group)
    db.flush()
    assignment = Assignment(
        class_id=group.id,
        title=title,
        instructions="ענו על כל השאלות.",
        rubric_path="assignments/exam_units_1_3_a.json",
        rubric_sha256="",
        exam_config=EXAM,
        published=True,
    )
    db.add(assignment)
    db.flush()
    return assignment


def _hand_in(
    db,
    assignment: Assignment,
    student_id: int,
    name: str,
    record: dict,
    *,
    signed: bool = False,
    meta: dict | None = None,
) -> int:
    """One graded hand-in carrying ``record``; returns the submission id.

    The account must already exist: creating it here would open a second
    session while this one holds a write transaction (SQLite locks).
    """
    db.add(Enrollment(class_id=assignment.class_id, student_id=student_id))
    now = utcnow()
    submission = Submission(
        assignment_id=assignment.id,
        student_id=student_id,
        text="{}",
        revision=1,
        status=SUBMISSION_GRADED,
        first_submitted_at=now,
        submitted_at=now,
    )
    db.add(submission)
    db.flush()
    grade = Grade(
        submission_id=submission.id,
        revision=1,
        configured_model="exam-checker",
        late_penalty_points=0,
    )
    exam_manual.write_record(grade, record)
    if signed:
        grade.reviewed_at = now
        grade.reviewed_by = 1
    if meta:
        stored = json.loads(grade.result_json or "{}")
        stored["auto_check"] = meta
        grade.result_json = json.dumps(stored, ensure_ascii=False)
    db.add(grade)
    db.flush()
    return submission.id


def _student(app, tag: int, index: int) -> tuple[int, str]:
    name = f"תלמיד {tag}-{index}"
    student_id = create_user(
        app,
        f"status-{tag}-{index}",
        PASSWORD,
        role=ROLE_STUDENT,
        display_name=name,
    )
    return student_id, name


def _legacy_record(score: int) -> dict:
    """An old stored result: exam block without ``llm_state``, no llm fields."""
    return {
        "student_name": "תלמיד",
        "exam": {
            "name": EXAM,
            "version": "a",
            "maximum": 100,
            "questions": [
                {
                    "key": "c1",
                    "title": "ג · שאלה פתוחה",
                    "points": 100,
                    "earned": score,
                    "status": checker.PARTIALLY_CORRECT,
                }
            ],
        },
    }


def _seed_one_waiting(app) -> tuple[int, int, str]:
    """One published exam assignment with a single waiting hand-in."""
    tag = _next_tag()
    teacher_id = create_user(app, f"status-mori-{tag}", PASSWORD, role=ROLE_TEACHER)
    student_id, name = _student(app, tag, 0)
    factory = app.state.session_factory
    with factory() as db:
        assignment = _assignment(db, teacher_id, tag, title="3.9 | מבחן מסכם")
        submission_id = _hand_in(
            db,
            assignment,
            student_id,
            name,
            _report(60, auto_check.WAITING).to_record("תלמיד"),
        )
        db.commit()
        return assignment.id, submission_id, name


def _runs(db) -> list[ExamAutoCheckRun]:
    return list(
        db.scalars(
            select(ExamAutoCheckRun).order_by(ExamAutoCheckRun.id)
        ).all()
    )


# --------------------------------------------------------------- the run log
def test_tick_records_a_run_row_with_per_paper_details(app, monkeypatch):
    _, submission_id, name = _seed_one_waiting(app)
    monkeypatch.setattr(
        exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)

    assert stats["written"] == 1
    with app.state.session_factory() as db:
        rows = _runs(db)
        assert len(rows) == 1
        run = rows[0]
        assert run.source == "scheduler"
        assert (run.selected, run.written, run.deferred, run.failed) == (1, 1, 0, 0)
        details = json.loads(run.detail_json)
        assert len(details) == 1
        assert details[0]["submission_id"] == submission_id
        assert details[0]["student"] == name
        assert details[0]["outcome"] == "written"
        assert details[0]["score"] == 90


def test_tick_records_an_idle_run_as_proof_of_life(app):
    tag = _next_tag()
    teacher_id = create_user(app, f"status-mori-{tag}", PASSWORD, role=ROLE_TEACHER)
    student_id, name = _student(app, tag, 0)
    with app.state.session_factory() as db:
        assignment = _assignment(db, teacher_id, tag, title="3.9 | מבחן מסכם")
        _hand_in(
            db,
            assignment,
            student_id,
            name,
            _report(80, auto_check.REVIEWED).to_record("תלמיד"),
        )
        db.commit()

    stats = auto_check.tick(app.state.session_factory, limit=4)

    assert stats == {"selected": 0, "written": 0, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        rows = _runs(db)
        assert len(rows) == 1
        assert rows[0].selected == 0
        assert json.loads(rows[0].detail_json) == []


def test_run_log_is_pruned_to_run_keep(app, monkeypatch):
    _seed_one_waiting(app)
    monkeypatch.setattr(auto_check, "RUN_KEEP", 3)
    monkeypatch.setattr(
        exam_service, "mark", _fake_mark(_report(60, auto_check.WAITING))
    )

    for _ in range(5):
        auto_check.tick(app.state.session_factory, limit=4)

    with app.state.session_factory() as db:
        rows = _runs(db)
        assert len(rows) == 3
        # The newest rows survive the prune; the oldest two are gone.
        assert [row.id for row in rows] == [3, 4, 5]


def test_cli_source_is_recorded(app, monkeypatch):
    _seed_one_waiting(app)
    monkeypatch.setattr(
        exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )
    auto_check.tick(app.state.session_factory, limit=4, source="cli")
    with app.state.session_factory() as db:
        assert _runs(db)[0].source == "cli"


# ------------------------------------------------- failure reasons (sanitised)
def _http_error(status: int, phrase: str, url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status} {phrase}' for url '{url}'",
        request=request,
        response=response,
    )


def test_failure_reason_keeps_the_status_but_never_the_url():
    url = "https://api.groq.com/openai/v1/chat/completions?key=gsk_SUPERSECRET"
    reason, endpoint = llm_review.failure_reason(_http_error(413, "Payload Too Large", url))
    assert reason == "HTTP 413 Payload Too Large"
    assert endpoint == url  # the caller may use the host for the provider label
    assert "gsk_SUPERSECRET" not in reason
    assert "https" not in reason


def test_failure_reason_handles_timeouts_and_strips_urls_from_other_errors():
    reason, _ = llm_review.failure_reason(httpx.ReadTimeout("read timed out"))
    assert reason == "request timed out"
    reason, _ = llm_review.failure_reason(
        ValueError("boom https://api.example.com/v1?key=SECRET happened")
    )
    assert "https" not in reason and "SECRET" not in reason
    assert "boom" in reason


def test_failure_journal_is_bounded_newest_first_and_labelled():
    for index in range(45):
        llm_review.note_failure(f"reason {index}", base_url="https://api.groq.com/openai/v1")
    entries = llm_review.recent_failures()
    assert len(entries) == 40
    assert entries[0]["reason"] == "reason 44"
    assert entries[0]["provider"] == "Groq"
    assert entries[-1]["reason"] == "reason 5"
    llm_review.note_failure("HTTP 401 Unauthorized", base_url="https://api.cloudflare.com/client/v4")
    assert llm_review.recent_failures()[0]["provider"] == "Cloudflare"


def test_a_deferred_paper_stores_the_sanitised_reason(app, monkeypatch):
    _, submission_id, _ = _seed_one_waiting(app)

    def _mark(db, assignment, submission, *, student_name, config=None, progress=None):
        llm_review.note_failure(
            "HTTP 413 Payload Too Large", base_url="https://api.groq.com/openai/v1"
        )
        llm_review.note_failure(
            "HTTP 401 Unauthorized", base_url="https://api.cloudflare.com/client/v4"
        )
        return _report(60, auto_check.WAITING)

    monkeypatch.setattr(exam_service, "mark", _mark)

    stats = auto_check.tick(app.state.session_factory, limit=4)

    assert stats["deferred"] == 1
    expected = "HTTP 413 Payload Too Large (Groq); HTTP 401 Unauthorized (Cloudflare)"
    with app.state.session_factory() as db:
        grade = db.scalar(select(Grade).where(Grade.submission_id == submission_id))
        meta = json.loads(grade.result_json)["auto_check"]
        assert meta["last_reason"] == expected
        assert meta["last_at"]
        assert "https" not in meta["last_reason"]
        run = _runs(db)[0]
        detail = json.loads(run.detail_json)[0]
        assert detail["outcome"] == "deferred"
        assert detail["reason"] == expected
        assert detail["attempts"] == 1
        assert "https" not in run.detail_json


# --------------------------------------------------------- Hebrew translation
def test_reason_hebrew_maps_known_failures_and_keeps_the_provider():
    raw = "HTTP 413 Payload Too Large (Groq); HTTP 401 Unauthorized (Cloudflare)"
    text = auto_status.reason_hebrew(raw)
    assert "הבקשה גדולה מדי לספק" in text
    assert "מפתח הגישה של הספק אינו תקף" in text
    assert "(Groq)" in text and "(Cloudflare)" in text
    assert "413" in text and "401" in text


def test_reason_hebrew_handles_quota_empty_and_unknown_reasons():
    assert "המכסה היומית" in auto_status.reason_hebrew("daily quota exhausted (Groq)")
    assert auto_status.reason_hebrew("") == ""
    assert auto_status.reason_hebrew("something odd (Groq)") == "something odd (Groq)"


# ------------------------------------------------------------- the live screen
def _seed_screen(app) -> dict:
    """One teacher, three exam assignments, every paper state represented.

    The second assignment carries a finished (``ready``) manual preview nobody
    applied, the third an actively ``running`` one - only the latter pauses the
    automatic pass. Also one regular (non-exam) assignment with a failed
    hand-in, so the re-enqueue bucket is represented too.
    """
    tag = _next_tag()
    teacher_id = create_user(app, f"status-mori-{tag}", PASSWORD, role=ROLE_TEACHER)
    # Accounts are created before the write session opens (SQLite locking).
    students = {index: _student(app, tag, index) for index in range(1, 9)}
    busy_student = _student(app, tag + 100, 1)
    regular_student = _student(app, tag + 200, 1)
    paused_student = _student(app, tag + 300, 1)
    factory = app.state.session_factory
    info: dict = {"teacher_username": f"status-mori-{tag}"}
    with factory() as db:
        first = _assignment(db, teacher_id, tag, title="3.9 | מבחן מסכם - יחידות 1-3")
        second = _assignment(db, teacher_id, tag + 100, title="3.9 | מבחן מסכם - כיתה ב")
        third = _assignment(db, teacher_id, tag + 300, title="3.9 | מבחן מסכם - כיתה ג")
        db.add(ExamRecheckPreview(assignment_id=second.id, state="ready"))
        db.add(ExamRecheckPreview(assignment_id=third.id, state="running"))
        waiting_id = _hand_in(
            db, first, *students[1], _report(60, auto_check.WAITING).to_record("תלמיד")
        )
        failed_id = _hand_in(
            db,
            first,
            *students[2],
            _report(55, auto_check.FAILED).to_record("תלמיד"),
            meta={
                "attempts": 2,
                "next_at": (utcnow() + timedelta(hours=2)).isoformat(timespec="seconds"),
                "last_reason": "HTTP 413 Payload Too Large (Groq); HTTP 401 Unauthorized (Cloudflare)",
            },
        )
        _hand_in(
            db, first, *students[3], _report(90, auto_check.REVIEWED).to_record("תלמיד")
        )
        _hand_in(
            db,
            first,
            *students[4],
            _report(60, auto_check.WAITING).to_record("תלמיד"),
            signed=True,
        )
        legacy_id = _hand_in(db, first, *students[5], _legacy_record(55))
        disqualified_record = _report(60, auto_check.WAITING).to_record("תלמיד")
        disqualified_record["disqualification"] = {"score": 0, "reason": "העתקה"}
        _hand_in(db, first, *students[6], disqualified_record)
        _hand_in(
            db, first, *students[7], _report(100, auto_check.OFF).to_record("תלמיד")
        )
        confirmed_off_id = _hand_in(
            db,
            first,
            *students[8],
            _report(100, auto_check.OFF).to_record("תלמיד"),
            meta={"off_confirmed": True},
        )
        busy_id = _hand_in(
            db, second, *busy_student, _report(60, auto_check.WAITING).to_record("תלמיד")
        )
        # Same bucket, but the manual check is running RIGHT NOW: the only
        # case that still pauses the automatic pass.
        paused_id = _hand_in(
            db, third, *paused_student, _report(60, auto_check.WAITING).to_record("תלמיד")
        )
        # A regular assignment whose grading failed: the pass re-enqueues it.
        regular_group = ClassGroup(
            name=f"כיתת מטלות {tag}", code=f"STAT{tag + 200:02d}", teacher_id=teacher_id
        )
        db.add(regular_group)
        db.flush()
        regular = Assignment(
            class_id=regular_group.id,
            title="2.3 | הגשת משימות",
            instructions="הגישו את הקוד.",
            rubric_path="assignments/lesson_2.json",
            rubric_sha256="",
            exam_config="",
            published=True,
        )
        db.add(regular)
        db.flush()
        db.add(Enrollment(class_id=regular_group.id, student_id=regular_student[0]))
        now = utcnow()
        regular_submission = Submission(
            assignment_id=regular.id,
            student_id=regular_student[0],
            text="print('hi')",
            revision=1,
            status=SUBMISSION_FAILED,
            first_submitted_at=now,
            submitted_at=now,
        )
        db.add(regular_submission)
        db.flush()
        db.add(
            GradingJob(
                submission_id=regular_submission.id,
                revision=1,
                state=SUBMISSION_FAILED,
                error="LLM API error: HTTP 503 Service Unavailable",
            )
        )
        db.commit()
        info.update(
            assignment_id=first.id,
            busy_assignment_id=second.id,
            paused_assignment_id=third.id,
            waiting=(waiting_id, students[1][1]),
            failed=(failed_id, students[2][1]),
            reviewed_name=students[3][1],
            signed_name=students[4][1],
            legacy=(legacy_id, students[5][1]),
            disqualified_name=students[6][1],
            off=(students[7][0], students[7][1]),
            confirmed_off=(confirmed_off_id, students[8][1]),
            open_preview=(busy_id, busy_student[1]),
            paused=(paused_id, paused_student[1]),
            regular=(regular_submission.id, regular_student[1]),
            regular_assignment_id=regular.id,
        )
    return info


def test_screen_is_teacher_only(app, client):
    info = _seed_screen(app)
    anonymous = client.get(SCREEN, follow_redirects=False)
    assert anonymous.status_code == 303
    assert "/login" in anonymous.headers["location"]

    student = create_user(app, "status-talmid", PASSWORD, role=ROLE_STUDENT)
    assert student
    login(client, "status-talmid", PASSWORD)
    assert client.get(SCREEN).status_code == 403


def test_screen_shows_every_bucket_and_stays_read_only(app, client):
    info = _seed_screen(app)
    login(client, info["teacher_username"], PASSWORD)
    factory = app.state.session_factory
    with factory() as db:
        before = {
            grade.submission_id: (grade.score, grade.result_json)
            for grade in db.scalars(select(Grade)).all()
        }
        runs_before = db.scalar(select(func.count(ExamAutoCheckRun.id)))

    response = client.get(SCREEN)
    assert response.status_code == 200
    html = response.text

    # The waiting and failed papers are listed with state, attempts and reason.
    assert info["waiting"][1] in html
    assert info["failed"][1] in html
    assert auto_status.STATE_LABELS["waiting"] in html
    assert auto_status.STATE_LABELS["failed"] in html
    assert "ניסיונות: 2" in html
    assert "הבקשה גדולה מדי לספק" in html
    assert "(Groq)" in html
    assert f'/teacher/submissions/{info["failed"][0]}' in html
    # A legacy paper is pending too, and the pass now queues it for its one
    # automatic model verdict instead of dropping it on the manual screen.
    assert info["legacy"][1] in html
    assert "רישום ישן" in html
    assert "תבדוק אותה במודל פעם אחת" in html
    # Only an ACTIVE manual run pauses a paper: the assignment with a ready
    # preview left open is listed normally, without the pause warning.
    assert info["open_preview"][1] in html
    assert f'/teacher/assignments/{info["busy_assignment_id"]}/llm-check' in html
    assert html.count("ריצה ידנית פעילה") == 1
    assert info["paused"][1] in html
    assert f'/teacher/assignments/{info["paused_assignment_id"]}/llm-check' in html
    # An unconfirmed "off" paper is pending until the pass re-marks it once:
    # the definition gained model questions after it was first marked.
    assert info["off"][1] in html
    assert "ממתין לבדיקת מודל" in html
    assert "לפני שהמחוון עודכן" in html
    # A confirmed-off paper is finished and stays out of the pending list.
    assert info["confirmed_off"][1] not in html
    # The failed regular assignment is listed for re-enqueue, with its reason.
    assert "מטלות רגילות שנכשלו וייבדקו שוב" in html
    assert info["regular"][1] in html
    assert f'/teacher/submissions/{info["regular"][0]}' in html
    assert "נכשל — ייבדק שוב" in html
    # Finished, signed-off and disqualified papers are not listed as pending.
    assert info["reviewed_name"] not in html
    assert info["signed_name"] not in html
    assert info["disqualified_name"] not in html

    # Looking at the screen changed nothing.
    with factory() as db:
        after = {
            grade.submission_id: (grade.score, grade.result_json)
            for grade in db.scalars(select(Grade)).all()
        }
        runs_after = db.scalar(select(func.count(ExamAutoCheckRun.id)))
    assert after == before
    assert runs_after == runs_before


def test_screen_shows_the_run_history_after_a_tick(app, client, monkeypatch):
    info = _seed_screen(app)
    monkeypatch.setattr(
        exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )
    auto_check.tick(app.state.session_factory, limit=5)
    login(client, info["teacher_username"], PASSWORD)

    response = client.get(SCREEN)
    assert response.status_code == 200
    html = response.text
    assert "יומן ריצות" in html
    assert "הושלם ונשמר" in html
    # Eligible exam papers come first and take the whole batch before any
    # homework: the unsigned waiting paper, the legacy paper the pass has never
    # seen, the unconfirmed-off paper (all on the unpaused assignment) and the
    # waiting paper behind the READY preview - an unapplied preview no longer
    # freezes its assignment. Only then the failed regular hand-in. The cooling
    # failed paper waits out its backoff and the paper on the RUNNING preview is
    # left for the manual check that is in progress.
    assert "נבחרו 5" in html
    assert "הושלמו 4" in html
    assert "נשלחו מחדש 1" in html
    assert "נשלח לבדיקה מחדש" in html


def test_screen_without_runs_says_so(app, client):
    info = _seed_screen(app)
    login(client, info["teacher_username"], PASSWORD)
    response = client.get(SCREEN)
    assert response.status_code == 200
    assert "עדיין אין ריצות מתועדות" in response.text


def test_dashboard_links_to_the_screen_with_a_pending_badge(app, client):
    info = _seed_screen(app)
    login(client, info["teacher_username"], PASSWORD)
    response = client.get("/teacher")
    assert response.status_code == 200
    assert 'href="/teacher/exam-auto-check"' in response.text
    # Unsigned waiting + failed exam papers (2 on the first assignment, 1 on
    # the ready-preview one, 1 on the running-preview one), the legacy paper
    # that still needs its first model verdict, the unconfirmed-off paper, and
    # the failed regular hand-in. Signed, reviewed, confirmed-off and
    # disqualified papers do not count.
    assert '<span class="count">7</span>' in response.text
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(ExamAutoCheckRun.id))) == 0
        user = db.scalar(select(User).where(User.username == info["teacher_username"]))
        assert auto_status.pending_total(db, user.id) == 7