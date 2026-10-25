"""Shared adaptive pacer and the between-students pause used by every batch path.

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
    EVAL_PAUSE_SECONDS,
)
from src.seams import resolve, seam_fn, set_on_facade

logger = logging.getLogger(__name__)

FACADE = "src.llm_evaluator"

_PACER: Any = None


def _pacer() -> Any:
    """Shared AdaptivePacer that replaces the fixed EVAL_PAUSE_SECONDS sleep."""
    global _PACER
    _cached = resolve(FACADE, "_PACER", _PACER)
    if _cached is None:
        try:
            from src.llm_router import AdaptivePacer
        except Exception:
            return None
        pause = resolve(FACADE, "EVAL_PAUSE_SECONDS", EVAL_PAUSE_SECONDS)
        _cached = AdaptivePacer(base_seconds=pause)
        _PACER = set_on_facade(FACADE, "_PACER", _cached, _PACER)
    return _cached


def paced_pause(on_tick: Any = None) -> None:
    """Sleep between students: adaptive when available, fixed otherwise.

    ``on_tick(remaining, total, reason)`` is forwarded to the pacer so a UI can
    render a live countdown across the wait instead of holding a frozen frame;
    without it the sleep is exactly the single blocking call it has always been.
    """
    pacer = _pacer()
    if pacer is None:
        from src.llm_router import ticked_sleep

        ticked_sleep(
            resolve(FACADE, "EVAL_PAUSE_SECONDS", EVAL_PAUSE_SECONDS),
            on_tick,
            "pacing",
        )
    else:
        pacer.pause(on_tick)

# ----------------------------------------------------------------------
# Seam wiring
# ----------------------------------------------------------------------
# Tests monkeypatch these on the ``src.llm_evaluator`` facade. The bodies above
# reference them as plain globals; Python resolves globals at call time,
# so rebinding them here to facade-resolving proxies honours a patch
# without touching any call site.
_pacer = seam_fn("src.llm_evaluator", "_pacer", _pacer)
