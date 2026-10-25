"""Unit tests for the deterministic Python-source scanner.

Run from the project root::

    python -m pytest tests/test_code_checks.py -q

These pin the scanner's contract: it must find the required variables and
calls for lesson_3's exercises, never credit a substring (``first_name`` must
not satisfy ``name``), tolerate RTL/zero-width noise, and stay conservative -
reporting a miss only when absence is provable. The scanner never executes
student code, so every case here is pure string analysis.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.code_checks import (  # noqa: E402
    CHECKS_BY_ASSIGNMENT,
    checks_for_assignment,
    is_effectively_empty,
    normalize_source,
    scan_submission,
)

ROOT = Path(__file__).resolve().parent.parent
LESSON_3 = ROOT / "assignments" / "lesson_3.json"


def test_lesson_3_has_checks_and_it_is_the_only_one():
    """lesson_3 is wired for static checks; other rubrics are model-only."""
    assert "lesson_3" in CHECKS_BY_ASSIGNMENT
    ids = [c.exercise_id for c in checks_for_assignment("lesson_3")]
    assert ids == ["ex3_1", "ex3_2", "ex3_3", "ex3_4"]
    assert checks_for_assignment("lesson_1") == ()
    assert checks_for_assignment("nonexistent") == ()


def test_lesson_3_json_matches_the_four_exercises():
    """The rubric file and the scanner must agree on what is being graded."""
    data = json.loads(LESSON_3.read_text(encoding="utf-8"))
    assert data["assignment_id"] == "lesson_3"
    names = [task["name"] for task in data["tasks"]]
    assert len(names) == 4
    for exercise_id in ("ex3_1", "ex3_2", "ex3_3", "ex3_4"):
        assert any(name.startswith(exercise_id) for name in names), exercise_id


COMPLETE = """\
name = input()
print(name)

first_name = input()
last_name = input()
print(first_name, last_name)

age = int(input())
height = float(input())
print(age, height)

q1 = input()
q2 = input()
q3 = input()
q4 = input()
q5 = input()
print(q1, q2, q3, q4, q5)
"""


def test_complete_submission_passes_all_exercises():
    scan = scan_submission(COMPLETE, checks_for_assignment("lesson_3"))
    assert not scan.empty
    assert scan.ok
    assert all(s.ok for s in scan.exercises)
    assert scan.input_assignments == 8
    assert scan.input_calls == 10  # 8 bare + int(input()) + float(input())


def test_ex3_1_missing_name_and_input():
    scan = scan_submission("print('hello')", checks_for_assignment("lesson_3"))
    ex3_1 = next(s for s in scan.exercises if s.exercise_id == "ex3_1")
    assert not ex3_1.ok
    assert ex3_1.verdict == "MISSING"
    assert "name" in ex3_1.missing_variables
    assert "input" in ex3_1.missing_commands


def test_ex3_2_requires_two_input_assignments():
    src = "first_name = input()\nprint(first_name)"
    scan = scan_submission(src, checks_for_assignment("lesson_3"))
    ex3_2 = next(s for s in scan.exercises if s.exercise_id == "ex3_2")
    assert not ex3_2.ok
    assert "last_name" in ex3_2.missing_variables
    assert ex3_2.short_input_assignments > 0


def test_ex3_3_requires_int_and_float_casts():
    src = (
        "first_name = input()\nlast_name = input()\n"
        "age = input()\nheight = input()\nprint(age)"
    )
    scan = scan_submission(src, checks_for_assignment("lesson_3"))
    ex3_3 = next(s for s in scan.exercises if s.exercise_id == "ex3_3")
    assert not ex3_3.ok
    assert "int" in ex3_3.missing_commands
    assert "float" in ex3_3.missing_commands


def test_ex3_4_needs_five_stored_answers():
    src = "q1 = input()\nq2 = input()\nq3 = input()\nprint(q1, q2, q3)"
    scan = scan_submission(src, checks_for_assignment("lesson_3"))
    ex3_4 = next(s for s in scan.exercises if s.exercise_id == "ex3_4")
    assert not ex3_4.ok
    assert ex3_4.short_input_assignments == 2  # has 3, needs 5
    assert "only 3 of 5" in "\n".join(scan.summary_lines())


def test_name_is_not_satisfied_by_first_name():
    """``first_name = input()`` must NOT count as declaring ``name``."""
    src = "first_name = input()\nprint(first_name)"
    scan = scan_submission(src, checks_for_assignment("lesson_3"))
    ex3_1 = next(s for s in scan.exercises if s.exercise_id == "ex3_1")
    assert "name" in ex3_1.missing_variables


def test_command_requires_call_parenthesis():
    """Mentioning ``input`` as a word is not a call site."""
    src = "name = 'I used input here'\nprint(name)"
    scan = scan_submission(src, checks_for_assignment("lesson_3"))
    ex3_1 = next(s for s in scan.exercises if s.exercise_id == "ex3_1")
    assert "input" in ex3_1.missing_commands


def test_rtl_and_zero_width_noise_is_ignored():
    noisy = "name‎ = input()\nprint(name)\u200c"
    cleaned = normalize_source(noisy)
    assert "‎" not in cleaned
    assert "\u200c" not in cleaned


def test_effectively_empty_placeholders():
    for placeholder in ("", "   ", "-", "--", "N/A", "none", "null", "."):
        assert is_effectively_empty(placeholder), repr(placeholder)
    assert not is_effectively_empty("name = input()")


def test_empty_submission_yields_empty_scan_and_no_failures():
    scan = scan_submission("-", checks_for_assignment("lesson_3"))
    assert scan.empty
    assert scan.exercises == ()
    assert scan.ok
    assert "No code text" in scan.summary_lines()[0]


def test_scanner_never_raises_on_garbage():
    for junk in ("", "\x00\x01\x02", "😀😀", "=" * 100, "def def def"):
        scan = scan_submission(junk, checks_for_assignment("lesson_3"))
        assert isinstance(scan.ok, bool)


def test_no_checks_means_no_exercises_even_for_valid_code():
    scan = scan_submission(COMPLETE, checks_for_assignment("lesson_1"))
    assert scan.exercises == ()
    assert scan.ok
