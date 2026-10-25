"""LLM transport: shared router/client singletons, retries, response-format staging, and fallback serving.

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
    MAX_TOKENS,
    REASONING_EFFORT,
    TEMPERATURE,
    _BACKOFF_MULTIPLIER,
    _MAX_ATTEMPTS,
    _MAX_BACKOFF_SECONDS,
    _MAX_RETRY_AFTER_SECONDS,
    _RATE_LIMIT_BACKOFF_BASE,
    _RATE_LIMIT_RETRIES,
    _RATE_LIMIT_WAIT_CAP,
    _REQUEST_TIMEOUT_SECONDS,
    _RETRYABLE_STATUS_CODES,
)
from src.evaluator.errors import (
    LLMAPIError,
    LLMEvaluationError,
    LLMJSONParsingError,
)
from src.evaluator.pacer import (
    _pacer,
)
from src.evaluator.parsing import (
    _one_line,
)
from src.seams import resolve, seam_fn, set_on_facade

logger = logging.getLogger(__name__)

FACADE = "src.llm_evaluator"

# --- Resilient routing: key pool + fallback + adaptive pacing ---------------
# Built lazily from settings so import-time never needs credentials. The pool
# is shared by the whole batch: every student goes through the same router.
_ROUTER: Any = None


_ROUTER_READY = False


def _router() -> Any:
    """Shared LLMRouter (or None when no key is configured at all)."""
    global _ROUTER, _ROUTER_READY
    if resolve(FACADE, "_ROUTER_READY", _ROUTER_READY):
        return resolve(FACADE, "_ROUTER", _ROUTER)
    _ROUTER_READY = set_on_facade(
        FACADE, "_ROUTER_READY", True, _ROUTER_READY
    )
    try:
        from src.llm_router import build_router_from_settings
    except Exception:
        return None
    try:
        built = build_router_from_settings(settings)
    except Exception as exc:
        logger.warning("LLM router disabled (%s); single-key mode.", exc)
        built = None
    _ROUTER = set_on_facade(FACADE, "_ROUTER", built, _ROUTER)
    if built is not None:
        logger.info("LLM routing: %s.", built.describe())
    return built


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Lazily build the Groq client from settings (GROQ_API_KEY).

    The official ``openai`` SDK is reused against Groq's OpenAI-compatible
    base URL, so no Groq-specific client library is needed. The SDK's own
    retry loop is disabled because tenacity owns retries here (it can pace
    itself with ``Retry-After`` and log every attempt).
    """
    global _client
    if resolve(FACADE, "_client", _client) is None:
        api_key = settings.groq_api_key
        if not api_key:
            try:
                from src.llm_router import split_keys as _split

                api_key = (_split(None, settings.groq_api_keys) or [None])[0]
            except Exception:
                api_key = None
        if not api_key:
            raise LLMEvaluationError(
                "GROQ_API_KEY is not set. Add it to .env (see .env.example) "
                "or export it in your environment."
            )
        built = OpenAI(
            api_key=api_key,
            base_url=settings.groq_base_url,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
        _client = set_on_facade(FACADE, "_client", built, _client)
    return resolve(FACADE, "_client", _client)


def _is_retryable_error(exc: BaseException) -> bool:
    """Retry only transient failures: HTTP 5xx or network drops.

    429 is intentionally excluded: a rate limit is handled by the router
    (rotate key / fail over to the fallback), never slept through here. See
    :data:`_RETRYABLE_STATUS_CODES`.
    """
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    # Raised before/without an HTTP response: connection resets, DNS drops,
    # read timeouts, and (defensively) raw transport errors.
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError))


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Parse Groq's ``Retry-After`` header (seconds) when the API sent one."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = headers.get("retry-after") if headers is not None else None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _wait_before_retry(retry_state: Any) -> float:
    """Sleep per ``Retry-After``, else exponentially (2s, 4s, 8s...).

    Groq's free-tier buckets refill per minute, so a server-provided delay is
    treated as a floor rather than a replacement for the backoff.
    """
    attempt = getattr(retry_state, "attempt_number", 1)
    delay = min(_BACKOFF_MULTIPLIER * 2 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
    outcome = getattr(retry_state, "outcome", None)
    if outcome is not None and outcome.failed:
        retry_after = _retry_after_seconds(outcome.exception())
        if retry_after is not None:
            delay = max(delay, retry_after)
    return min(delay, _MAX_RETRY_AFTER_SECONDS)


def _log_retry(retry_state: Any) -> None:
    """Warn before each retry sleep (tenacity ``before_sleep`` callback)."""
    outcome = getattr(retry_state, "outcome", None)
    error = outcome.exception() if outcome is not None and outcome.failed else "?"
    logger.warning(
        "Groq call failed (%s); retrying (attempt %s/%d)...",
        error,
        getattr(retry_state, "attempt_number", "?"),
        _MAX_ATTEMPTS,
    )


@retry(
    retry=retry_if_exception(_is_retryable_error),
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    wait=_wait_before_retry,
    reraise=True,
    before_sleep=_log_retry,
)
def _create_completion(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None = None,
    *,
    model: str | None = None,
) -> Any:
    """One Groq chat completion, retried on 429/5xx/network drops (tenacity).

    ``response_format`` defaults to the strict JSON-Schema payload; the
    fallback stages pass :data:`_JSON_OBJECT_FORMAT` or ``None`` for free text.
    ``model`` overrides the request model (fallback provider's model).

    The Groq-only reasoning knobs (``reasoning_effort`` + ``extra_body``) are
    sent **only** when the request is really going to Groq. A per-minute Groq
    429 parks the Groq keys and routing hands the rest of the batch to the
    fallback (Gemini/OpenRouter); those providers reject unknown body fields
    outright - Gemini answers ``HTTP 400: Unknown name "reasoning_format"`` -
    which used to fail *every* fallback-routed student across *every* remaining
    group. In a daily check that parks 10 of 14 groups off the Release tab at
    once. So they are attached here, on the wire, where the model is known, and
    simply left off for any other provider.
    """
    routed_model = model or settings.groq_model
    # Groq or not is decided by where the request is actually going, not by the
    # model name: the primary model is e.g. "qwen/qwen3.8-27b", which carries no
    # "groq" hint, so a name test would wrongly strip the reasoning knobs from
    # the real Groq path. The client's base URL is the honest signal.
    base_url = str(getattr(client, "base_url", "") or "")
    is_groq = "groq" in base_url.lower() or (
        not base_url and "groq" in str(routed_model).lower()
    )
    request: dict[str, Any] = {
        "model": routed_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    if is_groq:
        # ``reasoning_effort="none"`` selects Qwen's instruct (non-thinking)
        # mode, and ``reasoning_format="hidden"`` keeps Groq from folding any
        # thinking trace into ``message.content``. Both are Groq extensions and
        # are hard 400s on Gemini/OpenRouter, so they are never sent to those.
        request["reasoning_effort"] = REASONING_EFFORT
        request["extra_body"] = {"reasoning_format": "hidden"}
    if response_format is not None:
        if not is_groq and isinstance(response_format, dict):
            kind = response_format.get("type")
            if kind == "json_schema":
                response_format = {"type": "json_object"}
        request["response_format"] = response_format
    return client.chat.completions.create(**request)


def _error_detail(exc: APIError) -> str:
    """Best-effort human-readable message from a Groq error payload."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return str(exc)


def _is_json_generation_error(detail: str) -> bool:
    """True for Groq's structured-output rejection messages (HTTP 400)."""
    lowered = detail.lower()
    return "json" in lowered and (
        "failed to generate" in lowered
        or "could not generate" in lowered
        or "invalid json" in lowered
    )


def _is_degradable(exc: BaseException) -> bool:
    """True when a looser response format could still rescue this failure."""
    if isinstance(exc, LLMJSONParsingError):
        # Payload unusable (fences/prose/truncation/garbage) - ask again.
        return True
    if isinstance(exc, LLMAPIError):
        # 400 = request rejected (e.g. "Failed to generate JSON"); a looser
        # format is a genuinely different request. 401/403/404/5xx are not.
        return exc.status_code == 400
    return False


def _rate_limit_wait(exc: LLMAPIError, attempt: int) -> float:
    """Seconds to pause before re-serving a request the provider throttled.

    The provider's ``retry-after`` hint wins when present: it knows when its
    per-minute bucket refills (OTPM windows roll on the minute), and guessing
    shorter only buys another 429. With no hint we escalate
    10s -> 20s -> 40s (capped by :data:`_MAX_RETRY_AFTER_SECONDS`) - long
    enough for a silent bucket to roll over, short enough that the batch keeps
    moving. The router's own ceiling (``src.llm_router.MAX_RETRY_AFTER_SECONDS``)
    bounds a bad hint so one request can never hang a whole run.
    """
    from src.llm_router import MAX_RETRY_AFTER_SECONDS

    hint = float(getattr(exc, "retry_after", None) or 0.0)
    backoff = min(
        _RATE_LIMIT_BACKOFF_BASE * (2 ** (attempt - 1)),
        _RATE_LIMIT_WAIT_CAP,
    )
    return min(max(backoff, hint), MAX_RETRY_AFTER_SECONDS)


def _call_completion(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
    *,
    _key_index: int = 0,
) -> Any:
    """One graded request, waiting out a per-minute 429 instead of failing.

    The inner attempt (:func:`_call_completion_once`) owns routing: it rotates
    to the next Groq key, honours key cooldowns, and hands over to the fallback
    provider. One re-serve is still not enough on a tight OTPM bucket - the
    re-served attempt can be throttled too (the cooldown expired mid-window,
    the rotated key is limited as well, the fallback has its own per-minute
    cap, or there is no router at all). Tenacity deliberately never sleeps on
    a 429, so without this loop every one of those cases surfaced as
    ``LLMAPIError`` -> an ERROR row for the student.

    So a per-minute 429 re-runs the whole routing decision after a pause that
    honours the provider's ``retry-after`` hint (or an escalating back-off when
    the hint is missing), up to :data:`_RATE_LIMIT_RETRIES` extra rounds, and
    only then surfaces the failure. A *daily* quota still fails fast as
    ``DailyQuotaExhausted`` - it cannot refill inside this run.
    """
    from src.llm_router import ticked_sleep

    attempts = 0
    while True:
        try:
            return _call_completion_once(
                client,
                system_prompt,
                user_prompt,
                response_format,
                _key_index=_key_index,
            )
        except LLMAPIError as exc:
            if exc.status_code != 429 or attempts >= _RATE_LIMIT_RETRIES:
                raise
            attempts += 1
            wait = _rate_limit_wait(exc, attempts)
            logger.warning(
                "Rate limit hit (per-minute); backing off %.1fs before "
                "re-routing (attempt %d/%d): %s",
                wait,
                attempts,
                _RATE_LIMIT_RETRIES,
                _one_line(exc),
            )
            ticked_sleep(wait, None, "rate-limit backoff")


def _call_completion_once(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
    *,
    _key_index: int = 0,
) -> Any:
    """One graded request with transport/HTTP errors mapped to our own types.

    Rate-limit resilience: on HTTP 429 the shared router cools the serving key
    (honoring the ``retry-after`` header) and the *same attempt* is re-served
    by the next key - or by the fallback provider when every key is cooling.
    If that re-served attempt is throttled *again*, the wait-and-retry budget
    in :func:`_call_completion` takes over rather than failing the student.
    The adaptive pacer stretches the inter-student pause after each 429.

    The first attempt is routed too, rather than sent on whatever client the
    caller cached. That distinction is the whole TPD fix: ``_get_client()`` is
    built once from a single Groq key, so reusing it blindly fired a fresh 429
    at a bucket the router had *already* parked, for every remaining student,
    instead of letting Gemini serve them. Asking the router first means an
    exhausted pool costs one wasted request, not one per student.
    """
    from src.llm_router import DailyQuotaExhausted, is_daily_quota_error, parse_retry_after as _parse_ra

    router = _router()
    if router is not None:
        try:
            decision = router.pick()
        except DailyQuotaExhausted:
            raise
        except Exception:
            decision = None
        if decision is not None:
            if decision.endpoint.is_fallback:
                # Every Groq key is parked: serve the fallback directly, on the
                # decision already taken, so this request never touches Groq
                # and `fallback_uses` is not counted twice by a second pick().
                return _serve_decision(
                    decision, router, system_prompt, user_prompt, response_format
                )
            # A healthy Groq key: build from it so key rotation actually
            # reaches the wire and the 429 below is reported against the key
            # that was really throttled (never blindly index 0).
            client = _client_for(decision)
            _key_index = decision.key_index
    try:
        return _create_completion(
            client, system_prompt, user_prompt, response_format
        )
    except APIStatusError as exc:
        if exc.status_code == 429:
            daily = _note_rate_limited(exc, _key_index, _parse_ra)
            if daily:
                # The daily token quota is shared by every key in the
                # organization, so no other Groq key can serve this request.
                if router is not None and router.has_fallback:
                    # A fallback is configured: hand over immediately. pick()
                    # cannot return a Groq key now - they are all parked.
                    return _serve_from_router(router, system_prompt, user_prompt, response_format)
                # Nothing to hand over to. Fail fast and clearly rather than
                # burning the retry budget on a provider that is done until the
                # quota resets (hours away).
                raise DailyQuotaExhausted(
                    f"Groq daily token quota reached and no fallback provider is "
                    f"configured: {_error_detail(exc)}"
                ) from exc
            if router is not None:
                return _serve_from_router(
                    router, system_prompt, user_prompt, response_format
                )
        raise LLMAPIError(
            f"Groq API request failed (HTTP {exc.status_code}): "
            f"{_error_detail(exc)}",
            status_code=exc.status_code,
            # Carried so _call_completion can honour the hint on its wait
            # before re-serving this student instead of writing an ERROR row.
            retry_after=(_parse_ra(exc) if exc.status_code == 429 else None),
        ) from exc
    except ValidationError as exc:
        raise LLMJSONParsingError(
            f"Groq response violated the GradingResult schema: {_one_line(exc)}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - boundary wraps unexpected failures
        raise LLMEvaluationError(f"Groq evaluation failed: {exc}") from exc


def _serve_from_router(
    router: Any,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
) -> Any:
    """Re-serve one attempt from whatever the router can still reach.

    Single home for the "primary key failed, try the next one" path, used by
    both the per-minute retry and the daily-quota hand-over, so the two cannot
    drift apart in behaviour.
    """
    return _serve_decision(
        router.pick(), router, system_prompt, user_prompt, response_format
    )


def _serve_decision(
    decision: Any,
    router: Any,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
) -> Any:
    """Send one attempt on an *already-taken* routing decision.

    Split out of :func:`_serve_from_router` so a caller that has already called
    ``router.pick()`` reuses that decision instead of picking a second time
    (which inflated ``fallback_uses`` and skipped a key in the rotation).

    A 429 here is attributed to whatever is actually throttled. That matters
    most for the fallback: reporting Gemini's own limit against a *Groq* key
    index used to park a perfectly healthy key, and reporting it as a daily
    quota used to silently fail every remaining student with an opaque error
    instead of saying both providers are out.
    """
    from src.llm_router import DailyQuotaExhausted, is_daily_quota_error, parse_retry_after as _parse_ra

    is_fallback = bool(getattr(decision.endpoint, "is_fallback", False))
    routed = _client_for(decision)
    try:
        response = _create_completion(
            routed,
            system_prompt,
            user_prompt,
            response_format,
            model=decision.endpoint.model or None,
        )
    except APIStatusError as exc:
        if exc.status_code == 404 and is_fallback:
            # A retired fallback model id (e.g. gemini-2.0-flash answering
            # "no longer available"). Retire the id (Gemini endpoints walk
            # GEMINI_MODEL_CANDIDATES) and retry this same student on the next
            # live model instead of failing the whole batch on one dead id.
            from src.llm_router import is_unknown_model_error

            if is_unknown_model_error(_error_detail(exc)):
                try:
                    moved = router.report_unknown_model(
                        decision.key_index, decision.endpoint.model or ""
                    )
                except Exception:
                    moved = False
                if moved:
                    logger.warning(
                        "Fallback model '%s' retired (HTTP 404); retrying on '%s'.",
                        decision.endpoint.model,
                        router.fallback.model,
                    )
                    return _serve_from_router(
                        router, system_prompt, user_prompt, response_format
                    )
            raise LLMAPIError(
                f"{decision.endpoint.name} API request failed "
                f"(HTTP 404): model '{decision.endpoint.model}' is not "
                f"available - set LLM_FALLBACK_MODEL in .env to a live id: "
                f"{_error_detail(exc)}",
                status_code=exc.status_code,
                retry_after=None,
            ) from exc
        if exc.status_code == 429 or _is_retryable_error(exc):
            if exc.status_code == 429:
                if is_fallback:
                    # The fallback is out too. Do not report this against a Groq
                    # key (it never touched one) and do not retry a bucket that
                    # will not refill inside this run - stop with a message a
                    # teacher can act on.
                    if is_daily_quota_error(_error_detail(exc)):
                        raise DailyQuotaExhausted(
                            f"Groq daily quota is exhausted and the fallback "
                            f"({decision.endpoint.name}: {decision.endpoint.model}) "
                            f"is also out of daily quota: {_error_detail(exc)}"
                        ) from exc
                else:
                    _note_rate_limited(exc, decision.key_index, _parse_ra)
            raise LLMAPIError(
                f"{decision.endpoint.name} API request failed "
                f"(HTTP {exc.status_code}): {_error_detail(exc)}",
                status_code=exc.status_code,
                # The routed path re-serves through _call_completion's
                # back-off loop, which needs the provider's own hint.
                retry_after=(_parse_ra(exc) if exc.status_code == 429 else None),
            ) from exc
        raise LLMAPIError(
            f"{decision.endpoint.name} API request failed "
            f"(HTTP {exc.status_code}): {_error_detail(exc)}",
            status_code=exc.status_code,
            retry_after=None,
        ) from exc
    except (APITimeoutError, APIConnectionError) as exc:
        # A hung socket is transient by nature (server hiccup, middlebox
        # dropping an idle keep-alive). Handled like a 5xx so the outer
        # retry re-serves through the router (possibly on another key),
        # instead of writing the student off after one bad connection.
        raise LLMAPIError(
            f"{decision.endpoint.name} API request timed out: {exc}.",
            status_code=None,
            retry_after=None,
        ) from exc
    try:
        if router is not None:
            router.report_success(decision.key_index)
    except Exception:
        pass
    return response


def _client_for(decision: Any) -> OpenAI:
    """Build the SDK client for a routing decision (fresh: keys rotate)."""
    endpoint = decision.endpoint
    return OpenAI(
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
        timeout=_REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )


def _note_rate_limited(exc: BaseException, key_index: int, parse_ra: Any) -> bool:
    """Record a 429 on the router, parking the key if it is a *daily* quota.

    Returns True when the provider said the daily bucket is gone. That is the
    distinction the whole batch depends on: a per-minute 429 refills within the
    minute and is worth cooling for, while a tokens-per-day 429 is shared across
    every key in the organization and cannot be fixed by rotating keys or
    waiting - so the key is parked and the next request goes to the fallback.
    """
    # Imported in *this* scope, not inherited from the callers: Python has no
    # cross-function scope, so the callers' own imports left this helper
    # raising NameError on the very first 429 it existed to record.
    from src.llm_router import is_daily_quota_error

    try:
        retry_after = parse_ra(exc)
    except Exception:
        retry_after = None
    try:
        detail = _error_detail(exc) if isinstance(exc, APIStatusError) else str(exc)
    except Exception:
        detail = ""
    daily = is_daily_quota_error(detail)
    try:
        router = _router()
        if router is not None:
            if daily:
                router.report_daily_exhausted(key_index, retry_after)
            else:
                router.report_rate_limited(key_index, retry_after)
    except Exception:
        pass
    try:
        pacer = _pacer()
        if pacer is not None:
            pacer.report_rate_limited()
    except Exception:
        pass
    return daily

# ----------------------------------------------------------------------
# Seam wiring
# ----------------------------------------------------------------------
# Tests monkeypatch these on the ``src.llm_evaluator`` facade. The bodies above
# reference them as plain globals; Python resolves globals at call time,
# so rebinding them here to facade-resolving proxies honours a patch
# without touching any call site.
_get_client = seam_fn("src.llm_evaluator", "_get_client", _get_client)
_create_completion = seam_fn("src.llm_evaluator", "_create_completion", _create_completion)
_call_completion = seam_fn("src.llm_evaluator", "_call_completion", _call_completion)
