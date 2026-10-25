"""Tests for the headless rubric matcher (no network, no Streamlit).

``src.config_match`` holds the "which rubric JSON belongs to this Classroom
assignment?" logic shared by the Evaluate tab / Dashboard dry-run (in
``app.py``) and the daily check's opt-in ``--grade`` mode (``src.daily_check``,
headless under Task Scheduler - it must never import ``app``).

Resolution order (never guesses blindly): the remembered
``course_work_id -> config`` mapping first, then a fuzzy title match, then
give up (``None``) so the caller skips the assignment with a visible reason.

Run from the project root::

    python -m pytest tests/test_config_match.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config_match  # noqa: E402
from src.models import CourseWork  # noqa: E402


def _rubric(directory: Path, name: str, title: str) -> Path:
    path = directory / name
    path.write_text(
        json.dumps(
            {"assignment_id": Path(name).stem, "title": title, "tasks": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_normalize_text_casefolds_and_strips_accents():
    assert config_match.normalize_text("SHALOM") == "shalom"
    assert config_match.normalize_text("caf\u00e9") == "cafe"


def test_text_tokens_splits_hebrew_and_latin():
    tokens = config_match.text_tokens("lesson 2 worksheet")
    assert {"lesson", "2", "worksheet"} <= tokens
    heb = config_match.text_tokens("ABC DEF")
    assert heb == {"abc", "def"}


def test_score_exact_assignment_id_token(tmp_path):
    cfg = _rubric(tmp_path, "lesson_2_worksheet.json", "Some title")
    score, reason = config_match.score_config_for_assignment(
        cfg, "Lesson_2_worksheet submission"
    )
    assert score == 1.0
    assert "lesson_2_worksheet" in reason


def test_score_matching_number(tmp_path):
    cfg = _rubric(tmp_path, "lesson_2.json", "Unrelated words here")
    score, _ = config_match.score_config_for_assignment(cfg, "Task 2")
    assert score == 0.85


def test_score_shared_words(tmp_path):
    cfg = _rubric(tmp_path, "worksheet.json", "Input output variables")
    score, reason = config_match.score_config_for_assignment(
        cfg, "input output homework"
    )
    assert score >= 0.2
    assert "input" in reason and "output" in reason


def test_score_no_match_returns_zero(tmp_path):
    cfg = _rubric(tmp_path, "worksheet.json", "zzz qqq")
    assert config_match.score_config_for_assignment(cfg, "AAA BBB") == (0.0, "")


def test_suggest_picks_best_and_empty_gives_none(tmp_path):
    good = _rubric(tmp_path, "lesson_2.json", "Lesson two")
    bad = _rubric(tmp_path, "other.json", "zzz qqq")
    path, score, _ = config_match.suggest_config_for_assignment(
        "Lesson_2", [bad, good]
    )
    assert path == good
    assert score == 1.0
    assert config_match.suggest_config_for_assignment("Anything", []) == (
        None,
        0.0,
        "",
    )


def test_read_config_title_missing_or_broken(tmp_path):
    assert config_match.read_config_title(tmp_path / "nope.json") == ""
    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    assert config_match.read_config_title(broken) == ""
    lst = tmp_path / "list.json"
    lst.write_text("[1]", encoding="utf-8")
    assert config_match.read_config_title(lst) == ""


def test_build_config_resolver_prefers_remembered_mapping(tmp_path, monkeypatch):
    cfg = _rubric(tmp_path, "lesson_9.json", "Lesson nine")
    monkeypatch.setattr(
        config_match, "resolve_mapped_config", lambda work_id: cfg
    )
    resolve = config_match.build_config_resolver([])
    assignment = CourseWork(id="w1", title="Something else entirely")
    found = resolve(assignment)
    assert found is not None
    assert found[0] == cfg
    assert found[1].assignment_id == "lesson_9"


def test_build_config_resolver_falls_back_to_fuzzy(tmp_path, monkeypatch):
    cfg = _rubric(tmp_path, "lesson_2.json", "Lesson two")
    monkeypatch.setattr(config_match, "resolve_mapped_config", lambda w: None)
    resolve = config_match.build_config_resolver([cfg])
    assignment = CourseWork(id="w2", title="Lesson_2")
    found = resolve(assignment)
    assert found is not None and found[0] == cfg


def test_build_config_resolver_gives_up_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(config_match, "resolve_mapped_config", lambda w: None)
    resolve = config_match.build_config_resolver([])
    assert resolve(CourseWork(id="w3", title="Unknown thing")) is None
