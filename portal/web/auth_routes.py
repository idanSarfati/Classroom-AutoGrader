"""Login/logout routes (teacher-provisioned accounts, no OAuth)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.auth import ratelimit
from portal.auth.security import end_session, start_session, verify_password
from portal.models import User
from portal.validation import normalize_username
from portal.web.deps import (
    flash,
    get_db,
    get_optional_user,
    render,
    role_home,
    verify_csrf,
)

router = APIRouter()


def _safe_next(value: str | None) -> str | None:
    """Only allow same-site absolute paths as post-login destinations."""
    if not value:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or "\n" in value or "\r" in value:
        return None
    return value


@router.get("/login")
def login_page(
    request: Request,
    user: User | None = Depends(get_optional_user),
):
    if user is not None:
        return RedirectResponse(role_home(user), status_code=303)
    return render(
        request,
        "login.html",
        next_path=_safe_next(request.query_params.get("next")) or "",
    )


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next_path: str = Form(default=""),
    db: Session = Depends(get_db),
):
    host = request.client.host if request.client else None
    normalized = normalize_username(username)

    if ratelimit.is_locked(host, normalized):
        return render(
            request,
            "login.html",
            status_code=429,
            error="יותר מדי ניסיונות התחברות. נסו שוב בעוד כמה דקות.",
            username=username,
            next_path=_safe_next(next_path) or "",
        )

    account = (
        db.scalar(select(User).where(User.username == normalized))
        if normalized
        else None
    )
    # Verify even when the account is missing, against a fixed dummy hash, so
    # response timing does not reveal which usernames exist.
    stored = account.password_hash if account is not None else _DUMMY_HASH
    valid = verify_password(stored, password or "")
    if account is None or not valid or not account.is_active:
        ratelimit.record_failure(host, normalized or "-")
        return render(
            request,
            "login.html",
            status_code=200,
            error="שם המשתמש או הסיסמה אינם נכונים.",
            username=username,
            next_path=_safe_next(next_path) or "",
        )

    ratelimit.clear(host, normalized)
    start_session(
        request.session, user_id=account.id, role=account.role
    )
    target = _safe_next(next_path) or role_home(account)
    return RedirectResponse(target, status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    end_session(request.session)
    flash(request, "התנתקתם בהצלחה.", kind="success")
    return RedirectResponse("/login", status_code=303)


# A real Argon2id hash of a value nobody knows, embedded as a constant. It
# exists only so a missing user costs the same verification work as a wrong
# password, instead of returning early and leaking which usernames exist.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$JjJAGWPHCxpz/UcMgLxFjw$"
    "1e96TmIZpaH33Y43B0odjwAqTa3MY4QsBVqas130xa8"
)
