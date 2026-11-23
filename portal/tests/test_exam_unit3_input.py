"""The Unit 3 input quiz is a second in-portal exam, built like units_1_3.

These tests use the real definition, worksheets and answer key for
``unit3_input``. The shared exam machinery (random version, shuffle, window,
one attempt, marking, release, reopen) is already exercised against units_1_3
in ``test_exam_engine.py``; what is new here is the *content*. So these prove
the three versions load, parse at 100 points, stay internally consistent (both
the deterministic rubric parity and the model point parity), grade a perfect
sheet 100/100, and actually deduct for the mistakes this quiz is about: a wrong
choice, a mistyped output line, a hardcoded print, a missing conversion and a
reversed diagnosis.
"""
from __future__ import annotations

import pytest

from portal.exams import checker
from portal.exams import config as exam_config
from portal.exams.paper import load_paper
from portal.scripts import exam_tools

EXAM = "unit3_input"
VERSIONS = ("a", "b", "c")
A_KEYS = ("a1", "a2", "a3", "a4", "a5")
OPEN_KEYS = ("b1", "b2", "b3", "c1", "c2", "d1")


def _config():
    config = exam_config.load_exam(EXAM)
    assert config is not None, "the unit3_input exam definition must load"
    return config


def _rules(version_key: str) -> dict:
    return checker.version_rules(checker.load_checks(_config()), version_key)


def _perfect(version_key: str) -> dict:
    config = _config()
    return exam_tools._perfect_answers(
        config, config.version(version_key), _rules(version_key)
    )


def _graded(version_key: str, overrides: dict | None = None):
    """A perfect sheet with chosen parts replaced, graded by the real checker."""
    config = _config()
    version = config.version(version_key)
    answers = _perfect(version_key)
    for key, values in (overrides or {}).items():
        answers[key].update(values)
    report = checker.check(config, version, answers)
    return report, {question.key: question for question in report.questions}


# ------------------------------------------------------------------- structure


def test_exam_loads_with_three_versions_and_generated_rubrics() -> None:
    config = _config()
    assert config.name == EXAM
    assert config.total_points == 100
    assert config.version_keys == VERSIONS
    # Every version points at a worksheet and a rubric that actually exist; the
    # rubric is generated from the paper, so it cannot drift from what is sat.
    for version in config.versions:
        assert version.rubric_path.is_file(), version.rubric_path
        assert load_paper(version).slug == version.rubric_path.stem


@pytest.mark.parametrize("version_key", VERSIONS)
def test_each_version_parses_at_100_points_in_four_parts(version_key) -> None:
    paper = load_paper(_config().version(version_key))
    assert len(paper.items) == 11
    assert paper.total_points == 100
    sections = paper.sections()
    assert [sum(item.points for item in items) for _, items in sections] == [
        25, 35, 20, 20,
    ]
    assert [len(items) for _, items in sections] == [5, 3, 2, 1]


def test_the_three_versions_share_one_structure() -> None:
    """Same sections, same item keys and points - only the content differs."""
    shapes = []
    for version_key in VERSIONS:
        paper = load_paper(_config().version(version_key))
        shapes.append(
            (
                tuple(name for name, _ in paper.sections()),
                tuple((item.key, item.points) for item in paper.items),
            )
        )
    assert shapes[0] == shapes[1] == shapes[2]


# ---------------------------------------------------------------------- parity


@pytest.mark.parametrize("version_key", VERSIONS)
def test_deterministic_rubric_adds_up_for_every_version(version_key) -> None:
    """Checks sum to each question, lines/groups/criteria sum to their check,
    every chosen answer is a visible option, every program has a reference."""
    paper = load_paper(_config().version(version_key))
    problems = exam_tools._rubric_problems(paper, _rules(version_key))
    assert problems == [], problems


@pytest.mark.parametrize("version_key", VERSIONS)
def test_model_point_slices_equal_the_deterministic_rubric(version_key) -> None:
    """The standing parity rule: an open item's llm_review parts must cover the
    checks exactly, each maximum equal to that check's points."""
    rules = _rules(version_key)
    for key in OPEN_KEYS:
        rule = rules[key]
        block = rule.get("llm_review")
        assert isinstance(block, dict), f"{version_key}/{key} has no llm_review"
        assert str(block.get("guidelines") or "").strip()
        check_parts = {c["part"]: int(c["points"]) for c in rule["checks"]}
        specs = block["parts"]
        assert {s["part"] for s in specs} == set(check_parts), key
        for spec in specs:
            assert int(spec["max"]) == check_parts[spec["part"]], (key, spec)
            assert str(spec.get("guidance") or "").strip(), (key, spec)


@pytest.mark.parametrize("version_key", VERSIONS)
def test_multiple_choice_items_stay_deterministic(version_key) -> None:
    """Part a is marked by the key alone; no model block may ride along."""
    rules = _rules(version_key)
    for key in A_KEYS:
        assert rules[key]["checks"][0]["kind"] == "choice"
        assert "llm_review" not in rules[key], f"{version_key}/{key}"


# ------------------------------------------------------------------- marking


@pytest.mark.parametrize("version_key", VERSIONS)
def test_a_perfect_sheet_is_marked_100_with_every_question_correct(
    version_key,
) -> None:
    report, questions = _graded(version_key)
    assert report.score == 100
    assert all(q.status == checker.CORRECT for q in report.questions)
    assert len(questions) == 11


def test_the_exam_verifies_end_to_end() -> None:
    """Runs every tracing snippet in the sandbox and marks a perfect sheet."""
    assert exam_tools.verify(EXAM) == 0


def test_a_wrong_choice_loses_only_its_five_points() -> None:
    report, questions = _graded("a", {"a1": {"response": ["99"]}})
    assert report.score == 95
    assert questions["a1"].status == checker.INCORRECT
    assert all(q.status == checker.CORRECT for q in report.questions if q.key != "a1")


@pytest.mark.parametrize("version_key", VERSIONS)
def test_a_mistyped_output_line_loses_only_that_line(version_key) -> None:
    """The first tracing line is corrupted; its share is lost, the rest stays."""
    rules = _rules(version_key)
    entries = rules["b1"]["checks"][0]["lines"]
    expected = [str(entry["expected"]) for entry in entries]
    lost = int(entries[0]["points"])
    _report, questions = _graded(
        version_key, {"b1": {"response": "\n".join([expected[0] + "?"] + expected[1:])}}
    )
    assert questions["b1"].earned == int(rules["b1"]["checks"][0]["points"]) - lost
    assert questions["b1"].status == checker.PARTIALLY_CORRECT


def test_a_hardcoded_print_fails_the_second_input_of_the_full_program() -> None:
    """d1 runs twice with different inputs; a fixed print cannot serve both."""
    hardcoded = (
        'game = input("Enter game name: ")\n'
        'hours = int(input("Enter weekly hours: "))\n'
        'rating = float(input("Enter fun rating (1-10): "))\n'
        'print("Game: Minecraft Hours: 10 Rating: 8.5")'
    )
    report, questions = _graded("a", {"d1": {"code": hardcoded}})
    # It runs, converts, and matches the first run - but the second run (Roblox)
    # is wrong, so exactly that 4-point slice is lost.
    assert questions["d1"].earned == 16
    assert questions["d1"].status != checker.CORRECT
    assert report.score == 96


def test_a_missing_float_conversion_loses_its_own_share() -> None:
    """Reading the rating as text still prints it, but the float slice is gone."""
    missing_float = (
        'game = input("Enter game name: ")\n'
        'hours = int(input("Enter weekly hours: "))\n'
        'rating = input("Enter fun rating (1-10): ")\n'
        'print("Game:", game, "Hours:", hours, "Rating:", rating)'
    )
    report, questions = _graded("a", {"d1": {"code": missing_float}})
    assert questions["d1"].earned == 16
    assert report.score == 96


def test_a_reversed_diagnosis_loses_the_whole_explanation() -> None:
    """c1 is a str*str TypeError; saying the fix is 'make it a string' is the
    opposite diagnosis and earns nothing, while the corrected code still pays."""
    _report, questions = _graded(
        "a", {"c1": {"explain": "the correct type is string"}}
    )
    assert questions["c1"].earned == 5  # only the corrected code
    assert questions["c1"].needs_review