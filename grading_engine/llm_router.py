"""Resilient multi-key / multi-provider routing for LLM evaluations.

Free-tier reality (verified Oct 2026): Groq free gives qwen/qwen3.8-27b only
30 RPM / 1K RPD / 8K TPM per key; OpenRouter :free caps ~50 req/day without
credits; Gemini free gives gemini-2.0-flash ~15 RPM / 1M TPM / 1500 RPD.
No single free key survives a class batch, so this module layers defences:

1. Key rotation - GROQ_API_KEY + GROQ_API_KEYS form one pool; a 429 cools
   that key and the retry is served by the next key.
2. Retry-After honoring - sleep exactly what the 429 header asks for.
3. Provider fallback - LLM_FALLBACK_MODEL/API_KEY/BASE_URL (Gemini or
   OpenRouter, both OpenAI-compatible) serves when all Groq keys cool.
4. Daily-quota discrimination - a per-minute 429 refills within the minute and
   is worth waiting for. A per-day 429 (TPD/RPD) does not refill for hours, so
   that key is parked until reset and the request is served by the fallback at
   once. Sleeping on a daily quota is what stalls a batch: every Groq key in a
   pool shares one organization-wide daily bucket, so rotation cannot help.

Only openai + stdlib. No network at import; module-level shared state.
"""
from __future__ import annotations
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)
DEFAULT_KEY_COOLDOWN_SECONDS = 45.0
MAX_RETRY_AFTER_SECONDS = 300.0

# A daily quota (tokens/day, requests/day) resets on a rolling 24h window, so
# waiting it out inside one batch is never worthwhile. A key is parked for this
# long on a daily 429: long enough to hand the rest of the run to the fallback,
# short enough that a run after the reset still uses Groq.
DAILY_QUOTA_COOLDOWN_SECONDS = 6 * 3600.0

# How finely a paced pause is sliced when a caller wants to watch it (a UI
# repainting a countdown, a CLI printing a ticker). A single silent multi-second
# sleep is indistinguishable from a hang - which is exactly how a stretched OTPM
# backoff reads on screen - so narrated waits are cut into short ticks.
PAUSE_TICK_SECONDS = 0.25

# Markers providers use in a 429 body to say "this is a per-day bucket, not the
# per-minute one that refills in seconds".
#
# Matched as *whole words* on purpose. The original substring test matched a
# bare "rpd"/"tpd" anywhere, which meant an unrelated token containing those
# letters could be read as a daily cap and park a healthy key until the next
# reset - the failure mode being fought here is a misclassification that keeps
# the batch stuck, so it must err towards "per-minute" only when the message
# truly is silent about a day.
_DAILY_QUOTA_PATTERN = re.compile(
    r"""
    tokens?\s+per\s+day      # "tokens per day" / Groq's "tokens per day (TPD)"
    | tokens?[\s/]day        # "tokens/day"
    | requests?\s+per\s+day  # "requests per day"
    | requests?[\s/]day      # "requests/day"
    | \btpd\b                # the acronym on its own
    | \brpd\b
    | \bper[\s-]day\b        # "per day" / "per-day" (Gemini, OpenRouter)
    """,
    re.IGNORECASE | re.VERBOSE,
)


class DailyQuotaExhausted(RuntimeError):
    """Every configured provider is out of daily quota.

    Raised rather than slept on: retrying an exhausted daily bucket cannot
    succeed for hours, so stalling the batch buys nothing.
    """


def is_daily_quota_error(detail: str) -> bool:
    """True when a rate-limit message describes a per-day cap, not a per-minute one."""
    return bool(_DAILY_QUOTA_PATTERN.search(detail or ""))


# Named OpenAI-compatible fallback providers. Choosing one by name
# (LLM_FALLBACK_PROVIDER) fills in a correct base URL and a sane default model,
# so a teacher only has to paste an API key - no base URL to remember and re-enter
# on every machine. Explicit LLM_FALLBACK_BASE_URL / LLM_FALLBACK_MODEL still
# override either field, so the presets are a convenience, not a cage.
#
# Model ids retire without warning (gemini-2.0-flash was shut down in 2026 and
# started answering HTTP 404), so the router treats a 404/unknown-model response
# as a *permanent* endpoint failure and walks GEMINI_MODEL_CANDIDATES in order
# (see _fallback_candidates) instead of failing the whole batch on one dead id.
@dataclass(frozen=True)
class ProviderPreset:
    name: str
    base_url: str
    default_model: str


FALLBACK_PROVIDERS: dict[str, ProviderPreset] = {
    "gemini": ProviderPreset(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        # Google's current stable Flash id (Oct 2026 docs); an explicit
        # LLM_FALLBACK_MODEL still overrides it.
        default_model="gemini-3.8-flash",
    ),
    "openrouter": ProviderPreset(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        # A :free OpenRouter model; override with LLM_FALLBACK_MODEL.
        default_model="meta-llama/llama-3.3-70b-instruct:free",
    ),
    "cloudflare": ProviderPreset(
        name="cloudflare",
        # {account_id} is filled from CLOUDFLARE_ACCOUNT_ID at resolve time
        # (see _cloudflare_base_url); the literal template stays here so
        # describe()/tests can show the shape without credentials.
        base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        # Workers AI OpenAI-compatible chat model (docs, Sep 2026).
        default_model="@cf/meta/llama-3.1-8b-instruct",
    ),
}

# Gemini model ids to try, newest first. The *configured* model (explicit
# LLM_FALLBACK_MODEL, else the preset default) is always tried first; the rest
# are fallbacks for when that id has been retired server-side (HTTP 404
# "model ... is no longer available"). Kept small and stable-only: aliases
# like "latest" hot-swap under us and preview ids carry tighter rate limits.
GEMINI_MODEL_CANDIDATES: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
)
CLOUDFLARE_BASE_URL_TEMPLATE = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
)


def _cloudflare_base_url(account_id: str) -> str:
    """Build the Workers AI OpenAI-compatible base URL for an account.

    The account id is part of the path (``.../accounts/{id}/ai/v1``), so
    unlike gemini/openrouter the URL cannot be known from the provider name
    alone. Auth is ``Authorization: Bearer <API token>`` - the openai SDK
    sends that from ``api_key`` automatically.
    """
    return CLOUDFLARE_BASE_URL_TEMPLATE.format(
        account_id=(account_id or "").strip()
    )


# Markers in a provider error body meaning "this model id does not exist
# (anymore)" - never retryable, never worth cooling down over. Matched
# case-insensitively against the error detail string.
_UNKNOWN_MODEL_PATTERNS: tuple[str, ...] = (
    "is not available",
    "is no longer available",
    "model not found",
    "unknown model",
    "unsupported model",
)


def is_unknown_model_error(detail: str) -> bool:
    """True when a provider error says the model id is dead/unknown (HTTP 404)."""
    lowered = (detail or "").lower()
    if "404" not in lowered:
        return False
    return any(marker in lowered for marker in _UNKNOWN_MODEL_PATTERNS)


def _guess_provider_name(base_url: str) -> str:
    """Best-effort provider name for a base URL (logging/notice only)."""
    lowered = (base_url or "").lower()
    if "googleapis" in lowered or "generativelanguage" in lowered:
        return "gemini"
    if "openrouter" in lowered:
        return "openrouter"
    return "fallback"


@dataclass
class ProviderEndpoint:
    name: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    is_fallback: bool = False


@dataclass
class RoutingDecision:
    endpoint: ProviderEndpoint = field(default_factory=ProviderEndpoint)
    key_index: int = 0
    waited_seconds: float = 0.0


@dataclass
class _KeyState:
    api_key: str = ""
    cooldown_until: float = 0.0
    consecutive_429s: int = 0
    served: int = 0
    # Set when the provider reported a *daily* quota. A daily cap is shared by
    # the whole organization, so other keys cannot cover for it and waiting is
    # pointless; the key is skipped until this timestamp instead of cooled.
    daily_until: float = 0.0

    def cooling(self, now: float) -> bool:
        return self.cooldown_until > now

    def daily_exhausted(self, now: float) -> bool:
        return self.daily_until > now


def _clamp_retry_after(value: Optional[float], default: float) -> float:
    try:
        seconds = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if seconds != seconds or seconds <= 0:
        return default
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def parse_retry_after(exc: BaseException) -> Optional[float]:
    """Best-effort retry-after seconds from an OpenAI SDK error."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after")
        except Exception:
            raw = None
        if raw is not None:
            try:
                return max(float(str(raw).strip().rstrip("s")), 0.0)
            except ValueError:
                pass
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for key in ("retry-after", "retry_after", "retryAfter"):
            nested = body.get(key)
            if nested is not None:
                try:
                    return max(float(nested), 0.0)
                except (TypeError, ValueError):
                    continue
    return None
@dataclass
class _FallbackKeyState:
    """One key inside a fallback/tertiary pool (independent quotas)."""

    api_key: str
    cooldown_until: float = 0.0
    daily_until: float = 0.0




def split_keys(primary: Optional[str], extras: Optional[str]) -> list[str]:
    pool: list[str] = []
    for chunk in ((primary or ""), (extras or "").replace(";", ",")):
        for piece in chunk.split(","):
            piece = piece.strip().strip("\"'")
            if piece and piece not in pool:
                pool.append(piece)
    return pool


def _dedup_keys(keys: Optional[list]) -> list[str]:
    """Clean and deduplicate a list of API keys, preserving order."""
    seen: set = set()
    out: list[str] = []
    for k in keys or []:
        k = (k or "").strip().strip("\"'")
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


class LLMRouter:
    """Thread-safe pool of Groq keys with a fallback provider behind them."""

    def __init__(
        self,
        groq_keys: list,
        groq_model: str,
        groq_base_url: str,
        fallback: Optional[ProviderEndpoint] = None,
        key_cooldown_seconds: float = DEFAULT_KEY_COOLDOWN_SECONDS,
        fallback_keys: Optional[list] = None,
        tertiary: Optional[ProviderEndpoint] = None,
        tertiary_keys: Optional[list] = None,
    ) -> None:
        cleaned = [k.strip() for k in groq_keys if k and k.strip()]
        seen: set = set()
        unique = [k for k in cleaned if not (k in seen or seen.add(k))]
        if not unique:
            raise ValueError("LLMRouter needs at least one API key.")
        self._keys = [_KeyState(api_key=k) for k in unique]
        self._groq = ProviderEndpoint(name="groq", base_url=groq_base_url, model=groq_model)
        self._fallback = fallback  # legacy single-key view (first key); pools live in _fallback_keys
        self._cooldown = max(key_cooldown_seconds, 1.0)
        self._tertiary = tertiary
        self._fallback_keys = [_FallbackKeyState(api_key=k) for k in _dedup_keys(fallback_keys or [])]
        self._tertiary_keys = [_FallbackKeyState(api_key=k) for k in _dedup_keys(tertiary_keys or [])]
        self._lock = threading.Lock()
        self._round_robin = 0
        self._fallback_pos = 0
        self._tertiary_pos = 0
        self.fallback_uses = 0
        # Set the first time the batch had to leave Groq because the daily
        # token quota ran out (as opposed to a per-minute blip). The UI/CLI
        # reads this to warn the teacher *once* instead of per student.
        self.daily_fallback_reason: Optional[str] = None

    def _pool_pick(self, slots: list, pos_attr: str) -> Optional[tuple]:
        """Round-robin pick from a pool of key-state slots, skipping cooled/daily-until slots.

        Returns ``(index, slot)`` for the first live slot at or after the current
        position, advancing the position past it. Returns ``None`` when every slot
        is parked on a cooldown or daily quota.
        """
        now = time.monotonic()
        live = [i for i, s in enumerate(slots) if now >= s.cooldown_until and now >= s.daily_until]
        if not live:
            return None
        pos = getattr(self, pos_attr, 0) % len(slots)
        ordered = sorted(live, key=lambda i: (i - pos) % len(slots))
        chosen = ordered[0]
        setattr(self, pos_attr, (chosen + 1) % len(slots))
        return chosen, slots[chosen]

    @property
    def key_count(self) -> int:
        return len(self._keys)

    @property
    def has_fallback(self) -> bool:
        return self._fallback is not None and bool(self._fallback.api_key) and bool(self._fallback.model)

    @property
    def used_fallback(self) -> bool:
        """True once any request was served by the fallback provider."""
        return self.fallback_uses > 0

    def exhausted_key_count(self, now: Optional[float] = None) -> int:
        """How many Groq keys are currently parked on a daily quota."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            return sum(1 for k in self._keys if k.daily_exhausted(moment))

    @property
    def groq_daily_exhausted(self) -> bool:
        """True when the Groq daily token quota is gone for this run.

        Reported by :meth:`report_daily_exhausted` once the provider says so,
        and re-checked against live key state. This is the flag the UI polls to
        tell the teacher that a fallback provider is now doing the grading.
        """
        if self.daily_fallback_reason:
            return True
        return self.key_count > 0 and self.exhausted_key_count() >= self.key_count

    def fallback_notice(self) -> Optional[str]:
        """One-line user-facing warning, or None when Groq is still serving."""
        if self.daily_fallback_reason is None:
            return None
        if not self.has_fallback or self._fallback is None:
            return (
                "⚠️ Groq daily token quota is exhausted and no fallback provider "
                "is configured - grading is stopping early. "
                "Set LLM_FALLBACK_MODEL/_API_KEY/_BASE_URL to keep going."
            )
        return (
            f"⚠️ Groq daily token quota is exhausted - grading switched to the "
            f"fallback provider ({self._fallback.name}: {self._fallback.model}) "
            f"after {self.fallback_uses} request(s). Groq resumes after the quota resets."
        )

    def describe(self) -> str:
        parts = [f"{self.key_count}x Groq key(s) -> {self._groq.model}"]
        if self.has_fallback and self._fallback is not None:
            parts.append(f"fallback {self._fallback.name}:{self._fallback.model} ({self.fallback_uses} uses)")
        return " + ".join(parts)

    def pick(self) -> RoutingDecision:
        """Choose the endpoint for one request, never sleeping on a daily quota.

        Order of preference:
        1. a healthy Groq key (round-robin over the pool),
        2. a healthy fallback-pool key (round-robin over ``fallback_keys``),
        3. a healthy tertiary-pool key (round-robin over ``tertiary_keys``),
        4. the legacy single fallback provider when every Groq key is unavailable,
        5. wait out the shortest *per-minute* cooldown, then retry.

        A key parked on a daily quota is skipped outright: the bucket is shared
        organization-wide, so neither rotation nor sleeping can revive it. When
        nothing at all is usable the call raises :class:`DailyQuotaExhausted`
        (or returns the fallback) instead of blocking the batch for hours.

        The per-minute wait loops instead of recursing: a long batch reaches
        that branch once per throttled student, and recursion would grow the
        stack every time until it blows.
        """
        while True:
            now = time.monotonic()
            with self._lock:
                # 1. Groq pool
                for offset in range(len(self._keys)):
                    index = (self._round_robin + offset) % len(self._keys)
                    key = self._keys[index]
                    if key.cooling(now) or key.daily_exhausted(now):
                        continue
                    self._round_robin = (index + 1) % len(self._keys)
                    ep = ProviderEndpoint(
                        name="groq",
                        base_url=self._groq.base_url,
                        model=self._groq.model,
                        api_key=key.api_key,
                    )
                    return RoutingDecision(endpoint=ep, key_index=index)

                # 2. Fallback pool (round-robin over fallback_keys)
                if self._fallback_keys:
                    picked = self._pool_pick(self._fallback_keys, "_fallback_pos")
                    if picked is not None:
                        f_index, f_key = picked
                        self.fallback_uses += 1
                        ep = ProviderEndpoint(
                            name=(self._fallback.name if self._fallback else "fallback"),
                            base_url=(self._fallback.base_url if self._fallback else ""),
                            model=(self._fallback.model if self._fallback else ""),
                            api_key=f_key.api_key,
                            is_fallback=True,
                        )
                        return RoutingDecision(endpoint=ep, key_index=-(2 + f_index))

                # 3. Tertiary pool (round-robin over tertiary_keys)
                if self._tertiary_keys:
                    picked = self._pool_pick(self._tertiary_keys, "_tertiary_pos")
                    if picked is not None:
                        t_index, t_key = picked
                        ep = ProviderEndpoint(
                            name=(self._tertiary.name if self._tertiary else "tertiary"),
                            base_url=(self._tertiary.base_url if self._tertiary else ""),
                            model=(self._tertiary.model if self._tertiary else ""),
                            api_key=t_key.api_key,
                        )
                        return RoutingDecision(endpoint=ep, key_index=-(1000 + t_index))

                # 4. Legacy single fallback endpoint
                all_daily = all(k.daily_exhausted(now) for k in self._keys)
                if self.has_fallback and self._fallback is not None:
                    # No healthy Groq key: take the fallback now rather than
                    # stalling on a quota that will not refill inside this run.
                    # Counted here (not in report_success) so a single routing
                    # decision is a single use, whether it later succeeds or not.
                    self.fallback_uses += 1
                    logger.warning(
                        "All %d Groq key(s) unavailable%s; serving from fallback %s.",
                        len(self._keys),
                        " (daily token quota reached)" if all_daily else "",
                        self._fallback.model,
                    )
                    return RoutingDecision(endpoint=self._fallback, key_index=-1)

                if all_daily:
                    raise DailyQuotaExhausted(
                        "Every Groq key hit its daily token quota and no fallback "
                        "provider is configured. Set LLM_FALLBACK_MODEL/_API_KEY/"
                        "_BASE_URL (Gemini or OpenRouter) to keep grading, or wait "
                        "for the daily quota to reset."
                    )
                # Only per-minute cooldowns are left, and those genuinely refill
                # within the minute, so waiting is the right call here.
                earliest = min(k.cooldown_until for k in self._keys)
            wait = max(earliest - now, 0.0)
            if wait <= 0:
                # Cooldown expired while we were unlocked: loop instead of
                # recursing, so a long throttled batch cannot grow the stack.
                continue
            time.sleep(min(wait, MAX_RETRY_AFTER_SECONDS))

    def report_success(self, key_index: int) -> None:
        if key_index >= 0:
            with self._lock:
                key = self._keys[key_index]
                key.served += 1
                key.consecutive_429s = 0
                key.cooldown_until = 0.0
            return
        if key_index == -1:
            # Legacy single fallback endpoint: uses are counted at routing time
            # (see pick), so counting again here would double every call.
            return
        # Pool keys: fallback (-2, -3, ...) or tertiary (-1000, -1001, ...)
        if key_index <= -1000:
            pool = self._tertiary_keys
            p_index = -1000 - key_index
        else:
            pool = self._fallback_keys
            p_index = -2 - key_index

        if 0 <= p_index < len(pool):
            with self._lock:
                key = pool[p_index]
                key.served += 1
                key.consecutive_429s = 0
                key.cooldown_until = 0.0

    def report_unknown_model(self, key_index: int, model: str) -> bool:
        """Retire a dead fallback model id and switch to the next candidate.

        A 404 "model is no longer available" is permanent: the id was shut
        down server-side (gemini-2.0-flash in 2026) and no retry, cooldown, or
        key rotation will ever revive it. For Gemini endpoints the next id in
        :data:`GEMINI_MODEL_CANDIDATES` is adopted immediately so the very next
        request - including the retry of the failed one - uses a live model.
        Returns True when a new candidate was adopted, False when the dead id
        was already the last resort (caller must then surface the error).
        Groq keys (``key_index >= 0``) never reach this path: a Groq 404 is a
        misconfigured GROQ_MODEL and silently swapping it would hide the
        teacher's own setting.
        """
        if key_index >= 0 or self._fallback is None:
            return False
        with self._lock:
            fallback = self._fallback
            if _guess_provider_name(fallback.base_url) != "gemini":
                return False
            if fallback.model != model:
                # Another thread already moved past the dead id.
                return True
            ordered = _fallback_candidates(fallback)
            try:
                position = ordered.index(model)
            except ValueError:
                position = -1
            remaining = ordered[position + 1 :]
            if not remaining:
                return False
            nxt = remaining[0]
            fallback.model = nxt
            logger.error(
                "Fallback model '%s' is retired (HTTP 404); switched to '%s'.",
                model,
                nxt,
            )
            return True

    def report_daily_exhausted(
        self, key_index: int, retry_after: Optional[float] = None
    ) -> float:
        """Park keys after the provider reports a *daily* quota.

        Groq's Tokens-Per-Day bucket is shared by a whole **organization**, not
        by key, so keys inside one org die together - but the pool may also hold
        keys from *separate* orgs, which are fully independent. The two cases
        need opposite treatment, and a key string does not say which org issued
        it, so this escalates on evidence instead of guessing:

        * the reporting key is parked immediately, and
        * once a **second** key independently reports the same daily cap, the
          pool is known to share one org bucket, so every remaining key is
          parked too. That caps the wasted round-trips at one instead of
          burning a 429 per student for the rest of the batch.

        Returns the park duration in seconds.
        """
        if key_index >= 0:
            with self._lock:
                until = time.monotonic() + DAILY_QUOTA_COOLDOWN_SECONDS
                reporter = self._keys[key_index]
                # A daily cap is not a per-minute throttle: clear the strikes so
                # recovery does not compound backoff on top of the park.
                reporter.consecutive_429s = 0
                reporter.daily_until = until
                reporter.cooldown_until = until

                dead = [k for k in self._keys if k.daily_exhausted(time.monotonic())]
                # Two independent reporters = one shared org bucket: sideline the
                # rest rather than paying one 429 per student to rediscover it.
                parked = 1
                if len(dead) >= 2:
                    for key in self._keys:
                        key.consecutive_429s = 0
                        key.daily_until = until
                        key.cooldown_until = until
                    parked = len(self._keys)
                if parked >= len(self._keys) and self.daily_fallback_reason is None:
                    self.daily_fallback_reason = (
                        f"Groq daily token quota reached ({retry_after or 'TPD'})"
                    )
                suffix = reporter.api_key[-4:]
                dead_count = len(dead)
        elif key_index == -1:
            # Legacy single fallback endpoint: nothing to park.
            return 0.0
        else:
            # Pool keys: fallback (-2, -3, ...) or tertiary (-1000, -1001, ...)
            if key_index <= -1000:
                pool = self._tertiary_keys
                p_index = -1000 - key_index
            else:
                pool = self._fallback_keys
                p_index = -2 - key_index

            if 0 <= p_index < len(pool):
                until = time.monotonic() + DAILY_QUOTA_COOLDOWN_SECONDS
                reporter = pool[p_index]
                reporter.consecutive_429s = 0
                reporter.daily_until = until
                reporter.cooldown_until = until
                suffix = reporter.api_key[-4:]
                dead_count = 1
                parked = 1
            else:
                return 0.0
        logger.error(
            "Pool key ...%s hit its DAILY token quota; parked for %.1fh (%d key(s) confirmed dead).",
            suffix,
            DAILY_QUOTA_COOLDOWN_SECONDS / 3600.0,
            dead_count,
        )
        return DAILY_QUOTA_COOLDOWN_SECONDS

    def report_rate_limited(self, key_index: int, retry_after: Optional[float] = None) -> float:
        if key_index >= 0:
            wait = _clamp_retry_after(retry_after, default=self._cooldown)
            with self._lock:
                key = self._keys[key_index]
                key.consecutive_429s += 1
                wait = min(wait * key.consecutive_429s, MAX_RETRY_AFTER_SECONDS)
                key.cooldown_until = time.monotonic() + wait
                suffix = key.api_key[-4:]
                strikes = key.consecutive_429s
            logger.warning("Groq key ...%s rate-limited (strike %d); cooling %.0fs.", suffix, strikes, wait)
            return wait

        if key_index == -1:
            # Legacy single fallback endpoint: just pause.
            pause = _clamp_retry_after(retry_after, default=5.0)
            time.sleep(pause)
            return pause

        # Pool keys: fallback (-2, -3, ...) or tertiary (-1000, -1001, ...)
        if key_index <= -1000:
            pool = self._tertiary_keys
            p_index = -1000 - key_index
        else:
            pool = self._fallback_keys
            p_index = -2 - key_index

        if 0 <= p_index < len(pool):
            wait = _clamp_retry_after(retry_after, default=self._cooldown)
            with self._lock:
                key = pool[p_index]
                key.consecutive_429s += 1
                wait = min(wait * key.consecutive_429s, MAX_RETRY_AFTER_SECONDS)
                key.cooldown_until = time.monotonic() + wait
                suffix = key.api_key[-4:]
                strikes = key.consecutive_429s
            logger.warning(
                "Pool key ...%s rate-limited (strike %d); cooling %.0fs.", suffix, strikes, wait
            )
            return wait

        # Unknown key index: pause as a safe default.
        pause = _clamp_retry_after(retry_after, default=5.0)
        time.sleep(pause)
        return pause

def _fallback_candidates(fallback: ProviderEndpoint) -> list[str]:
    """Ordered model ids to try for a fallback endpoint, configured first.

    The teacher's explicit choice (``LLM_FALLBACK_MODEL``) or the preset
    default always leads, so a deliberate override is honoured. For Gemini
    endpoints the remaining stable ids from :data:`GEMINI_MODEL_CANDIDATES`
    follow newest-first, so a retired id fails over to a live one without any
    config edit. Other providers keep just their single configured model.
    """
    ordered = [fallback.model] if fallback.model else []
    if _guess_provider_name(fallback.base_url) == "gemini":
        ordered.extend(m for m in GEMINI_MODEL_CANDIDATES if m not in ordered)
    return ordered


def resolve_fallback(app_settings: Any) -> tuple:
    """Resolve the fallback provider from settings.

    Returns ``(endpoint_or_None, status)`` where *status* is a short
    human-readable explanation, safe to log and show in the UI. It explains the
    three real cases a teacher hits: fully configured, not configured at all,
    and half-configured (the easy mistake - a model with no key).

    Configuration is a preset name *or* explicit values, so the common path is
    just a provider name plus a key::

        LLM_FALLBACK_PROVIDER=gemini        # fills base_url + default model
        LLM_FALLBACK_API_KEY=...            # the only secret needed

    ``LLM_FALLBACK_BASE_URL`` / ``LLM_FALLBACK_MODEL`` override either field of
    the preset, and any other OpenAI-compatible endpoint works by setting both
    explicitly with no ``LLM_FALLBACK_PROVIDER`` at all.
    """
    provider = str(getattr(app_settings, "llm_fallback_provider", "") or "").strip().lower()
    key = str(getattr(app_settings, "llm_fallback_api_key", "") or "").strip()
    explicit_base = str(getattr(app_settings, "llm_fallback_base_url", "") or "").strip()
    explicit_model = str(getattr(app_settings, "llm_fallback_model", "") or "").strip()
    account_id = str(getattr(app_settings, "cloudflare_account_id", "") or "").strip()

    if provider and provider not in FALLBACK_PROVIDERS:
        known = ", ".join(sorted(FALLBACK_PROVIDERS))
        return None, f"unknown LLM_FALLBACK_PROVIDER '{provider}' (known: {known})"

    preset = FALLBACK_PROVIDERS.get(provider) if provider else None

    # Cloudflare needs the account id substituted into the base URL template.
    if provider == "cloudflare" and not explicit_base:
        base = _cloudflare_base_url(account_id) if account_id else preset.base_url
    else:
        base = explicit_base or (preset.base_url if preset else "")
    model = explicit_model or (preset.default_model if preset else "")
    name = preset.name if preset else (_guess_provider_name(base) if base else "fallback")

    # Report the missing piece by name: "no API key" is actionable, "not
    # configured" is not, and the difference is the whole point of the message.
    if not base and not model:
        if key:
            return None, "API key set but no model/base_url (set LLM_FALLBACK_PROVIDER or LLM_FALLBACK_BASE_URL + LLM_FALLBACK_MODEL)"
        return None, "no fallback configured (set LLM_FALLBACK_PROVIDER + LLM_FALLBACK_API_KEY)"
    if not key:
        return None, f"{name} model '{model}' has no LLM_FALLBACK_API_KEY"
    if not model:
        return None, f"{name} base_url set but no LLM_FALLBACK_MODEL"
    if not base:
        return None, f"model '{model}' has no base_url (set LLM_FALLBACK_PROVIDER or LLM_FALLBACK_BASE_URL)"

    endpoint = ProviderEndpoint(
        name=name,
        base_url=base,
        model=model,
        api_key=key,
        is_fallback=True,
    )
    return endpoint, f"{name}: {model}"


def _resolve_tertiary(app_settings: Any) -> tuple:
    """Resolve the tertiary provider from settings.

    Same preset mechanism as ``resolve_fallback``, but for the tertiary pool.
    Cloudflare is special-cased: its base URL template needs ``CLOUDFLARE_ACCOUNT_ID``
    substituted into ``.../accounts/{account_id}/ai/v1``.
    """
    provider = str(getattr(app_settings, "llm_tertiary_provider", "") or "").strip().lower()
    key = str(getattr(app_settings, "llm_tertiary_api_key", "") or "").strip()
    explicit_base = str(getattr(app_settings, "llm_tertiary_base_url", "") or "").strip()
    explicit_model = str(getattr(app_settings, "llm_tertiary_model", "") or "").strip()
    account_id = str(getattr(app_settings, "cloudflare_account_id", "") or "").strip()

    if provider and provider not in FALLBACK_PROVIDERS:
        known = ", ".join(sorted(FALLBACK_PROVIDERS))
        return None, f"unknown LLM_TERTIARY_PROVIDER '{provider}' (known: {known})"

    preset = FALLBACK_PROVIDERS.get(provider) if provider else None

    # Cloudflare needs the account id substituted into the base URL template.
    if provider == "cloudflare" and not explicit_base:
        base = _cloudflare_base_url(account_id) if account_id else preset.base_url
    else:
        base = explicit_base or (preset.base_url if preset else "")

    model = explicit_model or (preset.default_model if preset else "")
    name = preset.name if preset else (_guess_provider_name(base) if base else "tertiary")

    if not base and not model:
        if key:
            return None, "API key set but no model/base_url (set LLM_TERTIARY_PROVIDER or LLM_TERTIARY_BASE_URL + LLM_TERTIARY_MODEL)"
        return None, "no tertiary configured (set LLM_TERTIARY_PROVIDER + LLM_TERTIARY_API_KEY)"
    if not key:
        return None, f"{name} model '{model}' has no LLM_TERTIARY_API_KEY"
    if not model:
        return None, f"{name} base_url set but no LLM_TERTIARY_MODEL"
    if not base:
        return None, f"model '{model}' has no base_url (set LLM_TERTIARY_PROVIDER or LLM_TERTIARY_BASE_URL)"

    endpoint = ProviderEndpoint(
        name=name,
        base_url=base,
        model=model,
        api_key=key,
        is_fallback=True,
    )
    return endpoint, f"{name}: {model}"


def build_router_from_settings(app_settings: Any) -> Optional["LLMRouter"]:
    """Build the router from settings, or None when no Groq key exists.

    A missing or half-configured fallback is never fatal: the batch still runs
    on Groq and simply fails fast (instead of hanging) if the daily quota dies.
    """
    keys = split_keys(
        getattr(app_settings, "groq_api_key", None),
        getattr(app_settings, "groq_api_keys", None),
    )
    if not keys:
        return None

    fallback, status = resolve_fallback(app_settings)
    if fallback is None:
        # Expected on a plain install, so it is informational, not a warning.
        logger.info("Fallback provider disabled: %s.", status)
    else:
        logger.info("Fallback provider ready: %s.", status)

    tertiary, tertiary_status = _resolve_tertiary(app_settings)
    if tertiary is None:
        logger.info("Tertiary provider disabled: %s.", tertiary_status)
    else:
        logger.info("Tertiary provider ready: %s.", tertiary_status)

    # Fallback key pool: primary key + any extra comma-separated keys.
    fallback_key = str(getattr(app_settings, "llm_fallback_api_key", "") or "").strip()
    fallback_keys = split_keys(
        fallback_key or None,
        getattr(app_settings, "llm_fallback_api_keys", None),
    )

    # Tertiary key pool: just the single tertiary API key (no extras setting).
    tertiary_key = str(getattr(app_settings, "llm_tertiary_api_key", "") or "").strip()
    tertiary_keys = [tertiary_key] if tertiary_key else []

    return LLMRouter(
        groq_keys=keys,
        groq_model=getattr(app_settings, "groq_model", "qwen/qwen3.8-27b"),
        groq_base_url=getattr(
            app_settings, "groq_base_url", "https://api.groq.com/openai/v1"
        ),
        fallback=fallback,
        fallback_keys=fallback_keys,
        tertiary=tertiary,
        tertiary_keys=tertiary_keys,
    )


def router_fallback_status(router: Optional["LLMRouter"], app_settings: Any = None) -> str:
    """Why the fallback is (un)available, for the UI status line.

    Accepts a live router *or* just settings, so the Streamlit app can show the
    real reason before any grading has run.
    """
    if router is not None:
        if router.has_fallback and router._fallback is not None:
            base = f"fallback ready: {router._fallback.name}: {router._fallback.model}"
            if router.used_fallback:
                return f"{base} (in use: {router.fallback_uses} request(s))"
            return base
        if app_settings is None:
            return "no fallback configured (set LLM_FALLBACK_PROVIDER + LLM_FALLBACK_API_KEY)"
    if app_settings is None:
        return "fallback status unavailable"
    _, status = resolve_fallback(app_settings)
    return f"fallback unavailable: {status}"



def ticked_sleep(
    seconds: float,
    on_tick: Optional[Any] = None,
    reason: str = "pacing",
) -> None:
    """Sleep for ``seconds``, narrating the wait through ``on_tick`` when asked.

    ``on_tick(remaining, total, reason)`` fires before every slice and once more
    with ``remaining == 0`` at the end. The wait is sliced into
    ``PAUSE_TICK_SECONDS`` chunks only when a callback is present; without one
    this is a single ``time.sleep`` exactly as before, so the CLI and every
    existing test keep their original timing.

    The callback exists because a screen that receives no update for 30 seconds
    is indistinguishable from a crash: on a stretched OTPM backoff that silence
    is what makes a batch look frozen. A callback that raises is dropped rather
    than propagated - a broken widget must never abort a grading run.

    The tick count is computed up front rather than looped off a wall-clock
    deadline, so the wait always terminates even if the clock does not advance
    (tests replace ``time.sleep`` with a recorder).
    """
    total = max(float(seconds), 0.0)
    if total <= 0:
        return
    if on_tick is None:
        time.sleep(total)
        return
    started = time.monotonic()
    slices = max(1, math.ceil(total / PAUSE_TICK_SECONDS))
    for step in range(slices):
        elapsed = time.monotonic() - started
        remaining = max(0.0, total - elapsed)
        try:
            on_tick(remaining, total, reason)
        except Exception:
            logger.debug("pause tick callback failed", exc_info=True)
        if remaining <= 0:
            break
        time.sleep(min(PAUSE_TICK_SECONDS, remaining))
    try:
        on_tick(0.0, total, reason)
    except Exception:
        logger.debug("pause tick callback failed", exc_info=True)


@dataclass
class AdaptivePacer:
    """Self-throttling pause between students, driven by observed 429s."""

    base_seconds: float = 2.0
    max_seconds: float = 30.0
    backoff_factor: float = 1.6
    recover_after: int = 8
    _current: float = field(default=2.0, init=False, repr=False)
    _streak: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._current = max(self.base_seconds, 0.0)

    @property
    def current(self) -> float:
        return self._current

    def pause(self, on_tick: Optional[Any] = None) -> None:
        """Sleep the current delay, optionally narrating it through ``on_tick``.

        See :func:`ticked_sleep`: with no callback the delay is one silent
        ``time.sleep``; with one it is sliced into ticks carrying
        ``(remaining, total, reason)`` so a UI can count the wait down instead
        of sitting still. ``reason`` is ``"rate-limit backoff"`` once the pacer
        has stretched past its base delay - i.e. the batch just hit a 429 - so
        the screen can say *why* it is waiting, not merely that it is.
        """
        total = float(self._current)
        if total <= 0:
            return
        reason = "rate-limit backoff" if total > self.base_seconds else "pacing"
        ticked_sleep(total, on_tick, reason)

    def report_success(self) -> None:
        self._streak += 1
        if self._streak >= self.recover_after and self._current > self.base_seconds:
            self._current = max(self.base_seconds, self._current / self.backoff_factor)
            self._streak = 0

    def report_rate_limited(self) -> None:
        self._streak = 0
        self._current = min(self.max_seconds, max(self._current * self.backoff_factor, 1.0))

