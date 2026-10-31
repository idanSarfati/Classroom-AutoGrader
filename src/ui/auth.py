"""Google authentication / service helpers extracted from :mod:`app`.

Owns the cached :class:`src.classroom_service.ClassroomService`, the retry
notifier wiring, and the auth-failure UI panel with its one-click reconnect
flow. Importing this module does **not** pull in any Streamlit tab rendering.
"""

from __future__ import annotations

import logging
import sys

import streamlit as st

from src.api_retry import add_retry_notifier, clear_retry_notifiers
from src.classroom_service import (
    ClassroomService,
    ClassroomServiceError,
    is_auth_failure,
)
from src.seams import st_proxy

logger = logging.getLogger(__name__)


def _register_retry_notifier() -> None:
    """Show automatic API retries in the UI instead of a silent stall.

    Called once per script run. The notifiers are module-level in
    :mod:`src.api_retry`, so the previous run's Streamlit widgets are dropped
    first to avoid writing into stale containers.
    """
    clear_retry_notifiers()

    def _on_retry(message: str) -> None:
        logger.info(message)
        st.warning(message, icon="🔄")

    add_retry_notifier(_on_retry)


@st.cache_resource(show_spinner="Connecting to Google Classroom...")
def _cached_classroom_service() -> ClassroomService:
    """Build (and cache) the ClassroomService with the current OAuth grant."""
    return ClassroomService()


def _render_google_auth_error(exc: BaseException) -> None:
    """Show a Google auth failure with a one-click recovery path."""
    st.error(
        "🔑 Your Google authorization has expired or was revoked "
        f"(`{type(exc).__name__}`).\n\n"
        "1. Delete `token.json` from the project folder (or run "
        "`python src/auth.py` and approve the consent screen in the browser).\n"
        "2. Press **Reconnect Google** below, then reload.",
        icon="🔑",
    )
    with st.expander("Technical details"):
        st.code(f"{type(exc).__name__}: {exc}")
    # The auth error is rendered for every Google-backed tab, so guard the button
    # with a session flag to avoid StreamlitDuplicateElementKey.
    if not st.session_state.get("_google_reconnect_button_rendered", False):
        st.session_state["_google_reconnect_button_rendered"] = True
        if st.button("🔄 Reconnect Google", key="reconnect_google"):
            _cached_classroom_service.clear()
            _clear_google_caches()
            st.rerun()


def _clear_google_caches() -> None:
    """Best-effort clear of the dashboard's Google-backed ``st.cache_data``
    entries (courses / coursework / pending scan).

    Resolved through ``sys.modules`` at *call* time instead of a static import:
    :mod:`src.ui.dashboard` imports this module, so importing it back at the
    bottom of :mod:`src.ui.auth` would either cycle (dashboard imported first
    -> names stay ``None`` forever) or silently skip the cache clear during a
    reconnect - exactly when it matters most.
    """
    dashboard = sys.modules.get("src.ui.dashboard")
    if dashboard is None:
        return
    for name in ("_load_courses", "_load_course_work", "_load_pending_dashboard"):
        try:
            fn = getattr(dashboard, name, None)
            if fn is not None and hasattr(fn, "clear"):
                fn.clear()
        except Exception:  # noqa: BLE001 - cache may not exist yet
            logger.debug("%s cache clear skipped", name, exc_info=True)


def _handle_google_error(exc: BaseException, fallback: str) -> bool:
    """Render ``exc`` and return True when it was an auth failure.

    Catches by message (via :func:`is_auth_failure`) rather than by class so
    it also works when Streamlit's hot-reload re-imported
    :mod:`src.classroom_service` - the ``except ClassroomServiceError`` in the
    tab bodies then holds a stale class object and would miss the live one.
    Non-auth errors fall back to a plain ``st.error`` with ``fallback``.
    """
    if is_auth_failure(exc):
        _render_google_auth_error(exc)
        return True
    st.error(f"{fallback}: {exc}")
    return False


def get_classroom_service() -> ClassroomService:
    """Return the cached service, rebuilding it when its OAuth grant is stale.

    ``st.cache_resource`` keeps the instance - and therefore the Credentials
    captured when it was built - for the lifetime of the Streamlit server
    process. ``auth.get_google_credentials`` is only called once, inside
    ``ClassroomService.__init__``, so its scope-change detection never runs
    again: a server started before the ``documents`` write scope was added
    keeps using the old token forever and every Doc write fails, while the CLI
    (which builds a fresh service on each run) works fine. The grant is
    therefore re-validated on every script run and the cache is dropped when
    it no longer covers everything we need.
    """
    service = _cached_classroom_service()
    missing = service.missing_scopes
    if not missing:
        return service

    st.warning(
        "The saved Google authorization is missing required scope(s): "
        + ", ".join(missing)
        + ". Reconnecting..."
    )
    _cached_classroom_service.clear()
    service = _cached_classroom_service()
    still_missing = service.missing_scopes
    if still_missing:
        st.error(
            "Google did not grant the scope(s) we need: "
            + ", ".join(still_missing)
            + ". Run `python src/auth.py`, approve the extra permissions in "
            "the browser, then reload this page."
        )
    return service


# ---------------------------------------------------------------------------
# Seam wiring
# ---------------------------------------------------------------------------
# Tests monkeypatch ``st`` on the ``app`` facade; resolve it at call time.
# (``@st.cache_resource`` above already bound real Streamlit at import time -
# decorators are not re-bound, exactly as in the former monolith.)
st = st_proxy("app", st)