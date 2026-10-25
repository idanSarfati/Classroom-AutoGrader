"""Deterministic audit verdicts attached to GradingResult via a sidecar map (GradingRow lives in src.main, importing it here would cycle).

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
    Rubric,
)

logger = logging.getLogger(__name__)

# --- Audit verdicts attached to GradingRow -----------------------------------
# ``GradingRow`` lives in ``src.main`` (importing it here would cycle), so the
# verdict travels on a sidecar map keyed by ``id(result)``: callers attach it
# to the row right after evaluation. Entries are tiny and popped on read.
_AUDIT_SIDECAR: dict[int, Any] = {}


def last_audit_for(result: Any) -> Any | None:
    """Pop the audit verdict recorded for ``result`` (None when absent)."""
    try:
        return _AUDIT_SIDECAR.pop(id(result), None)
    except Exception:
        return None


def _run_audit(
    result: GradingResult,
    config: Rubric,
    label: str,
) -> Any | None:
    """Run the deterministic audit pass; never breaks grading on error."""
    if not getattr(settings, "llm_audit_enabled", True):
        return None
    try:
        from src.grade_audit import audit_grading_result
    except Exception as exc:
        logger.debug("Audit pass unavailable: %s", exc)
        return None
    try:
        verdict = audit_grading_result(
            result,
            config,
            autofix=getattr(settings, "llm_audit_autofix", True),
        )
    except Exception as exc:  # noqa: BLE001 - audit must never break grading
        logger.warning("Audit pass failed for %s: %s", label, exc)
        return None
    if verdict is not None and not verdict.ok:
        logger.warning("Audit [%s]: %s", label, verdict.summary())
    try:
        _AUDIT_SIDECAR[id(result)] = verdict
    except Exception:
        pass
    return verdict
