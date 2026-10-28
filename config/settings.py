"""Application settings loaded from the environment and an optional ``.env`` file.

Only libraries pinned in ``requirements.txt`` are used: ``python-dotenv`` for
env loading and ``pydantic`` for validation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Repository root (parent of ``config/``).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env from the project root if present (no-op otherwise).
load_dotenv(PROJECT_ROOT / ".env")

# Default Groq model for evaluations. Switch models anytime by setting
# GROQ_MODEL in .env - no code changes required. Notes: qwen/qwen3.8-27b is
# a 27B dense model with JSON-Schema Mode support, a 131K context window and
# a 16K max output; Groq's free tier allows 30 RPM / 8K TPM for it, which the
# evaluator's retry logic absorbs.
DEFAULT_GROQ_MODEL = "qwen/qwen3.8-27b"

# Groq's OpenAI-compatible endpoint. The official ``openai`` SDK is pointed at
# this base URL, so the migration needs no Groq-specific client library.
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Points taken off a submission handed in after the deadline. Named so the
# field default, the environment fallback and the bounds below cannot drift
# apart; ``tests/test_cloud_secrets.py`` pins it against
# ``src.late_policy.DEFAULT_LATE_PENALTY_POINTS`` (that module cannot be
# imported from here - it imports this one - so the link is test-enforced).
DEFAULT_LATE_PENALTY_POINTS = 10

# A penalty is meaningless outside 0-100: 0 switches the rule off, and above
# 100 it could never be charged in full anyway.
MIN_LATE_PENALTY_POINTS = 0
MAX_LATE_PENALTY_POINTS = 100


def _secret(name: str) -> str | None:
    """Return a configuration value from the environment or Streamlit secrets.

    Streamlit Community Cloud injects configuration through ``st.secrets``
    rather than the process environment, while local runs use ``.env`` /
    ``os.environ``. Both are consulted here, environment first so a local
    ``.env`` always overrides a stale secret.

    Missing secrets are a normal state rather than an error: every CLI entry
    point (``src/main.py``, ``src/fetch_submissions.py``, ...) runs without a
    Streamlit runtime, and ``st.secrets`` raises ``StreamlitSecretNotFoundError``
    when no ``secrets.toml`` exists. That exception class is not stable across
    Streamlit versions, so the lookup is wrapped defensively instead.
    """
    raw = os.getenv(name)
    if raw is not None and raw.strip():
        return raw.strip()
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except Exception:  # noqa: BLE001 - absent secrets must never break a run
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    # ``st.secrets`` hands back whatever type secrets.toml declared, so an
    # unquoted ``LATE_PENALTY_POINTS = 15`` arrives as an int and is just as
    # valid as the quoted form. Reading strings only silently discarded those
    # and fell back to the default - which quietly turned a configured
    # ``= 0`` (meant to switch the penalty off) back into a 10-point charge.
    if isinstance(value, bool):
        # TOML booleans back the on/off flags; parsed case-insensitively.
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean setting robustly (environment or Streamlit secrets)."""
    raw = _secret(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(
    name: str, default: int, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Parse a bounded integer setting, ignoring and clamping unusable values.

    A typo in ``.env`` must not take a grading run down mid-way, so anything
    that is not an integer falls back to the default, and a number outside the
    accepted range is clamped to it.

    The clamping has to happen *here* rather than being left to the model. The
    ``ge``/``le`` bounds on :class:`Settings` make pydantic **reject** an
    out-of-range value, and :func:`load_settings` runs at import time - so an
    unclamped ``LATE_PENALTY_POINTS=150`` used to raise a ``ValidationError``
    that killed the whole app on startup, instead of degrading to a usable
    number. A misconfigured penalty can now only make itself smaller or equal
    to the whole grade, exactly as :func:`src.late_policy.penalty_for` promises.
    """
    raw = _secret(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


class Settings(BaseModel):
    """Runtime configuration for the Classroom AutoGrader."""

    log_level: str = Field(default="INFO", description="Python logging level.")
    dry_run: bool = Field(
        default=False,
        description="When true, grading runs without writing anything back.",
    )
    groq_api_key: Optional[str] = Field(
        default=None,
        description="Groq API key used for LLM evaluations.",
    )
    groq_api_keys: Optional[str] = Field(
        default=None,
        description=(
            "Optional comma-separated extra Groq keys (GROQ_API_KEYS). All keys "
            "share one token bucket via rotation: when one hits 429 the next "
            "key serves the retry, multiplying the free-tier budget."
        ),
    )
    llm_fallback_provider: str = Field(
        default="",
        description=(
            "Named fallback provider (LLM_FALLBACK_PROVIDER): 'gemini' or "
            "'openrouter'. Selecting one fills in a correct base URL and a sane "
            "default model, so only LLM_FALLBACK_API_KEY is needed. Leave empty "
            "to configure base_url/model explicitly instead."
        ),
    )
    llm_fallback_model: str = Field(
        default="",
        description=(
            "Fallback model id (LLM_FALLBACK_MODEL, e.g. a Gemini or OpenRouter "
            "model). Overrides the named provider's default; used only after every "
            "Groq key is exhausted; empty disables."
        ),
    )
    llm_fallback_api_key: Optional[str] = Field(
        default=None,
        description="API key for the fallback provider (LLM_FALLBACK_API_KEY).",
    )
    llm_fallback_base_url: str = Field(
        default="",
        description=(
            "OpenAI-compatible base URL of the fallback provider "
            "(LLM_FALLBACK_BASE_URL, e.g. https://openrouter.ai/api/v1 or "
            "https://generativelanguage.googleapis.com/v1beta/openai/)."
        ),
    )
    llm_fallback_api_keys: Optional[str] = Field(
        default=None,
        description=(
            "Extra fallback keys, comma-separated (LLM_FALLBACK_API_KEYS). "
            "Pooled with LLM_FALLBACK_API_KEY and rotated on 429, exactly "
            "like GROQ_API_KEYS. Use it for a second Gemini project key so "
            "one exhausted project does not kill the fallback."
        ),
    )
    llm_tertiary_provider: str = Field(
        default="",
        description=(
            "Tertiary provider used only when primary AND fallback pools are "
            "all exhausted (LLM_TERTIARY_PROVIDER, e.g. 'cloudflare'). Same "
            "preset mechanism as LLM_FALLBACK_PROVIDER."
        ),
    )
    llm_tertiary_model: str = Field(
        default="",
        description="Model id for the tertiary provider (LLM_TERTIARY_MODEL).",
    )
    llm_tertiary_api_key: Optional[str] = Field(
        default=None,
        description="API key/token for the tertiary provider (LLM_TERTIARY_API_KEY).",
    )
    llm_tertiary_base_url: str = Field(
        default="",
        description=(
            "Explicit base URL for the tertiary provider "
            "(LLM_TERTIARY_BASE_URL). Required for providers whose URL "
            "contains an account id (Cloudflare)."
        ),
    )
    cloudflare_account_id: str = Field(
        default="",
        description=(
            "Cloudflare account id (CLOUDFLARE_ACCOUNT_ID). Fills the "
            "https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1 "
            "base URL when the fallback/tertiary provider is 'cloudflare'."
        ),
    )
    llm_audit_enabled: bool = Field(
        default=True,
        description=(
            "Run the deterministic grade-audit pass after each evaluation "
            "(LLM_AUDIT_ENABLED). Zero extra tokens: pure local math/logic "
            "checks on score vs deductions, statuses, exempt tasks, late rule."
        ),
    )
    llm_audit_autofix: bool = Field(
        default=True,
        description=(
            "Let the audit pass repair a provably-wrong score (deductions sum "
            "mismatch) instead of only flagging it (LLM_AUDIT_AUTOFIX)."
        ),
    )
    groq_model: str = Field(
        default=DEFAULT_GROQ_MODEL,
        description="Groq model id used for LLM evaluations.",
    )
    groq_base_url: str = Field(
        default=DEFAULT_GROQ_BASE_URL,
        description="Base URL of Groq's OpenAI-compatible API.",
    )
    late_penalty_points: int = Field(
        default=DEFAULT_LATE_PENALTY_POINTS,
        ge=MIN_LATE_PENALTY_POINTS,
        le=MAX_LATE_PENALTY_POINTS,
        description=(
            "Points deducted from the score when a submission arrived after "
            "the assignment deadline. Unset, empty or unusable values fall "
            f"back to {DEFAULT_LATE_PENALTY_POINTS}. Set "
            "LATE_PENALTY_POINTS=0 to switch the penalty off (late work is "
            "then graded exactly like on-time work)."
        ),
    )
    dashboard_auto_dry_run: bool = Field(
        default=False,
        description=(
            "When true, the Dashboard runs its automated dry-run evaluation "
            "automatically whenever its pending data is refreshed, instead of "
            "waiting for the button. Off by default: a dry-run still costs "
            "Groq tokens, and starting one should stay a deliberate choice. "
            "It never writes to Google Classroom either way."
        ),
    )
    dashboard_ignored_submission_ids: str = Field(
        default="",
        description=(
            "Comma-separated list of Classroom submission ids that the "
            "Dashboard must never show as pending "
            "(DASHBOARD_IGNORED_SUBMISSION_IDS). Use it to hide a submission "
            "that is stuck in TURNED_IN and cannot be returned - it stays in "
            "Google Classroom but disappears from the dashboard's \"awaiting "
            "review\" groups, the pending counters and the automated dry-run."
        ),
    )

    @property
    def ignored_submission_ids(self) -> frozenset[str]:
        """Submission ids the Dashboard must hide from its pending groups.

        Parsed from ``dashboard_ignored_submission_ids`` (a comma-separated
        list). Empty when nothing is configured.
        """
        return frozenset(
            sid.strip()
            for sid in (self.dashboard_ignored_submission_ids or "").split(",")
            if sid.strip()
        )


def load_settings() -> Settings:
    """Build :class:`Settings` from the current environment."""
    return Settings(
        log_level=(_secret("LOG_LEVEL") or "INFO").upper(),
        dry_run=_env_bool("DRY_RUN", default=False),
        groq_api_key=_secret("GROQ_API_KEY"),
        groq_api_keys=_secret("GROQ_API_KEYS"),
        groq_model=_secret("GROQ_MODEL") or DEFAULT_GROQ_MODEL,
        groq_base_url=_secret("GROQ_BASE_URL") or DEFAULT_GROQ_BASE_URL,
        llm_fallback_provider=(
            _secret("LLM_FALLBACK_PROVIDER") or ""
        ).strip().lower(),
        llm_fallback_model=(_secret("LLM_FALLBACK_MODEL") or "").strip(),
        llm_fallback_api_key=_secret("LLM_FALLBACK_API_KEY"),
        llm_fallback_base_url=(_secret("LLM_FALLBACK_BASE_URL") or "").strip(),
        llm_fallback_api_keys=_secret("LLM_FALLBACK_API_KEYS"),
        llm_tertiary_provider=(
            _secret("LLM_TERTIARY_PROVIDER") or ""
        ).strip().lower(),
        llm_tertiary_model=(_secret("LLM_TERTIARY_MODEL") or "").strip(),
        llm_tertiary_api_key=_secret("LLM_TERTIARY_API_KEY"),
        llm_tertiary_base_url=(_secret("LLM_TERTIARY_BASE_URL") or "").strip(),
        cloudflare_account_id=(_secret("CLOUDFLARE_ACCOUNT_ID") or "").strip(),
        late_penalty_points=_env_int(
            "LATE_PENALTY_POINTS",
            default=DEFAULT_LATE_PENALTY_POINTS,
            minimum=MIN_LATE_PENALTY_POINTS,
            maximum=MAX_LATE_PENALTY_POINTS,
        ),
        dashboard_auto_dry_run=_env_bool(
            "DASHBOARD_AUTO_DRY_RUN", default=False
        ),
        dashboard_ignored_submission_ids=_secret(
            "DASHBOARD_IGNORED_SUBMISSION_IDS"
        )
        or "",
        llm_audit_enabled=_env_bool("LLM_AUDIT_ENABLED", default=True),
        llm_audit_autofix=_env_bool("LLM_AUDIT_AUTOFIX", default=True),
    )


# Convenience singleton for application-wide use.
settings = load_settings()
