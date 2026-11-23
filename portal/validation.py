"""Input validation shared by the auth and teacher routes.

The rules are deliberately conservative and small: this is a school tool, not
an identity provider. Every message is returned as a Hebrew string ready for
the UI.
"""
from __future__ import annotations

import re

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def username_error(raw: str) -> str | None:
    username = normalize_username(raw)
    if not username:
        return "יש להזין שם משתמש."
    if not _USERNAME_RE.fullmatch(username):
        return (
            "שם המשתמש חייב להיות באורך 3–32 תווים, באותיות אנגליות קטנות, "
            "ספרות, נקודה, מקף או קו תחתון, ולהתחיל באות או בספרה."
        )
    return None


def password_error(password: str) -> str | None:
    if not password:
        return "יש להזין סיסמה."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"הסיסמה חייבת להכיל לפחות {MIN_PASSWORD_LENGTH} תווים."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"הסיסמה ארוכה מדי (מקסימום {MAX_PASSWORD_LENGTH} תווים)."
    return None


def clean_display_name(raw: str) -> str:
    return " ".join((raw or "").split())


def display_name_error(raw: str) -> str | None:
    name = clean_display_name(raw)
    if not name:
        return "יש להזין שם תצוגה."
    if len(name) > 120:
        return "שם התצוגה ארוך מדי (מקסימום 120 תווים)."
    return None


def clean_class_name(raw: str) -> str:
    return " ".join((raw or "").split())


def class_name_error(raw: str) -> str | None:
    name = clean_class_name(raw)
    if not name:
        return "יש להזין שם כיתה."
    if len(name) > 120:
        return "שם הכיתה ארוך מדי (מקסימום 120 תווים)."
    return None


def assignment_title_error(raw: str) -> str | None:
    title = " ".join((raw or "").split())
    if not title:
        return "יש להזין שם מטלה."
    if len(title) > 160:
        return "שם המטלה ארוך מדי (מקסימום 160 תווים)."
    return None


def lesson_title_error(raw: str) -> str | None:
    title = " ".join((raw or "").split())
    if not title:
        return "יש להזין שם שיעור."
    if len(title) > 160:
        return "שם השיעור ארוך מדי (מקסימום 160 תווים)."
    return None


def slide_title_error(raw: str) -> str | None:
    """Slide titles are optional; only their length is validated."""
    title = " ".join((raw or "").split())
    if len(title) > 160:
        return "שם השקופית ארוך מדי (מקסימום 160 תווים)."
    return None
