"""Non-interactive Google credentials for the ingestion pipeline.

The existing application owns ``token.json`` at the project root. The portal
treats that file as **read-only** and keeps its own refreshed copy under
``portal/data/google_token.json`` (git-ignored), so a portal refresh can never
overwrite or widen the grant the Streamlit app is using.

A FastAPI request must never start an OAuth browser flow: it would block the
worker and open a browser on the server. Therefore :func:`load_credentials`
only loads/refreshes. ``interactive=True`` is reserved for an explicit CLI
command run by a human at the machine.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from portal.paths import DATA_DIR

logger = logging.getLogger(__name__)

PORTAL_TOKEN_FILE: Path = DATA_DIR / "google_token.json"

# Classroom keeps two content areas: Coursework (assignments/questions) and
# Materials (the "Materials" tab). Reading the second one needs its own
# read-only scope; the coursework scopes the grading engine already has do
# not cover it. Adding this scope means the portal's token must be granted
# once through ``classroom_import auth``.
MATERIALS_SCOPE = (
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly"
)


class IngestionAuthError(RuntimeError):
    """Google access is missing, expired, or lacks a required scope."""


def _candidate_tokens() -> list[Path]:
    """Portal token first, then the existing application's token."""
    from src.auth import TOKEN_FILE as src_token_file

    return [PORTAL_TOKEN_FILE, Path(src_token_file)]


def _read_token(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _granted_scopes(payload: dict) -> set[str]:
    raw = payload.get("scopes") or payload.get("scope") or []
    if isinstance(raw, str):
        raw = raw.split()
    return {str(scope) for scope in raw}


def _save_portal_token(credentials: Credentials) -> None:
    """Atomically persist a refreshed token to the portal's private copy."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=str(DATA_DIR), prefix=".google_token_", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(credentials.to_json())
            try:
                os.chmod(temporary, 0o600)
            except OSError:  # Windows has no POSIX modes.
                pass
            os.replace(temporary, PORTAL_TOKEN_FILE)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
    except OSError as exc:
        # The in-memory credential is still valid for this process; losing the
        # refreshed copy only means the next start refreshes again.
        logger.warning("Could not persist the portal Google token: %s", exc)


def _required_scopes() -> tuple[str, ...]:
    from src.auth import SCOPES

    # ``src/`` is read-only for this project, so the extra Materials scope is
    # added here instead of changing the grading engine's grant list.
    return tuple(dict.fromkeys((*SCOPES, MATERIALS_SCOPE)))


def load_credentials(
    *,
    interactive: bool = False,
    required_scopes: tuple[str, ...] | None = None,
) -> Credentials:
    """Return usable credentials without ever blocking on a browser.

    Raises :class:`IngestionAuthError` with an actionable Hebrew/English
    message when the token is absent, expired beyond refresh, or missing a
    scope. The CLI may pass ``interactive=True`` for a one-time consent run.
    """
    required = tuple(required_scopes or _required_scopes())

    payload: dict | None = None
    for path in _candidate_tokens():
        payload = _read_token(path)
        if payload is not None:
            break

    if payload is not None:
        granted = _granted_scopes(payload)
        missing = sorted(set(required) - granted) if granted else []
        if missing:
            # A refresh token can never widen its grant, so only a fresh
            # consent can add the new scope. The explicit CLI auth command
            # (interactive=True) is allowed to run that consent.
            if interactive:
                return _run_consent(required)
            raise IngestionAuthError(
                "אסימון Google הקיים אינו כולל את ההרשאות הנדרשות לייבוא. "
                "הריצו מחדש את תהליך ההרשאה מהמחשב: "
                "python -m portal.scripts.classroom_import auth. "
                f"(missing: {', '.join(missing)})"
            )

        credentials = Credentials.from_authorized_user_info(
            payload, scopes=list(granted or required)
        )
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except (RefreshError, TransportError) as exc:
                if interactive:
                    # A dead grant (expired Testing-mode token, revoked access)
                    # can only be replaced by a fresh consent. Without this
                    # branch the auth CLI would tell the teacher to run the
                    # very command that just failed.
                    return _run_consent(required)
                raise IngestionAuthError(
                    "פג תוקף אסימון Google ולא ניתן לחדש אותו. הריצו מחדש את "
                    "תהליך ההרשאה: python -m portal.scripts.classroom_import auth."
                ) from exc
            else:
                _save_portal_token(credentials)
        if credentials.valid:
            return credentials

    if interactive:
        return _run_consent(required)

    raise IngestionAuthError(
        "לא נמצא אסימון Google תקין לייבוא. הריצו פעם אחת מהמחשב: "
        "python -m portal.scripts.classroom_import auth "
        "(או העתיקו token.json קיים לתיקיית הפרויקט)."
    )


def _run_consent(required: tuple[str, ...]) -> Credentials:
    """Interactive consent - only reachable from the CLI, never a request."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    from src.auth import CREDENTIALS_FILE

    if not Path(CREDENTIALS_FILE).is_file():
        raise IngestionAuthError(
            f"OAuth client secrets not found at '{CREDENTIALS_FILE}'. "
            "Place credentials.json at the project root first."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE), list(required)
    )
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    _save_portal_token(credentials)
    return credentials


def auth_status() -> tuple[bool, str]:
    """Cheap status for the teacher import page (never starts a flow)."""
    try:
        load_credentials()
    except IngestionAuthError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - shown as a status, not a crash
        logger.warning("Google auth status check failed: %s", exc)
        return False, "בדיקת הרשאות Google נכשלה. נסו שוב מאוחר יותר."
    return True, "הרשאות Google תקינות."
