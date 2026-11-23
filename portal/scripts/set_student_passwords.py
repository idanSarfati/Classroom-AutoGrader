"""Give every student a memorable password: given name + national id.

The school directory stores a name as ``"<family> <given> [<given> ...]"``,
so the given name is the **second word** of ``display_name``. The login name
is the national id, which makes the password ``אביה220019152`` for
``"אבורמד אביה"`` and ``אריאל221015514`` for ``"דדון אריאל אליהו"``.

The script is a dry run unless ``--apply`` is given, never prints a password,
and hashes every password **before** opening the write transaction, so the
live portal keeps accepting writes while the (slow) Argon2 work happens. All
rows are updated in one short transaction, and a failed verification rolls the
whole batch back.

Usage::

    python -m portal.scripts.set_student_passwords            # show the plan
    python -m portal.scripts.set_student_passwords --apply    # change them

    # rehearse against a copy without touching the live database:
    PORTAL_DATABASE_URL=sqlite:////tmp/portal-copy.db \
        python -m portal.scripts.set_student_passwords --apply
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.auth.security import hash_password, verify_password
from portal.config import load_settings
from portal.db import build_engine, build_session_factory, session_scope
from portal.models import ROLE_STUDENT, StudentRecord, User
from portal.validation import password_error


@dataclass(frozen=True)
class PasswordChange:
    user_id: int
    display_name: str
    password: str


@dataclass(frozen=True)
class SkippedStudent:
    display_name: str
    reason: str


def given_name(display_name: str) -> str:
    """The student's first given name: the second word of the school name."""
    parts = " ".join((display_name or "").split()).split()
    return parts[1] if len(parts) > 1 else ""


def student_password(display_name: str, national_id: str) -> str | None:
    """``<given name><national id>``, or ``None`` when a piece is missing."""
    given = given_name(display_name)
    identifier = (national_id or "").strip()
    if not given or not identifier:
        return None
    return given + identifier


def _identifier(user: User, record: StudentRecord | None) -> str:
    national_id = (record.national_id if record is not None else "") or ""
    if national_id.strip():
        return national_id.strip()
    # Older rows may predate the directory record; the login name is the id.
    username = (user.username or "").strip()
    return username if username.isdigit() else ""


def build_plan(db: Session) -> tuple[list[PasswordChange], list[SkippedStudent]]:
    """Read every student account and work out the new password, without writing."""
    rows = db.execute(
        select(User, StudentRecord)
        .outerjoin(StudentRecord, StudentRecord.student_id == User.id)
        .where(User.role == ROLE_STUDENT)
        .order_by(User.id)
    ).all()
    changes: list[PasswordChange] = []
    skipped: list[SkippedStudent] = []
    for user, record in rows:
        identifier = _identifier(user, record)
        password = student_password(user.display_name, identifier)
        if password is None:
            reason = "אין ת.ז." if not identifier else "אין שם פרטי בשם התצוגה"
            skipped.append(SkippedStudent(user.display_name, reason))
            continue
        problem = password_error(password)
        if problem:
            skipped.append(SkippedStudent(user.display_name, problem))
            continue
        changes.append(PasswordChange(user.id, user.display_name, password))
    return changes, skipped


def hash_plan(changes: list[PasswordChange]) -> list[tuple[int, str]]:
    """Hash every password outside the write transaction.

    Argon2 deliberately takes ~0.1 s per password. Holding SQLite's single
    writer for half a minute would stall every student autosave, so the slow
    part happens first and only the row updates are transactional.
    """
    return [(change.user_id, hash_password(change.password)) for change in changes]


def apply_hashes(
    factory, changes: list[PasswordChange], hashes: list[tuple[int, str]]
) -> None:
    """Write every hash in one short transaction.

    The caller has already verified a sample, so the write lock is held only
    for the row updates and not for any Argon2 work.
    """
    with session_scope(factory) as db:
        for user_id, password_hash in hashes:
            user = db.get(User, user_id)
            if user is None:  # pragma: no cover - deleted between plan and apply
                raise RuntimeError(f"user {user_id} disappeared")
            user.password_hash = password_hash


def _sample(changes: list[PasswordChange], size: int) -> list[PasswordChange]:
    if size <= 0:
        return []
    if len(changes) <= size:
        return changes
    step = max(1, len(changes) // size)
    return changes[::step][:size]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the new passwords. Without it the database is not changed.",
    )
    parser.add_argument(
        "--show-sample",
        type=int,
        default=3,
        help="How many planned passwords to verify after hashing (never printed).",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    settings = load_settings()
    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)

    with session_scope(factory) as db:
        changes, skipped = build_plan(db)

    print(f"database: {settings.database_url}")
    print(f"students with a new password: {len(changes)}")
    print(f"skipped: {len(skipped)}")
    for item in skipped:
        print(f"  - {item.display_name}: {item.reason}")
    if not changes:
        return 0
    if not args.apply:
        print("dry run: no password was changed. Add --apply to write.")
        return 0

    started = time.perf_counter()
    hashes = hash_plan(changes)
    by_id = dict(hashes)
    sample = _sample(changes, args.show_sample)
    for change in sample:
        if not verify_password(by_id[change.user_id], change.password):
            print(f"verification failed for user {change.user_id}", file=sys.stderr)
            return 1
    apply_hashes(factory, changes, hashes)
    elapsed = time.perf_counter() - started
    print(
        f"applied: {len(changes)} passwords in {elapsed:.1f} s; "
        f"sample of {len(sample)} verified."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
