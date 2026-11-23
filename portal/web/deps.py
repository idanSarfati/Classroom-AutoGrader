"""Shared FastAPI dependencies and template helpers."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from portal.auth.security import (
    SESSION_CSRF_KEY,
    SESSION_USER_KEY,
    session_csrf_token,
    tokens_equal,
)
from portal.models import ROLE_TEACHER, User


def get_db(request: Request) -> Iterator[Session]:
    """Yield a request-scoped session from the app's factory."""
    factory = request.app.state.session_factory
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


def get_optional_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    try:
        user = db.get(User, int(user_id))
    except (TypeError, ValueError):
        user = None
    if user is None or not user.is_active:
        # A deleted or disabled account must not keep a valid-looking cookie.
        request.session.clear()
        return None
    return user


def require_user(
    request: Request,
    user: User | None = Depends(get_optional_user),
) -> User:
    if user is None:
        target = request.url.path or "/"
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/login?next={quote(target)}"},
        )
    return user


def require_teacher(user: User = Depends(require_user)) -> User:
    if user.role != ROLE_TEACHER:
        raise HTTPException(status_code=403, detail="Teacher access required.")
    return user


def role_home(user: User) -> str:
    return "/teacher" if user.role == ROLE_TEACHER else "/student"


def csrf_token(request: Request) -> str:
    return session_csrf_token(request.session)


async def verify_csrf(request: Request) -> None:
    """Reject a state-changing request whose form token does not match.

    Starlette caches ``request.form()``, so the endpoint may still declare
    ``Form(...)`` parameters after this dependency has read the body.
    """
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    expected = request.session.get(SESSION_CSRF_KEY, "")
    form = await request.form()
    supplied = form.get("csrf_token")
    if not tokens_equal(str(supplied or ""), str(expected or "")):
        raise HTTPException(
            status_code=400,
            detail="אסימון אבטחה חסר או שגוי. רעננו את העמוד ונסו שוב.",
        )


def flash(request: Request, message: str, *, kind: str = "info") -> None:
    """Queue a one-shot message for the next rendered page."""
    items = list(request.session.get("_flashes") or [])
    items.append({"kind": kind, "message": message})
    request.session["_flashes"] = items[-5:]


def render(
    request: Request,
    template: str,
    *,
    user: User | None = None,
    status_code: int = 200,
    **context: Any,
):
    templates: Jinja2Templates = request.app.state.templates
    payload: dict[str, Any] = {
        "request": request,
        "user": user,
        "csrf_token": csrf_token(request),
        "app_name": request.app.state.portal_settings.app_name,
        "flashes": list(request.session.pop("_flashes", []) or []),
        "nav_unread": _nav_unread(request, user),
        **context,
    }
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=payload,
        status_code=status_code,
    )


def _nav_unread(request: Request, user: User | None) -> int:
    """The chat unread badge shown on the navigation tab of every page.

    One indexed COUNT per rendered page; the live polling endpoint keeps it
    current without a reload while the user stays on a page.
    """
    if user is None:
        return 0
    from portal import chat

    factory = request.app.state.session_factory
    with factory() as db:
        return chat.total_unread(db, user.id, user.role)
