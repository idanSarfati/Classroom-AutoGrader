"""Google OAuth2 helper for the Classroom AutoGrader.

Provides :func:`get_google_credentials`, which returns valid Google API
credentials by loading/refreshing ``token.json`` or starting the local-server
login flow using ``credentials.json`` (both files live at the project root).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Repository root — ``credentials.json`` and ``token.json`` live here.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    # Read access to Docs, used to extract the submitted text and to place
    # the feedback comment's anchor on the student's last line.
    "https://www.googleapis.com/auth/documents.readonly",
    # Full drive scope: required to CREATE comments on student documents
    # (comments.create is not covered by drive.readonly), and to update or
    # delete our own previous comment so re-publishing stays idempotent.
    "https://www.googleapis.com/auth/drive",
)

CREDENTIALS_FILE: Path = PROJECT_ROOT / "credentials.json"
TOKEN_FILE: Path = PROJECT_ROOT / "token.json"

logger = logging.getLogger(__name__)


def _save_token(creds: Credentials) -> None:
    """Atomically persist the authorized user token to ``token.json``.

    Writes to a temporary file in the same directory and swaps it into place
    with :func:`os.replace`, so an interrupted write cannot corrupt an
    existing valid token.
    """
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(TOKEN_FILE.parent), prefix=".token_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
        os.replace(tmp_path, TOKEN_FILE)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _granted_scopes(token_info: dict) -> set[str]:
    """OAuth scopes actually granted when token.json was issued."""
    raw = token_info.get("scopes") or token_info.get("scope") or []
    if isinstance(raw, str):
        raw = raw.split()
    return {str(scope) for scope in raw}


def _has_required_scopes(token_info: dict) -> bool:
    """True when the issued grant already covers every scope we request.

    Refreshing a token can never widen its granted scopes, so when SCOPES
    changes (e.g. adding ``drive`` to post comments) the saved token must be
    replaced through a new consent flow rather than silently refreshed.
    """
    granted = _granted_scopes(token_info)
    if not granted:
        return False  # Unknown grant - re-authorize to be safe.
    return set(SCOPES) <= granted


def get_google_credentials() -> Credentials:
    """Return a valid Google OAuth2 :class:`Credentials` object.

    Behavior:

    * Raises :class:`FileNotFoundError` if ``credentials.json`` is missing
      at the project root.
    * Loads ``token.json`` when present; if the access token is expired and a
      refresh token exists, refreshes it (falling back to a full login if the
      refresh fails, e.g. the grant was revoked).
    * If ``token.json`` is missing or unusable, starts the local-server
      OAuth flow via :class:`InstalledAppFlow`.
    * Saves refreshed or newly obtained tokens safely back to ``token.json``.
    """
    if not CREDENTIALS_FILE.is_file():
        raise FileNotFoundError(
            f"OAuth client secrets not found at '{CREDENTIALS_FILE}'. "
            "Download credentials.json (OAuth client ID, Desktop app) from "
            "the Google Cloud Console and place it at the project root."
        )

    creds: Credentials | None = None
    token_dirty = False

    # 1. Load an existing token, if any.
    if TOKEN_FILE.is_file():
        try:
            token_data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        except ValueError:
            # Corrupt or otherwise unusable token file — start fresh below.
            creds = None
        else:
            if creds is not None and not _has_required_scopes(token_data):
                # A refresh can never expand granted scopes, so a scope
                # change (e.g. drive.readonly -> drive for comments) requires
                # a fresh consent flow instead of a silent refresh.
                missing = sorted(set(SCOPES) - _granted_scopes(token_data))
                logger.warning(
                    "token.json is missing newly required OAuth scope(s): %s "
                    "- starting re-authorization.",
                    ", ".join(missing),
                )
                creds = None

    # 2. Refresh an expired token when possible.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_dirty = True
        except (RefreshError, TransportError):
            # Revoked grant or transient network failure — re-run the flow.
            creds = None

    # 3. No usable credentials -> interactive local-server login.
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )
        # Bind explicitly to IPv4 loopback: on Windows "localhost" can
        # resolve to ::1 (IPv6) while the server listens on IPv4, which the
        # browser reports as ERR_CONNECTION_REFUSED. Google allows plain-HTTP
        # loopback redirect URIs (127.0.0.1) for desktop apps.
        creds = flow.run_local_server(
            host="127.0.0.1", port=0, open_browser=True
        )
        token_dirty = True

    # 4. Persist refreshed/new credentials atomically.
    if token_dirty:
        _save_token(creds)

    return creds


if __name__ == "__main__":
    try:
        get_google_credentials()
    except Exception as exc:  # noqa: BLE001 - CLI entry point reports and exits
        print(f"Authentication failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Authentication successful!")
