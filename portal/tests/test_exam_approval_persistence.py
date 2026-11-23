"""A teacher sign-off ("סימון ההגשה כנבדקה") belongs to the hand-in.

Live bug (#180): the teacher approved exam papers, and days later the
"אושר על ידי המורה" badge was gone. Every automatic grade write - the bulk
model apply, the 3-hour auto-check tick, the deterministic recheck apply and
the grading worker's regrade - used to clear ``reviewed_at``/``reviewed_by``.
The policy these tests lock in:

* an automatic write on the same revision never clears the approval;
* the apply paths skip rows whose live grade is already signed off (protecting
  the stored score AND the badge), exactly like they skip disqualified rows;
* the auto-check re-checks the approval at write time, catching a sign-off that
  lands while the model is marking (selection happens minutes earlier);
* only a new student revision - a fresh ``Grade`` row - starts unapproved;
* the review screen projects the stored approval onto an unapplied preview, so
  an approved paper keeps its "נסקר" badge and note while a fresh verdict shows.

No key, network or real quota is involved: the fake-model fixtures from
``test_exam_llm_check`` and ``test_exam_auto_check`` are reused.
"""
from __future__ import annotations

import html
import json
from datetime import timedelta

import pytest

from portal.exams import auto_check, llm_check, recheck
from portal.exams import config as exam_config
from portal.exams import manual as exam_manual
from portal.grading import service as grading_service
from portal.grading.worker import GradingWorker
from portal.models import Assignment, ExamRecheckPreview, GradingJob, Submission
from portal.tests.helpers import login
from portal.timeutil import utcnow

from portal.tests.test_exam_llm_check import (  # noqa: F401  (env is a fixture)
    OLD_SCORE,
    TEACHER_PASSWORD,
    _db,
    _grade,
    _record,
    _report,
    _start,
    _wire,
    env,
)
from portal.tests.test_exam_auto_check import _seed
from portal.tests.test_phase3_grading import _make_artifact


@pytest.fixture(autouse=True)
def model_on(monkeypatch):
    """The auto-check pass only runs when the model is enabled; force it on."""
    monkeypatch.setattr(auto_check.llm_review, "enabled", lambda: True)


def _sign(env, submission_id: int, note: str = "") -> None:
    """The sign-off the review button performs, without going through the UI."""
    with _db(env["app"]) as db:
        grade = grading_service.grade_for_revision(db, submission_id, 1)
        grade.reviewed_at = utcnow()
        grade.reviewed_by = env["teacher"]
        grade.review_note = note
        db.commit()


def _stored(app, submission_id: int):
    """The revision-1 grade of an auto-check seed (always revision 1)."""
    with app.state.session_factory() as db:
        return grading_service.grade_for_revision(db, submission_id, 1)


def _worker(app) -> GradingWorker:
    return GradingWorker(
        app.state.session_factory,
        grade=lambda **kwargs: _make_artifact(),
        pause=lambda: None,
    )


def _job(env, submission_id: int, revision: int) -> int:
    """A real queued job, so the persist paths can close it (FK is enforced)."""
    with _db(env["app"]) as db:
        job = GradingJob(submission_id=submission_id, revision=revision)
        db.add(job)
        db.commit()
        return job.id


# ------------------------------------------------- auto-check: write-time guard
def test_auto_check_write_refuses_a_paper_signed_off_after_selection(app):
    """Selection skips reviewed papers, but the model call takes minutes: a
    sign-off can land after selection. The write itself must refuse it."""
    _, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)
    with app.state.session_factory() as db:
        grade = grading_service.grade_for_revision(db, submission_id, 1)
        grade.reviewed_at = utcnow()
        grade.reviewed_by = 1
        db.commit()
        submission = db.get(Submission, submission_id)
        written = auto_check._apply_result(
            db, submission, _record(90, auto_check.REVIEWED)
        )
        db.commit()
    assert written is False
    grade = _stored(app, submission_id)
    assert grade.score == 60  # the stored score is untouched
    assert grade.reviewed_at is not None


def test_tick_keeps_a_sign_off_that_lands_while_the_model_marks(app, monkeypatch):
    _, (submission_id,) = _seed(app, n=1, state=auto_check.WAITING)

    def mark_and_sign(db, assignment, submission, *, student_name, config=None, progress=None):
        # The teacher signs the paper off while the model call is in flight.
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        grade.reviewed_at = utcnow()
        grade.reviewed_by = 1
        db.commit()
        return _report(90, auto_check.REVIEWED)

    monkeypatch.setattr(auto_check.exam_service, "mark", mark_and_sign)
    stats = auto_check.tick(app.state.session_factory, limit=4)
    assert stats == {"selected": 1, "written": 0, "deferred": 0, "failed": 1}
    grade = _stored(app, submission_id)
    assert grade.score == 60
    assert grade.reviewed_at is not None


# ------------------------------------------- model apply: stale approved rows
def test_apply_drops_a_row_signed_off_after_the_preview(env, monkeypatch):
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED), s1: (85, llm_check.REVIEWED)})
    preview_id = _start(env, [s0, s1])
    llm_check.run(env["app"].state.session_factory, preview_id)
    _sign(env, s0)  # the teacher approves after the preview, before the apply

    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 1
    signed = _grade(env, s0)
    assert signed.score == OLD_SCORE  # the stored score is protected...
    assert signed.reviewed_at is not None  # ...and so is the badge
    assert _grade(env, s1).score == 85  # the other row applies as usual
    with _db(env["app"]) as db:
        # Nothing is left to retry: the stale approved row was dropped.
        assert db.get(ExamRecheckPreview, preview_id) is None


# ------------------------------------- deterministic recheck: approved + locked
def _recheck_preview(env, scores: dict[int, int]) -> int:
    """A ready deterministic preview built from controlled records."""
    with _db(env["app"]) as db:
        assignment = db.get(Assignment, env["assignment"])
        config = exam_config.for_assignment(assignment)
        preview = ExamRecheckPreview(
            assignment_id=assignment.id,
            created_by=env["teacher"],
            state=recheck.READY,
            total=len(scores),
            done=len(scores),
            fingerprint=recheck.fingerprint(config),
            finished_at=utcnow(),
        )
        preview.payload_json = json.dumps(
            {
                "generated_at": utcnow().isoformat(timespec="seconds"),
                "rows": [
                    {
                        "submission_id": sid,
                        "revision": 1,
                        "error": "",
                        "record": _record(score, llm_check.OFF),
                    }
                    for sid, score in scores.items()
                ]
            },
            ensure_ascii=False,
        )
        db.add(preview)
        db.commit()
        return preview.id


def test_recheck_apply_skips_signed_and_disqualified_papers(env):
    s0, s1, s2, s3 = env["submissions"]
    _sign(env, s0)
    with _db(env["app"]) as db:
        grade = grading_service.grade_for_revision(db, s3, 1)
        exam_manual.disqualify(
            grade, score=0, reason="העתקה", teacher_id=env["teacher"]
        )
        db.commit()
    preview_id = _recheck_preview(env, {s0: 90, s1: 80, s2: 70, s3: 95})

    with _db(env["app"]) as db:
        applied = recheck.apply_preview(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 2
    signed = _grade(env, s0)
    assert signed.score == OLD_SCORE
    assert signed.reviewed_at is not None
    assert _grade(env, s1).score == 80
    assert _grade(env, s2).score == 70
    locked = _grade(env, s3)
    assert locked.score == 0
    assert json.loads(locked.result_json)["disqualification"]["score"] == 0


def test_recheck_apply_drops_a_row_rewritten_after_the_preview(env):
    """A ready deterministic preview left open must not regress a newer
    automatic verdict written after the preview was generated."""
    s0, s1 = env["submissions"][:2]
    preview_id = _recheck_preview(env, {s0: 90, s1: 80})
    # Age the preview (in real life it sits open for days), then the automatic
    # pass re-marks s1 with a newer verdict. The untouched grade on s0 predates
    # the preview, exactly like a real hand-in graded before the check ran.
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        data = json.loads(preview.payload_json)
        data["generated_at"] = (
            utcnow() - timedelta(seconds=60)
        ).isoformat(timespec="seconds")
        preview.payload_json = json.dumps(data, ensure_ascii=False)
        older = grading_service.grade_for_revision(db, s0, 1)
        older.graded_at = utcnow() - timedelta(seconds=120)
        grade = grading_service.grade_for_revision(db, s1, 1)
        exam_manual.write_record(grade, _record(95, llm_check.REVIEWED))
        grade.graded_at = utcnow()
        db.commit()

    with _db(env["app"]) as db:
        applied = recheck.apply_preview(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 1
    assert _grade(env, s0).score == 90
    assert _grade(env, s1).score == 95  # the newer verdict survives
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


# ----------------------------------------------- grading worker: both persists
def test_exam_remark_keeps_the_approval(env):
    s0 = env["submissions"][0]
    _sign(env, s0, note="נבדק ידנית.")
    report = _report(90, llm_check.REVIEWED)
    snapshot = {
        "job_id": _job(env, s0, 1),
        "submission_id": s0,
        "revision": 1,
        "student_name": "noa",
        "rubric_sha256": "",
    }
    _worker(env["app"])._persist_exam_grade(snapshot, report, report.to_record("noa"))
    grade = _grade(env, s0)
    assert grade.score == 90  # the fresh verdict is written...
    assert grade.reviewed_at is not None  # ...but the sign-off survives
    assert grade.review_note == "נבדק ידנית."


def test_regular_regrade_keeps_the_approval(env):
    s0 = env["submissions"][0]
    _sign(env, s0)
    snapshot = {
        "job_id": _job(env, s0, 1),
        "submission_id": s0,
        "revision": 1,
        "student_name": "noa",
    }
    record = {
        "score": 88,
        "feedback_hebrew": "עבודה טובה",
        "deduction_breakdown": [],
        "rubric_sha256": "",
        "rubric_title": "",
        "late_penalty_points": 0,
        "configured_model": "fake-model",
    }
    _worker(env["app"])._persist_grade(snapshot, _make_artifact(score=88), record)
    grade = _grade(env, s0)
    assert grade.score == 88
    assert grade.reviewed_at is not None


def test_a_new_revision_starts_unapproved(env):
    s0 = env["submissions"][0]
    _sign(env, s0)
    with _db(env["app"]) as db:
        submission = db.get(Submission, s0)
        submission.revision = 2
        db.commit()
    snapshot = {
        "job_id": _job(env, s0, 2),
        "submission_id": s0,
        "revision": 2,
        "student_name": "noa",
    }
    record = {
        "score": 75,
        "feedback_hebrew": "",
        "deduction_breakdown": [],
        "rubric_sha256": "",
        "rubric_title": "",
        "late_penalty_points": 0,
        "configured_model": "fake-model",
    }
    _worker(env["app"])._persist_grade(snapshot, _make_artifact(score=75), record)
    with _db(env["app"]) as db:
        old = grading_service.grade_for_revision(db, s0, 1)
        new = grading_service.grade_for_revision(db, s0, 2)
    assert old.reviewed_at is not None  # the old approval stays on revision 1
    assert new.reviewed_at is None  # the fresh hand-in starts unapproved
    assert new.score == 75


# --------------------------------------- review screen: approval over preview
def test_review_page_shows_the_stored_approval_over_the_preview(client, app, env, monkeypatch):
    """While an unapplied model preview is on display, the review card must
    still show the stored "נסקר" badge and the teacher's note - the approval
    lives on the grade, not on the preview."""
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    _sign(env, s0, note="בדקתי ידנית, הציון תקין.")

    login(client, "llm-mori", TEACHER_PASSWORD)
    text = html.unescape(client.get(f"/teacher/submissions/{s0}").text)
    assert "נסקר" in text
    assert "טרם נסקר" not in text
    assert "בדקתי ידנית, הציון תקין." in text
    # The fresh, unapplied verdict is still what the page displays.
    assert "זהו הציון של בדיקת המודל האחרונה, שטרם הוחלה על ההגשה." in text
    assert _grade(env, s0).score == OLD_SCORE  # nothing was written