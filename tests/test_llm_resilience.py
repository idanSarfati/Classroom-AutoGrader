"""Tests for the resilient LLM router + deterministic grade audit.

No network, no API keys: router rotation/cooldown/pacing and every audit
flag are exercised with fakes. Run: python -m pytest tests/test_llm_resilience.py -q
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

import httpx
import pytest
from openai import APIStatusError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.llm_evaluator as evaluator  # noqa: E402
from src.grade_audit import (
    FLAG_CORRECT_TASK_WITH_DEDUCTION,
    FLAG_DEDUCTION_SUM_MISMATCH,
    FLAG_EXEMPT_TASK_GRADED,
    FLAG_MISSING_TASK_EVALUATION,
    FLAG_UNKNOWN_TASK_ID,
    AuditVerdict,
    audit_grading_result,
    summarize_verdicts,
)
from src.llm_router import (
    MAX_RETRY_AFTER_SECONDS,
    AdaptivePacer,
    DailyQuotaExhausted,
    FALLBACK_PROVIDERS,
    LLMRouter,
    ProviderEndpoint,
    build_router_from_settings,
    parse_retry_after,
    resolve_fallback,
    router_fallback_status,
    split_keys,
)
from src.models import (
    AssignmentConfig,
    DeductionItem,
    GradingResult,
    TaskDefinition,
    TaskEvaluation,
)


def _ev(name, status, pts=0):
    from src.models import TaskEvaluation
    return TaskEvaluation(task_name=name, student_answer_found="x", status=status, notes="n")


def _config() -> AssignmentConfig:
    return AssignmentConfig(assignment_id="l1", title="L1", tasks=[TaskDefinition(id=1, name="T1", description="d1"), TaskDefinition(id=2, name="T2", description="d2")])


def _result(score=95, total_ded=5) -> GradingResult:
    ded = [DeductionItem(task_name="T2", points_deducted=total_ded, reason="gap")] if total_ded else []
    status2 = "PARTIALLY_CORRECT" if total_ded else "CORRECT"
    return GradingResult(student_name="Noa", score=score, feedback_hebrew="עבודה טובה.", deduction_breakdown=ded, task_evaluations=[_ev("T1", "CORRECT"), _ev("T2", status2)])


def test_split_keys_combines_and_dedupes():
    assert split_keys(" a ", "a,b ; c") == ["a", "b", "c"]
    assert split_keys(None, None) == []


def test_router_round_robins_keys():
    router = LLMRouter(groq_keys=["k1", "k2"], groq_model="m", groq_base_url="u")
    picks = [router.pick().endpoint.api_key for _ in range(4)]
    assert picks == ["k1", "k2", "k1", "k2"]


def test_router_cools_limited_key_and_serves_next():
    router = LLMRouter(groq_keys=["k1", "k2"], groq_model="m", groq_base_url="u")
    first = router.pick()
    assert first.endpoint.api_key == "k1"
    router.report_rate_limited(first.key_index, retry_after=120)
    second = router.pick()
    assert second.endpoint.api_key == "k2"


def test_router_falls_back_when_all_keys_cool():
    fb = ProviderEndpoint(name="gemini", base_url="https://x", model="gemini-2.0-flash", api_key="fb", is_fallback=True)
    router = LLMRouter(groq_keys=["k1"], groq_model="m", groq_base_url="u", fallback=fb)
    only = router.pick()
    router.report_rate_limited(only.key_index, retry_after=120)
    decision = router.pick()
    assert decision.key_index == -1 and decision.endpoint.model == "gemini-2.0-flash"


def test_router_success_clears_cooldown():
    router = LLMRouter(groq_keys=["k1", "k2"], groq_model="m", groq_base_url="u")
    first = router.pick()
    router.report_rate_limited(first.key_index, retry_after=120)
    router.report_success(first.key_index)
    served = {router.pick().endpoint.api_key for _ in range(4)}
    assert served == {"k1", "k2"}


def test_parse_retry_after_reads_header():
    class Resp:
        headers = {"retry-after": "7"}
    class Exc(Exception):
        response = Resp()
        body = None
    assert parse_retry_after(Exc()) == 7.0
    assert parse_retry_after(Exception("nope")) is None


def test_pacer_stretches_and_recovers(monkeypatch):
    sleeps: list = []
    monkeypatch.setattr("src.llm_router.time.sleep", sleeps.append)
    pacer = AdaptivePacer(base_seconds=2.0, max_seconds=30.0)
    pacer.pause()
    assert sleeps == [2.0]
    pacer.report_rate_limited()
    assert pacer.current > 2.0
    for _ in range(8):
        pacer.report_success()
    assert pacer.current == 2.0


def test_audit_clean_result_passes():
    verdict = audit_grading_result(_result(95, 5), _config())
    assert verdict.ok and not verdict.autofixed and verdict.deductions_total == 5


def test_audit_autofixes_sum_mismatch():
    res = _result(90, 5)
    verdict = audit_grading_result(res, _config(), autofix=True)
    assert verdict.autofixed and res.score == 95
    assert FLAG_DEDUCTION_SUM_MISMATCH in verdict.flags


def test_audit_flags_without_autofix():
    res = _result(90, 5)
    verdict = audit_grading_result(res, _config(), autofix=False)
    assert not verdict.ok and not verdict.autofixed and res.score == 90


def test_audit_catches_correct_with_deduction_and_missing_task():
    res = GradingResult(student_name="N", score=100, feedback_hebrew="x", deduction_breakdown=[DeductionItem(task_name="T1", points_deducted=3, reason="gap")], task_evaluations=[_ev("T1", "CORRECT")])
    verdict = audit_grading_result(res, _config())
    assert FLAG_CORRECT_TASK_WITH_DEDUCTION in verdict.flags
    assert FLAG_MISSING_TASK_EVALUATION in verdict.flags


def test_audit_catches_exempt_and_unknown_tasks():
    cfg = AssignmentConfig(assignment_id="w", title="W", tasks=[TaskDefinition(id=5, name="T5", description="broken", exempt=True)])
    res = GradingResult(student_name="N", score=100, feedback_hebrew="x", deduction_breakdown=[], task_evaluations=[_ev("T5", "CORRECT"), _ev("Ghost", "CORRECT")])
    verdict = audit_grading_result(res, cfg)
    assert FLAG_EXEMPT_TASK_GRADED in verdict.flags
    assert FLAG_UNKNOWN_TASK_ID in verdict.flags


# --- Severity tiers: systemic notes must not masquerade as per-grade work ----
def test_systemic_flags_are_info_and_do_not_block():
    """A missing task row is identical for every student: advice, not review.

    Flagging it per grade would put all 40 students in the "needs review"
    bucket over one prompt bug, which is how an audit gets ignored.
    """
    res = GradingResult(
        student_name="N",
        score=100,
        feedback_hebrew="x",
        deduction_breakdown=[],
        task_evaluations=[_ev("T1", "CORRECT")],  # T2 never reported
    )
    verdict = audit_grading_result(res, _config())
    assert FLAG_MISSING_TASK_EVALUATION in verdict.flags
    assert verdict.findings  # still reported, so the cause stays visible
    assert not verdict.needs_review
    assert verdict.summary()
    assert verdict.systemic and not verdict.blocking


def test_math_error_blocks_but_systemic_note_does_not():
    """Deduction math is per-student: it must stay in the review bucket."""
    res = GradingResult(
        student_name="N",
        score=100,
        feedback_hebrew="x",
        deduction_breakdown=[DeductionItem(task_name="T2", points_deducted=10, reason="gap")],
        task_evaluations=[_ev("T1", "CORRECT"), _ev("T2", "PARTIALLY_CORRECT")],
    )
    verdict = audit_grading_result(res, _config(), autofix=False)
    assert FLAG_DEDUCTION_SUM_MISMATCH in verdict.flags
    assert verdict.needs_review
    assert verdict.blocking


def test_verdict_round_trips_through_json():
    """The verdict survives the report file so Release can warn days later."""
    verdict = audit_grading_result(_result(90, 5), _config(), autofix=False)
    rebuilt = AuditVerdict.from_dict(verdict.to_dict())
    assert rebuilt.needs_review == verdict.needs_review
    assert rebuilt.flags == verdict.flags
    assert [f.severity for f in rebuilt.findings] == [f.severity for f in verdict.findings]


def test_summarize_splits_blocking_from_systemic():
    verdicts = [
        audit_grading_result(_result(95, 5), _config()),          # clean
        audit_grading_result(_result(90, 5), _config(), autofix=False),  # mismatch
        audit_grading_result(_missing_task_result(), _config()),
    ]
    stats = summarize_verdicts(verdicts)
    assert stats["total"] == 3
    assert stats["clean"] == 1
    assert stats["needs_review"] == 1
    assert stats["notes"] == 1
    assert FLAG_MISSING_TASK_EVALUATION in stats["systemic"]


# --- Daily (TPD) quota: fall over to the fallback, never stall -------------


def _router_with_fallback():
    return LLMRouter(
        groq_keys=["k1", "k2", "k3"],
        groq_model="qwen/qwen3.8-27b",
        groq_base_url="https://api.groq.com/openai/v1",
        fallback=ProviderEndpoint(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="some-free-model",
            api_key="fb",
            is_fallback=True,
        ),
    )


def test_daily_quota_is_recognised_not_treated_as_per_minute():
    """The whole TPD fix hinges on telling the two kinds of 429 apart."""
    from src.llm_router import is_daily_quota_error

    assert is_daily_quota_error("Rate limit reached for qwen: 200000 Tokens Per Day")
    assert is_daily_quota_error("Limit 200000, tokens per day exceeded")
    assert is_daily_quota_error("TPD budget exhausted")
    # A per-minute limit must NOT be classified as daily, or healthy keys would
    # be parked for hours and the fallback would take over needlessly.
    assert not is_daily_quota_error("Rate limit reached: 6000 tokens per minute")
    assert not is_daily_quota_error("Request too large")


def test_daily_quota_falls_straight_to_fallback_without_sleeping(monkeypatch):
    """TPD on every key must switch provider immediately, not stall the batch."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    router = _router_with_fallback()
    for index in range(router.key_count):
        router.report_daily_exhausted(index, retry_after=None)

    assert router.groq_daily_exhausted
    decision = router.pick()
    assert decision.endpoint.is_fallback
    assert decision.key_index == -1
    # The regression this guards: the old code slept for the full retry window.
    assert slept == []
    assert router.fallback_notice() and "daily" in router.fallback_notice()


def test_daily_quota_without_fallback_raises_instead_of_hanging(monkeypatch):
    """With no fallback, stop the run loudly rather than sleep for hours."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    router = LLMRouter(
        groq_keys=["k1", "k2"],
        groq_model="qwen/qwen3.8-27b",
        groq_base_url="https://api.groq.com/openai/v1",
    )
    for index in range(router.key_count):
        router.report_daily_exhausted(index)

    with pytest.raises(DailyQuotaExhausted):
        router.pick()
    assert slept == []
    assert "no fallback" in router.fallback_notice()


def test_sibling_key_still_served_while_only_one_key_is_daily_exhausted():
    """One key hitting the org-wide TPD must not sideline the healthy ones."""
    router = _router_with_fallback()
    router.report_daily_exhausted(0)
    assert not router.groq_daily_exhausted
    decision = router.pick()
    assert not decision.endpoint.is_fallback
    assert router.fallback_notice() is None


def test_per_minute_cooldown_still_waits_unlike_daily_quota(monkeypatch):
    """A per-minute 429 genuinely refills, so waiting is still correct there."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    router = LLMRouter(
        groq_keys=["k1"],
        groq_model="qwen/qwen3.8-27b",
        groq_base_url="https://api.groq.com/openai/v1",
        key_cooldown_seconds=3.0,
    )
    router.report_rate_limited(0, retry_after=1.0)
    decision = router.pick()
    assert not decision.endpoint.is_fallback
    assert slept, "a per-minute cooldown should still be waited out"


def test_daily_exhausted_key_recovers_after_the_reset_window(monkeypatch):
    """The park window is finite: Groq must come back next session."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    router = LLMRouter(
        groq_keys=["k1"],
        groq_model="qwen/qwen3.8-27b",
        groq_base_url="https://api.groq.com/openai/v1",
        fallback=ProviderEndpoint(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-2.0-flash",
            api_key="g",
            is_fallback=True,
        ),
    )
    router.report_daily_exhausted(0)
    assert router.pick().endpoint.is_fallback

    clock["t"] += 7 * 3600.0  # quota resets overnight
    assert not router.pick().endpoint.is_fallback


def _missing_task_result():
    """Helper: a grade whose only note is the systemic missing-task row."""
    return GradingResult(
        student_name="N",
        score=100,
        feedback_hebrew="x",
        deduction_breakdown=[],
        task_evaluations=[_ev("T1", "CORRECT")],
    )


# --- Fallback provider configuration ---------------------------------------


class _FakeSettings:
    """Minimal settings stand-in (getattr-only, like the real Settings)."""

    def __init__(self, **kw):
        self.groq_api_key = kw.pop("groq_api_key", "gsk_primary")
        self.groq_api_keys = kw.pop("groq_api_keys", None)
        self.groq_model = kw.pop("groq_model", "qwen/qwen3.8-27b")
        self.groq_base_url = kw.pop("groq_base_url", "https://api.groq.com/openai/v1")
        self.llm_fallback_provider = kw.pop("llm_fallback_provider", "")
        self.llm_fallback_model = kw.pop("llm_fallback_model", "")
        self.llm_fallback_api_key = kw.pop("llm_fallback_api_key", None)
        self.llm_fallback_base_url = kw.pop("llm_fallback_base_url", "")


def test_preset_provider_fills_base_url_and_model():
    """One env var + a key is enough - no base URL to remember."""
    endpoint, status = resolve_fallback(
        _FakeSettings(llm_fallback_provider="gemini", llm_fallback_api_key="AIza-test")
    )
    assert endpoint is not None, status
    assert endpoint.name == "gemini"
    assert endpoint.base_url == FALLBACK_PROVIDERS["gemini"].base_url
    assert endpoint.model == FALLBACK_PROVIDERS["gemini"].default_model
    assert endpoint.is_fallback is True
    assert endpoint.api_key == "AIza-test"


def test_preset_is_case_insensitive_and_trims_whitespace():
    endpoint, status = resolve_fallback(
        _FakeSettings(
            llm_fallback_provider="  OpenRouter ", llm_fallback_api_key="or-test"
        )
    )
    assert endpoint is not None, status
    assert endpoint.name == "openrouter"
    assert endpoint.base_url == "https://openrouter.ai/api/v1"


def test_explicit_values_override_the_preset():
    """Presets are a convenience, not a cage."""
    endpoint, status = resolve_fallback(
        _FakeSettings(
            llm_fallback_provider="gemini",
            llm_fallback_api_key="k",
            llm_fallback_model="gemini-2.5-pro",
            llm_fallback_base_url="https://example.test/v1",
        )
    )
    assert endpoint is not None, status
    assert endpoint.model == "gemini-2.5-pro"
    assert endpoint.base_url == "https://example.test/v1"


def test_explicit_endpoint_without_a_preset_name_still_works():
    endpoint, status = resolve_fallback(
        _FakeSettings(
            llm_fallback_base_url="https://my-gateway.test/v1",
            llm_fallback_model="my-model",
            llm_fallback_api_key="k",
        )
    )
    assert endpoint is not None, status
    assert endpoint.base_url == "https://my-gateway.test/v1"
    assert endpoint.model == "my-model"


def test_no_fallback_is_reported_not_crashing():
    endpoint, status = resolve_fallback(_FakeSettings())
    assert endpoint is None
    assert "no fallback configured" in status


def test_half_configured_fallback_names_the_missing_piece():
    """The easy mistake: a model with no key. It must say so, not fail later."""
    endpoint, status = resolve_fallback(
        _FakeSettings(llm_fallback_provider="gemini")
    )
    assert endpoint is None
    assert "LLM_FALLBACK_API_KEY" in status


def test_key_without_a_provider_or_endpoint_names_the_missing_model():
    endpoint, status = resolve_fallback(_FakeSettings(llm_fallback_api_key="k"))
    assert endpoint is None
    assert "LLM_FALLBACK_PROVIDER" in status


def test_unknown_provider_lists_the_known_ones():
    endpoint, status = resolve_fallback(
        _FakeSettings(llm_fallback_provider="anthropic", llm_fallback_api_key="k")
    )
    assert endpoint is None
    assert "unknown" in status
    assert "gemini" in status and "openrouter" in status


def test_router_built_from_settings_carries_the_fallback():
    router = build_router_from_settings(
        _FakeSettings(
            groq_api_key="gsk_1",
            groq_api_keys="gsk_2,gsk_3",
            llm_fallback_provider="openrouter",
            llm_fallback_api_key="or-test",
        )
    )
    assert router is not None
    assert router.key_count == 3
    assert router.has_fallback is True
    assert router._fallback.name == "openrouter"


def test_router_without_any_groq_key_is_none():
    assert build_router_from_settings(_FakeSettings(groq_api_key=None)) is None


def test_router_without_a_fallback_still_builds():
    """A missing fallback degrades to fail-fast, it does not break grading."""
    router = build_router_from_settings(_FakeSettings(groq_api_key="gsk_1"))
    assert router is not None
    assert router.has_fallback is False
    assert router.key_count == 1


def test_fallback_status_explains_an_unconfigured_router():
    assert "no fallback configured" in router_fallback_status(
        build_router_from_settings(_FakeSettings(groq_api_key="gsk_1"))
    )


def test_fallback_status_reports_an_active_fallback():
    router = build_router_from_settings(
        _FakeSettings(
            groq_api_key="gsk_1",
            llm_fallback_provider="gemini",
            llm_fallback_api_key="k",
        )
    )
    status = router_fallback_status(router)
    assert "fallback ready" in status
    assert "gemini" in status


def test_fallback_status_from_settings_alone():
    """The UI can explain the gap before any grading has run."""
    status = router_fallback_status(None, _FakeSettings())
    assert "unavailable" in status
    assert "LLM_FALLBACK_PROVIDER" in status


def test_settings_read_the_fallback_provider_env(monkeypatch):
    """LLM_FALLBACK_PROVIDER reaches Settings normalised (lower/trimmed)."""
    from config.settings import load_settings

    monkeypatch.setenv("GROQ_API_KEY", "gsk_1")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "  Gemini  ")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "AIza-test")
    app_settings = load_settings()
    assert app_settings.llm_fallback_provider == "gemini"
    assert app_settings.llm_fallback_api_key == "AIza-test"


def test_daily_quota_word_boundaries_block_false_positives():
    """A token merely *containing* tpd/rpd must not park a healthy key.

    ``_DAILY_QUOTA_PATTERN`` matches those acronyms as whole words. Anything
    less precise parks a key for six hours on an unrelated message - the exact
    misclassification bug the word boundaries were added to stop.
    """
    from src.llm_router import is_daily_quota_error

    # Substrings inside larger words are NOT daily caps.
    assert not is_daily_quota_error("tpdx exceeded")
    assert not is_daily_quota_error("crpd lookup failed")
    assert not is_daily_quota_error("the update failed")
    # Slash/hyphen variants that ARE daily caps still match.
    assert is_daily_quota_error("tokens/day quota exhausted")
    assert is_daily_quota_error("requests-per-day limit hit")
    assert is_daily_quota_error("per-day budget gone")


def test_daily_quota_matches_case_insensitively():
    from src.llm_router import is_daily_quota_error

    assert is_daily_quota_error("TOKENS PER DAY exceeded")
    assert is_daily_quota_error("Tokens Per Day reached")
    assert is_daily_quota_error("TPD exhausted")


def test_a_silent_429_is_treated_as_per_minute():
    """A 429 with no day/minute marker defaults to the wait-and-retry path."""
    from src.llm_router import is_daily_quota_error

    assert not is_daily_quota_error("Rate limit reached for requests")
    assert not is_daily_quota_error("")
    assert not is_daily_quota_error("quota exceeded for this window")


# --- The 429 plumbing in src.llm_evaluator: no helper left nameless ---------
# _note_rate_limited used to reference is_daily_quota_error without importing
# it into its own scope (the import existed only in its callers), so the very
# first 429 of a run died with NameError instead of parking the key. These
# tests drive every 429 site in llm_evaluator with a real router, fakes only.


def _http429(message: str) -> APIStatusError:
    """A Groq-shaped HTTP 429 carrying ``message`` in its error body."""
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return APIStatusError(
        message,
        response=httpx.Response(429, request=request),
        body={"error": {"message": message}},
    )


def _http429_after(message: str, retry_after: float) -> APIStatusError:
    """A Groq-shaped HTTP 429 that also sends a ``retry-after`` header hint."""
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return APIStatusError(
        message,
        response=httpx.Response(
            429, request=request, headers={"retry-after": str(retry_after)}
        ),
        body={"error": {"message": message}},
    )


def _install_router(monkeypatch, router) -> None:
    """Make ``_router()`` return ``router`` without touching settings/keys."""
    monkeypatch.setattr(evaluator, "_ROUTER", router)
    monkeypatch.setattr(evaluator, "_ROUTER_READY", True)


def _no_sleeps(monkeypatch) -> list[float]:
    """Record any ``time.sleep`` so a test can assert the path never blocks."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
    return slept


def test_note_rate_limited_classifies_a_daily_429_and_parks_the_key(monkeypatch):
    """The exact reported bug: a daily 429 must not raise NameError here."""
    router = LLMRouter(
        groq_keys=["k1", "k2"],
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
    )
    _install_router(monkeypatch, router)
    slept = _no_sleeps(monkeypatch)

    daily = evaluator._note_rate_limited(
        _http429("Rate limit reached for qwen: 200000 Tokens Per Day"),
        0,
        parse_retry_after,
    )

    assert daily is True
    # k1 is parked for the day; rotation reaches k2 with no sleeping at all.
    assert router.pick().endpoint.api_key == "k2"
    assert slept == []


def test_note_rate_limited_cools_only_for_a_per_minute_429(monkeypatch):
    """A per-minute 429 cools its key - it must not be read as a daily cap."""
    router = LLMRouter(
        groq_keys=["k1", "k2"],
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
    )
    _install_router(monkeypatch, router)
    slept = _no_sleeps(monkeypatch)

    daily = evaluator._note_rate_limited(
        _http429("Rate limit reached: 6000 tokens per minute"),
        0,
        parse_retry_after,
    )

    assert daily is False
    # Cooled, not parked: the healthy k2 serves the next pick without waiting.
    assert router.pick().endpoint.api_key == "k2"
    assert slept == []


def test_call_completion_hands_a_daily_quota_to_the_fallback(monkeypatch):
    """Daily quota + configured fallback: re-serve from the fallback cleanly."""
    router = LLMRouter(
        groq_keys=["k1"],
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
        fallback=ProviderEndpoint(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="some-free-model",
            api_key="fb",
            is_fallback=True,
        ),
    )
    _install_router(monkeypatch, router)
    served: list[str] = []

    def fake_completion(client, _system, _user, _response_format=None, **_kwargs):
        served.append(client.api_key)
        if len(served) == 1:
            raise _http429("Rate limit reached for qwen: 200000 Tokens Per Day")
        return {"graded": True}

    monkeypatch.setattr(evaluator, "_create_completion", fake_completion)

    # The router decision replaces the caller's client, so None is fine here.
    out = evaluator._call_completion(None, "sys", "user", None)

    assert out == {"graded": True}
    assert served == ["k1", "fb"]
    assert router.fallback_uses == 1


def test_call_completion_fails_fast_when_the_daily_quota_has_no_fallback(monkeypatch):
    """No fallback left: stop with DailyQuotaExhausted, not NameError/retries."""
    router = LLMRouter(
        groq_keys=["k1"],
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
    )
    _install_router(monkeypatch, router)

    def always_daily(client, *_args, **_kwargs):
        raise _http429("TPD budget exhausted")

    monkeypatch.setattr(evaluator, "_create_completion", always_daily)

    with pytest.raises(DailyQuotaExhausted, match="daily token quota"):
        evaluator._call_completion(None, "sys", "user", None)

    assert router.groq_daily_exhausted


def test_call_completion_rotates_to_the_next_key_after_a_per_minute_429(monkeypatch):
    """A per-minute 429 rotates k1 -> k2 on the same attempt, without sleeping."""
    router = LLMRouter(
        groq_keys=["k1", "k2"],
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
    )
    _install_router(monkeypatch, router)
    slept = _no_sleeps(monkeypatch)
    served: list[str] = []

    def throttled_once(client, *_args, **_kwargs):
        served.append(client.api_key)
        if len(served) == 1:
            raise _http429("Rate limit reached for requests")
        return {"graded": True}

    monkeypatch.setattr(evaluator, "_create_completion", throttled_once)

    out = evaluator._call_completion(None, "sys", "user", None)

    assert out == {"graded": True}
    assert served == ["k1", "k2"]
    assert slept == []


def test_serve_decision_reports_a_groq_429_before_raising_llmapierror(monkeypatch):
    """The routed-retry path records the 429 (no NameError) then surfaces it."""
    router = LLMRouter(
        groq_keys=["k1", "k2"],
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
    )
    _install_router(monkeypatch, router)
    slept = _no_sleeps(monkeypatch)
    decision = router.pick()
    assert decision.key_index == 0

    def throttled(client, *_args, **_kwargs):
        raise _http429("Rate limit reached for requests")

    monkeypatch.setattr(evaluator, "_create_completion", throttled)

    with pytest.raises(evaluator.LLMAPIError) as excinfo:
        evaluator._serve_decision(decision, router, "sys", "user", None)

    assert "429" in str(excinfo.value)
    # The report cooled k1: the next pick is served by k2 with no sleep.
    assert router.pick().endpoint.api_key == "k2"
    assert slept == []


def test_serve_decision_stops_when_the_fallback_is_out_of_daily_quota(monkeypatch):
    """A fallback daily-429 stops the run - it must not crash or misreport."""
    router = _router_with_fallback()
    _install_router(monkeypatch, router)
    router.report_daily_exhausted(0)
    router.report_daily_exhausted(1)  # second reporter -> whole pool parked
    decision = router.pick()
    assert decision.endpoint.is_fallback

    def fallback_daily(client, *_args, **_kwargs):
        raise _http429("Limit 200000, tokens per day exceeded")

    monkeypatch.setattr(evaluator, "_create_completion", fallback_daily)

    with pytest.raises(DailyQuotaExhausted, match="also out of daily quota"):
        evaluator._serve_decision(decision, router, "sys", "user", None)


# --------------------------------------------------------------------------- #
# Per-minute OTPM back-off: ride out a 429 instead of writing an ERROR row
#
# The reported bug: a heavy batch fires completions back-to-back, exhausts
# Groq's ~1000-OTPM free-tier bucket mid-run, and the failing students land in
# the exported CSV as ERROR rows. Tenacity never sleeps on a 429 (routing owns
# it) and the router only re-serves a throttled attempt once - not always
# enough when the rotated key or the fallback is limited too. These tests pin
# the wait-and-re-route loop that closes that gap.
# --------------------------------------------------------------------------- #


def test_rate_limit_wait_prefers_the_provider_hint_over_guesswork() -> None:
    """A retry-after hint wins: the provider knows when its OTPM bucket rolls.

    The OTPM window is per-minute, so guessing shorter than the hint only buys
    another 429 - but a hint must never *shorten* the escalating floor, and a
    wild hint is bounded by the router's own ceiling so one request can never
    hang a whole batch for hours.
    """
    # No hint: escalating back-off 10s -> 20s -> 40s, capped at 60s.
    silent = evaluator.LLMAPIError("429", status_code=429)
    assert evaluator._rate_limit_wait(silent, 1) == 10.0
    assert evaluator._rate_limit_wait(silent, 2) == 20.0
    assert evaluator._rate_limit_wait(silent, 3) == 40.0
    assert evaluator._rate_limit_wait(silent, 4) == 60.0

    # A hint longer than the current back-off is honoured verbatim.
    hinted = evaluator.LLMAPIError("429", status_code=429, retry_after=25.0)
    assert evaluator._rate_limit_wait(hinted, 1) == 25.0
    # A small hint never shortens the wait below the escalating floor.
    small = evaluator.LLMAPIError("429", status_code=429, retry_after=3.0)
    assert evaluator._rate_limit_wait(small, 1) == 10.0

    # A wild hint is clamped to the router's ceiling, not slept on literally.
    huge = evaluator.LLMAPIError("429", status_code=429, retry_after=10_000.0)
    assert evaluator._rate_limit_wait(huge, 1) == MAX_RETRY_AFTER_SECONDS


def test_call_completion_honours_retry_after_then_succeeds(monkeypatch) -> None:
    """A single-key OTPM 429 must wait the hint and re-serve, not fail the student.

    The exact CSV regression: OTPM exhausted mid-batch -> 429 carrying a
    retry-after header. The runner waits that long, retries once, and the
    student is graded instead of written out as an ERROR row.
    """
    _install_router(monkeypatch, None)  # single-key mode: no rotation to save us
    slept = _no_sleeps(monkeypatch)
    attempts = {"n": 0}

    def throttled_then_ok(client, *_args, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http429_after(
                "Rate limit reached for qwen/qwen3.8-27b on output tokens per "
                "minute (OTPM): Limit 1000, Used 527, Requested 670.",
                retry_after=12.0,
            )
        return {"graded": True}

    monkeypatch.setattr(evaluator, "_create_completion", throttled_then_ok)

    out = evaluator._call_completion(None, "sys", "user", None)

    assert out == {"graded": True}
    assert attempts["n"] == 2
    # It waited the provider's own hint before re-serving, not a blind retry.
    assert slept == [12.0]


def test_call_completion_escalates_backoff_when_the_provider_is_silent(monkeypatch) -> None:
    """No retry-after header: escalate 10s -> 20s -> 40s rather than hammering."""
    _install_router(monkeypatch, None)
    slept = _no_sleeps(monkeypatch)
    attempts = {"n": 0}

    def throttled_then_ok(client, *_args, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise _http429("Rate limit reached: output tokens per minute (OTPM)")
        return {"graded": True}

    monkeypatch.setattr(evaluator, "_create_completion", throttled_then_ok)

    out = evaluator._call_completion(None, "sys", "user", None)

    assert out == {"graded": True}
    assert attempts["n"] == 4
    assert slept == [10.0, 20.0, 40.0]


def test_call_completion_surfaces_the_error_after_the_rate_limit_budget(monkeypatch) -> None:
    """A bucket that never refills still fails - after waiting, not instantly.

    The re-serve budget is bounded (:data:`_RATE_LIMIT_RETRIES`), so a dead
    provider cannot stall the batch forever; and the surfaced error still
    carries the provider's hint for any outer handler that wants it.
    """
    _install_router(monkeypatch, None)
    slept = _no_sleeps(monkeypatch)

    def always_throttled(client, *_args, **_kwargs):
        raise _http429_after(
            "Rate limit reached: output tokens per minute (OTPM)", retry_after=12.0
        )

    monkeypatch.setattr(evaluator, "_create_completion", always_throttled)

    with pytest.raises(evaluator.LLMAPIError) as excinfo:
        evaluator._call_completion(None, "sys", "user", None)

    # One escalating wait per re-serve round: the hint wins on round 1, the
    # floor overtakes it on rounds 2-3.
    assert slept == [12.0, 20.0, 40.0]
    assert len(slept) == evaluator._RATE_LIMIT_RETRIES
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == 12.0


# --- Batch engine: grade_students_batch -------------------------------

def _make_batch_request(student_name="Student", doc_text="doc text", late_penalty_points=0):
    return evaluator.StudentGradeRequest(
        student_name=student_name,
        raw_doc_text=doc_text,
        late_penalty_points=late_penalty_points,
    )


def _fake_grade_result(name: str, score: int = 90):
    from src.models import GradingResult
    return GradingResult(
        student_name=name,
        score=score,
        feedback_hebrew="טוב",
        task_evaluations=[],
    )


def test_grade_students_batch_preserves_input_order(monkeypatch):
    """Concurrent grading must return outcomes in request order, not completion order."""
    import threading

    delays = {"Alice": 0.05, "Bob": 0.01, "Carol": 0.03}  # Bob finishes first
    lock = threading.Lock()

    def fake_grade(name, doc_text, rubric, late_penalty_points=0):
        time.sleep(delays[name])
        return _fake_grade_result(name)

    monkeypatch.setattr(evaluator, "evaluate_student_submission", fake_grade)

    requests = [
        _make_batch_request("Alice"),
        _make_batch_request("Bob"),
        _make_batch_request("Carol"),
    ]
    outcomes = evaluator.grade_students_batch(requests, _config(), max_workers=3)

    assert [o.student_name for o in outcomes] == ["Alice", "Bob", "Carol"]
    assert all(o.result is not None for o in outcomes)
    assert all(o.result.score == 90 for o in outcomes)


def test_grade_students_batch_sequential_worker_matches_concurrent(monkeypatch):
    """max_workers=1 grades one at a time but produces identical outcomes."""
    names = ["A", "B", "C"]
    call_log: list[str] = []

    def fake_grade(name, doc_text, rubric, late_penalty_points=0):
        call_log.append(name)
        return _fake_grade_result(name)

    monkeypatch.setattr(evaluator, "evaluate_student_submission", fake_grade)

    requests = [_make_batch_request(n) for n in names]
    concurrent = evaluator.grade_students_batch(requests, _config(), max_workers=3)
    call_log.clear()
    sequential = evaluator.grade_students_batch(requests, _config(), max_workers=1)

    assert [o.student_name for o in concurrent] == names
    assert [o.student_name for o in sequential] == names
    assert [o.result.score for o in concurrent] == [90, 90, 90]
    assert [o.result.score for o in sequential] == [90, 90, 90]
    assert call_log == names


def test_grade_students_batch_one_failure_does_not_lose_the_batch(monkeypatch):
    """One student raising must not prevent the others from being graded."""

    def fake_grade(name, doc_text, rubric, late_penalty_points=0):
        if name == "Fail":
            raise evaluator.LLMEvaluationError("boom")
        return _fake_grade_result(name)

    monkeypatch.setattr(evaluator, "evaluate_student_submission", fake_grade)

    requests = [
        _make_batch_request("Good1"),
        _make_batch_request("Fail"),
        _make_batch_request("Good2"),
    ]
    outcomes = evaluator.grade_students_batch(requests, _config(), max_workers=3)

    by_name = {o.student_name: o for o in outcomes}
    assert by_name["Good1"].result is not None
    assert by_name["Good1"].result.score == 90
    assert by_name["Fail"].result is None
    assert "boom" in by_name["Fail"].error
    assert by_name["Good2"].result is not None
    assert by_name["Good2"].result.score == 90


def test_grade_students_batch_daily_quota_stops_new_work(monkeypatch):
    """DailyQuotaExhausted from one worker stops new starts; skipped students get quota_exhausted=True."""
    from src.llm_router import DailyQuotaExhausted

    started: list[str] = []

    def fake_grade(name, doc_text, rubric, late_penalty_points=0):
        started.append(name)
        if name == "Quota Victim":
            # Fail instantly so the stop_event is set before any later
            # student can be submitted.
            raise DailyQuotaExhausted("daily quota gone")
        if name == "First":
            # Slow enough that Quota Victim always finishes first.
            time.sleep(0.05)
        return _fake_grade_result(name)

    monkeypatch.setattr(evaluator, "evaluate_student_submission", fake_grade)

    requests = [
        _make_batch_request("First"),
        _make_batch_request("Quota Victim"),
        _make_batch_request("Later"),
        _make_batch_request("Never"),
    ]
    outcomes = evaluator.grade_students_batch(requests, _config(), max_workers=2)

    by_name = {o.student_name: o for o in outcomes}
    assert by_name["First"].result is not None
    assert by_name["Quota Victim"].quota_exhausted is True
    assert by_name["Later"].quota_exhausted is True
    assert by_name["Never"].quota_exhausted is True
    assert "Later" not in started
    assert "Never" not in started


def test_grade_students_batch_empty_input_returns_empty():
    """No requests -> empty list, no crash."""
    assert evaluator.grade_students_batch([], _config()) == []


def test_grade_students_batch_on_result_callback(monkeypatch):
    """on_result fires once per student with (outcome, done, total)."""
    calls: list[tuple] = []

    def fake_grade(name, doc_text, rubric, late_penalty_points=0):
        return _fake_grade_result(name)

    monkeypatch.setattr(evaluator, "evaluate_student_submission", fake_grade)

    requests = [_make_batch_request("A"), _make_batch_request("B"), _make_batch_request("C")]
    evaluator.grade_students_batch(requests, _config(), max_workers=2, on_result=lambda o, d, t: calls.append((o.student_name, d, t)))

    assert len(calls) == 3
    assert all(t == 3 for _, _, t in calls)
    assert sorted(d for _, d, _ in calls) == [1, 2, 3]
    assert sorted(n for n, _, _ in calls) == ["A", "B", "C"]


def test_student_grade_outcome_defaults():
    """StudentGradeOutcome defaults: no result, no error, not quota-exhausted."""
    o = evaluator.StudentGradeOutcome(student_name="Noa")
    assert o.result is None
    assert o.error is None
    assert o.quota_exhausted is False

    o2 = evaluator.StudentGradeOutcome(student_name="Noa", error="boom", quota_exhausted=True)
    assert o2.error == "boom"
    assert o2.quota_exhausted is True

