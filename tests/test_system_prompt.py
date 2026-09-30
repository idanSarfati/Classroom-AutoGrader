"""Guard tests for the grading instructions in ``src.llm_evaluator``.

The ``SYSTEM_PROMPT`` is the only enforcement of the grading policy (there is
no post-processing of the score), so the rules students depend on are pinned
here: a link instead of pasted code must never cost points, typos must be free,
a broken task must be skipped, and only real requirement/logic gaps may dock
points.

Run from the project root::

    python -m pytest tests/test_system_prompt.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm_evaluator import (  # noqa: E402
    SYSTEM_PROMPT,
    _build_prompt,
    _exempt_tasks_block,
)
import src.llm_evaluator as evaluator  # noqa: E402
from src.models import AssignmentConfig, TaskDefinition  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ASSIGNMENTS = ROOT / "assignments"

# The sentence students must see, verbatim, when they submit a link.
LINK_REMINDER_HE = (
    "שימו לב: בהגשות הבאות יש להדביק את הקוד עצמו ישירות בתוך המסמך "
    "כדי שאוכל לבדוק אותו בקלות ולתת משוב מפורט יותר."
)


def test_link_submissions_earn_full_credit() -> None:
    assert "Full Credit for Link Submissions" in SYSTEM_PROMPT
    assert "VALID, genuine attempt" in SYSTEM_PROMPT
    assert "NEVER deduct points" in SYSTEM_PROMPT


def test_link_grading_section_is_present() -> None:
    assert "Grading Submissions That Use Links Instead of Pasted Code" in SYSTEM_PROMPT
    # The model must not pretend it can open the link.
    assert "You cannot fetch or open URLs" in SYSTEM_PROMPT
    # A link-only document is still graded in the generous range.
    assert "just a link" in SYSTEM_PROMPT


def test_hebrew_link_reminder_is_verbatim() -> None:
    assert LINK_REMINDER_HE in SYSTEM_PROMPT
    assert "MUST NOT reduce the score" in SYSTEM_PROMPT
    # The reminder must not blow the 3-5 sentence output budget.
    assert "3-5 sentence budget" in SYSTEM_PROMPT


def test_scoring_curve_targets_90_to_95() -> None:
    assert "Target 90-95 for good work" in SYSTEM_PROMPT
    assert "2 to 5 points from the WHOLE assignment" in SYSTEM_PROMPT
    assert "never 85" in SYSTEM_PROMPT
    # The old 80-90 target must be gone from every section.
    assert "80-90" not in SYSTEM_PROMPT
    # Explicit score bands, with 80-85 reserved for substantial gaps.
    assert "* 90-95: thoughtful and correct overall" in SYSTEM_PROMPT
    assert "* 80-85: a substantial part is missing or wrong" in SYSTEM_PROMPT
    # The two examples the teacher called out are priced explicitly.
    assert "printing the sum of the values instead of the type of each one" in (
        SYSTEM_PROMPT
    )
    assert "hardcoding a string instead of using the variable or interpolation" in (
        SYSTEM_PROMPT
    )


def test_deduction_breakdown_contract_is_prompted() -> None:
    assert "Deduction Breakdown (deduction_breakdown" in SYSTEM_PROMPT
    assert "MUST sum exactly to (100 - score)" in SYSTEM_PROMPT
    assert "return an empty list []" in SYSTEM_PROMPT
    # The loose-mode fallback prompt must ask for it too, or those stages lose it.
    assert "deduction_breakdown" in evaluator._JSON_ONLY_INSTRUCTION


def test_prompt_stays_within_the_request_budget() -> None:
    # Rough token proxy: the prompt is sent on every single request, so keep it
    # from drifting into an essay as rules accumulate.
    assert len(SYSTEM_PROMPT) < 13000, "SYSTEM_PROMPT has grown too large"


# --------------------------------------------------------------------------
# Typo tolerance / real-gap deductions
# --------------------------------------------------------------------------


def test_typos_cost_nothing() -> None:
    assert "Typo & Formatting Tolerance" in SYSTEM_PROMPT
    assert "`pint` instead of `print`" in SYSTEM_PROMPT
    assert "keep the status CORRECT, deduct 0 points" in SYSTEM_PROMPT
    # Curly quotes and stray underscores are explicitly free.
    assert "smart” quotes" in SYSTEM_PROMPT or "“smart” quotes" in SYSTEM_PROMPT
    assert 'name = "_bob"_____' in SYSTEM_PROMPT


def test_only_real_gaps_are_deducted() -> None:
    assert "Deduct Points ONLY for Real Requirements / Logic Gaps" in SYSTEM_PROMPT
    # Docked: unmarked multiple-choice answers, hardcoded strings.
    assert "multiple-choice / true-false options" in SYSTEM_PROMPT
    assert "hardcoding a literal instead of using the variable" in SYSTEM_PROMPT
    # Never docked: the typo/formatting/naming classes.
    assert "typos, English spelling inside strings, quote style" in SYSTEM_PROMPT
    assert "When in doubt, do not deduct" in SYSTEM_PROMPT


def test_typo_feedback_is_friendly_and_score_neutral() -> None:
    assert "Typos & formatting slips (0 points)" in SYSTEM_PROMPT
    assert "does not affect the grade" in SYSTEM_PROMPT
    assert "never deduct for it" in SYSTEM_PROMPT


# --------------------------------------------------------------------------
# Exempt (broken) tasks
# --------------------------------------------------------------------------


def _load(name: str) -> AssignmentConfig:
    data = json.loads((ASSIGNMENTS / name).read_text(encoding="utf-8"))
    return AssignmentConfig.model_validate(data)


def test_prompt_tells_the_model_to_skip_exempt_tasks() -> None:
    assert '"exempt": true' in SYSTEM_PROMPT
    assert "ignore it completely" in SYSTEM_PROMPT


def test_worksheet_task_5_is_exempt() -> None:
    config = _load("lesson_2_worksheet.json")
    exempt = [task for task in config.tasks if task.exempt]
    assert [task.id for task in exempt] == [5]
    assert "תרגיל 5" in exempt[0].name

    block = _exempt_tasks_block(config)
    assert "SKIP THESE TASKS ENTIRELY" in block
    assert "task id 5" in block
    assert "do NOT dock points for it" in block


def test_well_formed_rubrics_get_no_skip_block() -> None:
    for name in ("lesson_1.json", "lesson_2.json"):
        config = _load(name)
        assert _exempt_tasks_block(config) == ""
        prompt = _build_prompt("Noa", "print('hi')", config)
        assert "SKIP THESE TASKS" not in prompt
        # No noise for rubrics with nothing broken.
        assert '"exempt"' not in prompt


def test_exempt_flag_is_visible_in_the_tasks_json() -> None:
    prompt = _build_prompt("Noa", "print('hi')", _load("lesson_2_worksheet.json"))
    tasks_json = prompt.split("Tasks (JSON):\n", 1)[1].split("\n\n", 1)[0]
    flagged = [task for task in json.loads(tasks_json) if task.get("exempt")]
    assert [task["id"] for task in flagged] == [5]


def test_exempt_defaults_to_false() -> None:
    task = TaskDefinition(id=1, name="Task 1", description="Print hello")
    assert task.exempt is False
    assert AssignmentConfig(
        assignment_id="x",
        title="X",
        tasks=[task],
    ).tasks[0].exempt is False
