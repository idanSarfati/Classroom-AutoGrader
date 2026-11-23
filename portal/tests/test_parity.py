"""Phase 0 parity tests: the adapter must equal a direct engine call.

No network and no API key: the LLM boundary is replaced using the same seam
the existing suite uses (``src.llm_evaluator._create_completion``), so both
paths traverse the real prompt builder, parser, late policy and audit.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import grading_engine.llm_evaluator as evaluator
from grading_engine.settings import settings as engine_settings
from grading_engine.models import AssignmentConfig, AssignmentSpec, TaskDefinition
from portal.grading import adapter

PAYLOAD = {
    "student_name": "Noa",
    "score": 88,
    "feedback_hebrew": "עבודה טובה מאוד! הקוד עובד, אך יש לשים לב לשמות המשתנים.",
    "deduction_breakdown": [
        {
            "task_name": "Task 1",
            "points_deducted": 6,
            "reason": "Used a cryptic variable name instead of a descriptive one.",
        },
        {
            "task_name": "Task 2",
            "points_deducted": 6,
            "reason": "Missed the final print statement required by the task.",
        },
    ],
    "task_evaluations": [
        {
            "task_name": "Task 1",
            "student_answer_found": "x = 5",
            "status": "PARTIALLY_CORRECT",
            "notes": "Works, but the variable name is unclear.",
        },
        {
            "task_name": "Task 2",
            "student_answer_found": "print(x)",
            "status": "PARTIALLY_CORRECT",
            "notes": "Prints the value but not the required label.",
        },
    ],
}

RUBRIC = AssignmentConfig(
    assignment_id="parity_lesson",
    title="Parity lesson",
    tasks=[
        TaskDefinition(id=1, name="Task 1", description="Create a variable."),
        TaskDefinition(id=2, name="Task 2", description="Print the result."),
    ],
)

RAW_TEXT = "  x = 5\nprint(x)  # keep this spacing exactly  \n"


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the network boundary; record every user prompt we send."""
    recorded: dict = {"prompts": [], "formats": []}

    monkeypatch.setattr(evaluator, "_client", object())
    monkeypatch.setattr(evaluator, "_first_working_stage", 0)
    monkeypatch.setattr(evaluator, "_ROUTER_READY", True)
    monkeypatch.setattr(evaluator, "_ROUTER", None)
    # Pin the policy knobs so the parity test does not depend on a developer's
    # .env: audit must run, and the late penalty is exactly 10 points.
    monkeypatch.setattr(engine_settings, "llm_audit_enabled", True)
    monkeypatch.setattr(engine_settings, "llm_audit_autofix", True)
    monkeypatch.setattr(engine_settings, "late_penalty_points", 10)

    def fake_completion(client, system_prompt, user_prompt, response_format=None):
        recorded["prompts"].append(user_prompt)
        recorded["formats"].append(response_format)
        return _completion(json.dumps(PAYLOAD))

    monkeypatch.setattr(evaluator, "_create_completion", fake_completion)
    return recorded


def test_adapter_matches_direct_engine_call(fake_llm: dict) -> None:
    direct = evaluator.evaluate_student_submission("נועה", RAW_TEXT, RUBRIC)
    direct_audit = evaluator.last_audit_for(direct)

    artifact = adapter.grade_submission(
        student_id="student-1",
        student_name="נועה",
        submission_id="submission-1",
        raw_doc_text=RAW_TEXT,
        rubric=adapter.bind_rubric(RUBRIC),
    )

    # Grade, deductions and per-task verdicts are byte-for-byte identical.
    assert artifact.result.model_dump() == direct.model_dump()
    assert artifact.result.student_name == "נועה"

    # The prompt is the real one, unchanged, and the text was not altered.
    direct_prompt, adapter_prompt = fake_llm["prompts"]
    assert adapter_prompt == direct_prompt
    assert RAW_TEXT in adapter_prompt
    # Fidelity invariant: the prompt still calls it a Google Doc on purpose.
    assert "Google Doc" in adapter_prompt

    # Same audit verdict, serialised the same way the worker will store it.
    assert artifact.audit_dict() == adapter.audit_to_dict(direct_audit)
    assert artifact.audit is not None
    assert artifact.rubric_sha256 == adapter.bind_rubric(RUBRIC).sha256

    # The destructive sidecar was consumed exactly once, by the adapter.
    assert evaluator.last_audit_for(artifact.result) is None


@pytest.mark.parametrize(
    ("first_offset_hours", "expected_points"),
    [(-2, 0), (2, 10)],
)
def test_late_penalty_matches_direct_engine(
    fake_llm: dict,
    first_offset_hours: int,
    expected_points: int,
) -> None:
    due = datetime(2026, 5, 1, 12, 0)
    first = due + timedelta(hours=first_offset_hours)

    direct = evaluator.evaluate_student_submission(
        "נועה", RAW_TEXT, RUBRIC, late_penalty_points=expected_points
    )
    direct_audit = evaluator.last_audit_for(direct)

    artifact = adapter.grade_submission(
        student_id="student-1",
        student_name="נועה",
        submission_id="submission-1",
        raw_doc_text=RAW_TEXT,
        rubric=adapter.bind_rubric(RUBRIC),
        due_at=due,
        first_submitted_at=first,
        submitted_at=first,
    )

    assert artifact.late_penalty_points == expected_points
    assert artifact.result.score == direct.score == 88 - expected_points
    assert artifact.result.model_dump() == direct.model_dump()
    assert artifact.audit_dict() == adapter.audit_to_dict(direct_audit)


def test_first_submitted_at_wins_over_a_late_resubmission() -> None:
    due = datetime(2026, 5, 1, 12, 0)
    # Handed in on time, re-uploaded later: the original instant must win.
    points = adapter.late_penalty_points_for(
        student_id="s",
        student_name="נועה",
        submission_id="sub",
        due_at=due,
        first_submitted_at=due - timedelta(minutes=30),
        submitted_at=due + timedelta(hours=5),
    )
    assert points == 0

    # Handed in late, re-uploaded after: still late.
    points = adapter.late_penalty_points_for(
        student_id="s",
        student_name="נועה",
        submission_id="sub",
        due_at=due,
        first_submitted_at=due + timedelta(minutes=30),
        submitted_at=due + timedelta(hours=5),
    )
    assert points == 10


def test_late_penalty_is_zero_without_a_deadline() -> None:
    points = adapter.late_penalty_points_for(
        student_id="s",
        student_name="נועה",
        submission_id="sub",
        due_at=None,
        first_submitted_at=datetime(2030, 1, 1),
        submitted_at=datetime(2030, 1, 1),
    )
    assert points == 0


def test_bind_rubric_accepts_assignment_spec() -> None:
    spec = AssignmentSpec(
        assignment_title="Spec parity",
        topic="Loops",
        total_points=100,
        sections=[],
    )
    binding = adapter.bind_rubric(spec)
    assert isinstance(binding.rubric, AssignmentSpec)
    assert binding.title == "Spec parity"
    assert binding.question_count == 0


def test_load_rubric_hashes_and_rejects_a_changed_file(tmp_path) -> None:
    rubric_path = tmp_path / "rubric.json"
    rubric_path.write_text(
        json.dumps(
            {
                "assignment_id": "tmp",
                "title": "Temporary rubric",
                "tasks": [
                    {
                        "id": 1,
                        "name": "Task 1",
                        "description": "Do the thing.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # allowed_roots=() opts out of the project-directory allowlist for this
    # temporary file only; production uses the default roots.
    binding = adapter.load_rubric(rubric_path, allowed_roots=())

    assert binding.sha256 == adapter.hash_file(rubric_path)
    assert binding.title == "Temporary rubric"
    assert isinstance(binding.rubric, AssignmentConfig)

    with pytest.raises(adapter.RubricHashMismatch):
        adapter.load_rubric(
            rubric_path,
            expected_sha256="0" * 64,
            allowed_roots=(),
        )

    # A temp file outside assignments/ and configs/ is refused by default.
    with pytest.raises(adapter.UnsafeRubricPath):
        adapter.load_rubric(rubric_path)


def test_load_rubric_rejects_files_outside_the_rubric_roots() -> None:
    with pytest.raises(adapter.UnsafeRubricPath):
        # Exists in the repository root but is not teacher content.
        adapter.load_rubric(".env")


def test_load_rubric_uses_the_project_loader_for_both_schemas() -> None:
    legacy = adapter.load_rubric("assignments/lesson_1.json")
    assert isinstance(legacy.rubric, AssignmentConfig)
    assert legacy.sha256

    spec = adapter.load_rubric("configs/units_1_2_spec.json")
    assert isinstance(spec.rubric, AssignmentSpec)
    assert spec.question_count > 0
