"""The automatic model pass.

:mod:`portal.exams.auto_check` re-marks a few exam hand-ins whose stored
model verdict is still ``waiting``/``failed`` (or ``off`` but never confirmed) -
exams and quizzes before regular assignments, oldest first within each group -
writes the finished result into the ``Grade`` row - the same write the manual
apply performs - and re-enqueues failed regular assignments for the grading
worker. These tests drive it with a fake ``exam_service.mark`` returning
controlled reports, so no key, network or sandbox is involved (the same
technique as test_exam_llm_check).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from portal.exams import auto_check
from portal.exams import checker
from portal.exams import manual as exam_manual
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    SUBMISSION_QUEUED,
    Assignment,
    ClassGroup,
    Enrollment,
    ExamAutoCheckRun,
    ExamRecheckPreview,
    Grade,
    GradingJob,
    Submission,
)
from portal.tests.helpers import create_user
from portal.timeutil import utcnow

EXAM = "units_1_3"
PASSWORD = "AutoCheck123"


# --------------------------------------------------------------------- reports
def _report(score: int, state: str) -> checker.ExamReport:
    """One open-item paper whose model flags produce exactly ``state``."""
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


def _record(score: int, state: str) -> dict:
    return _report(score, state).to_record("תלמיד")


def _stored_state(grade: Grade) -> str:
    result = json.loads(grade.result_json or "{}")
    return str((result.get("exam") or {}).get("llm_state") or "")


# ---------------------------------------------------------------------- seeds
_SEED_TAG = 0


def _seed(app, n: int = 1, *, state: str = auto_check.WAITING, published: bool = True):
    """One exam assignment with ``n`` graded hand-ins in ``state``.

    ``published=False`` is the between-sittings state: the paper is hidden from
    the class on purpose, while the hand-ins already made still await a mark.
    """
    global _SEED_TAG
    _SEED_TAG += 1
    tag = _SEED_TAG
    teacher_id = create_user(
        app, f"auto-mori-{tag}", PASSWORD, role=ROLE_TEACHER
    )
    student_ids = [
        create_user(
            app,
            f"auto-student-{tag}-{index}",
            PASSWORD,
            role=ROLE_STUDENT,
            display_name=f"תלמיד {tag}-{index}",
        )
        for index in range(n)
    ]
    now = utcnow()
    factory = app.state.session_factory
    with factory() as db:
        group = ClassGroup(
            name="כיתת אוטומציה", code=f"AUTO{tag:02d}", teacher_id=teacher_id
        )
        db.add(group)
        db.flush()
        for student_id in student_ids:
            db.add(Enrollment(class_id=group.id, student_id=student_id))
        assignment = Assignment(
            class_id=group.id,
            title="3.9 | מבחן מסכם - יחידות 1-3",
            instructions="ענו על כל השאלות.",
            rubric_path="assignments/exam_units_1_3_a.json",
            rubric_sha256="",
            exam_config=EXAM,
            published=published,
        )
        db.add(assignment)
        db.flush()
        submissions = []
        for index, student_id in enumerate(student_ids):
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
            exam_manual.write_record(grade, _record(60, state))
            db.add(grade)
            submissions.append(submission)
        db.commit()
        return assignment.id, [submission.id for submission in submissions]


def _grade(db, submission_id: int) -> Grade:
    return db.scalar(select(Grade).where(Grade.submission_id == submission_id))


def _one_waiting(app, assignment_id: int) -> int:
    """A single hand-in in the waiting state on ``assignment_id``."""
    global _SEED_TAG
    _SEED_TAG += 1
    factory = app.state.session_factory
    with factory() as db:
        submission = Submission(
            assignment_id=assignment_id,
            student_id=create_user(
                app, f"auto-wait-{_SEED_TAG}", PASSWORD, role=ROLE_STUDENT
            ),
            text="{}",
            revision=1,
            status=SUBMISSION_GRADED,
            first_submitted_at=utcnow(),
            submitted_at=utcnow(),
        )
        db.add(submission)
        db.flush()
        grade = Grade(
            submission_id=submission.id,
            revision=1,
            configured_model="exam-checker",
            late_penalty_points=0,
        )
        exam_manual.write_record(grade, _record(60, auto_check.WAITING))
        db.add(grade)
        db.commit()
        return submission.id


@pytest.fixture(autouse=True)
def model_on(monkeypatch):
    """The pass only runs when the model is enabled; force it on."""
    monkeypatch.setattr(auto_check.llm_review, "enabled", lambda: True)


def _fake_mark(report: checker.ExamReport):
    """Monkeypatch ``exam_service.mark`` to return one controlled report."""

    def _mark(db, assignment, submission, *, student_name, config=None, progress=None):
        return report

    return _mark


# ---------------------------------------------------------------------- tests
def test_tick_marks_and_writes_a_waiting_paper(app, monkeypatch):
    assignment_id, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)

    assert stats == {"selected": 1, "written": 1, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        assert grade.score == 90
        assert _stored_state(grade) == auto_check.REVIEWED
        assert grade.configured_model == "exam-checker"


def test_tick_retries_but_never_rewrites_a_still_waiting_paper(app, monkeypatch):
    assignment_id, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    monkeypatch.setattr(
        auto_check.exam_service,
        "mark",
        _fake_mark(_report(60, auto_check.WAITING)),
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)

    assert stats == {"selected": 1, "written": 0, "deferred": 1, "failed": 0}
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        assert grade.score == 60
        assert _stored_state(grade) == auto_check.WAITING


def test_tick_only_selects_waiting_and_failed_papers(app, monkeypatch):
    assignment_id, submission_ids = _seed(app, n=4, state=auto_check.REVIEWED)
    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats["selected"] == 0 and stats["written"] == 0

    waiting_id = _one_waiting(app, assignment_id)
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(80, auto_check.REVIEWED))
    )
    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats == {"selected": 1, "written": 1, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        assert _stored_state(_grade(db, waiting_id)) == auto_check.REVIEWED


def test_tick_remarks_an_off_paper_once_and_confirms_it(app, monkeypatch):
    """An ``off`` paper earns one re-mark after a definition change; when it
    stays ``off`` the pass confirms it and never touches it again."""
    _, (submission_id,) = _seed(app, n=1, state=auto_check.OFF)
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(60, auto_check.OFF))
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats == {"selected": 1, "written": 0, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        assert grade.score == 60  # the stored deterministic score is untouched
        assert _stored_state(grade) == auto_check.OFF
        meta = json.loads(grade.result_json or "{}").get("auto_check") or {}
        assert meta.get("off_confirmed") is True

    second = auto_check.tick(app.state.session_factory, limit=4)
    assert second["selected"] == 0 and second["written"] == 0


def test_tick_sends_an_off_paper_to_the_model_when_it_now_has_open_questions(
    app, monkeypatch
):
    _, (submission_id,) = _seed(app, n=1, state=auto_check.OFF)
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats == {"selected": 1, "written": 1, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        assert grade.score == 90
        assert _stored_state(grade) == auto_check.REVIEWED


def test_tick_respects_the_batch_limit_oldest_first(app, monkeypatch):
    _, submission_ids = _seed(app, n=4, state=auto_check.FAILED)
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(70, auto_check.REVIEWED))
    )
    stats = auto_check.tick(app.state.session_factory, limit=2)
    assert stats == {"selected": 2, "written": 2, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        stored = [_stored_state(_grade(db, sid)) for sid in submission_ids]
    assert stored.count(auto_check.REVIEWED) == 2
    assert stored.count(auto_check.FAILED) == 2


def test_tick_skips_a_teacher_reviewed_paper(app, monkeypatch):
    assignment_id, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        grade.reviewed_at = utcnow()
        grade.reviewed_by = 1
        db.commit()

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats["selected"] == 0 and stats["written"] == 0


def test_tick_skips_a_disqualified_paper(app, monkeypatch):
    assignment_id, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        grade.reviewed_at = None
        grade.reviewed_by = None
        result = json.loads(grade.result_json or "{}")
        result["disqualification"] = {"score": 0, "reason": "העתקה"}
        grade.result_json = json.dumps(result, ensure_ascii=False)
        db.commit()

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats["selected"] == 0 and stats["written"] == 0


def test_tick_skips_an_assignment_with_a_running_preview(app, monkeypatch):
    assignment_id, _ = _seed(app, n=1, state=auto_check.WAITING)
    with app.state.session_factory() as db:
        db.add(ExamRecheckPreview(assignment_id=assignment_id, state="running"))
        db.commit()

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats["selected"] == 0 and stats["written"] == 0


def test_tick_checks_papers_behind_a_ready_preview(app, monkeypatch):
    """A ready preview left open must not freeze the assignment for days: the
    pass re-marks its papers, and the apply buttons drop stale rows instead."""
    assignment_id, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    with app.state.session_factory() as db:
        db.add(ExamRecheckPreview(assignment_id=assignment_id, state="ready"))
        db.commit()
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats == {"selected": 1, "written": 1, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        assert grade.score == 90
        assert _stored_state(grade) == auto_check.REVIEWED


def test_tick_marks_a_legacy_paper_without_a_model_state(app, monkeypatch):
    """Hand-ins graded before the model pass existed carry no ``llm_state``;
    they must still earn a model pass instead of being skipped forever."""
    assignment_id, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        result = json.loads(grade.result_json or "{}")
        result.pop("exam", None)  # a legacy record: no model state at all
        grade.result_json = json.dumps(result, ensure_ascii=False)
        db.commit()
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats == {"selected": 1, "written": 1, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        assert grade.score == 90
        assert _stored_state(grade) == auto_check.REVIEWED

    # Once marked, the paper is final and never re-selected.
    second = auto_check.tick(app.state.session_factory, limit=4)
    assert second["selected"] == 0 and second["written"] == 0


def test_legacy_paper_staying_off_is_confirmed_once(app, monkeypatch):
    """A legacy paper with nothing open for the model is confirmed ``off``
    after one re-mark, so the pass does not retry it every tick."""
    _, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        result = json.loads(grade.result_json or "{}")
        result.pop("exam", None)
        grade.result_json = json.dumps(result, ensure_ascii=False)
        db.commit()
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(60, auto_check.OFF))
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats == {"selected": 1, "written": 0, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        meta = json.loads(grade.result_json or "{}").get("auto_check") or {}
        assert meta.get("off_confirmed") is True

    second = auto_check.tick(app.state.session_factory, limit=4)
    assert second["selected"] == 0 and second["written"] == 0


def test_tick_is_a_noop_when_the_model_is_off(app, monkeypatch):
    assignment_id, _ = _seed(app, n=1, state=auto_check.WAITING)
    monkeypatch.setattr(auto_check.llm_review, "enabled", lambda: False)
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )

    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats == {"selected": 0, "written": 0, "deferred": 0, "failed": 0}


def test_scheduler_runs_a_tick_and_stops(app, monkeypatch):
    import time

    calls: list[int] = []
    monkeypatch.setattr(
        auto_check,
        "tick",
        lambda factory, *, limit: calls.append(limit)
        or {"selected": 0, "written": 0, "deferred": 0, "failed": 0},
    )
    scheduler = auto_check.ExamAutoCheckScheduler(
        app.state.session_factory,
        interval_seconds=60,
        startup_delay_seconds=0,
        batch_size=3,
    )
    scheduler.start()
    deadline = time.monotonic() + 3
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    scheduler.stop()
    assert calls and calls[-1] == 3


def _set_cooldown(db, submission_id: int, hours_from_now: float):
    """Stamp an ``auto_check`` next_at on a hand-in, for cooldown tests."""
    from datetime import timedelta

    from portal.timeutil import utcnow as _utcnow

    grade = _grade(db, submission_id)
    result = json.loads(grade.result_json or "{}")
    result["auto_check"] = {
        "attempts": 1,
        "next_at": (_utcnow() + timedelta(hours=hours_from_now)).isoformat(
            timespec="seconds"
        ),
    }
    grade.result_json = json.dumps(result, ensure_ascii=False)
    db.commit()


def test_a_deferred_paper_is_not_reselected_during_cooldown(app, monkeypatch):
    assignment_id, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    monkeypatch.setattr(
        auto_check.exam_service,
        "mark",
        _fake_mark(_report(60, auto_check.WAITING)),
    )

    first = auto_check.tick(app.state.session_factory, limit=4)
    # Deferred: the backoff metadata is now on the grade.
    assert first["deferred"] == 1
    with app.state.session_factory() as db:
        grade = _grade(db, submission_id)
        meta = json.loads(grade.result_json or "{}").get("auto_check") or {}
        assert meta.get("attempts") == 1
        assert meta.get("next_at")

    # The very next tick (still within the cooldown) must not pick it again.
    second = auto_check.tick(app.state.session_factory, limit=4)
    assert second["selected"] == 0 and second["written"] == 0


def test_cooldown_only_skips_a_future_next_at(app, monkeypatch):
    assignment_id, (submission_id,) = _seed(
        app, n=1, state=auto_check.WAITING
    )
    factory = app.state.session_factory
    # A past next_at means the paper is due again -> selected.
    with factory() as db:
        _set_cooldown(db, submission_id, hours_from_now=-1)
    assert auto_check.candidate_papers(factory(), limit=4)

    # A future next_at hides the paper from the pass.
    with factory() as db:
        _set_cooldown(db, submission_id, hours_from_now=2)
    assert auto_check.candidate_papers(factory(), limit=4) == []


def test_a_hidden_exam_still_gets_its_papers_checked(app) -> None:
    """Hiding the paper between sittings must not strand the marks.

    This is the half of the feature that has no screen: the teacher unpublishes
    an exam so the next class cannot open it, and the papers already handed in
    still need the model pass before their scores can be released.
    """
    _seed(app, n=2, state=auto_check.WAITING, published=False)
    factory = app.state.session_factory
    assert len(auto_check.candidate_papers(factory(), limit=4)) == 2


# ------------------------------------------------- failed regular assignments
def _seed_failed_assignment(app, n: int = 1, *, status: str = SUBMISSION_FAILED):
    """One published regular (non-exam) assignment with ``n`` failed hand-ins."""
    global _SEED_TAG
    _SEED_TAG += 1
    tag = _SEED_TAG
    teacher_id = create_user(
        app, f"auto-mori-{tag}", PASSWORD, role=ROLE_TEACHER
    )
    # Accounts are created before the write session opens (SQLite locking).
    student_ids = [
        create_user(
            app,
            f"auto-regular-{tag}-{index}",
            PASSWORD,
            role=ROLE_STUDENT,
            display_name=f"תלמיד רגיל {tag}-{index}",
        )
        for index in range(n)
    ]
    now = utcnow()
    factory = app.state.session_factory
    with factory() as db:
        group = ClassGroup(
            name="כיתת אוטומציה", code=f"AUTO{tag:02d}", teacher_id=teacher_id
        )
        db.add(group)
        db.flush()
        assignment = Assignment(
            class_id=group.id,
            title="2.3 | הגשת משימות",
            instructions="הגישו את הקוד.",
            rubric_path="assignments/lesson_2.json",
            rubric_sha256="",
            exam_config="",
            published=True,
        )
        db.add(assignment)
        db.flush()
        submission_ids = []
        for student_id in student_ids:
            db.add(Enrollment(class_id=group.id, student_id=student_id))
            submission = Submission(
                assignment_id=assignment.id,
                student_id=student_id,
                text="print('hi')",
                revision=1,
                status=status,
                first_submitted_at=now,
                submitted_at=now,
            )
            db.add(submission)
            db.flush()
            db.add(
                GradingJob(
                    submission_id=submission.id,
                    revision=1,
                    state=status,
                    error="LLM API error: HTTP 503",
                )
            )
            submission_ids.append(submission.id)
        db.commit()
        return assignment.id, submission_ids


def test_tick_requeues_a_failed_regular_assignment(app):
    _, (submission_id,) = _seed_failed_assignment(app)

    stats = auto_check.tick(app.state.session_factory, limit=5)

    assert stats == {"selected": 1, "written": 0, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == SUBMISSION_QUEUED
        jobs = db.scalars(
            select(GradingJob).where(GradingJob.submission_id == submission_id)
        ).all()
        assert any(job.state == SUBMISSION_QUEUED for job in jobs)
        run = db.scalar(
            select(ExamAutoCheckRun).order_by(ExamAutoCheckRun.id.desc())
        )
        details = json.loads(run.detail_json or "[]")
        assert details[0]["outcome"] == "requeued"
        assert details[0]["submission_id"] == submission_id


def test_tick_never_requeues_a_graded_regular_assignment(app):
    _seed_failed_assignment(app, status=SUBMISSION_GRADED)
    stats = auto_check.tick(app.state.session_factory, limit=5)
    assert stats["selected"] == 0


def test_tick_prefers_exams_over_older_regular_assignments(app, monkeypatch):
    """Exams/quizzes win the slot even when a regular hand-in is older: a model
    verdict on an exam paper matters more than re-queueing homework."""
    from datetime import timedelta

    _, (exam_submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    _, (regular_submission_id,) = _seed_failed_assignment(app)
    with app.state.session_factory() as db:
        # The regular hand-in is older than the exam paper.
        regular = db.get(Submission, regular_submission_id)
        regular.submitted_at = utcnow() - timedelta(days=2)
        db.commit()
    monkeypatch.setattr(
        auto_check.exam_service, "mark", _fake_mark(_report(90, auto_check.REVIEWED))
    )

    stats = auto_check.tick(app.state.session_factory, limit=1)
    assert stats == {"selected": 1, "written": 1, "deferred": 0, "failed": 0}
    with app.state.session_factory() as db:
        assert _stored_state(_grade(db, exam_submission_id)) == auto_check.REVIEWED
        # The older regular hand-in waited its turn behind the exam paper.
        assert db.get(Submission, regular_submission_id).status == SUBMISSION_FAILED

    # The next tick takes the regular assignment that waited.
    stats = auto_check.tick(app.state.session_factory, limit=1)
    assert stats["selected"] == 1
    with app.state.session_factory() as db:
        assert db.get(Submission, regular_submission_id).status == SUBMISSION_QUEUED



