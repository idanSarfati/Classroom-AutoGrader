"""The easier Unit 3 quiz: recognition plus one-line output tracing.

``unit3_input_easy`` exists for the classes that found ``unit3_input`` too hard
(ז'3 and ז'4). It keeps the unit's own skills - what ``input()`` returns, when a
conversion is needed, what a tiny program prints - but removes the two
production tasks: no "find and explain the bug" prose and no full program to
write. The multiple-choice questions are deterministic; the three tracing
questions (ב1-ב3) also carry an ``llm_review`` rubric at exact parity with the
deterministic lines check, so the model can accept an equivalent phrasing of
the expected output when it is on, and the deterministic mark stands when it
is off or out of quota.

The shared exam machinery (random version, shuffle, window, one attempt,
release, reopen) is already covered by the other exam test modules; these tests
prove the *content*: three versions at 100 points, a deterministic rubric that
adds up, a perfect sheet marked 100, and honest deductions for the mistakes the
quiz is about.
"""
from __future__ import annotations

import pytest

from portal.exams import checker
from portal.exams import config as exam_config
from portal.exams import llm_review
from portal.exams.paper import load_paper
from portal.scripts import exam_tools

EXAM = "unit3_input_easy"
VERSIONS = ("a", "b", "c")
A_KEYS = tuple(f"a{index}" for index in range(1, 11))
TRACE_KEYS = ("b1", "b2", "b3")


@pytest.fixture(autouse=True)
def _model_off(monkeypatch):
    """Content tests mark deterministically, whatever the environment says."""
    monkeypatch.setattr(llm_review, "enabled", lambda: False)


def _config():
    config = exam_config.load_exam(EXAM)
    assert config is not None, "the unit3_input_easy exam definition must load"
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
    for version in config.versions:
        assert version.rubric_path.is_file(), version.rubric_path
        assert load_paper(version).slug == version.rubric_path.stem


@pytest.mark.parametrize("version_key", VERSIONS)
def test_each_version_parses_at_100_points_in_two_parts(version_key) -> None:
    paper = load_paper(_config().version(version_key))
    assert len(paper.items) == 13
    assert paper.total_points == 100
    sections = paper.sections()
    assert [sum(item.points for item in items) for _, items in sections] == [
        70, 30,
    ]
    assert [len(items) for _, items in sections] == [10, 3]


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


@pytest.mark.parametrize("version_key", VERSIONS)
def test_the_paper_never_asks_the_student_to_write_code(version_key) -> None:
    """The easier exam is recognition and tracing only.

    A ``code_plain`` part is allowed only when it is ``given`` - the snippet to
    read. No editable code box, no explanation box, no debugging prose.
    """
    paper = load_paper(_config().version(version_key))
    for item in paper.items:
        for part in item.parts:
            if part.kind == "code_plain":
                assert part.given, f"{version_key}/{item.key}: editable code box"
                continue
            assert part.kind in {"choice", "short"}, (
                f"{version_key}/{item.key}: unexpected part {part.kind!r}"
            )


# ---------------------------------------------------------------------- parity


@pytest.mark.parametrize("version_key", VERSIONS)
def test_deterministic_rubric_adds_up_for_every_version(version_key) -> None:
    """Checks sum to each question, lines sum to their check, every chosen
    answer is a visible option."""
    paper = load_paper(_config().version(version_key))
    problems = exam_tools._rubric_problems(paper, _rules(version_key))
    assert problems == [], problems


@pytest.mark.parametrize("version_key", VERSIONS)
def test_the_tracing_questions_carry_a_model_rubric_at_parity(version_key) -> None:
    """ב1-ב3 are also judged by the model when it is on.

    The model's point slices must equal the deterministic rubric exactly (one
    ``response`` part at 10 points), and the multiple-choice questions stay
    deterministic-only. If either drifts, the checker falls back to the rules
    and the paper silently loses its model pass - so the parity is pinned here.
    """
    rules = _rules(version_key)
    for key in A_KEYS:
        assert "llm_review" not in rules[key], (version_key, key)
        assert rules[key]["checks"][0]["kind"] == "choice", (version_key, key)
    for key in TRACE_KEYS:
        rule = rules[key]
        assert rule["checks"][0]["kind"] == "lines", (version_key, key)
        review = rule.get("llm_review")
        assert isinstance(review, dict), f"{version_key}/{key} has no llm_review"
        assert str(review.get("guidelines") or "").strip(), (version_key, key)
        parts = review.get("parts") or []
        checks = rule["checks"]
        assert {p["part"] for p in parts} == {c["part"] for c in checks}, (
            version_key, key,
        )
        assert sum(p["max"] for p in parts) == sum(
            c["points"] for c in checks
        ) == 10, (version_key, key)


@pytest.mark.parametrize("version_key", VERSIONS)
def test_with_the_model_on_the_tracing_answers_go_to_one_batch(
    version_key, monkeypatch
) -> None:
    """The three tracing answers travel in ONE grade_paper call and a full
    verdict keeps the perfect score, now flagged as model-reviewed."""
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    seen: list[list] = []

    def _fake_grade_paper(request):
        seen.append(request)
        return {
            entry["item"]: {"response": (10, "ניסוח שקול תקין.")}
            for entry in request
        }

    monkeypatch.setattr(llm_review, "grade_paper", _fake_grade_paper)
    report, questions = _graded(version_key)
    assert report.score == 100
    assert report.llm_state == "reviewed"
    assert len(seen) == 1, "the whole paper must cost one model call"
    assert {entry["item"] for entry in seen[0]} == set(TRACE_KEYS)
    for key in TRACE_KEYS:
        assert questions[key].llm_reviewed
    for key in A_KEYS:
        assert not questions[key].llm_eligible


@pytest.mark.parametrize("version_key", VERSIONS)
def test_quota_stop_keeps_the_deterministic_marks_and_flags_waiting(
    version_key, monkeypatch
) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)

    def _quota(request):
        raise llm_review.QuotaExhausted("daily quota exhausted")

    monkeypatch.setattr(llm_review, "grade_paper", _quota)
    report, questions = _graded(version_key)
    assert report.score == 100  # deterministic marks stand
    assert report.llm_state == "waiting"
    for key in TRACE_KEYS:
        assert questions[key].llm_quota_blocked


# ------------------------------------------------------------------- marking


@pytest.mark.parametrize("version_key", VERSIONS)
def test_a_perfect_sheet_is_marked_100_with_every_question_correct(
    version_key,
) -> None:
    report, questions = _graded(version_key)
    assert report.score == 100
    assert all(q.status == checker.CORRECT for q in report.questions)
    assert len(questions) == 13


def test_the_exam_verifies_end_to_end() -> None:
    """Runs every tracing snippet in the sandbox and marks a perfect sheet."""
    assert exam_tools.verify(EXAM) == 0


def test_a_wrong_choice_loses_only_its_seven_points() -> None:
    report, questions = _graded("a", {"a1": {"response": ["int (מספר שלם)"]}})
    assert report.score == 93
    assert questions["a1"].status == checker.INCORRECT
    assert all(q.status == checker.CORRECT for q in report.questions if q.key != "a1")


@pytest.mark.parametrize("version_key", VERSIONS)
def test_a_mistyped_output_line_loses_its_ten_points(version_key) -> None:
    rules = _rules(version_key)
    entries = rules["b1"]["checks"][0]["lines"]
    expected = str(entries[0]["expected"])
    _report, questions = _graded(
        version_key, {"b1": {"response": expected + "?"}}
    )
    assert questions["b1"].earned == 0
    assert questions["b1"].status == checker.INCORRECT


def test_a_blank_tracing_answer_is_missing_not_wrong() -> None:
    """A student who wrote nothing loses the ten points but is not marked
    incorrect for a wrong answer."""
    report, questions = _graded("a", {"b2": {"response": ""}})
    assert questions["b2"].earned == 0
    assert questions["b2"].status == checker.MISSING
    assert report.score == 90
