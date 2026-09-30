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
    return None


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean setting robustly (environment or Streamlit secrets)."""
    raw = _secret(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Parse an integer setting, ignoring unusable values.

    A typo in ``.env`` must not take a grading run down mid-way, so anything
    that is not an integer falls back to the default (the model's own bounds
    clamp the result afterwards).
    """
    raw = _secret(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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
    groq_model: str = Field(
        default=DEFAULT_GROQ_MODEL,
        description="Groq model id used for LLM evaluations.",
    )
    groq_base_url: str = Field(
        default=DEFAULT_GROQ_BASE_URL,
        description="Base URL of Groq's OpenAI-compatible API.",
    )
    late_penalty_points: int = Field(
        default=10,
        ge=0,
        le=100,
        description=(
            "Points deducted from the score when a submission arrived after "
            "the assignment deadline. Set LATE_PENALTY_POINTS=0 to switch the "
            "penalty off (late work is then graded exactly like on-time work)."
        ),
    )


def load_settings() -> Settings:
    """Build :class:`Settings` from the current environment."""
    return Settings(
        log_level=(_secret("LOG_LEVEL") or "INFO").upper(),
        dry_run=_env_bool("DRY_RUN", default=False),
        groq_api_key=_secret("GROQ_API_KEY"),
        groq_model=_secret("GROQ_MODEL") or DEFAULT_GROQ_MODEL,
        groq_base_url=_secret("GROQ_BASE_URL") or DEFAULT_GROQ_BASE_URL,
        late_penalty_points=_env_int("LATE_PENALTY_POINTS", default=10),
    )


# Convenience singleton for application-wide use.
settings = load_settings()
