"""The portal-side quota handover shim (the stuck-homework half of #176).

The engine's transport raises ``DailyQuotaExhausted`` the moment a *fallback
pool* key answers with a daily-quota 429, but it never parks that key and
never consults the tertiary pool afterwards: every retry picks the same dead
key again and the configured tertiary provider (Cloudflare) never serves
homework. ``src/`` is read-only, so ``adapter._park_spent_fallback_pool``
records the evidence on the live router and ``grade_submission`` retries once.

These tests use a fake router and a fake engine call - no network, no keys,
and the engine's prompts/parsing/scores are never touched (parity preserved).
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from portal.grading import adapter
from grading_engine import llm_evaluator
from grading_engine.llm_router import DAILY_QUOTA_COOLDOWN_SECONDS, DailyQuotaExhausted
from grading_engine.models import GradingResult

# The exact shape of the message the engine's transport raises when a
# fallback-pool key reports its daily cap (see src/evaluator/transport.py).
FALLBACK_QUOTA_MESSAGE = (
    "Groq daily quota is exhausted and the fallback (gemini: gemini-2.0-flash) "
    "is also out of daily quota: retryDelay 79882s"
)


@pytest.fixture
def fake_router(monkeypatch):
    """A stand-in for the facade-owned router, with two unparked slots."""
    slots = [
        SimpleNamespace(cooldown_until=0.0, daily_until=0.0),
        SimpleNamespace(cooldown_until=0.0, daily_until=0.0),
    ]
    router = SimpleNamespace(_fallback_keys=slots)
    monkeypatch.setattr(llm_evaluator, "_ROUTER", router, raising=False)
    return router


def _fake_result() -> GradingResult:
    return GradingResult(
        student_name="נועה כהן",
        score=90,
        feedback_hebrew="עבודה טובה.",
        deduction_breakdown=[],
        task_evaluations=[],
    )


def _grade_kwargs() -> dict:
    return dict(
        student_id="1",
        student_name="נועה כהן",
        submission_id="portal-test",
        raw_doc_text="x = 5\nprint(x)\n",
        rubric=adapter.load_rubric("assignments/lesson_1.json"),
    )


# ------------------------------------------------------------------ parking


def test_park_marks_every_fallback_slot_for_the_daily_window(fake_router) -> None:
    before = time.monotonic()
    parked = adapter._park_spent_fallback_pool(
        DailyQuotaExhausted(FALLBACK_QUOTA_MESSAGE)
    )
    assert parked is True
    for slot in fake_router._fallback_keys:
        assert slot.daily_until >= before + DAILY_QUOTA_COOLDOWN_SECONDS
        assert slot.cooldown_until == slot.daily_until


def test_park_ignores_messages_that_are_not_a_fallback_daily_cap(
    fake_router,
) -> None:
    # A generic "all keys spent" message (the Groq pool's own signal) must not
    # park the fallback pool - the worker's defer path handles that one.
    assert (
        adapter._park_spent_fallback_pool(
            DailyQuotaExhausted("every key is out for today")
        )
        is False
    )
    # A fallback *per-minute* strike is not a daily cap either.
    assert (
        adapter._park_spent_fallback_pool(
            DailyQuotaExhausted("fallback key hit a per-minute limit")
        )
        is False
    )
    for slot in fake_router._fallback_keys:
        assert slot.daily_until == 0.0
        assert slot.cooldown_until == 0.0


def test_park_without_a_pool_is_a_safe_noop(monkeypatch) -> None:
    monkeypatch.setattr(llm_evaluator, "_ROUTER", None, raising=False)
    assert (
        adapter._park_spent_fallback_pool(
            DailyQuotaExhausted(FALLBACK_QUOTA_MESSAGE)
        )
        is False
    )
    monkeypatch.setattr(
        llm_evaluator, "_ROUTER", SimpleNamespace(_fallback_keys=[]), raising=False
    )
    assert (
        adapter._park_spent_fallback_pool(
            DailyQuotaExhausted(FALLBACK_QUOTA_MESSAGE)
        )
        is False
    )


# ------------------------------------------------------- the bounded retry


def test_grade_submission_retries_once_after_parking_the_dead_pool(
    monkeypatch, fake_router
) -> None:
    calls: list[dict] = []
    result = _fake_result()

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise DailyQuotaExhausted(FALLBACK_QUOTA_MESSAGE)
        return result

    monkeypatch.setattr(adapter, "evaluate_student_submission", fake_evaluate)
    artifact = adapter.grade_submission(**_grade_kwargs())

    assert len(calls) == 2
    assert artifact.result is result
    # The retry carries exactly the same inputs - parity is preserved.
    assert calls[0] == calls[1]
    # The dead pool is parked, so the router's next pick reaches tertiary.
    assert fake_router._fallback_keys[0].daily_until > 0.0


def test_grade_submission_reraises_other_quota_errors_without_retry(
    monkeypatch, fake_router
) -> None:
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        raise DailyQuotaExhausted("every key is out for today")

    monkeypatch.setattr(adapter, "evaluate_student_submission", fake_evaluate)
    with pytest.raises(DailyQuotaExhausted):
        adapter.grade_submission(**_grade_kwargs())

    assert len(calls) == 1
    assert fake_router._fallback_keys[0].daily_until == 0.0


def test_grade_submission_never_loops_when_the_retry_is_spent_too(
    monkeypatch, fake_router
) -> None:
    calls: list[dict] = []

    def fake_evaluate(**kwargs):
        calls.append(kwargs)
        raise DailyQuotaExhausted(FALLBACK_QUOTA_MESSAGE)

    monkeypatch.setattr(adapter, "evaluate_student_submission", fake_evaluate)
    with pytest.raises(DailyQuotaExhausted):
        adapter.grade_submission(**_grade_kwargs())

    # One bounded retry, never a loop: the worker defers the job as usual.
    assert len(calls) == 2