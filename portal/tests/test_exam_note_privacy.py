"""The teacher's correction note is for the model's calibration only.

When the teacher reviews the model's marking, fixes a score and types a note
(הערה), that note exists to teach the model - it must never reach the student
who gets the exam back. Two independent layers guarantee it:

* the student projection (:func:`service.student_grade_view`) structurally
  removes every teacher-only field - the per-question / per-part ``manual``
  override (which carries the note) and the review note - while keeping the
  score, the general feedback and the model's own per-task explanation. The
  teacher pages and the teacher preview keep the full :func:`service.grade_view`.
* the model prompts forbid quoting, paraphrasing or even mentioning the internal
  calibration (teacher notes, the grading-style profile, past-hand-in examples)
  in the student-visible ``reason``.

These tests build a corrected grade directly (no network, model or quota) and
also drive the real student pages end to end on a released exam, reusing the
exam fixtures from :mod:`portal.tests.test_exam_llm_check`.
"""
from __future__ import annotations

import html
import json

from portal.exams import config as exam_config
from portal.exams import llm_review
from portal.exams import manual as exam_manual
from portal.exams.paper import ensure_allocation
from portal.grading import service
from portal.models import Assignment, Grade
from portal.tests.helpers import login

from portal.tests.test_exam_llm_review import (  # noqa: F401  (shared prompt fixtures)
    CONTEXT,
    PARTS,
    PAPER_ITEMS,
    RULE,
)
from portal.tests.test_exam_llm_check import (  # noqa: F401  (env is a fixture)
    EXAM,
    STUDENT_PASSWORD,
    STUDENTS,
    _db,
    env,
)

# Distinctive notes that appear in no template, rubric or generic label, so a
# leak is unmistakable: if either string shows up on a student page, the
# teacher's private calibration has escaped.
QUESTION_NOTE = "הערת מורה פנימית למודל: להקל כי התלמיד הבין את הרעיון"
PART_NOTE = "הערת כיול לחלק ההסבר: הכיוון נכון אך חסרה התוצאה"


def _record() -> dict:
    """One single-part question (a1) and one two-part question (c1)."""
    return {
        "student_name": "תלמיד",
        "score": 12,
        "feedback_hebrew": "",
        "deduction_breakdown": [],
        "task_evaluations": [
            {"task_name": "א1", "student_answer_found": "נכון",
             "status": "CORRECT", "notes": "נכון"},
            {"task_name": "ג1", "student_answer_found": "הקלט מחרוזת",
             "status": "PARTIALLY_CORRECT", "notes": "הסבר חלקי"},
        ],
        "exam": {
            "name": "units_1_3", "version": "a", "version_label": "גרסה א'",
            "maximum": 100,
            "questions": [
                {"key": "a1", "title": "א1", "points": 5, "earned": 5,
                 "status": "CORRECT", "needs_review": False,
                 "parts": [
                     {"part": "response", "label": "התשובה", "points": 5,
                      "earned": 5, "note": "נכון", "needs_review": False},
                 ]},
                {"key": "c1", "title": "ג1", "points": 10, "earned": 7,
                 "status": "PARTIALLY_CORRECT", "needs_review": True,
                 "parts": [
                     {"part": "explain", "label": "ההסבר", "points": 5,
                      "earned": 2, "note": "כיוון נכון", "needs_review": True},
                     {"part": "code", "label": "הקוד המתוקן", "points": 5,
                      "earned": 5, "note": "רץ", "needs_review": False},
                 ]},
            ],
        },
    }


def _corrected_grade() -> Grade:
    """A grade the teacher corrected: a whole-question note and a part note."""
    grade = Grade(submission_id=1, revision=1, configured_model="exam-checker")
    exam_manual.write_record(grade, _record())
    exam_manual.adjust(
        grade, "a1", points=0, note=QUESTION_NOTE,
        teacher_id=7, teacher_name="מורה",
    )
    exam_manual.adjust_part(
        grade, "c1", "explain", points=0, note=PART_NOTE,
        teacher_id=7, teacher_name="מורה",
    )
    return grade


# ----------------------------------------------------------- the projection
def test_the_teacher_projection_still_carries_the_notes():
    """Sanity: the notes really are stored, so the strip test is meaningful."""
    grade = _corrected_grade()
    teacher = service.grade_view(grade)
    blob = json.dumps(teacher, ensure_ascii=False)
    assert QUESTION_NOTE in blob
    assert PART_NOTE in blob
    assert QUESTION_NOTE in teacher["review_note"]
    assert PART_NOTE in teacher["review_note"]
    # a1 keeps a whole-question override; c1's explain keeps a part override.
    a1 = next(q for q in teacher["exam_questions"] if q["key"] == "a1")
    c1 = next(q for q in teacher["exam_questions"] if q["key"] == "c1")
    assert a1["manual"]["note"] == QUESTION_NOTE
    explain = next(p for p in c1["parts"] if p["part"] == "explain")
    assert explain["manual"]["note"] == PART_NOTE


def test_the_student_projection_removes_every_teacher_note():
    grade = _corrected_grade()
    student = service.student_grade_view(grade)
    blob = json.dumps(student, ensure_ascii=False)
    assert QUESTION_NOTE not in blob
    assert PART_NOTE not in blob
    assert student["review_note"] == ""
    # No manual-override metadata survives on a task, a question or a part.
    for task in student["tasks"]:
        assert "manual" not in task
    for question in student["exam_questions"]:
        assert "manual" not in question
        for part in question.get("parts") or []:
            assert "manual" not in part


def test_the_student_projection_keeps_the_students_own_feedback():
    grade = _corrected_grade()
    teacher = service.grade_view(grade)
    student = service.student_grade_view(grade)
    # The score, the general feedback and the model's per-task explanation are
    # the student's feedback - they stay, byte for byte.
    assert student["score"] == grade.score == 5
    assert student["feedback"] == grade.feedback_hebrew
    assert [t.get("notes") for t in student["tasks"]] == [
        t.get("notes") for t in teacher["tasks"]
    ]
    assert any(task.get("notes") for task in student["tasks"])
    # The generic "updated by the teacher" label is allowed; the custom note is not.
    assert exam_manual.MANUAL_NOTE in json.dumps(student, ensure_ascii=False)


def test_the_student_projection_of_no_grade_is_none():
    assert service.student_grade_view(None) is None


# ------------------------------------------------------- the model prompt guard
# Both needles appear in the single-explain rules and the shared open-item rules:
# the reason is shown to the student, and internal calibration must never be quoted.
_SHOWN = "מוצג לתלמיד/ה"
_NEVER_QUOTE = "לעולם אל תצטט"


def test_the_open_item_prompt_forbids_echoing_internal_notes():
    prompt = llm_review._item_prompt(PARTS, CONTEXT)
    assert _SHOWN in prompt
    assert _NEVER_QUOTE in prompt
    # The guard names the distilled profile and the past-hand-in examples too.
    assert "פרופיל" in prompt
    assert "דוגמאות" in prompt


def test_the_whole_paper_prompt_forbids_echoing_internal_notes():
    prompt = llm_review._paper_prompt(PAPER_ITEMS)
    assert _SHOWN in prompt
    assert _NEVER_QUOTE in prompt


def test_the_single_explain_prompt_forbids_echoing_internal_notes():
    prompt = llm_review._prompt(RULE, "המשתנה בתוך המרכאות", {"title": "שאלה 9"})
    assert _SHOWN in prompt
    assert _NEVER_QUOTE in prompt


# ------------------------------------------------- the real student pages (e2e)
def _release_full(app, assignment_id: int) -> None:
    with _db(app) as db:
        db.get(Assignment, assignment_id).exam_release = "full"
        db.commit()


def test_the_student_result_page_hides_the_teacher_note(client, app, env):
    app.state.grading_worker.stop()  # no background pass may regrade behind us
    s0 = env["submissions"][0]
    with _db(app) as db:
        grade = service.grade_for_revision(db, s0, 1)
        exam_manual.adjust(
            grade, "c1", points=5, note=QUESTION_NOTE,
            teacher_id=env["teacher"], teacher_name="מורה",
        )
        db.commit()
    _release_full(app, env["assignment"])

    login(client, STUDENTS[0], STUDENT_PASSWORD)
    response = client.get(f"/student/submissions/{s0}")
    assert response.status_code == 200
    text = html.unescape(response.text)
    # The private note is gone, but the page really rendered the graded result.
    assert QUESTION_NOTE not in text
    assert 'aria-label="ציון 5"' in text
    assert "המשוב של המורה" in text
    # The student still gets the model's explanation and the generic label.
    assert exam_manual.MANUAL_NOTE in text


def test_the_student_exam_paper_hides_the_teacher_note(client, app, env):
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    student_id = env["students"][0]
    with _db(app) as db:
        grade = service.grade_for_revision(db, s0, 1)
        exam_manual.adjust_part(
            grade, "c1", "explain", points=0, note=PART_NOTE,
            teacher_id=env["teacher"], teacher_name="מורה",
        )
        db.commit()
    _release_full(app, env["assignment"])
    # The read-only paper is drawn from the student's allocated version.
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    with _db(app) as db:
        ensure_allocation(db, config, env["assignment"], student_id)
        db.commit()

    login(client, STUDENTS[0], STUDENT_PASSWORD)
    response = client.get(f"/student/assignments/{env['assignment']}")
    assert response.status_code == 200
    text = html.unescape(response.text)
    assert PART_NOTE not in text
    assert QUESTION_NOTE not in text
    # The full-release locked paper rendered (not the gate or a held page).
    assert "המבחן שהוגש" in text


def test_a_held_exam_shows_the_student_nothing(client, app, env):
    """While results are held, neither the score nor any note may appear."""
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    with _db(app) as db:
        grade = service.grade_for_revision(db, s0, 1)
        exam_manual.adjust(
            grade, "c1", points=5, note=QUESTION_NOTE,
            teacher_id=env["teacher"],
        )
        db.commit()
    # exam_release stays "held" (the fixture default).
    login(client, STUDENTS[0], STUDENT_PASSWORD)
    response = client.get(f"/student/submissions/{s0}")
    assert response.status_code == 200
    text = html.unescape(response.text)
    assert QUESTION_NOTE not in text
    assert "הציון טרם פורסם" in text
    assert 'aria-label="ציון 5"' not in text