"""The Gemini daily-quota fix (2026-11-19 deferral wave).

Three linked defects made the auto-check defer papers on "gemini 429":

1. Gemini answers quota errors as a JSON *array*, and the portal only parsed
   dict bodies - then truncated the raw fallback to 300 chars, cutting away
   the "requests per day" description that marks a *daily* cap. Every daily
   429 was misread as a per-minute blip, so keys cooled 45s instead of being
   parked for the day.
2. The engine router keeps no cooldown state for the legacy single fallback
   endpoint (key_index -1): report_rate_limited(-1) only sleeps ~5s and pick()
   hands the same endpoint straight back, so one paper burned all 8 route
   attempts on the same exhausted Gemini key.
3. The "every provider is out" check ignored a healthy tertiary pool, giving
   up before Cloudflare could take the paper.

These tests drive the production transport with fake HTTP responses (no key,
network or real quota) and prove all three are fixed.
"""
from __future__ import annotations

import pytest

from portal.exams import llm_review
from portal.tests.test_exam_llm_routing import (
    Decision,
    Endpoint,
    FakeClient,
    FakeResponse,
    FakeRouter,
    _item_verdict,
    _rate_limited,
    _wire,
)
from grading_engine.llm_router import LLMRouter, ProviderEndpoint

# The real shape of Gemini's free-tier daily 429: a JSON array whose verbose
# message carries the daily description ("requests per day ... Free Tier")
# well past character 300 - exactly where the old truncation cut it off.
GEMINI_DAILY_MESSAGE = (
    "You exceeded your current quota, please check your plan and billing "
    "details. For more information on this error, head to: "
    "https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your "
    "current usage, head to: https://ai.dev/usage?tab=rate-limit. "
    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash, "
    "description: Generate content requests per day limit exceeded on Free "
    "Tier. Please retry in 30s."
)


def _gemini_daily_429():
    return FakeResponse(
        status_code=429,
        payload=[
            {
                "error": {
                    "code": 429,
                    "message": GEMINI_DAILY_MESSAGE,
                    "status": "RESOURCE_EXHAUSTED",
                }
            }
        ],
    )


def _legacy_endpoint(key="f1"):
    return Endpoint(
        "gemini",
        is_fallback=True,
        model="gemini-x",
        key=key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (
        "EXAM_LLM_REVIEW", "EXAM_LLM_MODEL", "EXAM_LLM_API_KEY",
        "EXAM_LLM_BASE_URL", "EXAM_LLM_TIMEOUT", "GROQ_API_KEY",
        "GROQ_API_KEYS", "GROQ_MODEL", "GROQ_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXAM_LLM_REVIEW", "1")
    monkeypatch.setenv("EXAM_LLM_MODEL", "m")
    monkeypatch.setenv("EXAM_LLM_API_KEY", "k")
    llm_review._cache.clear()
    llm_review.reset_routing()
    llm_review._patch_fallback_key_state()
    yield
    llm_review._cache.clear()
    llm_review.reset_routing()


def _body():
    return {"model": "m", "messages": []}


# ------------------------------------------------- array-shaped error parsing
def test_the_gemini_array_error_body_keeps_the_daily_description():
    detail = llm_review._response_error_detail(_gemini_daily_429())
    assert detail.startswith("HTTP 429:")
    # The marker sits past char 300 in the real message; it must survive.
    assert "requests per day" in detail
    assert len(detail) > 300
    assert llm_review._is_daily_quota_error(detail)


def test_a_long_raw_body_also_keeps_the_daily_marker():
    # No parseable JSON at all: the raw-text fallback must not cut the marker.
    raw = "x" * 350 + " Generate content requests per day limit exceeded"
    detail = llm_review._response_error_detail(
        FakeResponse(status_code=429, payload=None, text=raw)
    )
    assert "requests per day" in detail
    assert llm_review._is_daily_quota_error(detail)


def test_the_daily_matcher_understands_gemini_wording_and_not_per_minute():
    assert llm_review._is_daily_quota_error(
        "HTTP 429: " + GEMINI_DAILY_MESSAGE
    )
    # The metric name alone is a daily bucket even without the description.
    assert llm_review._is_daily_quota_error(
        "Quota exceeded for metric: generate_content_free_tier_requests"
    )
    assert not llm_review._is_daily_quota_error(
        "Rate limit reached for requests per minute. Please try again in 2s."
    )
    assert not llm_review._is_daily_quota_error(
        "Quota exceeded for metric: generate_content_free_tier_input_token_count"
    )


# ------------------------------------- daily handover reaches the tertiary
def _real_router():
    return LLMRouter(
        groq_keys=["g"],
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
        fallback=ProviderEndpoint(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-x",
            api_key="f1",
        ),
        fallback_keys=["f1", "f2"],
        tertiary=ProviderEndpoint(
            name="cloudflare",
            base_url="https://api.cloudflare.com/client/v4/accounts/x/ai/v1",
            model="cf-m",
            api_key="c1",
        ),
        tertiary_keys=["c1"],
    )


def test_gemini_daily_quota_parks_the_keys_and_cloudflare_takes_the_paper(monkeypatch):
    """The live failure: Groq daily-dead, both Gemini keys out for the day.

    Before the fix the array body was misread as per-minute, the keys cooled
    45s, and the legacy endpoint was hammered 8x while Cloudflare never got a
    turn. Now both Gemini keys are parked for the day and the request lands
    on the healthy tertiary key in one pass.
    """
    router = _real_router()
    client = FakeClient(
        _rate_limited("Rate limit reached for tokens per day (TPD)"),
        _gemini_daily_429(),
        _gemini_daily_429(),
        _item_verdict(),
    )
    _wire(monkeypatch, router, client)
    response = llm_review._send_routed(client, llm_review.settings(), _body())
    assert response.status_code == 200
    assert len(client.calls) == 4  # groq, gemini f1, gemini f2, cloudflare
    assert client.calls[-1]["headers"]["Authorization"] == "Bearer c1"
    import time

    now_daily = [k.daily_until > time.monotonic() for k in router._fallback_keys]
    assert now_daily == [True, True]
    # The legacy view of the same dead key is parked too.
    assert llm_review._legacy_parked()


def test_a_healthy_tertiary_prevents_the_premature_quota_stop(monkeypatch):
    """Groq + Gemini daily-dead must NOT raise while Cloudflare is alive."""
    router = _real_router()
    client = FakeClient(
        _rate_limited("tokens per day (TPD) exceeded"),
        _gemini_daily_429(),
        _gemini_daily_429(),
        _item_verdict(),
    )
    _wire(monkeypatch, router, client)
    # No QuotaExhausted: the tertiary pool still has a live key.
    assert llm_review._send_routed(client, llm_review.settings(), _body()) is not None


# ------------------------------------------------ legacy hammering guard
def test_an_exhausted_legacy_fallback_stops_after_one_probe(monkeypatch):
    """Every pool key unavailable -> pick() offers the legacy endpoint.

    Its daily 429 must park it portal-side; the next pick offers it again and
    the transport must stop with QuotaExhausted instead of re-sending 7 more
    times on the same dead key (the observed 8x hammering per paper).
    """
    router = _real_router()
    router.report_daily_exhausted(0)          # Groq out for the day
    router.report_daily_exhausted(-2)         # Gemini f1 out for the day
    router.report_daily_exhausted(-3)         # Gemini f2 out for the day
    router.report_rate_limited(-1000, 300.0)  # Cloudflare cooling 5 minutes
    client = FakeClient(_gemini_daily_429())
    _wire(monkeypatch, router, client)
    with pytest.raises(llm_review.QuotaExhausted):
        llm_review._send_routed(client, llm_review.settings(), _body())
    assert len(client.calls) == 1  # was 8 before the fix
    assert llm_review._legacy_parked()


def test_a_per_minute_429_on_the_legacy_endpoint_parks_it_too(monkeypatch):
    router = FakeRouter(
        [
            Decision(_legacy_endpoint(), key_index=-1),
            Decision(_legacy_endpoint(), key_index=-1),
        ],
        has_fallback=True,
    )
    client = FakeClient(
        _rate_limited("Rate limit reached for requests per minute"),
        _item_verdict(),
    )
    _wire(monkeypatch, router, client)
    with pytest.raises(llm_review.QuotaExhausted):
        llm_review._send_routed(client, llm_review.settings(), _body())
    assert len(client.calls) == 1
    assert llm_review._legacy_parked()


def test_a_legacy_success_after_the_park_expires_clears_the_strikes(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(llm_review.time, "monotonic", lambda: clock["now"])
    router = FakeRouter(
        [
            Decision(_legacy_endpoint(), key_index=-1),
            Decision(_legacy_endpoint(), key_index=-1),
            Decision(_legacy_endpoint(), key_index=-1),
        ],
        has_fallback=True,
    )
    client = FakeClient(
        _rate_limited("Rate limit reached for requests per minute"),
        _item_verdict(),
    )
    _wire(monkeypatch, router, client)
    with pytest.raises(llm_review.QuotaExhausted):
        llm_review._send_routed(client, llm_review.settings(), _body())
    assert llm_review._legacy_parked()
    clock["now"] += 46.0  # the 45s per-minute park expires
    response = llm_review._send_routed(client, llm_review.settings(), _body())
    assert response.status_code == 200
    assert not llm_review._legacy_parked()
    assert llm_review._LEGACY_STRIKES == 0
    assert len(client.calls) == 2


def test_reset_routing_clears_the_legacy_park():
    llm_review._park_legacy(300.0)
    assert llm_review._legacy_parked()
    llm_review.reset_routing()
    assert not llm_review._legacy_parked()
    assert llm_review._LEGACY_STRIKES == 0