"""Single-pass LLM evaluation of student submissions (Teacher Context Injection).

One chat completion to Groq (model from ``settings.groq_model``, default
``qwen/qwen3.8-27b``, via the official ``openai`` SDK pointed at Groq's
OpenAI-compatible endpoint ``settings.groq_base_url``) receives the
assignment's task definitions plus the raw text of the student's Google Doc
and returns a structured :class:`~src.models.GradingResult`.

Structured output is enforced server-side with Groq's strict JSON-Schema mode
(``response_format={"type": "json_schema", ...}``, ``strict: true``), so the
payload normally matches the :class:`GradingResult` schema generated from the
Pydantic model, and it is validated again locally as a safety net.

Not every Groq model/runtime honours that mode, and Groq rejects some
structured-output requests with ``HTTP 400: Failed to generate JSON``. When
that happens the evaluator degrades one step at a time (strict schema ->
``json_object`` -> free text) and sanitises each answer locally: markdown
fences, prose around the payload, trailing commas and truncated output are all
repaired by :func:`repair_json` before the strict Pydantic validation runs.
"""

# ----------------------------------------------------------------------
# Backward-compatible facade
# ----------------------------------------------------------------------
# src/llm_evaluator.py is now a thin facade: every name the old monolith
# defined lives in a single-responsibility module under src/evaluator/ and
# is re-exported here, so ``import src.llm_evaluator`` and
# ``src.llm_evaluator.<name>`` - including test monkeypatching of seams and
# mutable state - keeps working unchanged.

from src.evaluator.audit import (
    _AUDIT_SIDECAR,
    _run_audit,
    last_audit_for,
)
from src.evaluator.batch import (
    StudentGradeOutcome,
    StudentGradeRequest,
    _grade_one_request,
    grade_students_batch,
)
from src.evaluator.config import (
    EVAL_PAUSE_SECONDS,
    MAX_DOC_CHARS,
    MAX_TOKENS,
    REASONING_EFFORT,
    Rubric,
    SYSTEM_PROMPT,
    TEMPERATURE,
    _BACKOFF_MULTIPLIER,
    _MAX_ATTEMPTS,
    _MAX_BACKOFF_SECONDS,
    _MAX_RETRY_AFTER_SECONDS,
    _RATE_LIMIT_BACKOFF_BASE,
    _RATE_LIMIT_RETRIES,
    _RATE_LIMIT_WAIT_CAP,
    _REQUEST_TIMEOUT_SECONDS,
    _RETRYABLE_STATUS_CODES,
    _SCHEMA_NAME,
    _STRICT_SCHEMA_KEYWORDS,
    _TRIM_MARKER,
    _TRIM_SEARCH_CHARS,
    _max_workers,
)
from src.evaluator.core import (
    _first_working_stage,
    evaluate_student_submission,
)
from src.evaluator.errors import (
    LLMAPIError,
    LLMEvaluationError,
    LLMJSONParsingError,
)
from src.evaluator.pacer import (
    _PACER,
    _pacer,
    paced_pause,
)
from src.evaluator.parsing import (
    _CLOSE_TO_OPEN,
    _DEDUCTION_KEY_ALIASES,
    _ERROR_SNIPPET_CHARS,
    _FENCE_LINE_RE,
    _GRADING_KEY_ALIASES,
    _STATUS_ALIASES,
    _TASK_KEY_ALIASES,
    _TRAILING_COMMA_RE,
    _WRAPPER_KEYS,
    _audit_deductions,
    _canonical_key,
    _close_truncated,
    _coerce_deduction,
    _coerce_deductions,
    _coerce_payload,
    _coerce_points,
    _coerce_score,
    _coerce_status,
    _coerce_task,
    _extract_content,
    _json_span,
    _norm_key,
    _one_line,
    _parse_payload,
    _parse_result,
    _rename_keys,
    _repair_candidates,
    _strip_code_fences,
    repair_json,
)
from src.evaluator.prompts import (
    _EXEMPT_HEADER,
    _build_prompt,
    _code_scan_block,
    _exempt_tasks_block,
    _snap_back,
    _snap_forward,
    _spec_scan_block,
    rubric_tasks_json,
    rubric_title,
    trim_document_text,
)
from src.evaluator.schema import (
    _CONCISE_REMINDER,
    _JSON_OBJECT_FORMAT,
    _JSON_ONLY_INSTRUCTION,
    _RESPONSE_FORMAT,
    _STAGES,
    _build_response_format,
    _to_strict_json_schema,
)
from src.evaluator.transport import (
    _ROUTER,
    _ROUTER_READY,
    _call_completion,
    _call_completion_once,
    _client,
    _client_for,
    _create_completion,
    _error_detail,
    _get_client,
    _is_degradable,
    _is_json_generation_error,
    _is_retryable_error,
    _log_retry,
    _note_rate_limited,
    _rate_limit_wait,
    _retry_after_seconds,
    _router,
    _serve_decision,
    _serve_from_router,
    _wait_before_retry,
)
