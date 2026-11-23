"""Pure helpers behind the demo student seeder.

The seeder writes fabricated grades into the live database, so its rubric and
task shaping must be provably correct before anyone runs ``--apply``.
"""
from __future__ import annotations

from types import SimpleNamespace

from portal.scripts.seed_demo_student import (
    PASSWORD_ALPHABET,
    PASSWORD_LENGTH,
    build_tasks,
    generate_password,
    task_names,
)


def test_task_names_reads_lesson_rubrics() -> None:
    rubric = SimpleNamespace(
        tasks=[SimpleNamespace(name="הגדרה והשמה"), SimpleNamespace(name="פלט")]
    )

    assert task_names(rubric) == ["הגדרה והשמה", "פלט"]


def test_task_names_reads_exam_specs() -> None:
    rubric = SimpleNamespace(
        sections=[
            SimpleNamespace(
                section_name="חלק א",
                questions=[SimpleNamespace(id="q1"), SimpleNamespace(id="q2")],
            ),
            SimpleNamespace(
                section_name="",
                questions=[SimpleNamespace(id="q3")],
            ),
        ]
    )

    assert task_names(rubric) == ["חלק א · q1", "חלק א · q2", "q3"]


def test_task_names_falls_back_when_the_rubric_has_no_items() -> None:
    assert task_names(SimpleNamespace()) == ["המשימה"]


def test_partial_score_deducts_the_missing_points_once() -> None:
    tasks, deductions = build_tasks(["א", "ב", "ג"], 74, 1)

    assert [task["status"] for task in tasks] == [
        "CORRECT",
        "PARTIALLY_CORRECT",
        "CORRECT",
    ]
    assert deductions == [
        {
            "task_name": "ב",
            "points_deducted": 26,
            "reason": "חלק מהשלב חסר או לא מדויק.",
        }
    ]


def test_a_perfect_score_has_no_deductions() -> None:
    tasks, deductions = build_tasks(["א"], 100, 0)

    assert deductions == []
    assert tasks[0]["status"] == "CORRECT"


def test_partial_index_is_clamped_into_range() -> None:
    tasks, deductions = build_tasks(["א", "ב"], 90, 99)

    assert tasks[1]["status"] == "PARTIALLY_CORRECT"
    assert deductions[0]["task_name"] == "ב"


def test_generated_password_uses_the_safe_alphabet() -> None:
    password = generate_password()

    assert len(password) == PASSWORD_LENGTH
    assert set(password) <= set(PASSWORD_ALPHABET)