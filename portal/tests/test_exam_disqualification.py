"""A whole-paper fixed score: disqualifying a hand-in (for example a copied exam).

The teacher can lock one hand-in to a fixed score. Unlike a per-question manual
override, this decides the *final* score of the entire paper, and it must survive
every path that rewrites a grade: a manual question edit, a deterministic re-mark
(``carry_over``), and a selective model check (``llm_check.apply``). The reason is
teacher-only; the student sees that the paper was disqualified and the fixed score,
never the reason.

The record-level logic is driven directly with synthetic records (no DB, network
or model). The ``Grade`` wrappers, the student projection and the real teacher /
student pages are exercised end to end on the shared exam fixtures from
:mod:`portal.tests.test_exam_llm_check`.
"""
from __future__ import annotations

import html
import json

import pytest

from portal.exams import checker
from portal.exams import llm_check
from portal.exams import manual as exam_manual
from portal.grading import service
from portal.models import Assignment, Grade
from portal.tests.helpers import csrf, login

from portal.tests.test_exam_llm_check import (  # noqa: F401  (env is a fixture)
    EXAM,
    OLD_SCORE,
    STUDENT_PASSWORD,
    STUDENTS,
    TEACHER_PASSWORD,
    _db,
    _grade,
    _report,
    _start,
    env,
)

# A distinctive reason that appears in no template or generic label, so a leak
# into a student page is unmistakable.
SECRET_REASON = "העתקה מוכחת מתלמיד אחר - פסילה מלאה של המבחן"


def _record() -> dict:
    """One single-part question (a1=5) and one two-part question (c1=7): 12."""
    return {
        "student_name": "תלמיד",
        "score": 12,
        "feedback_hebrew": "",
        "deduction_breakdown": [],
        "task_evaluations": [
            {"task_name": "א1", "student_answer_found": "נכון",
             "status": checker.CORRECT, "notes": ""},
            {"task_name": "ג1", "student_answer_found": "הקלט מחרוזת",
             "status": checker.PARTIALLY_CORRECT, "notes": "הסבר חלקי"},
        ],
        "exam": {
            "name": "units_1_3", "version": "a", "version_label": "גרסה א'",
            "maximum": 100,
            "questions": [
                {"key": "a1", "title": "א1", "points": 5, "earned": 5,
                 "status": checker.CORRECT, "needs_review": False},
                {"key": "c1", "title": "ג1", "points": 10, "earned": 7,
                 "status": checker.PARTIALLY_CORRECT, "needs_review": True},
            ],
        },
    }


def _synthetic_grade() -> Grade:
    grade = Grade(submission_id=1, revision=1, configured_model="exam-checker")
    exam_manual.write_record(grade, _record())
    return grade


# ------------------------------------------------------------- record level
def test_disqualify_record_locks_the_score_through_refresh():
    record = _record()
    info = exam_manual.disqualify_record(
        record, score=0, reason=SECRET_REASON, teacher_id=7, teacher_name="מורה"
    )
    assert info["score"] == 0 and info["reason"] == SECRET_REASON
    exam_manual.refresh(record)
    # The sum of the questions is 12, but the fixed score wins.
    assert record["score"] == 0
    assert record["deduction_breakdown"] == []
    assert record["audit"]["ok"] is True
    assert record["audit"]["needs_review"] is False
    assert "נפסלה" in record["feedback_hebrew"]


def test_a_non_zero_fixed_score_is_also_honored():
    record = _record()
    exam_manual.disqualify_record(record, score=40)
    exam_manual.refresh(record)
    assert record["score"] == 40
    assert "40" in record["feedback_hebrew"]


def test_the_reason_never_enters_the_student_feedback():
    record = _record()
    exam_manual.disqualify_record(record, score=0, reason=SECRET_REASON)
    exam_manual.refresh(record)
    assert SECRET_REASON not in record["feedback_hebrew"]
    assert SECRET_REASON not in exam_manual.disqualification_feedback(0)
    # The reason is stored on the decision itself, for the teacher only.
    assert exam_manual.disqualification(record)["reason"] == SECRET_REASON


def test_disqualify_record_validates_the_score():
    for bad in (-1, 101, "abc"):
        record = _record()
        with pytest.raises(exam_manual.ExamAdjustmentError):
            exam_manual.disqualify_record(record, score=bad)
        assert not exam_manual.is_disqualified(record)


def test_the_reason_is_trimmed_to_a_bound():
    record = _record()
    exam_manual.disqualify_record(record, score=0, reason="א" * 5000)
    assert len(exam_manual.disqualification(record)["reason"]) == 1000


def test_refresh_honors_disqualification_even_after_a_question_edit():
    record = _record()
    exam_manual.disqualify_record(record, score=0)
    # A later per-question override must not lift the locked headline score.
    exam_manual.adjust_record(record, "a1", points=5)
    exam_manual.refresh(record)
    assert record["score"] == 0


def test_carry_over_preserves_disqualification_without_manual_questions():
    old = _record()
    exam_manual.disqualify_record(old, score=0, reason=SECRET_REASON)
    new = _record()  # a fresh automatic verdict, no hand-adjusted question
    kept = exam_manual.carry_over(old, new)
    assert kept == 0  # no individual question was overridden
    assert exam_manual.is_disqualified(new)
    assert exam_manual.disqualification(new)["reason"] == SECRET_REASON
    exam_manual.refresh(new)
    assert new["score"] == 0  # the fixed score survived the re-mark


def test_clear_disqualification_record_restores_the_automatic_sum():
    record = _record()
    exam_manual.disqualify_record(record, score=0)
    exam_manual.refresh(record)
    assert record["score"] == 0
    exam_manual.clear_disqualification_record(record)
    assert not exam_manual.is_disqualified(record)
    exam_manual.refresh(record)
    assert record["score"] == 12


def test_clearing_a_disqualification_that_is_not_there_is_refused():
    record = _record()
    with pytest.raises(exam_manual.ExamAdjustmentError):
        exam_manual.clear_disqualification_record(record)


def test_helpers_on_a_plain_record():
    assert exam_manual.disqualification(None) is None
    assert exam_manual.disqualification({}) is None
    assert exam_manual.is_disqualified({}) is False
    assert exam_manual.is_disqualified({"disqualification": "nope"}) is False


# ------------------------------------------------------------- Grade wrappers
def test_disqualify_on_a_grade_locks_score_and_shields_from_recheck():
    grade = _synthetic_grade()
    exam_manual.disqualify(
        grade, score=0, reason=SECRET_REASON, teacher_id=7, teacher_name="מורה"
    )
    assert grade.score == 0
    assert grade.reviewed_at is not None  # shields it from the selective check
    assert grade.reviewed_by == 7
    assert "נפסלה" in grade.feedback_hebrew
    assert SECRET_REASON in grade.review_note  # the teacher's own log
    result = json.loads(grade.result_json)
    assert exam_manual.is_disqualified(result)


def test_clear_disqualification_on_a_grade_restores_score_and_review():
    grade = _synthetic_grade()
    exam_manual.disqualify(grade, score=0, teacher_id=7)
    exam_manual.clear_disqualification(grade, teacher_id=7)
    assert grade.score == 12
    assert grade.reviewed_at is None
    assert grade.reviewed_by is None
    assert not exam_manual.is_disqualified(json.loads(grade.result_json))


def test_disqualify_then_adjust_a_question_keeps_the_fixed_score():
    grade = _synthetic_grade()
    exam_manual.disqualify(grade, score=0, teacher_id=7)
    exam_manual.adjust(grade, "a1", points=5, teacher_id=7)
    assert grade.score == 0  # refresh still honors the disqualification


# ------------------------------------------------------------- the projection
def test_the_teacher_projection_carries_the_disqualification_reason():
    grade = _synthetic_grade()
    exam_manual.disqualify(
        grade, score=0, reason=SECRET_REASON, teacher_id=7, teacher_name="מורה"
    )
    teacher = service.grade_view(grade)
    assert teacher["disqualification"]["score"] == 0
    assert teacher["disqualification"]["reason"] == SECRET_REASON
    assert teacher["disqualification"]["by_name"] == "מורה"


def test_the_student_projection_strips_the_reason_but_keeps_flag_and_score():
    grade = _synthetic_grade()
    exam_manual.disqualify(
        grade, score=0, reason=SECRET_REASON, teacher_id=7, teacher_name="מורה"
    )
    student = service.student_grade_view(grade)
    assert student["disqualification"] == {"active": True, "score": 0}
    blob = json.dumps(student, ensure_ascii=False, default=str)
    assert SECRET_REASON not in blob
    assert "by_name" not in blob
    assert student["review_note"] == ""


def test_the_student_feedback_announces_the_disqualification():
    grade = _synthetic_grade()
    exam_manual.disqualify(grade, score=0, reason=SECRET_REASON, teacher_id=7)
    student = service.student_grade_view(grade)
    assert student["score"] == 0
    assert "נפסלה" in student["feedback"]
    assert SECRET_REASON not in student["feedback"]


def test_a_plain_grade_has_no_disqualification_in_either_projection():
    grade = _synthetic_grade()
    assert service.grade_view(grade)["disqualification"] is None
    assert service.student_grade_view(grade)["disqualification"] is None


# ------------------------------------------------- the real teacher/student pages
def _release_full(app, assignment_id: int) -> None:
    with _db(app) as db:
        db.get(Assignment, assignment_id).exam_release = "full"
        db.commit()


def test_teacher_disqualifies_and_the_student_sees_the_fixed_score(client, app, env):
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, f"/teacher/submissions/{s0}")
    response = client.post(
        f"/teacher/submissions/{s0}/disqualify",
        data={"csrf_token": token, "score": "0", "reason": SECRET_REASON},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _grade(env, s0).score == 0

    _release_full(app, env["assignment"])
    login(client, STUDENTS[0], STUDENT_PASSWORD)
    page = client.get(f"/student/submissions/{s0}")
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert "ההגשה נפסלה" in text
    assert 'aria-label="ציון 0"' in text
    assert SECRET_REASON not in text  # the reason stays teacher-only


def test_teacher_can_clear_a_disqualification(client, app, env):
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, f"/teacher/submissions/{s0}")
    client.post(
        f"/teacher/submissions/{s0}/disqualify",
        data={"csrf_token": token, "score": "0", "reason": SECRET_REASON},
    )
    assert _grade(env, s0).score == 0
    response = client.post(
        f"/teacher/submissions/{s0}/disqualify/clear",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _grade(env, s0).score == OLD_SCORE


def test_disqualify_requires_a_teacher(client, app, env):
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    login(client, STUDENTS[0], STUDENT_PASSWORD)
    token = csrf(client, "/student")
    response = client.post(
        f"/teacher/submissions/{s0}/disqualify",
        data={"csrf_token": token, "score": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert _grade(env, s0).score == OLD_SCORE  # unchanged


def test_disqualify_requires_csrf(client, app, env):
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    login(client, "llm-mori", TEACHER_PASSWORD)
    response = client.post(
        f"/teacher/submissions/{s0}/disqualify",
        data={"score": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 400  # verify_csrf rejects a missing token
    assert _grade(env, s0).score == OLD_SCORE


def test_an_invalid_fixed_score_is_refused(client, app, env):
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, f"/teacher/submissions/{s0}")
    response = client.post(
        f"/teacher/submissions/{s0}/disqualify",
        data={"csrf_token": token, "score": "150"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert _grade(env, s0).score == OLD_SCORE  # nothing was locked


# ------------------------------------------------------- the apply-time guard
def test_apply_skips_a_disqualified_grade(client, app, env, monkeypatch):
    """A model check prepared before the disqualification must not overwrite it."""
    app.state.grading_worker.stop()
    s0, s1 = env["submissions"][:2]
    monkeypatch.setattr(llm_check.llm_review, "enabled", lambda: False)
    monkeypatch.setattr(llm_check.llm_review, "pace", lambda *a, **k: None)

    def fake_mark(db, assignment, submission, *, student_name, config=None, progress=None):
        return _report(95 if submission.id == s0 else 80, llm_check.OFF)

    monkeypatch.setattr(llm_check.exam_service, "mark", fake_mark)
    preview_id = _start(env, [s0, s1])
    assert llm_check.run(app.state.session_factory, preview_id) == llm_check.READY

    # The teacher locks s0 *after* the preview was built but before it is applied.
    with _db(app) as db:
        grade = service.grade_for_revision(db, s0, 1)
        exam_manual.disqualify(grade, score=0, reason=SECRET_REASON, teacher_id=env["teacher"])
        db.commit()

    with _db(app) as db:
        from portal.models import ExamRecheckPreview

        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 1  # s0 skipped, s1 written
    assert _grade(env, s0).score == 0  # the fixed score survived the apply
    assert _grade(env, s1).score == 80