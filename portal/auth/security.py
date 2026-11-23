"""Password hashing, CSRF tokens and sessions.

Passwords use Argon2id (the ``argon2-cffi`` default and the current OWASP
recommendation). Sessions are Starlette's signed cookie: the browser holds a
signed payload with the user id and a CSRF token, never a password or a
server-side token table. Logging out clears the cookie.
"""
from __future__ import annotations

import hmac
import secrets

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Parameters are the library defaults for interactive logins (RFC 9106 low
# memory profile): 64 MiB, 3 iterations, 4 lanes, 32-byte tag.
_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

SESSION_USER_KEY = "user_id"
SESSION_ROLE_KEY = "role"
SESSION_CSRF_KEY = "csrf"


def hash_password(password: str) -> str:
    """Hash a plaintext password (never log or store the plaintext)."""
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verification; malformed hashes never raise."""
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_equal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left, right)


def start_session(session: dict, *, user_id: int, role: str) -> None:
    """Replace the session payload after a successful login."""
    session.clear()
    session[SESSION_USER_KEY] = int(user_id)
    session[SESSION_ROLE_KEY] = role
    session[SESSION_CSRF_KEY] = new_csrf_token()


def end_session(session: dict) -> None:
    session.clear()


def session_csrf_token(session: dict) -> str:
    token = session.get(SESSION_CSRF_KEY)
    if not token:
        token = new_csrf_token()
        session[SESSION_CSRF_KEY] = token
    return token
