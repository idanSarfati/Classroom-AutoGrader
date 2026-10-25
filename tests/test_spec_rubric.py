"""Spec-rubric wiring: resolution, schema detection, and prompt helpers."""

from __future__ import annotations

import json
from pathlib import Path

from src.config_match import (
    build_config_resolver,
    is_spec_file,
    list_config_files,
    read_config_title,
    score_config_for_assignment,
)
from src.llm_evaluator import (
    _build_prompt,
    _exempt_tasks_block,
    rubric_tasks_json,
    rubric_title,
)
from src.main import load_rubric, rubric_config_id
from src.models import AssignmentConfig, AssignmentSpec, CourseWork, TaskDefinition

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "configs" / "units_1_2_spec.json"
LESSON_PATH = ROOT / "assignments" / "lesson_1.json"
SHEET_PATH = ROOT / "assignments" / "lesson_2_worksheet.json"


def _load_spec() -> AssignmentSpec:
    spec = load_rubric(SPEC_PATH)
    assert isinstance(spec, AssignmentSpec)
    return spec


# --- schema detection -------------------------------------------------------


def test_load_rubric_detects_spec_schema() -> None:
    spec = load_rubric(SPEC_PATH)
    assert isinstance(spec, AssignmentSpec)
    assert spec.assignment_title == "תרגול יחידות 1-2"
    assert len(spec.sections) == 4
    assert sum(len(section.questions) for section in spec.sections) == 11
    assert spec.total_points == 100


def test_load_rubric_detects_legacy_schema() -> None:
    cfg = load_rubric(LESSON_PATH)
    assert isinstance(cfg, AssignmentConfig)
    assert cfg.tasks


def test_is_spec_file_and_title_readers() -> None:
    assert is_spec_file(SPEC_PATH) is True
    assert is_spec_file(LESSON_PATH) is False
    assert read_config_title(SPEC_PATH) == "תרגול יחידות 1-2"
    assert read_config_title(LESSON_PATH)  # non-empty legacy title


def test_list_config_files_includes_configs_dir() -> None:
    names = {path.name for path in list_config_files()}
    assert "units_1_2_spec.json" in names
    assert "lesson_1.json" in names


# --- resolution ------------------------------------------------------------


def test_resolver_maps_hebrew_title_to_spec() -> None:
    resolve = build_config_resolver()
    found = resolve(CourseWork(id="cw_spec_test", title="תרגול יחידות 1-2"))
    assert found is not None
    path, rubric = found
    assert path.name == "units_1_2_spec.json"
    assert isinstance(rubric, AssignmentSpec)


def test_spec_match_outranks_generic_number_heuristic() -> None:
    spec_score, _reason = score_config_for_assignment(
        SPEC_PATH, "תרגול יחידות 1-2"
    )
    lesson_score, _l_reason = score_config_for_assignment(
        LESSON_PATH, "תרגול יחידות 1-2"
    )
    assert spec_score >= 0.9  # title match, not the 0.85 number fallback
    assert spec_score > lesson_score


def test_partial_word_sets_do_not_match_the_spec() -> None:
    # A generic "תרגול" sheet must not steal the spec via intersection-only.
    score, _reason = score_config_for_assignment(SPEC_PATH, "תרגול אלגברה שיעור 3")
    assert score < 0.9


# --- prompt helpers for both schemas ---------------------------------------


def test_spec_prompt_is_question_by_question() -> None:
    spec = _load_spec()
    prompt, trimmed = _build_prompt("Noa", "Q1: 10 apples True\nQ2: first name", spec)
    assert not trimmed
    assert "q1" in prompt and "q11" in prompt
    assert "question-by-question" in prompt
    assert "correct_answer" in prompt
    assert _exempt_tasks_block(spec) == ""


def test_legacy_prompt_unchanged_shape() -> None:
    cfg = load_rubric(LESSON_PATH)
    assert isinstance(cfg, AssignmentConfig)
    prompt, trimmed = _build_prompt("Noa", "print('hi')", cfg)
    assert not trimmed
    assert "question-by-question" not in prompt
    assert "SKIP THESE TASKS" not in prompt
    # Well-formed rubrics keep ``exempt`` out of their JSON (defaults excluded).
    after = prompt.split("Tasks (JSON):\n", 1)[1]
    tasks_json, end = json.JSONDecoder().raw_decode(after)
    assert '"exempt"' not in after[:end]  # exact slice of the embedded JSON
    assert isinstance(tasks_json, list) and tasks_json  # still a valid JSON array


def test_exempt_sheet_still_flags_exempt_in_prompt() -> None:
    cfg = load_rubric(SHEET_PATH)
    assert isinstance(cfg, AssignmentConfig)
    prompt, _trimmed = _build_prompt("Noa", "answers", cfg)
    assert '"exempt": true' in prompt
    assert "SKIP THESE TASKS" in prompt


def test_rubric_tasks_json_for_both_schemas() -> None:
    spec = _load_spec()
    spec_json = rubric_tasks_json(spec)
    # Spec path joins per-question JSON objects; decode them sequentially.
    decoder = json.JSONDecoder()
    entries: list[dict] = []
    idx = 0
    while idx < len(spec_json):
        obj, offset = decoder.raw_decode(spec_json, idx)
        entries.append(obj)
        idx = offset
        while idx < len(spec_json) and spec_json[idx] in "\n\r ":
            idx += 1
    ids = {entry["id"] for entry in entries}
    assert "q1" in ids and "q11" in ids
    assert len(entries) == 11
    assert all("points" in entry for entry in entries)

    cfg = load_rubric(LESSON_PATH)
    legacy = json.loads(rubric_tasks_json(cfg))
    assert isinstance(legacy, list)
    assert all("id" in task for task in legacy)


def test_rubric_title_and_config_id() -> None:
    spec = _load_spec()
    assert rubric_title(spec) == "תרגול יחידות 1-2"
    assert rubric_config_id(spec) == "תרגול יחידות 1-2"
    cfg = load_rubric(LESSON_PATH)
    assert isinstance(cfg, AssignmentConfig)
    assert rubric_title(cfg) == cfg.title
    assert rubric_config_id(cfg) == cfg.assignment_id


def test_spec_assignment_total_is_consistent() -> None:
    spec = _load_spec()
    assert spec.total_points == 100
    assert sum(section.weight for section in spec.sections) == spec.total_points
