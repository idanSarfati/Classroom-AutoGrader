"""Smoke-test the evaluator split seams (temporary, deleted after the run)."""
from __future__ import annotations

import sys

failures = 0


def check(label: str, cond: bool) -> None:
    global failures
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures += 1


import src.llm_evaluator as m  # noqa: E402

# 1. Facade exports (consumer + test surface).
required = [
    "EVAL_PAUSE_SECONDS", "LLMEvaluationError", "LLMJSONParsingError",
    "LLMAPIError", "SYSTEM_PROMPT", "StudentGradeRequest",
    "StudentGradeOutcome", "Rubric", "_AUDIT_SIDECAR", "evaluate_student_submission",
    "grade_students_batch", "last_audit_for", "paced_pause", "_router",
    "repair_json", "_parse_payload", "_parse_result", "_build_prompt",
    "_exempt_tasks_block", "rubric_title", "rubric_tasks_json", "_STAGES",
    "_RESPONSE_FORMAT", "_JSON_ONLY_INSTRUCTION", "REASONING_EFFORT",
    "_RATE_LIMIT_RETRIES", "_call_completion", "_create_completion",
    "_get_client", "_pacer", "_first_working_stage", "_ROUTER",
    "_ROUTER_READY", "_client", "_serve_decision", "_note_rate_limited",
    "_rate_limit_wait", "grade_students_batch", "trim_document_text",
]
missing = [n for n in required if not hasattr(m, n)]
check(f"facade exports ({len(required)} names)", not missing)
if missing:
    print("   missing:", missing)

# 2. Function seam: patch on facade -> seen through the chain.
_orig_cc = m._create_completion
m._create_completion = lambda *a, **k: "PATCHED_COMPLETION"
check(
    "seam _create_completion via facade",
    m._create_completion(None, "s", "u", None) == "PATCHED_COMPLETION",
)
check(
    "seam _create_completion via transport module",
    __import__("src.evaluator.transport", fromlist=["x"])._create_completion(
        None, "s", "u", None
    )
    == "PATCHED_COMPLETION",
)
m._create_completion = _orig_cc  # restore

# 3. State seams: _ROUTER / _ROUTER_READY resolve + write-back.
m._ROUTER = "FAKE_ROUTER"
m._ROUTER_READY = True
check("state _router resolves facade", m._router() == "FAKE_ROUTER")
m._ROUTER_READY = False
# Rebuild attempt must not raise; result may be None (no keys in env).
try:
    m._router()
    check("state _router rebuild path", True)
except Exception as exc:  # noqa: BLE001
    check(f"state _router rebuild path ({exc})", False)
m._ROUTER = None
m._ROUTER_READY = False

# 4. _client state resolves through _get_client.
m._client = "STUB_CLIENT"
check("state _client via _get_client", m._get_client() == "STUB_CLIENT")
m._client = None

# 5. EVAL_PAUSE_SECONDS + _pacer patch visible to paced_pause fallback.
_orig_pacer = m._pacer
m._pacer = lambda: None
m.EVAL_PAUSE_SECONDS = 0.0
try:
    m.paced_pause()  # falls back to ticked_sleep(0) - instant
    check("paced_pause honours patched EVAL_PAUSE_SECONDS", True)
except Exception as exc:  # noqa: BLE001
    check(f"paced_pause honours patched EVAL_PAUSE_SECONDS ({exc})", False)
m._pacer = _orig_pacer
m.EVAL_PAUSE_SECONDS = 2.0

# 6. _first_working_stage read path resolves the facade value.
import src.evaluator.core as core  # noqa: E402

m._first_working_stage = 1
check(
    "state _first_working_stage resolves in core",
    __import__("src.seams", fromlist=["resolve"]).resolve(
        "src.llm_evaluator", "_first_working_stage", 0
    )
    == 1,
)
m._first_working_stage = 0
check("core module loads", core is not None)

# 7. Consumer imports.
from src.llm_evaluator import (  # noqa: E402
    EVAL_PAUSE_SECONDS,
    LLMEvaluationError,
    StudentGradeRequest,
    StudentGradeOutcome,
    evaluate_student_submission,
    grade_students_batch,
    last_audit_for,
    paced_pause,
    _router,
)

check("consumer from-imports", callable(evaluate_student_submission))

# 8. app facade still imports (UI split + evaluator split together).
try:
    import app  # noqa: F401, E402

    check("app imports", True)
except Exception as exc:  # noqa: BLE001
    check(f"app imports ({exc})", False)

print("SMOKE", "FAILED" if failures else "OK")
sys.exit(1 if failures else 0)
