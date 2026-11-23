"""Small helpers shared by the portal web tests."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from portal.auth.security import hash_password
from portal.models import ROLE_STUDENT, ROLE_TEACHER, User

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match is not None, "no CSRF token in page"
    return match.group(1)


def csrf(client: TestClient, path: str = "/teacher") -> str:
    response = client.get(path, follow_redirects=True)
    assert response.status_code == 200, (path, response.status_code, response.text[:300])
    return csrf_from(response.text)


def login(
    client: TestClient,
    username: str,
    password: str,
    *,
    follow_redirects: bool = False,
):
    page = client.get("/login")
    token = csrf_from(page.text)
    return client.post(
        "/login",
        data={"csrf_token": token, "username": username, "password": password},
        follow_redirects=follow_redirects,
    )


def logout(client: TestClient, path: str = "/teacher") -> None:
    token = csrf(client, path)
    response = client.post(
        "/logout", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303


def create_user(
    app,
    username: str,
    password: str,
    *,
    role: str = ROLE_TEACHER,
    display_name: str | None = None,
) -> int:
    """Insert an account directly, bypassing the UI (setup only)."""
    factory = app.state.session_factory
    with factory() as db:
        user = User(
            username=username.strip().lower(),
            display_name=display_name or username,
            password_hash=hash_password(password),
            role=role if role in (ROLE_TEACHER, ROLE_STUDENT) else ROLE_STUDENT,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
