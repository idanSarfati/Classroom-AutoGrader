"""Create or update the first teacher account.

Students are provisioned by a teacher from the class page; teachers are not
self-service. Run this once per teacher, from the project root::

    python -m portal.scripts.bootstrap_teacher --username mori --name "מורי כהן"

The password is read from ``PORTAL_TEACHER_PASSWORD`` (useful for scripted
setup) or prompted with ``getpass`` so it never lands in shell history.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import select

from portal.auth.security import hash_password
from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.models import ROLE_TEACHER, User
from portal.validation import (
    clean_display_name,
    display_name_error,
    normalize_username,
    password_error,
    username_error,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--name", required=True, help="Display name shown in the UI.")
    parser.add_argument(
        "--password",
        default=os.getenv("PORTAL_TEACHER_PASSWORD"),
        help="Prefer the PORTAL_TEACHER_PASSWORD env var or the interactive prompt.",
    )
    args = parser.parse_args(argv)

    for error in (
        username_error(args.username),
        display_name_error(args.name),
    ):
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            print("error: passwords do not match.", file=sys.stderr)
            return 2
    error = password_error(password)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    cfg = load_settings()
    engine = build_engine(cfg.database_url)
    init_db(engine)
    factory = build_session_factory(engine)
    username = normalize_username(args.username)

    try:
        with factory() as session:
            existing = session.scalar(select(User).where(User.username == username))
            if existing is None:
                existing = User(
                    username=username,
                    display_name=clean_display_name(args.name),
                    password_hash=hash_password(password),
                    role=ROLE_TEACHER,
                )
                session.add(existing)
                action = "created"
            else:
                existing.display_name = clean_display_name(args.name)
                existing.password_hash = hash_password(password)
                existing.role = ROLE_TEACHER
                existing.is_active = True
                action = "updated"
            session.commit()
            print(f"Teacher '{username}' {action}. Database: {cfg.database_url}")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
