"""Small in-process login throttle.

Keyed by client address + submitted username. Five failures in five minutes
lock that pair for five minutes. This is a first line of defence for a
single-worker school deployment; a multi-worker deployment should move the
counters to the database (documented in the plan). Successful logins clear
the counter.
"""
from __future__ import annotations

import threading
import time

_MAX_FAILURES = 5
_WINDOW_SECONDS = 5 * 60
_LOCK_SECONDS = 5 * 60

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def _key(host: str | None, username: str) -> str:
    return f"{(host or 'unknown').lower()}|{username.strip().lower()}"


def is_locked(host: str | None, username: str) -> bool:
    key = _key(host, username)
    now = time.monotonic()
    with _lock:
        until = _locked_until.get(key, 0.0)
        if until and until > now:
            return True
        if until:
            _locked_until.pop(key, None)
        return False


def record_failure(host: str | None, username: str) -> None:
    key = _key(host, username)
    now = time.monotonic()
    with _lock:
        recent = [stamp for stamp in _failures.get(key, []) if now - stamp < _WINDOW_SECONDS]
        recent.append(now)
        _failures[key] = recent
        if len(recent) >= _MAX_FAILURES:
            _locked_until[key] = now + _LOCK_SECONDS
            _failures[key] = []


def clear(host: str | None, username: str) -> None:
    key = _key(host, username)
    with _lock:
        _failures.pop(key, None)
        _locked_until.pop(key, None)


def clear_username(username: str) -> None:
    """Drop every failure count and lock for one username, on all hosts.

    A teacher who resets a password does not know which machine the student
    was locked out on, and the student should be able to sign in immediately
    with the new password.
    """
    suffix = "|" + username.strip().lower()
    with _lock:
        for store in (_failures, _locked_until):
            for key in [key for key in store if key.endswith(suffix)]:
                store.pop(key, None)


def reset_for_tests() -> None:
    with _lock:
        _failures.clear()
        _locked_until.clear()
