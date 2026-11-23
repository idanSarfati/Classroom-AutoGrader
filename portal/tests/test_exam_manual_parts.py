"""Part-level manual overrides: each scored part gets its own points.

A question with several scored parts (an explanation and a corrected program)
is graded part by part within each part's rubric slice; the question total is
the sum of its parts. These tests drive the record-level logic directly with
synthetic records - no DB, network or model - plus the ``Grade`` wrappers and
the carry-over that keeps a teacher's part decisions across a re-mark.

The legacy whole-question override still works, and editing a part supersedes it
so the two never contradict each other. Single-part questions are unchanged.
"""
from __future__ import annotations

import json

import pytest

from portal.exams import checker
from portal.exams import manual as exam_manual
from portal.models import Grade


def _record() -> dict:
    """One single-part question (a1) and one two-part question (c1)."""
    return {
        "student_name": "תלמיד",
        "score": 12,
        "feedback_hebrew": "",
        "deduction_breakdown": [],
        "task_evaluations": [
            {"task_name": "א1", "student_answer_found": "נכון",
             "status": checker.CORRECT, "notes": ""},
            {"task_name": "ג1", "student_answer_found": "הקלט מחרוזת; age=int(input())",
             "status": checker.PARTIALLY_CORRECT, "notes": "הסבר חלקי"},
        ],
        "exam": {
            "name": "units_1_3", "version": "a", "version_label": "גרסה א'",
            "maximum": 100,
            "questions": [
                {"key": "a1", "title": "א1", "points": 5, "earned": 5,
                 "status": checker.CORRECT, "needs_review": False,
                 "parts": [
                     {"part": "response", "label": "התשובה", "points": 5,
                      "earned": 5, "note": "נכון", "needs_review": False},
                 ]},
                {"key": "c1", "title": "ג1", "points": 10, "earned": 7,
                 "status": checker.PARTIALLY_CORRECT, "needs_review": True,
                 "parts": [
                     {"part": "explain", "label": "ההסבר", "points": 5,
                      "earned": 2, "note": "כיוון נכון", "needs_review": True},
                     {"part": "code", "label": "הקוד המתוקן", "points": 5,
                      "earned": 5, "note": "רץ", "needs_review": False},
                 ]},
            ],
        },
    }


def _question(record: dict, key: str) -> dict:
    return next(q for q in record["exam"]["questions"] if q["key"] == key)


def _part(record: dict, key: str, part: str) -> dict:
    return next(p for p in _question(record, key)["parts"] if p["part"] == part)


# --------------------------------------------------------------- adjust a part
def test_adjusting_a_part_sets_its_points_and_sums_the_question():
    record = _record()
    info = exam_manual.adjust_part_record(
        record, "c1", "explain", points=0, note="לא נכון", teacher_id=7,
        teacher_name="מורה",
    )
    assert info["auto_points"] == 2 and info["points"] == 0
    assert info["maximum"] == 5 and info["part_key"] == "explain"
    assert _part(record, "c1", "explain")["earned"] == 0
    # The question total is now the sum of its parts: 0 + 5.
    assert _question(record, "c1")["earned"] == 5
    assert _question(record, "c1")["needs_review"] is False
    exam_manual.refresh(record)
    assert record["score"] == 10  # a1 (5) + c1 (5)


def test_full_credit_gives_a_part_its_slice():
    record = _record()
    _part(record, "c1", "explain")["earned"] = 2
    exam_manual.adjust_part_record(record, "c1", "explain", points=None, full_credit=True)
    assert _part(record, "c1", "explain")["earned"] == 5
    assert _question(record, "c1")["earned"] == 10
    assert _question(record, "c1")["status"] == checker.CORRECT


def test_a_part_override_is_reversible():
    record = _record()
    exam_manual.adjust_part_record(record, "c1", "explain", points=0)
    exam_manual.clear_part_record(record, "c1", "explain")
    part = _part(record, "c1", "explain")
    assert part["earned"] == 2 and "manual" not in part
    assert part["needs_review"] is True  # the automatic review flag is back
    assert _question(record, "c1")["earned"] == 7


def test_points_outside_the_part_slice_are_refused():
    record = _record()
    with pytest.raises(exam_manual.ExamAdjustmentError):
        exam_manual.adjust_part_record(record, "c1", "explain", points=6)
    with pytest.raises(exam_manual.ExamAdjustmentError):
        exam_manual.adjust_part_record(record, "c1", "explain", points=-1)
    assert _part(record, "c1", "explain")["earned"] == 2  # unchanged


def test_an_unknown_part_is_refused():
    record = _record()
    with pytest.raises(exam_manual.ExamAdjustmentError):
        exam_manual.adjust_part_record(record, "c1", "nope", points=1)


def test_editing_a_part_supersedes_a_legacy_whole_question_override():
    record = _record()
    # A legacy whole-question override (the old one-box-per-question path).
    exam_manual.adjust_record(record, "c1", points=0)
    assert _question(record, "c1")["earned"] == 0
    assert isinstance(_question(record, "c1")["manual"], dict)
    # Editing one part replaces it: the total becomes the sum of the parts.
    exam_manual.adjust_part_record(record, "c1", "explain", points=3)
    assert "manual" not in _question(record, "c1")
    assert _question(record, "c1")["earned"] == 8  # explain 3 + code 5


def test_a_single_part_question_still_adjusts_by_its_part():
    record = _record()
    exam_manual.adjust_part_record(record, "a1", "response", points=0)
    assert _question(record, "a1")["earned"] == 0
    exam_manual.refresh(record)
    assert record["score"] == 7  # a1 (0) + c1 (7)


# ------------------------------------------------------------------- carry over
def test_carry_over_keeps_part_overrides_across_a_remark():
    old = _record()
    exam_manual.adjust_part_record(old, "c1", "explain", points=0, note="לא נכון")
    new = _record()  # a fresh automatic verdict, same shape
    kept = exam_manual.carry_over(old, new)
    assert kept == 1
    part = _part(new, "c1", "explain")
    assert part["earned"] == 0 and part["manual"]["points"] == 0
    assert part["manual"]["note"] == "לא נכון"
    assert _question(new, "c1")["earned"] == 5
    # The untouched part keeps its fresh automatic mark.
    assert _part(new, "c1", "code")["earned"] == 5


def test_carry_over_clamps_a_part_override_to_a_new_slice():
    old = _record()
    exam_manual.adjust_part_record(old, "c1", "explain", points=5)
    new = _record()
    _part(new, "c1", "explain")["points"] = 3  # a tighter slice on the re-mark
    exam_manual.carry_over(old, new)
    assert _part(new, "c1", "explain")["earned"] == 3


def test_carry_over_still_keeps_a_legacy_whole_question_override():
    old = _record()
    exam_manual.adjust_record(old, "c1", points=1)
    new = _record()
    kept = exam_manual.carry_over(old, new)
    assert kept == 1
    assert _question(new, "c1")["earned"] == 1
    assert isinstance(_question(new, "c1")["manual"], dict)


# ------------------------------------------------------------- Grade wrappers
def _grade() -> Grade:
    grade = Grade(submission_id=1, revision=1, configured_model="exam-checker")
    exam_manual.write_record(grade, _record())
    return grade


def test_adjust_part_on_a_grade_updates_the_total_and_the_log():
    grade = _grade()
    exam_manual.adjust_part(
        grade, "c1", "explain", points=0, note="לא נכון",
        teacher_id=7, teacher_name="מורה",
    )
    assert grade.score == 10
    result = json.loads(grade.result_json)
    assert _question(result, "c1")["earned"] == 5
    assert "עדכון ניקוד ידני" in grade.review_note
    assert "ההסבר" in grade.review_note and "לא נכון" in grade.review_note


def test_clear_part_on_a_grade_restores_the_total():
    grade = _grade()
    exam_manual.adjust_part(grade, "c1", "explain", points=0)
    exam_manual.clear_part(grade, "c1", "explain")
    assert grade.score == 12
    result = json.loads(grade.result_json)
    assert _question(result, "c1")["earned"] == 7