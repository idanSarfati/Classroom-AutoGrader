"""Regression tests: non-Groq providers must not receive Groq-only body fields.

The reported bug: a daily ``--grade`` run finds 50 pending submissions across
12 courses, but the Release tab's dropdown shows only **two** groups. The
cause is not a filter in the tab (it has none) - it is that one per-minute Groq
OTPM 429 parks the Groq keys, routing fails the rest of the batch over to the
Gemini fallback, and the fallback is hit with Groq-only request-body fields
(``reasoning_effort`` + ``extra_body.reasoning_format``). Gemini answers
``HTTP 400: Unknown name "reasoning_format"`` for *every* fallback-routed
student, so those groups grade entirely into error rows and - because a report
JSON is only written when at least one student graded - silently vanish from
the Release tab. Only the two groups graded before Groq throttled ever appear.

These tests pin the wire-level fix: the Groq-only knobs are attached only when
the request really goes to Groq (decided by the client's base URL), so a
fallback-routed student grades successfully and its group shows up for release.
"""
from __future__ import annotations

import pytest
from openai import OpenAI

from src import llm_evaluator as evaluator

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _capture(client: OpenAI, *, model: str | None = None, response_format=None) -> dict:
    """Call the real ``_create_completion`` against a recording client.

    Returns the exact kwargs that would have been sent over the wire, so a test
    can assert on the presence/absence of Groq-only body fields. The client's
    ``chat`` attribute is swapped for a recorder for the single call, then
    restored.
    """
    original = client.chat
    captured: dict = {}

    def _create(**kwargs):
        captured.update(kwargs)
        return object()  # _create_completion just returns this through

    client.chat = type("_Chat", (), {})()
    client.chat.completions = type("_C", (), {"create": staticmethod(_create)})()
    try:
        evaluator._create_completion(
            client, "system prompt", "user prompt", response_format, model=model
        )
    finally:
        client.chat = original
    return captured


def test_fallback_provider_request_omits_groq_only_fields() -> None:
    """The exact CSV regression: a Gemini-routed request must not carry them.

    Gemini's OpenAI-compatible endpoint rejects unknown body fields outright -
    sending ``reasoning_effort``/``extra_body`` there is what turned every
    remaining group into an ERROR row and hid it from the Release tab.
    """
    gemini = OpenAI(api_key="k", base_url=GEMINI_BASE_URL, max_retries=0)

    kwargs = _capture(gemini, model="gemini-2.0-flash")

    assert kwargs["model"] == "gemini-2.0-flash"
    assert "reasoning_effort" not in kwargs
    assert "extra_body" not in kwargs
    # The universal fields are still there - the request is otherwise intact.
    assert "messages" in kwargs and "temperature" in kwargs and "max_tokens" in kwargs


def test_primary_groq_request_still_sends_the_reasoning_knobs() -> None:
    """Groq keeps its knobs: they are real and load-bearing on the Groq path.

    ``reasoning_effort="none"`` selects Qwen's instruct mode (fewer output
    tokens against the OTPM budget) and ``reasoning_format="hidden"`` keeps
    thinking traces out of ``message.content`` - the fix must not disable them.
    """
    groq = OpenAI(api_key="k", base_url=GROQ_BASE_URL, max_retries=0)

    # No explicit model: exercises the primary path (settings.groq_model), whose
    # name (e.g. qwen/...) carries no "groq" hint - the base URL decides it.
    kwargs = _capture(groq)

    assert kwargs["reasoning_effort"] == evaluator.REASONING_EFFORT
    assert kwargs["extra_body"] == {"reasoning_format": "hidden"}


def test_openrouter_fallback_request_also_omits_groq_only_fields() -> None:
    """Any non-Groq provider is treated the same - not just Gemini."""
    openrouter = OpenAI(api_key="k", base_url="https://openrouter.ai/api/v1", max_retries=0)

    kwargs = _capture(openrouter, model="some-free-model")

    assert "reasoning_effort" not in kwargs
    assert "extra_body" not in kwargs


def test_groq_detection_prefers_base_url_over_model_name() -> None:
    """A Groq-hosted model whose name has no 'groq' is still sent the knobs.

    Guards the exact trap the naive name-based heuristic would fall into: the
    primary model id is vendor-hosted (qwen/...), so only the base URL is a
    trustworthy Groq signal.
    """
    groq = OpenAI(api_key="k", base_url=GROQ_BASE_URL, max_retries=0)

    kwargs = _capture(groq, model="qwen/qwen3.8-27b")

    assert kwargs["extra_body"] == {"reasoning_format": "hidden"}
    assert kwargs["reasoning_effort"] == evaluator.REASONING_EFFORT
