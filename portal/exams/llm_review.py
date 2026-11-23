"""An optional model pass for the open questions of the exam.

The keyword rubric is reproducible and free, but it reads words, not meaning:
a student who writes "the quotes cover the whole sentence" and a student who
writes "the variable is in quotes" can both match, even when only one of them
understands the bug. The deterministic program rules are exact about what the
code printed, but they cannot tell a real fix from a hardcoded print. A model
that is given the question, the student's words and code, the sandbox output
and the teacher's detailed guidelines can judge the whole question the way a
teacher would.

Two entry points share the same plumbing:

* :func:`grade` marks one free-text check against its keyword rubric. It stays
  for rules that opt in per check (``"llm": true``).
* :func:`grade_item` marks every answered part of an open item in one call -
  the explanation and the corrected code together - against a per-part point
  rubric. This is the exam's main path: the checks file carries an
  ``llm_review`` block with the guidelines and the parts.

The module is deliberately narrow:

* **LLM first, rules as fallback.** The caller tries the model; anything other
  than a valid verdict - no key, a timeout, a malformed reply, a missing part -
  returns ``None`` and the deterministic checker decides. Marking never
  depends on the network being up.
* **The rubric is the prompt.** Each part's points, the teacher's guidelines
  and the sandbox evidence travel with the request, so the model awards the
  same slices the teacher wrote, not a score of its own invention.
* **It never writes.** The result is a points tuple like any other check,
  stored by the normal exam record.

Configuration (all optional; without the first three the feature is off):

``EXAM_LLM_REVIEW=1``          turn the pass on
``EXAM_LLM_API_KEY``           key for the OpenAI-compatible endpoint
``EXAM_LLM_MODEL``             model name
``EXAM_LLM_BASE_URL``          default ``https://api.groq.com/openai/v1``
``EXAM_LLM_TIMEOUT``           seconds, default 180 (matches the grading engine)

``GROQ_API_KEY`` / ``GROQ_API_KEYS`` / ``GROQ_MODEL`` / ``GROQ_BASE_URL`` are
accepted as fallbacks, because that is what the grading engine already uses.
The same interface works with OpenAI and with Gemini's OpenAI-compatible
endpoint, so no provider is baked into the code.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import httpx

from portal.grading.envguard import sanitize_no_proxy_env
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
_TRUTHY = {"1", "true", "yes", "on"}
_CACHE_LIMIT = 2000
_cache: dict[str, dict[str, Any]] = {}

# A small, bounded, in-memory journal of the most recent provider failures. The
# automatic exam pass reads it to tell the teacher *why* a paper could not be
# judged (daily quota, a 413/401 from a provider, a timeout) instead of a bare
# "failed". It is deliberately lossy and process-local: only the last few events
# are kept, nothing is persisted here, and it never contains a key or a full
# request body - just a short, already-sanitised reason string and a timestamp.
_FAILURE_LIMIT = 40
_failures: deque[dict[str, Any]] = deque(maxlen=_FAILURE_LIMIT)
_failure_lock = threading.Lock()


def _provider_label(base_url: Any) -> str:
    """A short, key-free name for the endpoint that failed."""
    url = str(base_url or "").lower()
    if "groq" in url:
        return "Groq"
    if "cloudflare" in url:
        return "Cloudflare"
    if "googleapis" in url or "gemini" in url:
        return "Gemini"
    if "openai" in url:
        return "OpenAI"
    return "ספק המודל"


def note_failure(reason: str, *, base_url: Any = "") -> None:
    """Record one sanitised provider-failure reason for the activity screen.

    ``reason`` must already be free of keys, tokens and request bodies. The
    caller passes a short human string (e.g. ``"HTTP 413 Payload Too Large"``);
    this only adds the provider name and a UTC timestamp. Safe to call from any
    thread; never raises.
    """
    try:
        text = str(reason or "").strip()
        if not text:
            return
        label = _provider_label(base_url)
        entry = {
            "at": utcnow().isoformat(timespec="seconds"),
            "provider": label,
            "reason": text[:300],
        }
        with _failure_lock:
            _failures.append(entry)
    except Exception:  # noqa: BLE001 - journalling must never break grading
        logger.debug("could not record exam LLM failure", exc_info=True)


def recent_failures(limit: int = _FAILURE_LIMIT) -> list[dict[str, Any]]:
    """The most recent failure entries, newest first (a copy, safe to read)."""
    with _failure_lock:
        items = list(_failures)
    items.reverse()
    return items[: max(0, int(limit))]


def clear_failures() -> None:
    """Drop the journal (used by tests and after a successful recovery)."""
    with _failure_lock:
        _failures.clear()


# Hard bounds on the model prompt so a long student answer or a long sandbox
# trace can never grow a request into a provider "413 Payload Too Large". The
# student's raw text is clipped per part, each item's run evidence is clipped,
# and the assembled prompt is refused (the deterministic rules then mark the
# paper) if it still exceeds the ceiling - falling back is always preferable to
# a hard provider rejection that leaves every open item unmarked.
MAX_ANSWER_CHARS = 1500
MAX_EVIDENCE_CHARS = 2500
# A generous ceiling used only as a safety net: the per-item path is already
# bounded (each part's answer and evidence are clipped), so a whole paper whose
# batched prompt crosses a ceiling is split into byte-fitting batches rather
# than risk a provider "413". It is high enough that ordinary papers keep using
# the single efficient batched call.
MAX_PROMPT_CHARS = 30000
# Hebrew weighs ~2 bytes per character in UTF-8 and the transport posts the body
# unescaped (ensure_ascii=False), so the char ceiling alone let a 30k-char paper
# grow into a ~60KB body that Groq rejects with "413 Payload Too Large". The
# byte ceiling is the one that actually matches the provider's payload limit;
# it is kept conservative so the whole JSON body (prompt + system message +
# rubric scaffolding) stays comfortably inside it.
MAX_PROMPT_BYTES = 24000

# How many routing rounds one request may take before it gives up. The router
# itself waits out per-minute cooldowns and skips daily-parked keys, so this is
# only a ceiling against a pathological pool, not the normal retry budget.
_MAX_ROUTE_ATTEMPTS = 8


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _first_key() -> str:
    for name in ("EXAM_LLM_API_KEY", "GROQ_API_KEY"):
        value = _env(name)
        if value:
            return value
    # ``GROQ_API_KEYS`` is a comma-separated rotation list.
    for value in _env("GROQ_API_KEYS").split(","):
        if value.strip():
            return value.strip()
    return ""


def settings() -> dict[str, Any]:
    """Current configuration; ``enabled`` is false unless fully set."""
    model = _env("EXAM_LLM_MODEL") or _env("GROQ_MODEL")
    key = _first_key()
    base = (
        _env("EXAM_LLM_BASE_URL")
        or _env("GROQ_BASE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    try:
        timeout = float(_env("EXAM_LLM_TIMEOUT") or 180)
    except ValueError:
        timeout = 180.0
    return {
        "enabled": _env("EXAM_LLM_REVIEW").lower() in _TRUTHY
        and bool(model)
        and bool(key),
        "model": model,
        "key": key,
        "base_url": base,
        # Match the grading engine's 180s ceiling (src/evaluator/config). The
        # check runs in a background thread and, with reasoning suppressed, a
        # reply is fast; the long ceiling only guards a cold start behind load.
        "timeout": max(3.0, min(300.0, timeout)),
    }


def enabled() -> bool:
    return bool(settings()["enabled"])


def _client(timeout: float) -> httpx.Client:
    sanitize_no_proxy_env()
    return httpx.Client(timeout=timeout, headers={"Accept": "application/json"})


# ----------------------------------------------------------- resilient routing
class QuotaExhausted(RuntimeError):
    """Every configured provider is out of daily quota.

    Distinct from a generic failure on purpose: the caller keeps the
    deterministic marks *and* records that this paper still waits for a model
    pass, so a teacher can retry it once the quota resets. A timeout, a network
    drop or a malformed reply is not this - those simply fall back to the rules.
    """


# The exam reuses the grading engine's key router (read-only import): a pool of
# Groq keys with round-robin rotation, a Gemini/tertiary fallback behind them,
# and daily-quota discrimination. One key hammered during a class burst is
# exactly what made the model "not check" - every call answered 429 and the
# paper silently fell back to the rules. Routing spreads the load and hands the
# request to another provider when Groq's daily bucket is gone.
_ROUTER: Any = None
_ROUTER_READY = False
_ROUTER_LOCK = threading.Lock()
_PACER: Any = None

# The engine router keeps no cooldown state for the *legacy* single fallback
# endpoint (``key_index == -1``): ``report_rate_limited(-1)`` only sleeps a few
# seconds and ``pick()`` hands the same endpoint straight back, so a paper
# whose Groq pool is daily-parked burned all 8 route attempts on an exhausted
# Gemini key while Cloudflare never got a turn (the 2026-11-19 deferral wave).
# The portal tracks that endpoint's strikes/park itself; while the park is
# active and the router still offers the legacy endpoint (nothing else is
# usable), the request stops with QuotaExhausted and the paper waits for a
# later pass instead of hammering the dead key.
_LEGACY_LOCK = threading.Lock()
_LEGACY_COOLDOWN_UNTIL = 0.0
_LEGACY_STRIKES = 0


def _exam_router_settings() -> Any:
    """A settings-like object the engine's router builder understands.

    Exam-specific ``EXAM_LLM_*`` values win for the primary endpoint; the
    fallback and tertiary providers come from the engine settings
    (``LLM_FALLBACK_*`` / ``LLM_TERTIARY_*``), so Gemini is configured once for
    the whole app. Importing :mod:`config.settings` also loads ``.env``, which
    is where the keys live - the same place the grading engine reads them.
    """
    from config.settings import settings as engine

    def pick(*names: str, fallback: str = "") -> str:
        for name in names:
            value = _env(name)
            if value:
                return value
        return fallback

    return SimpleNamespace(
        groq_api_key=pick(
            "EXAM_LLM_API_KEY", "GROQ_API_KEY",
            fallback=str(getattr(engine, "groq_api_key", "") or ""),
        ),
        groq_api_keys=pick(
            "GROQ_API_KEYS", fallback=str(getattr(engine, "groq_api_keys", "") or "")
        ),
        groq_model=pick(
            "EXAM_LLM_MODEL", "GROQ_MODEL",
            fallback=str(getattr(engine, "groq_model", "") or ""),
        ),
        groq_base_url=pick(
            "EXAM_LLM_BASE_URL", "GROQ_BASE_URL",
            fallback=str(getattr(engine, "groq_base_url", "") or DEFAULT_BASE_URL),
        ),
        llm_fallback_provider=str(getattr(engine, "llm_fallback_provider", "") or ""),
        llm_fallback_model=str(getattr(engine, "llm_fallback_model", "") or ""),
        llm_fallback_api_key=str(getattr(engine, "llm_fallback_api_key", "") or ""),
        llm_fallback_base_url=str(getattr(engine, "llm_fallback_base_url", "") or ""),
        llm_fallback_api_keys=str(getattr(engine, "llm_fallback_api_keys", "") or ""),
        llm_tertiary_provider=str(getattr(engine, "llm_tertiary_provider", "") or ""),
        llm_tertiary_model=str(getattr(engine, "llm_tertiary_model", "") or ""),
        llm_tertiary_api_key=str(getattr(engine, "llm_tertiary_api_key", "") or ""),
        llm_tertiary_base_url=str(getattr(engine, "llm_tertiary_base_url", "") or ""),
        cloudflare_account_id=str(getattr(engine, "cloudflare_account_id", "") or ""),
    )


def _patch_fallback_key_state() -> None:
    """Give fallback/tertiary pool keys the counters the router increments.

    ``src.llm_router._FallbackKeyState`` ships without ``consecutive_429s`` and
    ``served``, yet ``report_rate_limited``/``report_success`` do ``+= 1`` on
    them for pool keys. Without the counters, a fallback (Gemini) key that
    answers 429 raises ``AttributeError`` mid-review and the whole paper drops
    back to the deterministic rules - the model silently "does not check".

    The engine file is read-only for the portal, so the two missing counters are
    added here as class attributes before the router is built. ``+= 1`` then
    reads the class default (0) and writes an instance attribute, exactly like
    the Groq ``_KeyState``; the dataclass constructor and fields are unchanged.
    """
    try:
        from src.llm_router import _FallbackKeyState
    except Exception:  # noqa: BLE001 - the shim must never block marking
        return
    for name in ("consecutive_429s", "served"):
        if not hasattr(_FallbackKeyState, name):
            setattr(_FallbackKeyState, name, 0)


def _router() -> Any:
    """The shared exam router, built once from the current configuration."""
    global _ROUTER, _ROUTER_READY
    with _ROUTER_LOCK:
        if _ROUTER_READY:
            return _ROUTER
        _ROUTER_READY = True
        try:
            from src.llm_router import build_router_from_settings

            _patch_fallback_key_state()
            _ROUTER = build_router_from_settings(_exam_router_settings())
        except Exception:  # noqa: BLE001 - no pool must never crash marking
            logger.warning("exam LLM router unavailable; single-key mode", exc_info=True)
            _ROUTER = None
        if _ROUTER is not None:
            logger.info("Exam LLM routing: %s.", _ROUTER.describe())
        return _ROUTER


def _pacer() -> Any:
    """Shared adaptive pause between papers, stretched by observed 429s."""
    global _PACER
    if _PACER is None:
        from src.llm_router import AdaptivePacer

        try:
            base = float(_env("EXAM_LLM_PACE_SECONDS") or 1.0)
        except ValueError:
            base = 1.0
        _PACER = AdaptivePacer(
            base_seconds=max(0.0, min(10.0, base)), max_seconds=30.0
        )
    return _PACER


def reset_routing() -> None:
    """Forget the cached router/pacer (tests, or a configuration change)."""
    global _ROUTER, _ROUTER_READY, _PACER
    with _ROUTER_LOCK:
        _ROUTER = None
        _ROUTER_READY = False
        _PACER = None
    _clear_legacy()


def _legacy_parked() -> bool:
    """True while the legacy fallback endpoint is serving a quota park."""
    with _LEGACY_LOCK:
        return time.monotonic() < _LEGACY_COOLDOWN_UNTIL


def _park_legacy(seconds: float) -> None:
    global _LEGACY_COOLDOWN_UNTIL
    with _LEGACY_LOCK:
        _LEGACY_COOLDOWN_UNTIL = max(
            _LEGACY_COOLDOWN_UNTIL, time.monotonic() + max(0.0, float(seconds))
        )


def _clear_legacy() -> None:
    """A success (or a routing reset) clears the legacy strikes and park."""
    global _LEGACY_COOLDOWN_UNTIL, _LEGACY_STRIKES
    with _LEGACY_LOCK:
        _LEGACY_COOLDOWN_UNTIL = 0.0
        _LEGACY_STRIKES = 0


def _is_daily_quota_error(detail: str) -> bool:
    """Daily-cap detection that also understands Gemini's free-tier wording.

    The engine matcher covers "tokens/requests per day" and friends; Gemini's
    verbose quota message additionally names the daily metric
    ``generate_content_free_tier_requests``. Either signal parks the key for
    the day instead of cooling it for a minute that never refills.
    """
    try:
        from src.llm_router import is_daily_quota_error

        if is_daily_quota_error(detail):
            return True
    except Exception:  # noqa: BLE001 - fall back to the local wording check
        pass
    lowered = (detail or "").lower()
    if "generate_content_free_tier_requests" in lowered:
        return True
    return "per day" in lowered and any(
        word in lowered for word in ("quota", "limit", "exceeded")
    )


def _pool_has_live(keys: Any) -> bool:
    """True when at least one pool key is neither cooling nor daily-parked."""
    now = time.monotonic()
    try:
        return any(
            now >= getattr(k, "cooldown_until", 0.0)
            and now >= getattr(k, "daily_until", 0.0)
            for k in (keys or [])
        )
    except Exception:  # noqa: BLE001 - an unreadable pool is treated as empty
        return False


def _park_matching_fallback_slot(router: Any, endpoint: Any, retry_after: Any) -> None:
    """Park the fallback-pool slot that holds the legacy endpoint's key.

    The legacy endpoint is the first fallback key viewed without pool state;
    when it reports a daily cap, its pool slot must be parked too so rotation
    does not re-probe the same dead key under a different index.
    """
    pool = getattr(router, "_fallback_keys", None) or []
    for i, slot in enumerate(pool):
        if getattr(slot, "api_key", None) == getattr(endpoint, "api_key", None):
            try:
                router.report_daily_exhausted(-(2 + i), retry_after)
            except Exception:  # noqa: BLE001 - parking must never break marking
                logger.debug("could not park fallback slot", exc_info=True)
            return


def _report_legacy_rate_limit(
    router: Any, endpoint: Any, daily: bool, retry_after: Any
) -> None:
    """Record a 429 on the legacy fallback endpoint in portal-side state.

    The engine's ``report_rate_limited(-1)`` only sleeps and records nothing,
    which is what let one paper spend all 8 attempts on the same exhausted
    key. A daily cap parks the endpoint for the day (and its pool slot); a
    per-minute blip escalates 45s x strikes up to 5 minutes, exactly like the
    pool keys. While the park holds, ``_send_routed`` refuses to re-send on
    the legacy endpoint and raises QuotaExhausted instead.
    """
    global _LEGACY_STRIKES
    if daily:
        from src.llm_router import DAILY_QUOTA_COOLDOWN_SECONDS

        wait = DAILY_QUOTA_COOLDOWN_SECONDS
        _park_legacy(wait)
        _park_matching_fallback_slot(router, endpoint, retry_after)
    else:
        with _LEGACY_LOCK:
            _LEGACY_STRIKES += 1
            strikes = _LEGACY_STRIKES
        wait = min(45.0 * strikes, 300.0)
        _park_legacy(wait)
    logger.warning(
        "exam LLM legacy fallback rate-limited; parked %.0fs (daily=%s)",
        wait,
        daily,
    )


def pace() -> None:
    """Pause between papers in a batch; the transport reports 429s to stretch it."""
    _pacer().pause()


def routing_status() -> str:
    """One line describing the pool, for the teacher's status panel."""
    router = _router()
    if router is None:
        return "מפתח יחיד (ללא רוטציה)"
    try:
        return router.describe()
    except Exception:  # noqa: BLE001 - status must never raise
        return "רוטציית מפתחות פעילה"


def _response_retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None)
    raw = ""
    try:
        if headers is not None:
            raw = str(headers.get("Retry-After") or headers.get("retry-after") or "")
    except (AttributeError, TypeError):
        raw = ""
    try:
        return max(float(raw.strip().rstrip("s")), 0.0)
    except (TypeError, ValueError):
        return None


def _response_error_detail(response: Any) -> str:
    """A provider error message, prefixed with the status so 404 tests work.

    Gemini answers quota errors as a JSON *array* (``[{"error": {...}}]``),
    not the dict every OpenAI-compatible provider uses, and its daily-quota
    description ("requests per day ... Free Tier") sits a few hundred chars
    into a verbose message. Both shapes are parsed and enough text is kept
    that the daily marker survives - truncating it away made every Gemini
    daily 429 look like a per-minute blip (the 2026-11-19 deferral wave).
    """
    status = int(getattr(response, "status_code", 0) or 0)
    text = ""
    try:
        payload = response.json()
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            error = entry.get("error")
            if isinstance(error, dict) and error.get("message"):
                text = str(error["message"])
                break
            if entry.get("message"):
                text = str(entry["message"])
                break
    except Exception:  # noqa: BLE001 - fall back to the raw body
        text = ""
    if not text:
        text = str(getattr(response, "text", "") or "")
    return f"HTTP {status}: {text[:1200]}"


def failure_reason(exc: BaseException) -> tuple[str, str]:
    """A short, key-free (reason, endpoint-url) pair for the failure journal.

    The raw exception text of an HTTP error carries the full request URL; only
    the status line and the host are kept, so nothing resembling a credential
    or an account path can reach the activity screen.
    """
    text = str(exc or "")
    match = re.search(
        r"(?:Client|Server) error '([^']+)' for url '(https?://[^']+)'", text
    )
    if match:
        return f"HTTP {match.group(1)}", match.group(2)
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out", ""
    if isinstance(exc, httpx.TransportError):
        return "network error reaching the provider", ""
    cleaned = re.sub(r"https?://\S+", "", text).strip()
    return (cleaned or type(exc).__name__)[:160], ""


# Groq's Qwen3 models are hybrid "thinking" models: by default they emit a long
# chain-of-thought before the answer, which runs past a short read timeout and
# burns the free-tier output-token budget. The grading engine runs them in
# instruct (non-thinking) mode with ``reasoning_effort="none"`` and
# ``reasoning_format="hidden"`` (src/evaluator/transport._create_completion) -
# the method that grades the whole classroom reliably. The exam reuses exactly
# those two knobs, sent only to Groq: Gemini/Cloudflare reject unknown body
# fields, so they are never added for a fallback endpoint.
_GROQ_REASONING_EFFORT = "none"


def _apply_groq_reasoning(body: dict[str, Any], base_url: Any) -> None:
    """Put a Groq Qwen3 model in fast non-thinking mode, in place.

    ``reasoning_format`` is a top-level Groq body field. The OpenAI SDK exposes
    it through ``extra_body`` (which the SDK merges into the JSON root); this
    transport posts raw JSON with httpx, so the field goes straight into the
    body rather than nested under an ``extra_body`` key Groq would reject.
    """
    if "groq" in str(base_url or "").lower():
        body["reasoning_effort"] = _GROQ_REASONING_EFFORT
        body["reasoning_format"] = "hidden"


def _send_routed(
    http: httpx.Client, config: Mapping[str, Any], body: dict[str, Any]
) -> Any:
    """POST one chat request through the key router, surviving a quota burst.

    Each round asks the router for an endpoint (a healthy Groq key, else a
    fallback/tertiary key), sends the request on it, and reports the outcome:

    * 200 -> success, return the response;
    * 429 daily -> park the key, hand over to the fallback; when every provider
      is out, raise :class:`QuotaExhausted` so the paper can wait and retry;
    * 429 per-minute -> cool the key and re-route (the router waits it out);
      the legacy single fallback endpoint (key_index -1) has no router-side
      cooldown state, so the portal parks it itself and refuses to re-send on
      it while the park holds - one exhausted key can no longer eat all 8
      attempts of every paper;
    * 413 payload too large -> cool the key and hand over to a provider with a
      bigger body limit (the byte ceiling + paper chunking make this rare);
    * 401/403 -> park the dead key for the day and hand over to the next
      provider, instead of probing a revoked key on every paper;
    * 404 unknown model -> retire the dead fallback id and try the next;
    * 400 on ``response_format`` -> resend once without it (older models);
    * 5xx / network drop -> a short bounded retry on another endpoint.

    Every failed attempt is logged at WARNING (provider, model, status, payload
    bytes) so the server journal shows exactly which provider rejected what.
    The injected-client path (:func:`_send`) is kept for tests and for a
    single-key install with no pool at all.
    """
    from src.llm_router import (
        DAILY_QUOTA_COOLDOWN_SECONDS,
        DailyQuotaExhausted,
        is_unknown_model_error,
    )

    router = _router()
    if router is None:
        return _send(http, config, body)

    daily_providers: set[str] = set()
    last: Any = None
    for attempt in range(_MAX_ROUTE_ATTEMPTS):
        try:
            decision = router.pick()
        except DailyQuotaExhausted as exc:
            last_status = int(getattr(last, "status_code", 0) or 0)
            if last is not None and last_status in (401, 403, 413):
                # The pool ran dry because keys were parked on auth/payload
                # errors, not on a daily quota: surface the real HTTP failure
                # so the failure journal stays honest instead of claiming
                # "quota exhausted" for a dead key.
                last.raise_for_status()
            raise QuotaExhausted(str(exc)) from exc
        if decision.key_index == -1 and _legacy_parked():
            # The router only offers the legacy endpoint when every pool key
            # is unavailable, and the portal park says that endpoint is out of
            # quota too. Re-sending would just burn another 429 on the same
            # dead key: stop honestly and let the paper wait for a later pass.
            raise QuotaExhausted(
                "the fallback provider is parked on a quota and no other "
                "provider is available right now"
            )
        endpoint = decision.endpoint
        url = f"{str(endpoint.base_url).rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        attempt_body = dict(body)
        attempt_body["model"] = endpoint.model or config["model"]
        # Non-thinking mode on Groq only; a Gemini/Cloudflare endpoint must not
        # receive these fields (it answers HTTP 400 on unknown names).
        _apply_groq_reasoning(attempt_body, endpoint.base_url)
        try:
            response = http.post(url, headers=headers, json=attempt_body)
        except httpx.TransportError:
            logger.warning("exam LLM transport error; re-routing", exc_info=True)
            last = None
            continue
        last = response
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 200:
            router.report_success(decision.key_index)
            if decision.key_index == -1:
                _clear_legacy()
            _pacer().report_success()
            return response
        detail = _response_error_detail(response)
        retry_after = _response_retry_after(response)
        # Routing debug trail (WARNING so it survives the server's root level):
        # which provider answered what, and how heavy the request body was.
        payload_bytes = len(
            json.dumps(attempt_body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        logger.warning(
            "exam LLM route %d/%d: provider=%s model=%s status=%d payload=%dB",
            attempt + 1,
            _MAX_ROUTE_ATTEMPTS,
            endpoint.name or "?",
            attempt_body.get("model"),
            status,
            payload_bytes,
        )
        if status == 400 and "response_format" in attempt_body:
            body.pop("response_format", None)
            continue
        if status == 404 and endpoint.is_fallback and is_unknown_model_error(detail):
            router.report_unknown_model(decision.key_index, attempt_body["model"])
            continue
        if status == 429:
            daily = _is_daily_quota_error(detail)
            if decision.key_index == -1:
                # The legacy endpoint has no router-side cooldown state; the
                # portal parks it so the next pick cannot hammer the same key.
                _report_legacy_rate_limit(router, endpoint, daily, retry_after)
                _pacer().report_rate_limited()
                if daily:
                    daily_providers.add(endpoint.name or "fallback")
                continue
            if daily:
                router.report_daily_exhausted(decision.key_index, retry_after)
                _pacer().report_rate_limited()
                daily_providers.add(endpoint.name or "groq")
                legacy = getattr(router, "_fallback", None)
                if (
                    legacy is not None
                    and getattr(legacy, "api_key", None) == endpoint.api_key
                ):
                    # This pool key IS the legacy endpoint's key: park the
                    # legacy view too, so step 4 of pick() cannot re-offer it.
                    _park_legacy(DAILY_QUOTA_COOLDOWN_SECONDS)
                fallback_gone = any(name != "groq" for name in daily_providers)
                tertiary_live = _pool_has_live(getattr(router, "_tertiary_keys", None))
                if (
                    router.groq_daily_exhausted
                    and (not router.has_fallback or fallback_gone)
                    and not tertiary_live
                ):
                    raise QuotaExhausted(
                        "every configured provider reached its daily quota"
                    )
                continue
            router.report_rate_limited(decision.key_index, retry_after)
            _pacer().report_rate_limited()
            continue
        if status >= 500:
            time.sleep(min(2.0, 0.5 * (attempt + 1)))
            continue
        if status == 413:
            # This provider's body limit is smaller than the request. Retrying
            # the same payload on the same key cannot help, so cool the key and
            # let the router hand the paper to the next provider (Gemini takes
            # much larger bodies). Upstream chunking keeps prompts under the
            # byte ceiling, so this branch is a safety net, not the normal path.
            if decision.key_index == -1:
                break  # the legacy single endpoint has nowhere to hand over
            router.report_rate_limited(decision.key_index, 60.0)
            continue
        if status in (401, 403):
            # A dead or revoked key: park it for the day so the pool stops
            # spending a round-trip on it for every paper, and try the next
            # provider. (The legacy single endpoint cannot be parked.)
            if decision.key_index == -1:
                break
            router.report_daily_exhausted(decision.key_index, None)
            continue
        # Other client errors (a malformed request, a bad model id on the
        # primary endpoint) are not something rotation fixes.
        break
    if last is not None:
        last.raise_for_status()
    raise httpx.HTTPError("exam LLM request failed after routing")


# ------------------------------------------------------------------ transport
def _delay(response: Any, attempt: int) -> float:
    """Honour ``Retry-After`` when a provider sends one, else back off."""
    headers = getattr(response, "headers", None)
    raw = ""
    try:
        if headers is not None:
            raw = str(headers.get("Retry-After") or "")
    except (AttributeError, TypeError):
        raw = ""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.5 * (2**attempt)
    return max(0.2, min(5.0, value))


def _send(
    http: httpx.Client, config: Mapping[str, Any], body: dict[str, Any]
) -> Any:
    """POST the chat request, retrying a rate limit and a rejected reply mode.

    A 400 on ``response_format`` is common on older models: the same prompt is
    sent once more without it. 429 and 5xx responses are retried twice with a
    short, bounded wait, because a class-worth of marks can otherwise fail on
    one burst of quota.
    """
    url = f"{config['base_url']}/chat/completions"
    headers = {"Authorization": f"Bearer {config['key']}"}
    _apply_groq_reasoning(body, config["base_url"])
    response = None
    for attempt in range(3):
        response = http.post(url, headers=headers, json=body)
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 400 and "response_format" in body:
            body.pop("response_format", None)
            continue
        if (status == 429 or status >= 500) and attempt < 2:
            time.sleep(_delay(response, attempt))
            continue
        break
    response.raise_for_status()
    return response


def _ask(
    config: Mapping[str, Any],
    prompt: str,
    *,
    max_tokens: int,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """One chat request; the parsed JSON object, or ``None`` on any failure."""
    prompt_bytes = len(prompt.encode("utf-8"))
    if len(prompt) > MAX_PROMPT_CHARS or prompt_bytes > MAX_PROMPT_BYTES:
        # Refuse rather than send: a provider rejection is a hard failure that
        # leaves every open item unmarked, while this lets the rules fall back
        # (the paper still gets a defensible deterministic score). The byte
        # check matters most: Hebrew text weighs ~2 bytes per character.
        logger.warning(
            "Exam model prompt too large (%d chars / %d bytes, ceilings %d / %d); "
            "marking with the rules",
            len(prompt),
            prompt_bytes,
            MAX_PROMPT_CHARS,
            MAX_PROMPT_BYTES,
        )
        note_failure(
            f"prompt too large ({len(prompt)} chars / {prompt_bytes} bytes)",
            base_url=config.get("base_url"),
        )
        return None
    body = {
        "model": config["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "אתה מורה מנוסה שמסמן בדיוק לפי מחוון. "
                    "החזר JSON תקין בלבד, בלי טקסט נוסף."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    try:
        owned = client is None
        http = client or _client(config["timeout"])
        try:
            # An injected client (tests, or a caller that owns the transport)
            # uses the simple single-key send. Production owns no client, so it
            # goes through the key router: rotation, fallback and quota signal.
            if client is not None:
                response = _send(http, config, body)
            else:
                response = _send_routed(http, config, body)
        finally:
            if owned:
                http.close()
        payload = response.json()
    except QuotaExhausted:
        # Propagated, not swallowed: the caller records that this paper waits
        # for a model pass instead of treating it as a generic failure.
        note_failure("daily quota exhausted", base_url=config.get("base_url"))
        raise
    except Exception as exc:  # noqa: BLE001 - the rules must still mark the paper
        logger.warning("LLM review unavailable", exc_info=True)
        reason, url = failure_reason(exc)
        note_failure(reason, base_url=url or config.get("base_url"))
        return None

    choices = payload.get("choices") if isinstance(payload, dict) else None
    message = (
        choices[0].get("message")
        if isinstance(choices, list) and choices
        else None
    )
    content = message.get("content") if isinstance(message, dict) else None
    verdict = _extract_json(str(content or ""))
    if not verdict:
        logger.info("LLM review returned no JSON")
        note_failure("model reply was not usable JSON", base_url=config.get("base_url"))
    return verdict


def _remember(key: str, verdict: dict[str, Any]) -> None:
    if len(_cache) >= _CACHE_LIMIT:
        _cache.clear()
    _cache[key] = verdict


def _cache_key(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _prompt(rule: dict[str, Any], answer: str, context: dict[str, Any]) -> str:
    total = int(rule.get("points") or 0)
    groups = [
        group
        for group in rule.get("groups") or []
        if isinstance(group, dict)
    ]
    rubric = "\n".join(
        f"- {group.get('label') or 'רכיב'}: {int(group.get('points') or 0)} נקודות"
        for group in groups
    )
    rejected = [str(word) for word in rule.get("reject_if") or []]
    wrong = ""
    if rejected:
        wrong = (
            "\nאבחנה הפוכה (אל תיתן עליה ניקוד): "
            + "; ".join(rejected[:8])
        )
    code = str(context.get("code") or "").strip()
    return (
        "אתה בודק תשובת הסבר במבחן פייתון בכיתה ז' בישראל.\n"
        f"השאלה: {context.get('title') or ''}\n"
        f"{context.get('intro') or ''}\n"
        + (f"הקוד שניתן לתלמיד:\n{code}\n" if code else "")
        + f"\nתשובת התלמיד (הסבר מה השגיאה):\n{answer}\n\n"
        + f"מחוון בן {total} נקודות:\n{rubric}\n"
        + wrong
        + "\n\nכללים:\n"
        "1. תן ניקוד רק לרכיבים שהתלמיד באמת אומר, לא למילה משותפת.\n"
        "2. תשובה שמצביעה על הסיבה ההפוכה מקבלת 0.\n"
        "3. תשובה נכונה בניסוח אחר מקבלת את מלוא הניקוד של הרכיב.\n"
        "4. אל תוסיף ואל תוריד נקודות שלא לפי המחוון.\n"
        "5. הנימוק (reason) מוצג לתלמיד/ה: הסבר לפי התשובה בלבד, ולעולם אל "
        "תצטט, תנסח מחדש או תזכיר בנימוק הערות מורה פנימיות, פרופיל ניקוד או "
        "דוגמאות מהגשות קודמות.\n"
        'החזר JSON בלבד: {"points": <0..%d>, "reason": "<משפט קצר בעברית>"}'
        % total
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start : end + 1])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def grade(
    rule: dict[str, Any],
    answer: str,
    context: dict[str, Any] | None = None,
    *,
    client: httpx.Client | None = None,
) -> tuple[int, str, bool] | None:
    """Ask the model for the points of one explanation, or ``None``.

    ``None`` means "no verdict" - the caller must fall back to the keyword
    pass. A verdict always stays inside ``0..points`` of the check.
    """
    config = settings()
    if not config["enabled"]:
        return None
    text = (answer or "").strip()
    if not text:
        return None
    total = int(rule.get("points") or 0)
    if total <= 0:
        return None
    context = context or {}
    key = _cache_key({"rule": rule, "answer": text, "context": context})
    cached = _cache.get(key)
    if cached is not None:
        return _verdict(cached, total)

    verdict = _ask(
        config,
        _prompt(rule, text, context),
        max_tokens=400,
        client=client,
    )
    if verdict is None:
        return None
    _remember(key, verdict)
    return _verdict(verdict, total)


def _verdict(value: dict[str, Any], total: int) -> tuple[int, str, bool] | None:
    try:
        points = int(value.get("points"))
    except (TypeError, ValueError):
        return None
    if points < 0 or points > total:
        return None
    reason = str(value.get("reason") or "").strip()[:500]
    note = f"הסבר נבדק על ידי המודל: {reason}" if reason else "הסבר נבדק על ידי המודל."
    return points, note, points < total


# ------------------------------------------------------------------- one item
def _format_evidence(runs: Sequence[Mapping[str, Any]]) -> str:
    """The sandbox results, written so the model can trust them as facts."""
    lines: list[str] = []
    for index, run in enumerate(runs, 1):
        stdin = str(run.get("stdin") or "").replace("\r\n", "\n").strip()
        if not run.get("ok"):
            error = str(run.get("stderr") or "").strip()[:300]
            lines.append(
                f"הרצה {index}: לא הורצה. {error or 'שגיאת הרצה.'}"
            )
            continue
        if run.get("timed_out"):
            lines.append(f"הרצה {index}: לא הסתיימה בזמן.")
            continue
        stdout = str(run.get("stdout") or "").strip()[:1500] or "(לא הודפס דבר)"
        stderr = str(run.get("stderr") or "").strip()[:600]
        block = (
            f"הרצה {index} (קלט: {stdin or 'ללא'}): "
            f"קוד יציאה {run.get('exit_code')}.\n"
            f"פלט:\n{stdout}"
        )
        if stderr:
            block += f"\nפלט שגיאה:\n{stderr}"
        lines.append(block)
    return "\n".join(lines)[:MAX_EVIDENCE_CHARS]


# The shared marking rules, kept verbatim so the single-item call and the
# whole-paper call grade by exactly the same contract (rubric parity). Only the
# trailing JSON shape differs between the two prompts.
_ITEM_RULES = (
    "כללים:\n"
    "1. דרג כל חלק בנפרד, בין 0 למקסימום שלו, ורק לפי המחוון וההנחיות.\n"
    "2. תן ניקוד על המשמעות, לא על מילה בודדת; תשובה נכונה בניסוח אחר "
    "מקבלת את מלוא הניקוד של אותו רכיב. בהסבר: אבחנה כללית שמצביעה על "
    "הכיוון או הגורם הנכון אך אינה מלאה (למשל 'הגרשיים לא נכונים' כשהבאג "
    "הוא שהמשתנה בתוך המרכאות, או 'צריך המרה' בלי לפרט לאיזה טיפוס) מקבלת "
    "ניקוד חלקי - כמחצית מנקודות הסיבה (למשל 2 מתוך 4), ולא 0. אל תאפס "
    "הסבר רק משום שהוא כללי או מקוצר, כל עוד הכיוון נכון; אך אבחנה הפוכה "
    "או שגויה מקבלת 0 לפי כלל 3.\n"
    "3. אבחנה הפוכה לזו שבהנחיות מקבלת 0 על אותו חלק.\n"
    "4. בקוד, הסתמך על ראיות ההרצה: קוד שלא רץ, או שמדפיס ערכים קבועים "
    "במקום לקלוט ולחשב, לא מקבל נקודות על פלט - למעט החריג המפורש בכלל 5, "
    "שגובר על כלל זה ועל הנחיות החלק.\n"
    "5. חריג שגיאת תחביר קלה (גובר על כלל 4 ועל הנחיות החלק): שגיאת תחביר "
    "קלה כוללת במפורש פסיק חסר בין ארגומנטים של print, מרכאה חסרה או נקודה "
    "חסרה. אם התלמיד/ה הראו בבירור שהבינו את הרעיון המרכזי - למשל המשתנה "
    "מופיע מחוץ למרכאות (וזה הרעיון של שאלה זו), או שזוהתה ההמרה הנכונה - "
    "אזי עצם הצבת המשתנה מחוץ למחרוזת היא חיבור נכון רעיונית, גם אם חסר "
    "פסיק או אופרטור (+, f-string, format). במקרה כזה אל תסווגו את חוסר "
    "הפסיק כ'שגיאה קריטית' ואל תאפסו את חלק הקוד: תנו את מלוא נקודות "
    "הריצה, הפלט והחיבור כאילו הקוד רץ והדפיס את הפלט הנכון (התלמיד התכוון "
    "אליו והמבנה נכון), ואז הורידו 1-2 נקודות בלבד מהמקסימום על התחביר - "
    "כלומר 3-4 מתוך 5. אל תשאירו את נקודות הריצה והפלט על 0 רק משום שהפסיק "
    "החסר מנע ריצה. אך אם הרעיון עצמו שגוי - המשתנה עדיין בתוך המחרוזת, המרה "
    "הפוכה, או פלט קבוע בלי הבנה - אין ניקוד על אותו חלק.\n"
    "6. אל תוסיף ואל תוריד נקודות שלא לפי המחוון.\n"
    "7. חלק שלא נענה לא נשלח לבדיקה ואין עליו ניקוד.\n"
    "8. הנימוק (reason) מוצג לתלמיד/ה: הסבר את הניקוד לפי התשובה של התלמיד/ה "
    "בלבד. הערות המורה, פרופיל סגנון הניקוד ודוגמאות מהגשות קודמות הם מידע "
    "פנימי לכיול הניקוד בלבד - לעולם אל תצטט, אל תנסח מחדש ואל תזכיר אותם "
    "בנימוק, גם לא כמקור להחלטה."
)

_ITEM_JSON = (
    'החזר JSON בלבד: {"parts": [{"part": "<key>", "points": <int>, '
    '"reason": "<משפט קצר בעברית>"}], "reason": "<סיכום קצר בעברית>"}'
)


def _item_body(
    parts: Sequence[Mapping[str, Any]], context: Mapping[str, Any]
) -> list[str]:
    """The question, guidelines, rubric, calibration and answers of one item.

    Shared by the single-item prompt and the whole-paper prompt so both carry
    identical per-question context; only the trailing rules/JSON shape differs.
    """
    code = str(context.get("code") or "").strip()
    reference = str(context.get("reference") or "").strip()
    chunks = [
        "אתה בודק שאלה פתוחה במבחן פייתון בכיתה ז' בישראל.",
        f"השאלה: {context.get('title') or ''}",
    ]
    intro = str(context.get("intro") or "").strip()
    if intro:
        chunks.append(intro)
    if code:
        chunks.append(f"הקוד שניתן לתלמיד (עם השגיאה או הדרישה):\n{code}")
    if reference:
        chunks.append(
            "פתרון נכון, להשוואה בלבד - אל תיתן נקודות על התאמה מילולית אליו:\n"
            + reference
        )
    chunks.append(
        "הנחיות הבדיקה המפורטות של המורה:\n"
        + str(context.get("guidelines") or "").strip()
    )
    # The teacher's bounded calibration from past hand-ins of this same question.
    # Already capped upstream (portal.exams.feedback), so it adds a fixed, small
    # token cost - never an unbounded note dump that could exhaust the request.
    feedback_block = str(context.get("feedback") or "").strip()
    if feedback_block:
        chunks.append(feedback_block)
    rubric = [
        f"- {part['label']} ({part['part']}): עד {part['max']} נקודות. "
        f"{part['guidance']}".strip()
        for part in parts
    ]
    chunks.append("מחוון לפי חלקי השאלה:\n" + "\n".join(rubric))
    answers: list[str] = []
    for part in parts:
        entry = [
            f"### {part['label']} ({part['part']})",
            str(part["answer"]).strip()[:MAX_ANSWER_CHARS],
        ]
        runs = part.get("evidence") or []
        if runs:
            entry.append("ראיות הרצה אוטומטיות של הקוד:\n" + _format_evidence(runs))
        answers.append("\n".join(entry))
    chunks.append("תשובת התלמיד, חלק אחר חלק:\n\n" + "\n\n".join(answers))
    return chunks


def _item_prompt(
    parts: Sequence[Mapping[str, Any]], context: Mapping[str, Any]
) -> str:
    chunks = _item_body(parts, context)
    chunks.append(_ITEM_RULES + "\n" + _ITEM_JSON)
    return "\n\n".join(chunks)


def grade_item(
    parts: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any] | None = None,
    *,
    client: httpx.Client | None = None,
) -> dict[str, tuple[int, str]] | None:
    """Mark every answered part of one open item in a single model call.

    Each part is ``{"part", "label", "max", "guidance", "answer"}`` and may
    carry ``"evidence"`` - the sandbox runs of a code part. The return value
    maps a part key to ``(points, reason)`` and covers exactly the parts that
    were sent. ``None`` on any failure, so the caller keeps the deterministic
    marks.
    """
    config = settings()
    if not config["enabled"]:
        return None
    context = dict(context or {})
    guidelines = str(context.get("guidelines") or "").strip()
    if not guidelines or not parts:
        return None
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in parts:
        key = str(part.get("part") or "")
        try:
            maximum = int(part.get("max"))
        except (TypeError, ValueError):
            return None
        answer = str(part.get("answer") or "").strip()
        if not key or key in seen or maximum <= 0 or not answer:
            return None
        seen.add(key)
        cleaned.append(
            {
                "part": key,
                "label": str(part.get("label") or key),
                "max": maximum,
                "guidance": str(part.get("guidance") or "").strip(),
                "answer": answer,
                "evidence": [
                    dict(run)
                    for run in part.get("evidence") or []
                    if isinstance(run, Mapping)
                ],
            }
        )

    cache_key = _cache_key({"item": cleaned, "context": context})
    cached = _cache.get(cache_key)
    if cached is not None:
        return _item_verdict(cached, cleaned)

    verdict = _ask(
        config,
        _item_prompt(cleaned, context),
        max_tokens=700,
        client=client,
    )
    if verdict is None:
        return None
    _remember(cache_key, verdict)
    return _item_verdict(verdict, cleaned)


def _item_verdict(
    value: dict[str, Any], parts: Sequence[Mapping[str, Any]]
) -> dict[str, tuple[int, str]] | None:
    """Validate a whole-item reply; one bad part rejects the whole verdict."""
    entries = value.get("parts")
    if not isinstance(entries, list):
        return None
    maxima = {str(part["part"]): int(part["max"]) for part in parts}
    marks: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        key = str(entry.get("part") or "")
        if key not in maxima or key in marks:
            return None
        try:
            points = int(entry.get("points"))
        except (TypeError, ValueError):
            return None
        if points < 0 or points > maxima[key]:
            return None
        marks[key] = (points, str(entry.get("reason") or "").strip()[:300])
    if set(marks) != set(maxima):
        return None
    return marks


# ------------------------------------------------------------- whole paper
def _paper_prompt(items: Sequence[Mapping[str, Any]]) -> str:
    """One prompt carrying every open item of a paper, graded in a single call.

    Mirrors the original worksheet evaluator (``src/evaluator/core.py``): all
    questions travel together and the model returns one JSON with a verdict per
    question. Each item keeps its own guidelines, rubric and calibration so the
    model never blends one question's marking scheme into another.
    """
    blocks = [
        "אתה בודק הגשה שלמה במבחן פייתון בכיתה ז' בישראל.",
        "לפניך כמה שאלות פתוחות מאותה הגשה. לכל שאלה יש מחוון והנחיות משלה.",
        "עבור על כל שאלה בנפרד ודרג כל חלק שלה בין 0 למקסימום שלו, רק לפי "
        "המחוון וההנחיות של אותה שאלה. אל תערבב בין שאלות.",
    ]
    for item in items:
        body = _item_body(item["parts"], item["context"])
        blocks.append(f"=== שאלה {item['item']} ===\n" + "\n\n".join(body))
    blocks.append(_ITEM_RULES)
    blocks.append(
        'החזר JSON בלבד: {"items": [{"item": "<מפתח השאלה>", '
        '"parts": [{"part": "<key>", "points": <int>, '
        '"reason": "<משפט קצר בעברית>"}], "reason": "<סיכום קצר בעברית>"}]}'
    )
    return "\n\n".join(blocks)


def _paper_batches(
    items: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    """Greedily pack items into the fewest prompts that fit both ceilings.

    An ordinary paper fits one call. A long one (long Hebrew answers weigh ~2
    bytes per character) is split into batches whose assembled prompt stays
    under the char *and* byte ceilings, so no request can grow into a provider
    "413 Payload Too Large". A single item too large for one prompt travels
    alone; :func:`_ask` then refuses it and the deterministic rules mark it.
    """
    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for item in items:
        candidate = current + [item]
        prompt = _paper_prompt(candidate)
        if current and (
            len(prompt) > MAX_PROMPT_CHARS
            or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES
        ):
            batches.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def grade_paper(
    items: Sequence[Mapping[str, Any]],
    *,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, tuple[int, str]]] | None:
    """Mark every open item of one paper in as few model calls as possible.

    Each item is ``{"item": <key>, "context": {...}, "parts": [...]}`` where the
    parts carry the same shape :func:`grade_item` expects. Returns a mapping
    ``item_key -> {part_key: (points, reason)}`` for the items the model answered
    validly; an item the reply missed or malformed is simply absent, so the
    caller can fall back to a per-item call for just that item. ``None`` when the
    feature is off, the input is incomplete, or the reply is unusable - the
    caller then falls back for every item. This is the fast path the original
    worksheet evaluator used: one round trip per paper instead of one per
    question, which also spends far less daily quota.
    """
    config = settings()
    if not config["enabled"]:
        return None
    if not items:
        return None
    cleaned: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for item in items:
        key = str(item.get("item") or "")
        context = dict(item.get("context") or {})
        parts = item.get("parts") or []
        guidelines = str(context.get("guidelines") or "").strip()
        if not key or key in seen_items or not guidelines or not parts:
            return None
        seen_items.add(key)
        cleaned_parts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for part in parts:
            pkey = str(part.get("part") or "")
            try:
                maximum = int(part.get("max"))
            except (TypeError, ValueError):
                return None
            answer = str(part.get("answer") or "").strip()
            if not pkey or pkey in seen or maximum <= 0 or not answer:
                return None
            seen.add(pkey)
            cleaned_parts.append(
                {
                    "part": pkey,
                    "label": str(part.get("label") or pkey),
                    "max": maximum,
                    "guidance": str(part.get("guidance") or "").strip(),
                    "answer": answer,
                    "evidence": [
                        dict(run)
                        for run in part.get("evidence") or []
                        if isinstance(run, Mapping)
                    ],
                }
            )
        cleaned.append({"item": key, "context": context, "parts": cleaned_parts})

    cache_key = _cache_key({"paper": cleaned})
    cached = _cache.get(cache_key)
    if cached is not None:
        return _paper_verdict(cached, cleaned)

    # One bounded output budget per batch: a short reason per part, scaled by
    # the number of parts so a long paper is not truncated mid-JSON. Ordinary
    # papers fit a single batch (one round trip); a paper whose prompt crosses
    # the char/byte ceilings is split into the fewest fitting batches and the
    # verdicts are merged, so no request can grow into a provider 413.
    merged: list[Any] = []
    for batch in _paper_batches(cleaned):
        n_parts = sum(len(item["parts"]) for item in batch)
        max_tokens = min(6000, 300 * n_parts + 400)
        verdict = _ask(
            config,
            _paper_prompt(batch),
            max_tokens=max_tokens,
            client=client,
        )
        if verdict is None:
            # This batch fell back (refusal, provider failure, bad reply): its
            # items stay absent so the caller re-asks them one by one.
            continue
        entries = verdict.get("items")
        if isinstance(entries, list):
            merged.extend(entries)
    if not merged:
        return None
    whole = {"items": merged}
    _remember(cache_key, whole)
    return _paper_verdict(whole, cleaned)


def _paper_verdict(
    value: dict[str, Any], items: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, tuple[int, str]]] | None:
    """Validate a whole-paper reply, item by item.

    Each item is accepted only when every one of its parts is present and inside
    its maximum (reusing :func:`_item_verdict`, so the batched call holds the
    same parity contract as the per-item call). A malformed or missing item is
    omitted rather than rejecting the whole reply, so the caller falls back for
    just that item. ``None`` only when the top-level shape is wrong.
    """
    entries = value.get("items")
    if not isinstance(entries, list):
        return None
    by_item = {str(item["item"]): item["parts"] for item in items}
    result: dict[str, dict[str, tuple[int, str]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("item") or "")
        parts = by_item.get(key)
        if parts is None or key in result:
            continue
        marks = _item_verdict(entry, parts)
        if marks is not None:
            result[key] = marks
    return result


# ------------------------------------------------------------- calibration
def _calibration_prompt(title: str, corrections_text: str, max_chars: int) -> str:
    """Ask the model to distill a teacher's corrections into general principles.

    This is a one-off background call, not a per-paper grade: its input is the
    bounded correction ledger for one question (already capped upstream), and its
    output is a short profile. The grading prompt later carries only that profile,
    so a large correction history never reaches the per-paper request.
    """
    return (
        "אתה עוזר למורה לכייל בדיקת מבחן פייתון בכיתה ז' בישראל.\n"
        f"השאלה: {title}\n\n"
        "להלן תיקוני ניקוד שהמורה ביצע על תשובות תלמידים בשאלה זו. כל שורה מציינת "
        "את תשובת התלמיד, את הניקוד האוטומטי, את הניקוד שהמורה נתן בפועל, ואת הערת "
        "המורה:\n\n"
        f"{corrections_text}\n\n"
        "המשימה: זקק את התיקונים לכללי ניקוד כלליים בסגנון של המורה - מתי לתת ניקוד "
        "חלקי, מתי לאפס, אילו שגיאות נחשבות קלות ואילו מהותיות. אל תזכיר תלמידים "
        "ספציפיים; כתוב עקרונות שחלים על כל תשובה עתידית בשאלה זו. אל תשנה את תקרות "
        "הניקוד של המחוון - ההנחיות רק מכוונות שיקול דעת בתוך התקרות הקיימות.\n"
        f"כתוב בעברית, עד {max_chars} תווים, כפסקה אחת קוהרנטית וברורה.\n"
        'החזר JSON בלבד: {"profile": "<כללי הניקוד המזוקקים>"}'
    )


def summarize_corrections(
    title: str,
    corrections_text: str,
    *,
    max_chars: int = 500,
    client: httpx.Client | None = None,
) -> str | None:
    """Distill one question's correction ledger into a bounded style profile.

    Returns the profile string (trimmed to ``max_chars``), or ``None`` when the
    model is off or returns nothing usable - the caller keeps the previous
    profile. :class:`QuotaExhausted` propagates so the caller can wait and retry
    instead of treating an exhausted quota as a generic failure. Shares the exact
    routing, pacing and fallback plumbing of grading.
    """
    config = settings()
    if not config["enabled"]:
        return None
    text = (corrections_text or "").strip()
    if not text:
        return None
    verdict = _ask(
        config,
        _calibration_prompt(str(title or "").strip(), text, max_chars),
        max_tokens=700,
        client=client,
    )
    if not verdict:
        return None
    profile = str(verdict.get("profile") or "").strip()
    if not profile:
        return None
    return profile[:max_chars]


__all__ = [
    "QuotaExhausted",
    "clear_failures",
    "enabled",
    "failure_reason",
    "grade",
    "grade_item",
    "note_failure",
    "pace",
    "recent_failures",
    "reset_routing",
    "routing_status",
    "settings",
    "summarize_corrections",
]
