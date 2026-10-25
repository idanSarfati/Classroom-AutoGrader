"""Concurrent batch grading: request/outcome dataclasses and the thread-pool runner.

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
    _max_workers,
)
from src.evaluator.core import (
    evaluate_student_submission,
)
from src.evaluator.errors import (
    LLMEvaluationError,
)

logger = logging.getLogger(__name__)

@dataclass
class StudentGradeRequest:
    """One student's grading input for :func:`grade_students_batch`."""

    student_name: str
    raw_doc_text: str
    late_penalty_points: int = 0


@dataclass
class StudentGradeOutcome:
    """One student's grading outcome: result, error, or quota stop."""

    student_name: str
    result: GradingResult | None = None
    error: str | None = None
    quota_exhausted: bool = False


def _grade_one_request(
    req: StudentGradeRequest, rubric: Rubric, stop_event: threading.Event
) -> StudentGradeOutcome:
    """Grade one batch student; never raises (errors ride on the outcome)."""
    try:
        result = evaluate_student_submission(
            req.student_name,
            req.raw_doc_text,
            rubric,
            late_penalty_points=req.late_penalty_points,
        )
        return StudentGradeOutcome(student_name=req.student_name, result=result)
    except DailyQuotaExhausted as exc:
        stop_event.set()
        return StudentGradeOutcome(
            student_name=req.student_name,
            error=f"Daily quota exhausted: {exc}",
            quota_exhausted=True,
        )
    except LLMEvaluationError as exc:
        return StudentGradeOutcome(
            student_name=req.student_name,
            error=f"LLM evaluation failed: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - one bad student never kills a batch
        return StudentGradeOutcome(
            student_name=req.student_name,
            error=f"LLM evaluation failed: {exc}",
        )


def grade_students_batch(
    requests: Sequence[StudentGradeRequest],
    rubric: Rubric,
    *,
    max_workers: int | None = None,
    on_result: Any = None,
    pause: Any = None,
) -> list[StudentGradeOutcome]:
    """Grade many students concurrently, preserving input order.

    The grading loop is I/O-bound (each student = one HTTP round trip +
    waiting), so sequential grading leaves the machine idle ~95% of the time.
    A small thread pool overlaps those waits: each worker grades through the
    shared module router (thread-safe via its own lock, round-robins Groq
    keys across workers), so concurrent workers land on different keys/tiers
    instead of queueing on one - the "rotate the provider" idea, happening
    naturally.

    ``on_result`` (optional) fires on the *worker* thread after each student
    finishes: ``on_result(outcome, done, total)``. UI callers must only use it
    to stash thread-safe state (never touch Streamlit widgets from it - they
    are main-thread only).

    ``pause`` (optional) is called before each student's evaluation is
    submitted to the pool, throttling the request rate so concurrent workers
    do not burst Groq's per-minute output-token bucket (which truncates
    responses mid-JSON). Pass ``paced_pause`` from callers that want the same
    adaptive pacing the dashboard dry-run and daily check use.

    A :class:`~src.llm_router.DailyQuotaExhausted` from any worker stops new
    work from starting (already-running workers finish), and every
    not-yet-started student comes back with ``quota_exhausted=True`` instead
    of N identical errors.
    """
    items = list(requests)
    total = len(items)
    if not total:
        return []
    workers = max(1, max_workers if max_workers is not None else _max_workers())
    workers = min(workers, total)
    # When a pacing pause is supplied, serialize submissions so only one LLM
    # request is in flight at a time. Groq's free tier caps output tokens per
    # minute (~1000 TPM on qwen/qwen3.8-27b); 3 concurrent Hebrew evaluations
    # easily burst that budget and get truncated mid-JSON, which then burns
    # the fallback provider's daily quota. One-at-a-time keeps TPM safely under
    # the limit while the 2s pause keeps RPM under the 30 RPM cap.
    if pause is not None:
        workers = 1
    outcomes: list[StudentGradeOutcome | None] = [None] * total
    stop_event = threading.Event()
    completed = 0
    completed_lock = threading.Lock()

    def _done(outcome: StudentGradeOutcome) -> None:
        nonlocal completed
        with completed_lock:
            completed += 1
            done = completed
        if on_result is not None:
            try:
                on_result(outcome, done, total)
            except Exception:
                logger.debug("batch on_result failed", exc_info=True)

    if workers == 1:
        for index in range(total):
            if stop_event.is_set():
                outcomes[index] = StudentGradeOutcome(
                    student_name=items[index].student_name,
                    error="Not graded: daily quota exhausted earlier in this batch.",
                    quota_exhausted=True,
                )
            else:
                if pause is not None and index > 0:
                    pause()
                outcomes[index] = _grade_one_request(items[index], rubric, stop_event)
            _done(outcomes[index])  # type: ignore[arg-type]
        return [o for o in outcomes if o is not None]

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="grade"
    ) as pool:
        pending: dict[Any, int] = {}
        next_to_start = 0
        # Prime the pool, then feed the next student as each worker frees up:
        # keeps exactly ``workers`` requests in flight without ever starting
        # new work after a quota stop. ``pause`` is applied before each
        # submission so requests to Groq are spaced out instead of bursting
        # the per-minute output-token bucket.
        while next_to_start < min(workers, total) and not stop_event.is_set():
            if pause is not None and next_to_start > 0:
                pause()
            pending[pool.submit(_grade_one_request, items[next_to_start], rubric, stop_event)] = next_to_start
            next_to_start += 1
        while pending:
            finished, _ = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in finished:
                index = pending.pop(future)
                try:
                    outcomes[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - never lose a slot
                    outcomes[index] = StudentGradeOutcome(
                        student_name=items[index].student_name,
                        error=f"LLM evaluation failed: {exc}",
                    )
                _done(outcomes[index])  # type: ignore[arg-type]
                if next_to_start < total and not stop_event.is_set():
                    if pause is not None:
                        pause()
                    pending[pool.submit(_grade_one_request, items[next_to_start], rubric, stop_event)] = next_to_start
                    next_to_start += 1
        for index in range(total):
            if outcomes[index] is None:
                outcomes[index] = StudentGradeOutcome(
                    student_name=items[index].student_name,
                    error="Not graded: daily quota exhausted earlier in this batch.",
                    quota_exhausted=True,
                )
    return [o for o in outcomes if o is not None]
