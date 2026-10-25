"""Single-submission evaluation: build prompt, walk the stage ladder, audit the result, apply the late penalty.

Extracted verbatim from :mod:`src.llm_evaluator` (the former monolith).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
)

from config.settings import settings
from src.code_checks import checks_for_assignment, scan_submission
from src.late_policy import apply_late_penalty
from src.llm_router import DailyQuotaExhausted
from src.models import (
    AssignmentConfig,
    AssignmentSpec,
    GradingResult,
    SpecSection,
)

from src.evaluator.audit import (
    _run_audit,
)
from src.evaluator.config import (
    Rubric,
)
from src.evaluator.errors import (
    LLMAPIError,
    LLMEvaluationError,
)
from src.evaluator.pacer import (
    _pacer,
)
from src.evaluator.parsing import (
    _one_line,
    _parse_result,
)
from src.evaluator.prompts import (
    _build_prompt,
    rubric_title,
)
from src.evaluator.schema import (
    _STAGES,
)
from src.evaluator.transport import (
    _call_completion,
    _get_client,
    _is_degradable,
    _is_json_generation_error,
)
from src.seams import resolve, seam_fn, set_on_facade

logger = logging.getLogger(__name__)

FACADE = "src.llm_evaluator"

# Index of the first stage that produced a usable payload. Groq rejections are
# deterministic for a given model, so remembering the working stage spares the
# batch a doomed round-trip (and its tokens) for every remaining student.
_first_working_stage = 0


def evaluate_student_submission(
    student_name: str,
    raw_doc_text: str,
    rubric: Rubric,
    late_penalty_points: int = 0,
) -> GradingResult:
    """Grade one submission with a single-pass Groq chat completion.

    Injects the assignment's task definitions (Teacher Context) together with
    the unparsed raw Google Doc text and keeps sampling tight for consistent,
    reproducible evaluations. Transient API failures (HTTP 429/500/502/503/504,
    network drops) are retried up to 4 attempts with exponential backoff.

    ``late_penalty_points`` is applied *after* grading by
    :func:`src.late_policy.apply_late_penalty` - the deadline is a policy, not
    something the model can be trusted to weigh in. It is applied here, at the
    one place every caller goes through, so the subtraction can never be lost
    in prompt drift or in one branch of a caller. The *value* however is the
    caller's to decide, because only the caller holds the submission and the
    coursework needed to tell whether the work was late at all
    (:func:`src.late_policy.penalty_for`). It therefore defaults to ``0``: an
    entry point that omits the argument grades late work identically to
    on-time work rather than inventing a penalty it has no evidence for. Every
    real entry point (``app.py``, ``src/main.py``) passes it explicitly from
    ``settings.late_penalty_points`` - see :func:`load_settings` for how that
    value resolves.

    If the strict JSON-Schema response cannot be produced (Groq answers
    ``HTTP 400: Failed to generate JSON``) or cannot be read back
    (:func:`_parse_payload`), the call degrades to ``json_object`` mode and
    then to free text, and only then raises :class:`LLMEvaluationError`.
    """
    global _first_working_stage

    client = _get_client()
    prompt, doc_trimmed = _build_prompt(student_name, raw_doc_text, rubric)

    failures: list[str] = []
    result: GradingResult | None = None
    start_stage = resolve(
        FACADE, "_first_working_stage", _first_working_stage
    )
    for index in range(start_stage, len(_STAGES)):
        label, system_prompt, response_format = _STAGES[index]
        try:
            response = _call_completion(
                client, system_prompt, prompt, response_format
            )
            result = _parse_result(response)
        except LLMEvaluationError as exc:
            failures.append(f"[{label}] {exc}")
            if not _is_degradable(exc):
                raise
            reason = (
                "structured output rejected by Groq"
                if isinstance(exc, LLMAPIError)
                and _is_json_generation_error(str(exc))
                else "unusable payload"
            )
            logger.warning(
                "Grading attempt '%s' failed (%s: %s); degrading the response format.",
                label,
                reason,
                _one_line(exc),
            )
            continue
        _first_working_stage = set_on_facade(
            FACADE, "_first_working_stage", index, _first_working_stage
        )
        break

    if result is None:
        raise LLMEvaluationError(
            "Groq returned no usable grading result after "
            f"{len(failures)} attempt(s): " + " || ".join(failures)
        )

    # The caller's student name is authoritative (guards against drift).
    result.student_name = student_name
    # Deadline policy runs last, on the finished grade, so it is applied the
    # same way for every caller and can never be lost in prompt drift.
    apply_late_penalty(result, late_penalty_points)
    # Deterministic audit pass (zero tokens): verifies score math, bounds,
    # status coherence, rubric coverage. Auto-fixes a provably-wrong score;
    # flags anything ambiguous. Verdict travels on the sidecar for GradingRow.
    try:
        _run_audit(result, rubric, f"{rubric_title(rubric)}/{student_name}")
        pacer = _pacer()
        if pacer is not None:
            pacer.report_success()
    except Exception:
        pass
    logger.info(
        "Graded '%s' for %s with %s: score=%d (%d task evaluations)%s",
        rubric_title(rubric),
        student_name,
        settings.groq_model,
        result.score,
        len(result.task_evaluations),
        " [document trimmed for the token budget]" if doc_trimmed else "",
    )
    return result

# ----------------------------------------------------------------------
# Seam wiring
# ----------------------------------------------------------------------
# Tests monkeypatch these on the ``src.llm_evaluator`` facade. The bodies above
# reference them as plain globals; Python resolves globals at call time,
# so rebinding them here to facade-resolving proxies honours a patch
# without touching any call site.
evaluate_student_submission = seam_fn("src.llm_evaluator", "evaluate_student_submission", evaluate_student_submission)
