"""Portal runtime configuration.

Deliberately separate from ``config/settings.py``: the portal has its own
secret, database URL and cookie policy, and must be deployable on a different
port without editing the existing system. Grading thresholds (for example the
late penalty) are **not** duplicated here - the adapter reads them from the
existing ``config.settings`` so both systems can never charge different
amounts.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from portal.paths import DATA_DIR


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _database_url() -> str:
    explicit = (os.getenv("PORTAL_DATABASE_URL") or "").strip()
    if explicit:
        return explicit
    # Absolute path, POSIX separators: SQLAlchemy accepts this on Windows and
    # on POSIX, and it keeps the database inside portal/ (never the repo root).
    return f"sqlite:///{(DATA_DIR / 'portal.db').as_posix()}"


def _persisted_secret() -> str:
    """Return the session secret, generating it once on first boot.

    A hardcoded default would make every school deployment share a signing
    key. The generated file lives under ``portal/data/`` (git-ignored) and is
    created with owner-only permissions where the OS supports it. Production
    deployments may set ``PORTAL_SECRET_KEY`` instead.
    """
    env_value = (os.getenv("PORTAL_SECRET_KEY") or "").strip()
    if env_value:
        return env_value

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_path = DATA_DIR / "secret_key"
    if key_path.is_file():
        stored = key_path.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    generated = secrets.token_urlsafe(48)
    key_path.write_text(generated, encoding="utf-8")
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        # Windows ACLs are not POSIX modes; the file is still inside the
        # application directory and never committed.
        pass
    return generated


@dataclass(frozen=True)
class PortalSettings:
    """Everything the FastAPI app needs to boot."""

    database_url: str
    secret_key: str
    app_name: str = "פורטל הכיתה"
    session_cookie: str = "portal_session"
    session_max_age: int = 14 * 24 * 60 * 60  # 14 days
    https_only: bool = False
    timezone: str = "Asia/Jerusalem"
    environment: str = "development"
    # Automatic model pass: the app re-marks a few hand-ins that still need the
    # model - exams and quizzes first, then failed regular assignments - once
    # every 3 hours, in the background.
    exam_auto_check_enabled: bool = True
    exam_auto_check_interval_seconds: int = 3 * 60 * 60
    exam_auto_check_batch_size: int = 5

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production"}


def load_settings() -> PortalSettings:
    """Build settings from ``PORTAL_*`` environment variables."""
    environment = (os.getenv("PORTAL_ENV") or "development").strip().lower()
    https_only = _env_bool(
        "PORTAL_HTTPS_ONLY", default=environment in {"prod", "production"}
    )
    try:
        max_age = int(os.getenv("PORTAL_SESSION_MAX_AGE", str(14 * 24 * 3600)))
    except ValueError:
        max_age = 14 * 24 * 3600
    return PortalSettings(
        database_url=_database_url(),
        secret_key=_persisted_secret(),
        app_name=(os.getenv("PORTAL_APP_NAME") or "פורטל הכיתה").strip(),
        session_cookie="portal_session",
        session_max_age=max_age,
        https_only=https_only,
        timezone=(os.getenv("PORTAL_TIMEZONE") or "Asia/Jerusalem").strip(),
        environment=environment,
        exam_auto_check_enabled=_env_bool("PORTAL_EXAM_AUTO_CHECK", True),
        exam_auto_check_interval_seconds=_env_int(
            "PORTAL_EXAM_AUTO_CHECK_INTERVAL", 3 * 60 * 60, 60, 24 * 60 * 60
        ),
        exam_auto_check_batch_size=_env_int(
            "PORTAL_EXAM_AUTO_CHECK_BATCH", 5, 1, 10
        ),
    )
