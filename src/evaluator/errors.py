"""Error hierarchy raised by the LLM evaluation pipeline.

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


logger = logging.getLogger(__name__)

class LLMEvaluationError(Exception):
    """Raised when the Groq request, or its structured output, fails."""


class LLMJSONParsingError(LLMEvaluationError):
    """A completion arrived, but no valid ``GradingResult`` could be read from it.

    Raised for empty/garbled answers after every local repair attempt
    (:func:`_repair_candidates`) failed. Subclasses :class:`LLMEvaluationError`
    so existing callers keep catching it, while
    :func:`evaluate_student_submission` uses the narrower type to decide that a
    second request with a looser ``response_format`` is worth making.
    """


class LLMAPIError(LLMEvaluationError):
    """A Groq request failed at the HTTP level (kept for error handling)."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        # The provider's ``retry-after`` hint parsed from the original 429
        # response. It is captured at the raise site because the APIStatusError
        # (and its headers) is gone by the time the batch runner decides
        # whether to wait-and-retry this student or surface the failure.
        self.retry_after = retry_after
