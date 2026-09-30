"""Shared retry policy for transient Google API failures.

The Google client stack (``google-api-python-client`` -> ``httplib2`` ->
``requests``/``urllib3``) talks plain sockets, so a momentary network blip - a
Wi-Fi roam, a VPN reconnect, a middlebox silently dropping an idle keep-alive
- surfaces as a low-level socket error instead of an HTTP status. On Windows
this is characteristically::

    [WinError 10053] A connection attempt was aborted

which Python raises as :class:`ConnectionAbortedError`. A grading run issues
hundreds of requests back to back, so one dropped socket used to abort the
whole job with a raw traceback in the Streamlit console.

Every network call in :mod:`src.classroom_service` therefore runs through
:func:`call_with_retry`, which pauses briefly (exponential backoff plus
jitter) and retries the request transparently, exactly like the Groq calls in
:mod:`src.llm_evaluator` already do. Failures that are *not* transient
(HTTP 403/404, a 400 from a malformed request) are raised immediately -
retrying those would only waste the user's time.

Callers can subscribe to retry notices with :func:`add_retry_notifier` so a
UI can show "network hiccup, retrying..." instead of silently stalling.
"""

from __future__ import annotations

import errno
import http.client
import logging
import random
import socket
import ssl
from collections.abc import Callable
from typing import Any

import httplib2
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
)

logger = logging.getLogger(__name__)

# Budget: 4 attempts, backing off 1s -> 2s -> 4s (each jittered) and capped.
# A dropped socket normally recovers on the next attempt, so this stays well
# inside a single "Fetch submissions" spinner.
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 15.0
# Fraction of the backoff added as random jitter, so parallel tabs/clients
# reconnecting after the same outage do not resynchronise into a thundering
# herd that knocks the link over again.
BACKOFF_JITTER_RATIO = 0.3

# HTTP statuses worth retrying: rate limiting plus the transient gateway
# family. 4xx client errors are deliberately excluded - they will not fix
# themselves.
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

# Socket-level errno values that always mean "the connection broke", never
# "the request was malformed". Windows maps its WSA* codes onto the same
# numbers (WSAECONNABORTED == 10053 == errno.ECONNABORTED), so one set
# covers both platforms. Names are resolved defensively because a few are
# platform-specific (e.g. ESHUTDOWN exists on Windows, not everywhere).
_TRANSIENT_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in (
            "ECONNABORTED",  # WinError 10053
            "ECONNRESET",  # WinError 10054
            "ECONNREFUSED",
            "EPIPE",
            "ETIMEDOUT",
            "EHOSTUNREACH",
            "EHOSTDOWN",
            "ENETDOWN",
            "ENETRESET",
            "ENETUNREACH",
            "ENOTCONN",
            "EAI_AGAIN",  # temporary DNS failure
            "ESHUTDOWN",
        )
    )
    if code is not None
)

# Exception types that always mean the HTTP round-trip never completed.
# ``ConnectionError`` covers ConnectionAbortedError/ConnectionResetError/
# ConnectionRefusedError/BrokenPipeError, and ``socket.timeout`` is an alias
# of ``TimeoutError`` on Python 3.10+. ``httplib2.HttpLib2Error`` covers
# ServerNotFoundError and friends; ``http.client.HTTPException`` covers
# RemoteDisconnected/IncompleteRead/BadStatusLine; ``TransportError`` covers
# a token refresh that could not reach Google.
TRANSIENT_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    socket.herror,
    ssl.SSLError,
    httplib2.HttpLib2Error,
    http.client.HTTPException,
    TransportError,
)


def is_transient_google_error(exc: BaseException) -> bool:
    """True when ``exc`` is worth retrying (socket drop or retryable status)."""
    if isinstance(exc, HttpError):
        status = exc.resp.status if exc.resp is not None else None
        return status in RETRYABLE_STATUS_CODES
    if isinstance(exc, TRANSIENT_NETWORK_ERRORS):
        return True
    # Any other OSError carrying a connection errno is a dropped socket too
    # (e.g. a raw socket.error surfaced by urllib3 mid-response).
    return isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS



# UI subscribers (Streamlit status lines, CLI progress). A notifier that
# raises must never break the retry loop, so every invocation is guarded.
_retry_notifiers: list[Callable[[str], None]] = []


def add_retry_notifier(callback: Callable[[str], None]) -> None:
    """Register ``callback`` to receive a message before each retry sleep."""
    if callback not in _retry_notifiers:
        _retry_notifiers.append(callback)


def clear_retry_notifiers() -> None:
    """Drop every registered notifier (used by the CLI and by tests)."""
    _retry_notifiers.clear()


def _notify_retry(message: str) -> None:
    for callback in tuple(_retry_notifiers):
        try:
            callback(message)
        except Exception:  # noqa: BLE001 - a broken UI hook must not abort the retry
            logger.debug("Retry notifier %r failed", callback, exc_info=True)


def _wait_before_retry(retry_state: Any) -> float:
    """Exponential backoff (1s, 2s, 4s...) with jitter, capped at the max."""
    attempt = max(getattr(retry_state, "attempt_number", 1), 1)
    base = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
    return min(base + random.uniform(0.0, base * BACKOFF_JITTER_RATIO), MAX_BACKOFF_SECONDS)


def _before_sleep(retry_state: Any) -> None:
    """Log and announce each retry before the backoff sleep."""
    outcome = getattr(retry_state, "outcome", None)
    error = outcome.exception() if outcome is not None and outcome.failed else "?"
    action = retry_state.kwargs.get("action", "Google API call")
    message = (
        f"Network hiccup while running {action} ({error}); "
        f"retrying (attempt {getattr(retry_state, 'attempt_number', '?')}/{MAX_ATTEMPTS})..."
    )
    logger.warning(message)
    _notify_retry(message)


@retry(
    retry=retry_if_exception(is_transient_google_error),
    stop=stop_after_attempt(MAX_ATTEMPTS),
    wait=_wait_before_retry,
    reraise=True,
    before_sleep=_before_sleep,
)
def call_with_retry(func: Callable[[], Any], action: str) -> Any:
    """Run ``func`` (``HttpRequest.execute``) with transient-failure retries.

    ``action`` is a human-readable description used only in retry logs. The
    original exception is re-raised once the budget is spent
    (``reraise=True``), so callers see the real cause instead of a
    ``RetryError`` wrapper.
    """
    return func()
