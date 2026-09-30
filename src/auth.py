"""Google OAuth2 helper for the Classroom AutoGrader.

Provides :func:`get_google_credentials`, which returns valid Google API
credentials by loading/refreshing ``token.json`` or starting the local-server
login flow using ``credentials.json`` (both files live at the project root).

Cloud deployments have no persistent filesystem and cannot run the interactive
browser flow, so both files may instead arrive as JSON in Streamlit's secret
store (:data:`CREDENTIALS_SECRET` / :data:`TOKEN_SECRET`). They are then
materialized to disk on demand — ``google_auth_oauthlib`` can only read a
client-secrets *file* — and a usable token is preferred over any flow.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Repository root — ``credentials.json`` and ``token.json`` live here.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Keys read from Streamlit's secret store when the corresponding file is
# absent. Each holds the full JSON document as a string.
CREDENTIALS_SECRET = "GOOGLE_CREDENTIALS"
TOKEN_SECRET = "GOOGLE_TOKEN"

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


def _secret_json(name: str) -> dict[str, Any] | None:
    """Return the JSON object stored in the Streamlit secret ``name``.

    Returns ``None`` when the secret is absent, blank, or not valid JSON — a
    misconfigured secret must surface as a clear "not configured" error rather
    than an opaque ``JSONDecodeError`` deep inside the OAuth flow.

    Secrets may be supplied either as a JSON *string* (the usual TOML form) or
    as a TOML table, so both are accepted.
    """
    try:
        import streamlit as st

        raw = st.secrets.get(name)
    except Exception:  # noqa: BLE001 - no Streamlit runtime / no secrets.toml
        return None

    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.error(
            "Streamlit secret %s is not valid JSON - copy the file contents "
            "verbatim into the secret.",
            name,
        )
        return None
    if not isinstance(parsed, dict):
        logger.error("Streamlit secret %s must contain a JSON object.", name)
        return None
    return parsed


def _materialize(target: Path, payload: dict[str, Any]) -> Path:
    """Write ``payload`` to ``target`` atomically and return the path written.

    ``google_auth_oauthlib`` reads the OAuth client from a file path, so on a
    cloud host the secret has to exist on disk first. The write is atomic
    (temp file + :func:`os.replace`) to match :func:`_save_token`, and the
    resulting file is owner-only on POSIX so the client secret is not
    world-readable.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:  # pragma: no cover - Windows has no POSIX modes
            pass
        os.replace(tmp_path, target)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return target


def _running_in_cloud() -> bool:
    """True when executing inside a Streamlit server process.

    Used to refuse the interactive browser flow on a cloud host, where no
    browser can reach the redirect URI and the run would hang until timeout.
    """
    try:
        from streamlit.runtime import exists

        return bool(exists())
    except Exception:  # noqa: BLE001 - older/absent runtime API
        return False


def _ensure_credential_files() -> None:
    """Populate ``credentials.json`` / ``token.json`` from Streamlit secrets.

    A no-op when the files already exist (local development always wins) or
    when no secret is configured. Anything written here is git-ignored.
    """
    if not CREDENTIALS_FILE.is_file():
        payload = _secret_json(CREDENTIALS_SECRET)
        if payload is not None:
            _materialize(CREDENTIALS_FILE, payload)
            logger.info("Wrote credentials.json from the %s secret.", CREDENTIALS_SECRET)

    if not TOKEN_FILE.is_file():
        payload = _secret_json(TOKEN_SECRET)
        if payload is not None:
            _materialize(TOKEN_FILE, payload)
            logger.info("Wrote token.json from the %s secret.", TOKEN_SECRET)


def _save_token(creds: Credentials) -> None:
    """Atomically persist the authorized user token to ``token.json``.

    Writes to a temporary file in the same directory and swaps it into place
    with :func:`os.replace`, so an interrupted write cannot corrupt an
    existing valid token.

    On a cloud host the filesystem is frequently read-only or wiped between
    sessions, so a failure to persist is logged and swallowed: the refreshed
    credentials are already valid in memory for this process, and crashing
    here would turn harmless bookkeeping into an outage.
    """
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(TOKEN_FILE.parent), prefix=".token_", suffix=".tmp"
        )
    except OSError as exc:
        logger.warning("Could not persist the refreshed token to %s: %s", TOKEN_FILE, exc)
        return

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
        os.replace(tmp_path, TOKEN_FILE)
    except OSError as exc:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        logger.warning("Could not persist the refreshed token to %s: %s", TOKEN_FILE, exc)
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

    * Materializes ``credentials.json`` / ``token.json`` from the Streamlit
      secrets :data:`CREDENTIALS_SECRET` / :data:`TOKEN_SECRET` when the files
      are absent, so a cloud deployment works without a checked-in secret.
    * Raises :class:`FileNotFoundError` if the OAuth client secrets are still
      missing afterwards.
    * Loads ``token.json`` when present; if the access token is expired and a
      refresh token exists, refreshes it (falling back to a full login if the
      refresh fails, e.g. the grant was revoked).
    * If ``token.json`` is missing or unusable, starts the local-server
      OAuth flow via :class:`InstalledAppFlow`. On a cloud host that flow can
      never complete, so a :class:`RuntimeError` is raised instead of hanging.
    * Saves refreshed or newly obtained tokens safely back to ``token.json``.
    """
    _ensure_credential_files()

    if not CREDENTIALS_FILE.is_file():
        raise FileNotFoundError(
            f"OAuth client secrets not found at '{CREDENTIALS_FILE}'. "
            "Download credentials.json (OAuth client ID, Desktop app) from "
            "the Google Cloud Console and place it at the project root, or "
            f"set the {CREDENTIALS_SECRET} secret when deploying to Streamlit "
            "Community Cloud."
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
        if _running_in_cloud():
            # No browser can reach the loopback redirect URI on a hosted
            # server, so run_local_server would block until it times out.
            # A human must mint a token locally and supply it as a secret.
            raise RuntimeError(
                "No usable Google credentials and an interactive login is not "
                "possible on a Streamlit server. Create token.json locally "
                "(python src/auth.py, then approve the consent screen) and add "
                f"its contents as the {TOKEN_SECRET} secret in Streamlit "
                "Community Cloud, making sure it was granted all required "
                "scopes."
            )
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
