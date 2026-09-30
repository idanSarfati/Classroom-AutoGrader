"""Unit tests for the Groq JSON repair / parsing pipeline.

Run from the project root::

    python -m pytest tests/test_json_repair.py -q

These are pure unit tests: no network, no API key. They cover the
``HTTP 400: Failed to generate JSON`` fallout - markdown fences, prose around
the payload, trailing commas, truncation and drifting key names - plus the
strict-schema -> ``json_object`` -> free-text degradation ladder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.llm_evaluator as evaluator  # noqa: E402
from src.llm_evaluator import (  # noqa: E402
    LLMAPIError,
    LLMJSONParsingError,
    LLMEvaluationError,
    repair_json,
)
from src.models import AssignmentConfig, DeductionItem, TaskDefinition  # noqa: E402

VALID_PAYLOAD = {
    "student_name": "Noa",
    "score": 93,
    "feedback_hebrew": "כל הכבוד! הקוד עובד מצוין, כדאי לשים לב לשמות משתנים.",
    "deduction_breakdown": [
        {
            "task_name": "Task 2",
            "points_deducted": 4,
            "reason": "Printed the sum instead of the type of each value.",
        },
        {
            "task_name": "Task 3",
            "points_deducted": 3,
            "reason": "Hardcoded the name in the string instead of using the variable.",
        },
    ],
    "task_evaluations": [
        {
            "task_name": "Task 1",
            "student_answer_found": "print('hello')",
            "status": "CORRECT",
            "notes": "Prints as expected.",
        },
        {
            "task_name": "Task 2",
            "student_answer_found": "print(sum)",
            "status": "PARTIALLY_CORRECT",
            "notes": "Sum instead of per-value types.",
        },
    ],
}

ASSIGNMENT = AssignmentConfig(
    assignment_id="lesson_1",
    title="Print statements",
    tasks=[TaskDefinition(id=1, name="Task 1", description="Print hello")],
)


def _completion(content: str) -> SimpleNamespace:
    """A minimal OpenAI-shaped chat completion carrying ``content``."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _http_error(status: int, message: str) -> APIStatusError:
    request = httpx.Request("POST", "https://api.groq.com/v1/chat/completions")
    return APIStatusError(
        message,
        response=httpx.Response(status, request=request),
        body={"error": {"message": message}},
    )


# --------------------------------------------------------------------------
# repair_json
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '~~~json\n{"a": 1}\n~~~',
        '  ```JSON\n  {"a": 1}\n  ```  ',
    ],
)
def test_repair_json_strips_markdown_fences(raw: str) -> None:
    assert repair_json(raw) == '{"a": 1}'


def test_repair_json_extracts_payload_from_prose() -> None:
    raw = (
        "Here is my evaluation of the submission:\n"
        '{"student_name": "Noa"}\n'
        "Let me know if you need anything else!"
    )
    assert repair_json(raw) == '{"student_name": "Noa"}'


def test_repair_json_removes_trailing_commas() -> None:
    raw = '{"a": 1, "b": [1, 2, ], }'
    assert json.loads(repair_json(raw)) == {"a": 1, "b": [1, 2]}


def test_repair_json_ignores_braces_inside_strings() -> None:
    raw = '```json\n{"notes": "uses print(\\"hi {name}\\") in the loop"}\n```'
    repaired = repair_json(raw)
    assert json.loads(repaired) == {"notes": 'uses print("hi {name}") in the loop'}


def test_repair_json_keeps_fence_like_text_inside_strings() -> None:
    raw = '{"notes": "write ``` to open a code block"}'
    assert json.loads(repair_json(raw))["notes"].startswith("write")


def test_repair_json_tolerates_empty_and_none() -> None:
    assert repair_json(None) == ""
    assert repair_json("   ") == ""


# --------------------------------------------------------------------------
# _parse_payload
# --------------------------------------------------------------------------


def test_parse_payload_accepts_a_clean_answer() -> None:
    result = evaluator._parse_payload(json.dumps(VALID_PAYLOAD))
    assert result.score == 93
    assert len(result.task_evaluations) == 2


def test_parse_payload_accepts_a_fenced_answer_with_preamble() -> None:
    raw = "```json\n" + json.dumps(VALID_PAYLOAD) + "\n```"
    assert evaluator._parse_payload(raw).score == 93


def test_parse_payload_recovers_truncated_output() -> None:
    truncated = json.dumps(VALID_PAYLOAD)[:-40]
    result = evaluator._parse_payload(truncated)
    assert result.score == 93
    assert result.task_evaluations[0].status == "CORRECT"


def test_parse_payload_coerces_drifting_key_names() -> None:
    raw = json.dumps(
        {
            "name": "Noa",
            "grade": "88/100",
            "feedback": "מעולה!",
            "tasks": [
                {
                    "name": "Task 1",
                    "answer": "print(1)",
                    "verdict": "correct",
                    "comment": "ok",
                },
                {"task": "Task 2", "answer": "", "verdict": "not found"},
            ],
        }
    )
    result = evaluator._parse_payload(raw)
    assert result.score == 88
    assert result.feedback_hebrew == "מעולה!"
    assert [task.status for task in result.task_evaluations] == [
        "CORRECT",
        "MISSING",
    ]


def test_parse_payload_clamps_and_rounds_the_score() -> None:
    raw = json.dumps({**VALID_PAYLOAD, "score": "ציון: 103.6"})
    assert evaluator._parse_payload(raw).score == 100


def test_parse_payload_unwraps_a_nested_result_object() -> None:
    raw = json.dumps({"grading_result": VALID_PAYLOAD})
    assert evaluator._parse_payload(raw).score == 93


def test_parse_payload_raises_with_a_content_snippet() -> None:
    with pytest.raises(LLMJSONParsingError) as excinfo:
        evaluator._parse_payload("I'm sorry, I cannot grade this submission.")
    # The new type must stay catchable through the documented base class.
    assert isinstance(excinfo.value, LLMEvaluationError)
    assert "cannot grade" in str(excinfo.value)


def test_parse_payload_raises_on_empty_content() -> None:
    with pytest.raises(LLMJSONParsingError):
        evaluator._parse_payload("   ")


def test_parse_result_reads_an_openai_shaped_response() -> None:
    result = evaluator._parse_result(
        _completion("```json\n" + json.dumps(VALID_PAYLOAD) + "\n```")
    )
    assert result.student_name == "Noa"


# --------------------------------------------------------------------------
# Stage degradation
# --------------------------------------------------------------------------


def _install_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip client construction (no API key needed) for the graded calls."""
    monkeypatch.setattr(evaluator, "_client", object())
    monkeypatch.setattr(evaluator, "_first_working_stage", 0)


def test_evaluate_degrades_after_failed_json_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch)
    formats: list[object] = []

    def fake_completion(client, system_prompt, user_prompt, response_format=None):
        formats.append(response_format)
        if response_format == evaluator._RESPONSE_FORMAT:
            raise _http_error(400, "Failed to generate JSON")
        return _completion("```json\n" + json.dumps(VALID_PAYLOAD) + "\n```")

    monkeypatch.setattr(evaluator, "_create_completion", fake_completion)

    result = evaluator.evaluate_student_submission("Noa", "print('hi')", ASSIGNMENT)

    assert result.score == 93
    assert result.student_name == "Noa"
    # Strict schema first, then the json_object fallback.
    assert formats == [evaluator._RESPONSE_FORMAT, {"type": "json_object"}]
    # The working stage is remembered for the rest of the batch.
    assert evaluator._first_working_stage == 1


def test_evaluate_degrades_after_an_unparseable_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch)
    answers = iter(["not json at all", json.dumps(VALID_PAYLOAD)])

    def fake_completion(client, system_prompt, user_prompt, response_format=None):
        return _completion(next(answers))

    monkeypatch.setattr(evaluator, "_create_completion", fake_completion)

    assert evaluator.evaluate_student_submission(
        "Noa", "print('hi')", ASSIGNMENT
    ).score == 93


def test_evaluate_does_not_retry_non_degradable_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch)
    calls: list[object] = []

    def fake_completion(client, system_prompt, user_prompt, response_format=None):
        calls.append(response_format)
        raise _http_error(403, "Invalid API Key")

    monkeypatch.setattr(evaluator, "_create_completion", fake_completion)

    with pytest.raises(LLMAPIError) as excinfo:
        evaluator.evaluate_student_submission("Noa", "print('hi')", ASSIGNMENT)

    assert excinfo.value.status_code == 403
    assert len(calls) == 1
    assert evaluator._first_working_stage == 0


def test_evaluate_reports_every_attempt_when_all_stages_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch)

    def fake_completion(client, system_prompt, user_prompt, response_format=None):
        raise _http_error(400, "Failed to generate JSON")

    monkeypatch.setattr(evaluator, "_create_completion", fake_completion)

    with pytest.raises(LLMEvaluationError) as excinfo:
        evaluator.evaluate_student_submission("Noa", "print('hi')", ASSIGNMENT)

    message = str(excinfo.value)
    assert "3 attempt(s)" in message
    assert "json_schema" in message and "json_object" in message


# --------------------------------------------------------------------------
# deduction_breakdown
# --------------------------------------------------------------------------


def test_deduction_breakdown_is_in_the_strict_schema() -> None:
    schema = evaluator._RESPONSE_FORMAT["json_schema"]["schema"]
    assert "deduction_breakdown" in schema["properties"]
    # Strict mode requires every property, so the model always emits it.
    assert "deduction_breakdown" in schema["required"]
    item = schema["$defs"]["DeductionItem"]
    assert set(item["required"]) == {"task_name", "points_deducted", "reason"}
    assert item["additionalProperties"] is False


def test_parse_payload_keeps_the_breakdown() -> None:
    result = evaluator._parse_payload(json.dumps(VALID_PAYLOAD))
    assert len(result.deduction_breakdown) == 2
    assert result.deduction_breakdown[0].task_name == "Task 2"
    assert result.deduction_breakdown[0].points_deducted == 4
    # The breakdown is what the teacher reads: it must survive into the report.
    dumped = json.loads(result.model_dump_json())
    assert dumped["deduction_breakdown"][1]["points_deducted"] == 3


def test_breakdown_defaults_to_empty_when_absent() -> None:
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "deduction_breakdown"}
    result = evaluator._parse_payload(json.dumps(payload))
    assert result.deduction_breakdown == []


def test_breakdown_coerces_alias_keys_and_shapes() -> None:
    payload = {
        "student_name": "Noa",
        "score": 95,
        "feedback": "מצוין",
        "deductions": [
            {"task": "Task 3", "points_lost": "-5", "why": "hardcoded string"},
        ],
        "tasks": [
            {
                "name": "Task 3",
                "answer": "print('x')",
                "verdict": "partially correct",
            }
        ],
    }
    result = evaluator._parse_payload(json.dumps(payload))
    assert len(result.deduction_breakdown) == 1
    item = result.deduction_breakdown[0]
    assert (item.task_name, item.points_deducted, item.reason) == (
        "Task 3",
        5,
        "hardcoded string",
    )


def test_breakdown_coerces_a_free_text_summary() -> None:
    payload = dict(VALID_PAYLOAD)
    payload["deduction_breakdown"] = "Task 3: -4 hardcoded string instead of variable"
    result = evaluator._parse_payload(json.dumps(payload))
    item = result.deduction_breakdown[0]
    assert item.task_name == "Task 3"
    assert item.points_deducted == 4
    assert "hardcoded string" in item.reason


def test_mismatched_breakdown_warns_but_keeps_the_grade(caplog) -> None:
    payload = dict(VALID_PAYLOAD)
    payload["deduction_breakdown"] = [
        {"task_name": "Task 2", "points_deducted": 2, "reason": "small gap"}
    ]  # sums to 2, but 100 - 93 = 7
    with caplog.at_level("WARNING", logger="src.llm_evaluator"):
        result = evaluator._parse_payload(json.dumps(payload))
    assert result.score == 93
    assert "sums to 2" in caplog.text


def test_deduction_item_rejects_negative_points() -> None:
    with pytest.raises(ValueError):
        DeductionItem(task_name="Task 1", points_deducted=-3, reason="bad")

