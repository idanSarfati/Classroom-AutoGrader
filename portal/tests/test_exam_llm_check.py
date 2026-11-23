"""The unified hand-in check: pick papers, succeed / wait / retry, apply.

The teacher does not re-grade the whole class by default. They choose a few exam
hand-ins (or all), the deterministic checker always runs, and - when the model is
on - it also judges the open parts. Every paper ends in one of four states:
``reviewed`` (model judged it; applicable), ``off`` (a finished deterministic-only
result; also applicable), ``waiting`` (daily quota ran out - retry later) or
``failed`` (a non-quota error - retry later). ``reviewed`` and ``off`` papers are
written into the grades; ``waiting``/``failed`` stay in the preview so the teacher
can retry them once the quota resets - exactly like the old classroom evaluator
where some submissions succeeded and some waited for another run.

These tests drive the production module (:mod:`portal.exams.llm_check`) with a
fake ``exam_service.mark`` that returns controlled reports, so no key, network,
sandbox or real quota is involved. The real exam definition and answer key are
used for the fingerprint, and the real manual-override and grade-writing paths
are exercised end to end.
"""
from __future__ import annotations

import html
import json
from datetime import timedelta

import pytest

from portal.exams import checker
from portal.exams import config as exam_config
from portal.exams import llm_check
from portal.exams import manual as exam_manual
from portal.exams import recheck
from portal.grading import service as grading_service
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    SUBMISSION_GRADED,
    Assignment,
    ClassGroup,
    Enrollment,
    ExamRecheckPreview,
    Grade,
    Submission,
)
from portal.tests.helpers import create_user, csrf, login
from portal.timeutil import utcnow

TEACHER_PASSWORD = "LlmTeacher123"
STUDENT_PASSWORD = "LlmStudent123"
EXAM = "units_1_3"
STUDENTS = ("llm-noa", "llm-dan", "llm-maya", "llm-eden")
OLD_SCORE = 60


def _db(app):
    return app.state.session_factory()


# --------------------------------------------------------------------- reports
def _report(score: int, state: str, *, key: str = "c1", points: int = 100):
    """One open-item paper whose model flags produce exactly ``state``."""
    question = checker.QuestionResult(
        key=key,
        title="ג · שאלה פתוחה",
        section="ג",
        points=points,
        earned=score,
        status=(
            checker.CORRECT
            if score >= points
            else checker.PARTIALLY_CORRECT
            if score > 0
            else checker.INCORRECT
        ),
        notes="נבדק במודל." if state == llm_check.REVIEWED else "נבדק אוטומטית.",
        given="תשובת התלמיד",
        expected="",
        llm_eligible=(state != llm_check.OFF),
        llm_reviewed=(state == llm_check.REVIEWED),
        llm_quota_blocked=(state == llm_check.WAITING),
        parts=[
            {
                "part": "explain",
                "label": "ההסבר",
                "points": points,
                "earned": score,
                "note": "נבדק במודל." if state == llm_check.REVIEWED else "נבדק אוטומטית.",
                "needs_review": False,
            }
        ],
    )
    return checker.ExamReport(
        exam=EXAM,
        version="a",
        version_label="גרסה א'",
        score=score,
        maximum=points,
        questions=[question],
    )


def _record(score: int, state: str, *, key: str = "c1", points: int = 100):
    return _report(score, state, key=key, points=points).to_record("תלמיד")


def _old_grade(db, submission_id: int, score: int, *, manual_points=None, teacher_id=None):
    """A deterministic grade already on the hand-in, optionally hand-adjusted."""
    grade = Grade(
        submission_id=submission_id,
        revision=1,
        configured_model="exam-checker",
        late_penalty_points=0,
    )
    exam_manual.write_record(grade, _record(score, llm_check.OFF))
    db.add(grade)
    db.flush()
    if manual_points is not None:
        exam_manual.adjust(grade, "c1", points=manual_points, teacher_id=teacher_id)
    return grade


# -------------------------------------------------------------------- fixtures
@pytest.fixture
def env(app):
    """A teacher, a class, a real exam assignment and four checked hand-ins."""
    exam_config.clear_cache()
    teacher_id = create_user(app, "llm-mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    student_ids = [
        create_user(
            app,
            name,
            STUDENT_PASSWORD,
            role=ROLE_STUDENT,
            display_name=name.replace("llm-", ""),
        )
        for name in STUDENTS
    ]
    config = exam_config.load_exam(EXAM)
    assert config is not None
    first = config.versions[0]
    now = utcnow()
    with _db(app) as db:
        group = ClassGroup(name="כיתת מודל", code="LLM01", teacher_id=teacher_id)
        db.add(group)
        db.flush()
        for student_id in student_ids:
            db.add(Enrollment(class_id=group.id, student_id=student_id))
        assignment = Assignment(
            class_id=group.id,
            title="3.9 | מבחן מסכם - יחידות 1-3",
            instructions="ענו על כל השאלות.",
            rubric_path=str(first.rubric_path),
            rubric_sha256="",
            exam_config=EXAM,
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            published=True,
        )
        db.add(assignment)
        db.flush()
        submission_ids = []
        for student_id in student_ids:
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
            _old_grade(db, submission.id, OLD_SCORE)
            submission_ids.append(submission.id)
        db.commit()
        return {
            "app": app,
            "teacher": teacher_id,
            "students": student_ids,
            "class": group.id,
            "assignment": assignment.id,
            "submissions": submission_ids,
        }


def _wire(monkeypatch, plan: dict):
    """Turn the model on, silence pacing, and replay ``plan`` from ``mark``.

    ``plan`` maps submission_id -> (score, state) or an Exception to raise. The
    returned list records which hand-ins were actually marked, so a test can
    prove only the selected papers ran.
    """
    calls: list[int] = []
    monkeypatch.setattr(llm_check.llm_review, "enabled", lambda: True)
    monkeypatch.setattr(llm_check.llm_review, "pace", lambda *a, **k: None)
    monkeypatch.setattr(llm_check.llm_review, "routing_status", lambda: "fake pool")

    def fake_mark(db, assignment, submission, *, student_name, config=None, progress=None):
        calls.append(submission.id)
        spec = plan[submission.id]
        if isinstance(spec, Exception):
            raise spec
        score, state = spec
        return _report(score, state)

    monkeypatch.setattr(llm_check.exam_service, "mark", fake_mark)
    return calls


def _assignment(env):
    with _db(env["app"]) as db:
        return db.get(Assignment, env["assignment"])


def _start(env, ids, *, teacher_id=None):
    with _db(env["app"]) as db:
        preview = llm_check.start(
            db, db.get(Assignment, env["assignment"]), ids,
            teacher_id=teacher_id if teacher_id is not None else env["teacher"],
        )
        return preview.id


def _grade(env, submission_id):
    with _db(env["app"]) as db:
        submission = db.get(Submission, submission_id)
        return grading_service.grade_for_revision(db, submission.id, submission.revision)


def _preview_row(env, preview_id):
    with _db(env["app"]) as db:
        return db.get(ExamRecheckPreview, preview_id)


# ----------------------------------------------------------------------- start
def test_model_off_runs_deterministically_and_applies(env, monkeypatch):
    """With the model off the unified screen still checks and applies the rules.

    ``start`` must not refuse a disabled model: the deterministic checker is a
    finished result on its own, so the selected papers are marked ``off`` and
    applied like any reviewed paper.
    """
    s0, s1 = env["submissions"][:2]
    calls: list[int] = []
    monkeypatch.setattr(llm_check.llm_review, "enabled", lambda: False)
    monkeypatch.setattr(llm_check.llm_review, "pace", lambda *a, **k: None)

    def fake_mark(db, assignment, submission, *, student_name, config=None, progress=None):
        calls.append(submission.id)
        return _report(95 if submission.id == s0 else 80, llm_check.OFF)

    monkeypatch.setattr(llm_check.exam_service, "mark", fake_mark)
    preview_id = _start(env, [s0, s1])  # no raise even though the model is off
    assert llm_check.run(env["app"].state.session_factory, preview_id) == llm_check.READY
    assert set(calls) == {s0, s1}
    with _db(env["app"]) as db:
        rows = {
            row["submission_id"]: row
            for row in llm_check.payload(db.get(ExamRecheckPreview, preview_id))["rows"]
        }
    assert rows[s0]["llm_state"] == llm_check.OFF
    assert rows[s0]["new_score"] == 95
    assert rows[s1]["new_score"] == 80
    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 2
    assert _grade(env, s0).score == 95
    assert _grade(env, s1).score == 80
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


def test_start_requires_a_real_selection(env, monkeypatch):
    _wire(monkeypatch, {})
    with _db(env["app"]) as db:
        assignment = db.get(Assignment, env["assignment"])
        with pytest.raises(llm_check.LLMCheckError, match="לא נבחרו"):
            llm_check.start(db, assignment, [], teacher_id=env["teacher"])
        # An id that is not a checked hand-in of this exam is not a selection.
        with pytest.raises(llm_check.LLMCheckError, match="לא נבחרו"):
            llm_check.start(db, assignment, [999999], teacher_id=env["teacher"])


def test_start_refuses_an_ordinary_assignment(env, monkeypatch):
    _wire(monkeypatch, {})
    with _db(env["app"]) as db:
        ordinary = Assignment(
            class_id=env["class"],
            title="שיעור רגיל",
            instructions="",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="",
            exam_config="",
            published=True,
        )
        db.add(ordinary)
        db.commit()
        with pytest.raises(llm_check.LLMCheckError, match="אינה מבחן"):
            llm_check.start(db, ordinary, [env["submissions"][0]], teacher_id=env["teacher"])


# ------------------------------------------------------------------------- run
def test_run_marks_only_the_selected_papers(env, monkeypatch):
    s0, s1, s2, s3 = env["submissions"]
    plan = {s0: (90, llm_check.REVIEWED), s1: (OLD_SCORE, llm_check.WAITING)}
    calls = _wire(monkeypatch, plan)
    preview_id = _start(env, [s0, s1])
    assert llm_check.run(env["app"].state.session_factory, preview_id) == llm_check.READY

    # Only the two chosen hand-ins were marked; the other two were untouched.
    assert len(calls) == 2 and set(calls) == {s0, s1}
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        data = llm_check.payload(preview)
        assert data is not None
        rows = {row["submission_id"]: row for row in data["rows"]}
        assert set(rows) == {s0, s1}
        assert rows[s0]["llm_state"] == llm_check.REVIEWED
        assert rows[s0]["new_score"] == 90
        assert rows[s0]["old_score"] == OLD_SCORE
        assert rows[s1]["llm_state"] == llm_check.WAITING
        assert preview.state == llm_check.READY
        assert preview.total == 2
    # A run never writes a grade: both papers keep their old score.
    assert _grade(env, s0).score == OLD_SCORE
    assert _grade(env, s1).score == OLD_SCORE


def test_run_records_a_non_quota_failure_as_retryable(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: RuntimeError("network dropped")})
    preview_id = _start(env, [s0])
    assert llm_check.run(env["app"].state.session_factory, preview_id) == llm_check.READY
    with _db(env["app"]) as db:
        row = llm_check.payload(db.get(ExamRecheckPreview, preview_id))["rows"][0]
        assert row["llm_state"] == llm_check.LLM_FAILED
        assert "network dropped" in row["error"]
        assert row["new_score"] is None


# ----------------------------------------------------------------------- apply
def test_apply_writes_only_reviewed_and_keeps_waiting(env, monkeypatch):
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED), s1: (OLD_SCORE, llm_check.WAITING)})
    preview_id = _start(env, [s0, s1])
    llm_check.run(env["app"].state.session_factory, preview_id)

    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 1

    reviewed = _grade(env, s0)
    assert reviewed.score == 90
    # The exam marker survives so the review screen keeps per-question parts.
    assert reviewed.configured_model == "exam-checker"
    assert grading_service.grade_view(reviewed)["is_exam"] is True
    assert json.loads(reviewed.result_json)["exam"]["llm_state"] == llm_check.REVIEWED

    waiting = _grade(env, s1)
    assert waiting.score == OLD_SCORE  # untouched

    # The preview survives with only the waiting paper, ready for a retry.
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        assert preview is not None and preview.state == llm_check.READY
        rows = llm_check.payload(preview)["rows"]
        assert [row["submission_id"] for row in rows] == [s1]
        assert rows[0]["llm_state"] == llm_check.WAITING


def test_apply_writes_off_papers_and_deletes_an_empty_preview(env, monkeypatch):
    s0, s3 = env["submissions"][0], env["submissions"][3]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED), s3: (50, llm_check.OFF)})
    preview_id = _start(env, [s0, s3])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 2
    assert _grade(env, s0).score == 90
    assert _grade(env, s3).score == 50  # a deterministic-only result is applied
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


def test_apply_all_reviewed_deletes_the_preview(env, monkeypatch):
    s0, s2 = env["submissions"][0], env["submissions"][2]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED), s2: (85, llm_check.REVIEWED)})
    preview_id = _start(env, [s0, s2])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 2
    assert _grade(env, s0).score == 90
    assert _grade(env, s2).score == 85
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


def test_apply_drops_a_row_rewritten_after_the_preview(env, monkeypatch):
    """The automatic pass may re-mark a paper while a ready preview is left
    open. Applying the old preview must not regress the newer verdict."""
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED), s1: (85, llm_check.REVIEWED)})
    preview_id = _start(env, [s0, s1])
    llm_check.run(env["app"].state.session_factory, preview_id)

    # Age the preview (in real life it sits open for days), then the automatic
    # pass writes a newer verdict on s0. The untouched grade on s1 predates the
    # preview, exactly like a real hand-in graded before the check ran.
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        data = llm_check.payload(preview)
        data["generated_at"] = (
            utcnow() - timedelta(seconds=60)
        ).isoformat(timespec="seconds")
        preview.payload_json = json.dumps(data, ensure_ascii=False)
        older = grading_service.grade_for_revision(db, s1, 1)
        older.graded_at = utcnow() - timedelta(seconds=120)
        grade = grading_service.grade_for_revision(db, s0, 1)
        exam_manual.write_record(grade, _record(95, llm_check.REVIEWED))
        grade.graded_at = utcnow()
        db.commit()

    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 1
    assert _grade(env, s0).score == 95  # the newer automatic verdict survives
    assert _grade(env, s1).score == 85
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


def test_apply_keeps_the_teachers_manual_override(env, monkeypatch):
    s0 = env["submissions"][0]
    # The teacher already judged c1 by hand (5 points); the model now says 90.
    with _db(env["app"]) as db:
        grade = grading_service.grade_for_revision(db, s0, 1)
        exam_manual.adjust(grade, "c1", points=5, teacher_id=env["teacher"])
        db.commit()
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        row = llm_check.payload(db.get(ExamRecheckPreview, preview_id))["rows"][0]
        assert row["manual_kept"] == 1
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 1
    grade = _grade(env, s0)
    record = json.loads(grade.result_json)
    question = record["exam"]["questions"][0]
    # The manual 5 wins; the model's 90 is kept aside as the automatic result.
    assert grade.score == 5
    assert question["earned"] == 5
    assert question["manual"]["points"] == 5
    assert question["manual"]["auto_points"] == 90


# ------------------------------------------------- part-level preview editing
def _parts_report(explain: int, code: int, state: str):
    """A c1 paper with two scored parts (explain + code), 50 points each."""
    earned = explain + code
    question = checker.QuestionResult(
        key="c1",
        title="ג · שאלה פתוחה",
        section="ג",
        points=100,
        earned=earned,
        status=(
            checker.CORRECT
            if earned >= 100
            else checker.PARTIALLY_CORRECT
            if earned > 0
            else checker.INCORRECT
        ),
        notes="נבדק במודל.",
        given="תשובת התלמיד",
        expected="",
        llm_eligible=True,
        llm_reviewed=(state == llm_check.REVIEWED),
        llm_quota_blocked=(state == llm_check.WAITING),
        parts=[
            {
                "part": "explain",
                "label": "ההסבר",
                "points": 50,
                "earned": explain,
                "note": "נבדק במודל.",
                "needs_review": False,
            },
            {
                "part": "code",
                "label": "הקוד",
                "points": 50,
                "earned": code,
                "note": "נבדק במודל.",
                "needs_review": False,
            },
        ],
    )
    return checker.ExamReport(
        exam=EXAM,
        version="a",
        version_label="גרסה א'",
        score=earned,
        maximum=100,
        questions=[question],
    )


def _wire_parts(monkeypatch, plan: dict):
    """Like :func:`_wire` but the plan is (explain, code, state) per hand-in."""
    calls: list[int] = []
    monkeypatch.setattr(llm_check.llm_review, "enabled", lambda: True)
    monkeypatch.setattr(llm_check.llm_review, "pace", lambda *a, **k: None)
    monkeypatch.setattr(llm_check.llm_review, "routing_status", lambda: "fake pool")

    def fake_mark(db, assignment, submission, *, student_name, config=None, progress=None):
        calls.append(submission.id)
        explain, code, state = plan[submission.id]
        return _parts_report(explain, code, state)

    monkeypatch.setattr(llm_check.exam_service, "mark", fake_mark)
    return calls


def test_adjust_row_scores_one_part_and_sums_the_question(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire_parts(monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        info = llm_check.adjust_row(
            db, preview, s0, "c1", points=0, part_key="explain",
            teacher_id=env["teacher"],
        )
        row = llm_check.payload(preview)["rows"][0]
    assert info["points"] == 0
    assert info["maximum"] == 50
    assert info["auto_points"] == 20
    # explain 0 + code 50 = 50; the row and the diff follow the part edit.
    assert row["new_score"] == 50
    assert row["manual_kept"] == 1
    changed = row["questions"][0]
    assert changed["manual_kept"] is True
    assert changed["new_earned"] == 50
    # The question's automatic total: explain's snapshot 20 + code's 50.
    assert changed["auto_points"] == 70
    question = row["record"]["exam"]["questions"][0]
    assert question["earned"] == 50
    assert "manual" not in question  # no whole-question override
    explain = question["parts"][0]
    assert explain["earned"] == 0
    assert explain["manual"]["auto_points"] == 20


def test_clear_row_restores_the_part_on_the_preview(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire_parts(monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        llm_check.adjust_row(
            db, preview, s0, "c1", points=0, part_key="explain",
            teacher_id=env["teacher"],
        )
        llm_check.clear_row(
            db, preview, s0, "c1", part_key="explain", teacher_id=env["teacher"]
        )
        row = llm_check.payload(preview)["rows"][0]
    assert row["new_score"] == 70
    assert row["manual_kept"] == 0
    question = row["record"]["exam"]["questions"][0]
    assert question["earned"] == 70
    assert "manual" not in question["parts"][0]


def test_apply_carries_the_part_override(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire_parts(monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        llm_check.adjust_row(
            db, preview, s0, "c1", points=0, part_key="explain",
            teacher_id=env["teacher"],
        )
        applied = llm_check.apply(db, preview)
    assert applied == 1
    grade = _grade(env, s0)
    record = json.loads(grade.result_json)
    question = record["exam"]["questions"][0]
    assert grade.score == 50
    assert question["earned"] == 50
    assert question["parts"][0]["manual"]["points"] == 0
    assert question["parts"][0]["manual"]["auto_points"] == 20


def test_rerun_keeps_the_part_override(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire_parts(monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        llm_check.adjust_row(
            db, preview, s0, "c1", points=0, part_key="explain",
            teacher_id=env["teacher"],
        )
        llm_check.apply(db, preview)
    # A fresh model pass (explain now 40) must keep the teacher's 0 on explain.
    _wire_parts(monkeypatch, {s0: (40, 50, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        row = llm_check.payload(db.get(ExamRecheckPreview, preview_id))["rows"][0]
    assert row["manual_kept"] == 1
    assert row["new_score"] == 50  # explain 0 (kept) + code 50
    question = row["record"]["exam"]["questions"][0]
    assert question["parts"][0]["earned"] == 0
    assert question["parts"][0]["manual"]["auto_points"] == 40


def test_apply_refuses_a_changed_answer_key(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    # The key changed after the preview was computed.
    monkeypatch.setattr(llm_check.recheck, "fingerprint", lambda config: "different")
    with _db(env["app"]) as db:
        with pytest.raises(llm_check.LLMCheckError, match="השתנה"):
            llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert _grade(env, s0).score == OLD_SCORE
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is not None


def test_apply_skips_a_hand_in_that_was_resubmitted(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    # The student handed in again between the preview and the apply.
    with _db(env["app"]) as db:
        db.get(Submission, s0).revision = 2
        db.commit()
    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 0
    # The revision-1 grade is untouched (the live hand-in is now revision 2).
    with _db(env["app"]) as db:
        assert grading_service.grade_for_revision(db, s0, 1).score == OLD_SCORE
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


# ----------------------------------------------------------------------- retry
def test_retry_reruns_waiting_and_keeps_reviewed(env, monkeypatch):
    s0, s1 = env["submissions"][:2]
    plan = {s0: (90, llm_check.REVIEWED), s1: (OLD_SCORE, llm_check.WAITING)}
    calls = _wire(monkeypatch, plan)
    preview_id = _start(env, [s0, s1])
    llm_check.run(env["app"].state.session_factory, preview_id)
    assert len(calls) == 2 and set(calls) == {s0, s1}

    # The quota reset: the waiting paper can now be judged.
    plan[s1] = (75, llm_check.REVIEWED)
    with _db(env["app"]) as db:
        preview = llm_check.retry(
            db, db.get(Assignment, env["assignment"]),
            db.get(ExamRecheckPreview, preview_id), teacher_id=env["teacher"],
        )
        assert preview.state == llm_check.RUNNING
        assert preview.total == 1  # only the waiting paper is re-run
    assert llm_check.run(env["app"].state.session_factory, preview_id) == llm_check.READY
    # The reviewed paper was carried forward, not marked again.
    assert calls.count(s0) == 1
    assert calls.count(s1) == 2 and calls[-1] == s1

    with _db(env["app"]) as db:
        rows = {
            row["submission_id"]: row
            for row in llm_check.payload(db.get(ExamRecheckPreview, preview_id))["rows"]
        }
        assert set(rows) == {s0, s1}
        assert rows[s0]["llm_state"] == llm_check.REVIEWED
        assert rows[s0]["new_score"] == 90
        assert rows[s1]["llm_state"] == llm_check.REVIEWED
        assert rows[s1]["new_score"] == 75
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 2
    assert _grade(env, s0).score == 90
    assert _grade(env, s1).score == 75
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


def test_retry_without_waiting_papers_raises(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        with pytest.raises(llm_check.LLMCheckError, match="שממתינות"):
            llm_check.retry(
                db, db.get(Assignment, env["assignment"]),
                db.get(ExamRecheckPreview, preview_id), teacher_id=env["teacher"],
            )


# ---------------------------------------------------------------------- extend
def test_extend_adds_papers_and_keeps_the_checked_rows(env, monkeypatch):
    s0, s1 = env["submissions"][:2]
    plan = {s0: (90, llm_check.REVIEWED), s1: (75, llm_check.REVIEWED)}
    calls = _wire(monkeypatch, plan)
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    assert calls == [s0]

    # The teacher picks one more student after the run.
    with _db(env["app"]) as db:
        preview = llm_check.extend(
            db, db.get(Assignment, env["assignment"]),
            db.get(ExamRecheckPreview, preview_id), [s1],
            teacher_id=env["teacher"],
        )
        assert preview.state == llm_check.RUNNING
        assert preview.total == 1  # only the new paper is re-run
    assert llm_check.run(env["app"].state.session_factory, preview_id) == llm_check.READY
    # The first paper was carried forward, not marked again.
    assert calls.count(s0) == 1
    assert calls.count(s1) == 1

    with _db(env["app"]) as db:
        rows = {
            row["submission_id"]: row
            for row in llm_check.payload(db.get(ExamRecheckPreview, preview_id))["rows"]
        }
        assert set(rows) == {s0, s1}
        assert rows[s0]["new_score"] == 90
        assert rows[s1]["new_score"] == 75
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 2
    assert _grade(env, s0).score == 90
    assert _grade(env, s1).score == 75
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


def test_extend_repicks_a_paper_and_replaces_its_row(env, monkeypatch):
    s0 = env["submissions"][0]
    plan = {s0: (90, llm_check.REVIEWED)}
    calls = _wire(monkeypatch, plan)
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    plan[s0] = (80, llm_check.REVIEWED)
    with _db(env["app"]) as db:
        llm_check.extend(
            db, db.get(Assignment, env["assignment"]),
            db.get(ExamRecheckPreview, preview_id), [s0],
            teacher_id=env["teacher"],
        )
    llm_check.run(env["app"].state.session_factory, preview_id)
    assert calls.count(s0) == 2
    with _db(env["app"]) as db:
        rows = llm_check.payload(db.get(ExamRecheckPreview, preview_id))["rows"]
        assert len(rows) == 1
        assert rows[0]["new_score"] == 80


def test_extend_requires_a_selection(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        with pytest.raises(llm_check.LLMCheckError, match="לא נבחרו"):
            llm_check.extend(
                db, db.get(Assignment, env["assignment"]),
                db.get(ExamRecheckPreview, preview_id), [],
                teacher_id=env["teacher"],
            )


def test_extend_requires_a_ready_preview(env, monkeypatch):
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])  # still RUNNING, not ready
    with _db(env["app"]) as db:
        with pytest.raises(llm_check.LLMCheckError, match="מוכנה"):
            llm_check.extend(
                db, db.get(Assignment, env["assignment"]),
                db.get(ExamRecheckPreview, preview_id), [s1],
                teacher_id=env["teacher"],
            )


def test_teacher_can_check_more_students_after_a_run(client, app, env, monkeypatch):
    real = app.state.grading_worker
    real.stop()  # no background pass may grade behind this test
    s0, s1 = env["submissions"][:2]
    plan = {s0: (90, llm_check.REVIEWED), s1: (75, llm_check.REVIEWED)}
    _wire(monkeypatch, plan)
    monkeypatch.setattr(
        llm_check, "run_in_thread",
        lambda factory, preview_id: llm_check.run(factory, preview_id),
    )
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, base)
    started = client.post(
        base, data={"csrf_token": token, "submission_ids": [str(s0)]},
        follow_redirects=False,
    )
    assert started.status_code == 303
    text = html.unescape(client.get(base).text)
    assert "תוצאות הבדיקה" in text
    # The results screen offers to check more students, merging into the run.
    assert "בדיקת הגשות נוספות" in text
    assert f'action="{base}/extend"' in text

    token = csrf(client, base)
    extended = client.post(
        f"{base}/extend",
        data={"csrf_token": token, "submission_ids": [str(s1)]},
        follow_redirects=False,
    )
    assert extended.status_code == 303
    text = html.unescape(client.get(base).text)
    assert "החלת הציונים שנבדקו (2)" in text
    token = csrf(client, base)
    applied = client.post(
        f"{base}/apply", data={"csrf_token": token}, follow_redirects=False
    )
    assert applied.status_code == 303
    assert _grade(env, s0).score == 90
    assert _grade(env, s1).score == 75
    with _db(app) as db:
        assert db.query(ExamRecheckPreview).count() == 0


# --------------------------------------------------------------- mode isolation
def test_the_two_preview_modes_never_cross(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        llm_preview = db.get(ExamRecheckPreview, preview_id)
        assert llm_check.owns(llm_preview) is True
        assert llm_check.payload(llm_preview) is not None
        # The deterministic reader ignores a model payload...
        assert recheck.payload(llm_preview) is None
        # ...and the deterministic apply refuses to write it.
        with pytest.raises(recheck.RecheckError):
            recheck.apply_preview(db, llm_preview)

    # A deterministic payload (no mode tag) is invisible to the model reader.
    # A transient row is enough: both readers only inspect state + payload_json,
    # and the one-preview-per-assignment constraint forbids a second live row.
    deterministic = ExamRecheckPreview(
        assignment_id=env["assignment"],
        state=recheck.READY,
        payload_json=json.dumps({"rows": []}, ensure_ascii=False),
    )
    assert llm_check.owns(deterministic) is False
    assert llm_check.payload(deterministic) is None
    assert recheck.payload(deterministic) is not None


def test_owns_reads_the_tag_while_the_run_is_still_running(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])  # start writes the tag before the run
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        assert preview.state == llm_check.RUNNING
        assert llm_check.owns(preview) is True
        # Not ready yet, so there is no payload to apply.
        assert llm_check.payload(preview) is None


# ------------------------------------------------------------------ route smoke
def test_teacher_screen_selects_runs_applies_and_retries(client, app, env, monkeypatch):
    real = app.state.grading_worker
    real.stop()  # no background pass may grade behind this test
    s0, s1 = env["submissions"][:2]
    plan = {s0: (90, llm_check.REVIEWED), s1: (OLD_SCORE, llm_check.WAITING)}
    _wire(monkeypatch, plan)
    # The route starts a thread; run the same work synchronously instead.
    monkeypatch.setattr(
        llm_check, "run_in_thread",
        lambda factory, preview_id: llm_check.run(factory, preview_id),
    )
    assignment_id = env["assignment"]
    base = f"/teacher/assignments/{assignment_id}/llm-check"

    login(client, "llm-mori", TEACHER_PASSWORD)
    page = client.get(base)
    assert page.status_code == 200, page.text[:600]
    text = html.unescape(page.text)
    assert "בחירת הגשות לבדיקה" in text
    assert "בדוק את ההגשות שנבחרו" in text
    assert 'name="submission_ids"' in text
    assert "noa" in text  # a candidate is listed

    token = csrf(client, base)
    started = client.post(
        base,
        data={"csrf_token": token, "submission_ids": [str(s0), str(s1)]},
        follow_redirects=False,
    )
    assert started.status_code == 303

    page = client.get(base)
    assert page.status_code == 200, page.text[:600]
    text = html.unescape(page.text)
    assert "תוצאות הבדיקה" in text
    assert "נבדקו במודל" in text
    assert "ממתינות למכסה" in text
    assert "החלת הציונים שנבדקו (1)" in text
    assert "ניסיון חוזר לממתינות (1)" in text

    token = csrf(client, base)
    applied = client.post(f"{base}/apply", data={"csrf_token": token}, follow_redirects=False)
    assert applied.status_code == 303
    assert _grade(env, s0).score == 90
    assert _grade(env, s1).score == OLD_SCORE

    # After applying, only the waiting paper remains, offered for retry.
    text = html.unescape(client.get(base).text)
    assert "ניסיון חוזר לממתינות (1)" in text
    assert "החלת הציונים שנבדקו" not in text

    # The quota reset: retry the waiting paper, then apply it too.
    plan[s1] = (75, llm_check.REVIEWED)
    token = csrf(client, base)
    retried = client.post(f"{base}/retry", data={"csrf_token": token}, follow_redirects=False)
    assert retried.status_code == 303
    text = html.unescape(client.get(base).text)
    assert "החלת הציונים שנבדקו (1)" in text
    token = csrf(client, base)
    applied = client.post(f"{base}/apply", data={"csrf_token": token}, follow_redirects=False)
    assert applied.status_code == 303
    assert _grade(env, s1).score == 75
    with _db(app) as db:
        assert db.query(ExamRecheckPreview).count() == 0


def test_every_row_links_to_the_students_hand_in_form(client, app, env, monkeypatch):
    """Each student row opens the actual submission form, in both screen states."""
    real = app.state.grading_worker
    real.stop()  # no background pass may grade behind this test
    s0, s1 = env["submissions"][:2]
    plan = {s0: (90, llm_check.REVIEWED), s1: (OLD_SCORE, llm_check.WAITING)}
    _wire(monkeypatch, plan)
    monkeypatch.setattr(
        llm_check, "run_in_thread",
        lambda factory, preview_id: llm_check.run(factory, preview_id),
    )
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)

    # Picker: every candidate offers the form, and the link sits outside the
    # checkbox label (its own class) so opening it never ticks the row.
    text = html.unescape(client.get(base).text)
    assert "llm-pick-view" in text
    assert 'target="_blank"' in text
    for submission_id in env["submissions"]:
        assert f'href="/teacher/submissions/{submission_id}"' in text
    assert text.count("צפייה בטופס ההגשה") == len(env["submissions"])

    # Results: every row - reviewed and waiting alike - still offers the form.
    token = csrf(client, base)
    started = client.post(
        base,
        data={"csrf_token": token, "submission_ids": [str(s0), str(s1)]},
        follow_redirects=False,
    )
    assert started.status_code == 303
    text = html.unescape(client.get(base).text)
    assert "תוצאות הבדיקה" in text
    assert "recheck-view" in text
    # The old→new arrow carries the direction-aware class so the RTL mirror
    # points it at the new grade instead of back at the old one.
    assert "score-arrow" in text
    for submission_id in (s0, s1):
        assert f'href="/teacher/submissions/{submission_id}"' in text
    # Two result rows, plus the "check more" picker that lists every candidate.
    assert text.count("צפייה בטופס ההגשה") == 2 + len(env["submissions"])
    assert "בדיקת הגשות נוספות" in text


def test_picker_offers_a_deterministic_check_when_the_model_is_off(
    client, app, env, monkeypatch
):
    """No dead-end: with the model off the picker still offers the rules check."""
    real = app.state.grading_worker
    real.stop()  # no background pass may grade behind this test
    monkeypatch.setattr(llm_check.llm_review, "enabled", lambda: False)
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    text = html.unescape(client.get(base).text)
    assert "בחירת הגשות לבדיקה" in text
    assert 'name="submission_ids"' in text
    assert "בדוק את ההגשות שנבחרו" in text
    # The model-off hint explains the deterministic-only run, but the form is
    # still offered - the old dead-end card no longer blocks the screen.
    assert "EXAM_LLM_REVIEW" in text
    assert "בדיקה תרוץ אוטומטית בלבד" in text