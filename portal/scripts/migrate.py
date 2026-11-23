"""List or apply the portal's schema migrations.

Status and ``--dry-run`` are strictly read-only: they never create the tracking
table and never write to the database. ``--apply`` runs each pending migration
in its own transaction and stops on the first failure.

Stop the portal server before applying migrations by hand. The server also
applies pending migrations itself on startup, so a deployed bundle normally
upgrades on the next restart without this script.

Usage::

    python -m portal.scripts.migrate
    python -m portal.scripts.migrate --dry-run
    python -m portal.scripts.migrate --apply
    python -m portal.scripts.migrate --database-url sqlite:///portal/data/copy.db --apply
"""
from __future__ import annotations

import argparse

from portal.config import load_settings
from portal.db import build_engine
from portal.migrations import (
    MigrationError,
    apply_migrations,
    migration_status,
    pending_migrations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="apply every pending migration (stop the server first)",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be applied, without writing anything",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="target database (default: the configured portal database)",
    )
    args = parser.parse_args(argv)

    url = args.database_url or load_settings().database_url
    engine = build_engine(url)
    try:
        print(f"database: {engine.url.render_as_string(hide_password=True)}")
        if args.apply:
            pending = pending_migrations(engine)
            if not pending:
                print("No pending migrations; the schema is up to date.")
                return 0
            print(f"Applying {len(pending)} migration(s). The server must be stopped.")
            for version in apply_migrations(engine):
                print(f"  applied {version}")
            return 0

        rows = migration_status(engine)
        if not rows:
            print("No migrations registered; create_all owns the current schema.")
        for row in rows:
            stamp = f"  at {row['applied_at']}" if row["applied_at"] else ""
            print(f"{row['version']}  {row['state']:<8}  {row['description']}{stamp}")
        pending_count = sum(1 for row in rows if row["state"] == "pending")
        if args.dry_run:
            print(
                f"{pending_count} migration(s) would be applied."
                if pending_count
                else "Nothing to apply."
            )
        elif pending_count:
            print(f"{pending_count} pending. Use --apply with the server stopped.")
        return 0
    except MigrationError as exc:
        print(f"migration problem: {exc}")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())