"""Strict JSON-Schema response format and the graded request stage ladder (json_schema -> json_object -> text).

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

from src.evaluator.config import (
    SYSTEM_PROMPT,
    _SCHEMA_NAME,
    _STRICT_SCHEMA_KEYWORDS,
)

logger = logging.getLogger(__name__)

def _to_strict_json_schema(node: Any) -> Any:
    """Project a Pydantic JSON schema onto Groq's strict-mode subset.

    Strict mode requires every object to declare ``additionalProperties:
    false`` and to list all of its properties in ``required``; annotations it
    does not understand are dropped. Numeric bounds such as ``score``'s
    ``ge=0/le=100`` therefore leave the wire schema, but they still hold: the
    response is validated against the untouched Pydantic model, and the system
    prompt states the 0-100 range.
    """
    if isinstance(node, list):
        return [_to_strict_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    strict: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _STRICT_SCHEMA_KEYWORDS:
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            # These two are name -> subschema maps, so their keys are field or
            # definition names rather than JSON-Schema keywords: recurse into
            # the values only, never filter the mapping itself.
            strict[key] = {
                name: _to_strict_json_schema(subschema)
                for name, subschema in value.items()
            }
        else:
            strict[key] = _to_strict_json_schema(value)
    properties = strict.get("properties")
    if isinstance(properties, dict) or strict.get("type") == "object":
        strict["additionalProperties"] = False
        if isinstance(properties, dict):
            strict["required"] = sorted(properties)
    return strict


def _build_response_format() -> dict[str, Any]:
    """OpenAI-compatible ``response_format`` enforcing the GradingResult schema."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": _SCHEMA_NAME,
            "strict": True,
            "schema": _to_strict_json_schema(
                GradingResult.model_json_schema()
            ),
        },
    }


# Built once: the schema is static, so there is no need to regenerate it per
# student (and any pydantic-schema mistake surfaces at import time).
_RESPONSE_FORMAT = _build_response_format()


# Degraded mode: Groq's ``json_object`` only guarantees syntactically valid JSON
# (no schema adherence), but it is supported by every model - used when strict
# mode is rejected or when a payload cannot be read.
_JSON_OBJECT_FORMAT: dict[str, Any] = {"type": "json_object"}


# Appended to the system prompt for the fallback stages. Without server-side
# schema enforcement the prompt itself has to pin the key names, otherwise the
# model freely invents its own (``grade``/``comments``/``tasks``...).
_JSON_ONLY_INSTRUCTION = """

OUTPUT FORMAT (overrides any conflicting instruction above):
Reply with ONLY the raw JSON object - no markdown, no code fences, no preamble,
no text before or after it. Use exactly these keys and these types:
{"student_name": string, "score": integer 0-100, "feedback_hebrew": string,
 "deduction_breakdown": [{"task_name": string, "points_deducted": integer,
 "reason": string}],
 "task_evaluations": [{"task_name": string, "student_answer_found": string,
 "status": "CORRECT" | "PARTIALLY_CORRECT" | "INCORRECT" | "MISSING",
 "notes": string}]}
Include one task_evaluations entry per assignment task, in order.
deduction_breakdown is REQUIRED: one entry per task that lost points, with the
points summing to (100 - score), or an empty list [] when nothing was deducted."""


# A truncated answer is unparseable; the fallback stages ask for a shorter one.
_CONCISE_REMINDER = """

IMPORTANT: your previous answer was cut off before the JSON was complete. Keep
the whole JSON short enough to finish: feedback_hebrew of at most 2-3
sentences, and at most 15 words per notes field."""


# Grading attempts, strongest guarantee first. Groq's strict JSON-Schema mode
# covers only select models and the endpoint can still answer HTTP 400 "Failed
# to generate JSON"; instead of losing the student we walk down this ladder and
# rely on the local repair/validation (``_parse_payload``) to keep the contract.
_STAGES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("json_schema", SYSTEM_PROMPT, _RESPONSE_FORMAT),
    (
        "json_object",
        SYSTEM_PROMPT + _JSON_ONLY_INSTRUCTION,
        _JSON_OBJECT_FORMAT,
    ),
    (
        "text",
        SYSTEM_PROMPT + _JSON_ONLY_INSTRUCTION + _CONCISE_REMINDER,
        None,
    ),
)
