"""Versioned schema changes for the portal's own database.

``Base.metadata.create_all`` only creates tables that are missing; it never
alters a table that already exists. Adding a column, changing a type or
backfilling data on a live database therefore needs an explicit migration.

Conventions
-----------

* A migration is a :class:`Migration` with a four-digit ``version`` (``0001``,
  ``0002``, …), a short ``description`` and an ``upgrade(connection)`` callable
  that receives a SQLAlchemy connection **inside an open transaction**.
* Migrations run in version order, once each. The version is recorded in the
  same transaction as the change, so a crash cannot leave a half-applied
  migration marked as done. Python's sqlite3 driver would normally autocommit
  DDL, so migrations run on a private engine with manual transaction control
  (see :func:`_ddl_engine`): a failing migration really does roll back, and
  startup stops rather than serving a mixed schema.
* **New tables belong in ``portal/models.py``**, not here — ``create_all``
  already creates them before migrations run. This module is for changes to
  tables that are already on disk.
* Never edit a migration that has been applied anywhere. Add a new one.
* The portal runs one web process against one SQLite file. Two processes
  starting at the same instant can race; the loser fails loudly instead of
  corrupting anything. Stop the server before applying migrations by hand.

Usage::

    python -m portal.scripts.migrate            # status, read-only
    python -m portal.scripts.migrate --dry-run  # what would run, no writes
    python -m portal.scripts.migrate --apply    # apply pending migrations

The server also applies pending migrations on startup, right after
``create_all``, so a deployed bundle upgrades itself on the next restart.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Connection, Engine, create_engine, event, inspect, text

MIGRATIONS_TABLE = "portal_schema_migrations"
_VERSION_RE = re.compile(r"\d{4}")
_MAX_DESCRIPTION = 300


class MigrationError(RuntimeError):
    """A migration is malformed, or could not be applied."""


@dataclass(frozen=True)
class Migration:
    """One ordered, single-use schema change."""

    version: str
    description: str
    upgrade: Callable[[Connection], None]


# The registry. Append new migrations here, in version order, importing them
# from sibling modules. Every migration must tolerate a database that
# ``create_all`` already built with the new shape - see m0001.
from portal.migrations.m0001_exam_window import (  # noqa: E402
    DESCRIPTION as _M0001_DESCRIPTION,
    upgrade as _m0001_upgrade,
)
from portal.migrations.m0002_exam_release import (  # noqa: E402
    DESCRIPTION as _M0002_DESCRIPTION,
    upgrade as _m0002_upgrade,
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration("0001", _M0001_DESCRIPTION, _m0001_upgrade),
    Migration("0002", _M0002_DESCRIPTION, _m0002_upgrade),
)


def _validate(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    seen: set[str] = set()
    for migration in ordered:
        version = migration.version
        if not _VERSION_RE.fullmatch(version or ""):
            raise MigrationError(
                f"Migration version {version!r} must be four digits, e.g. '0001'."
            )
        if version in seen:
            raise MigrationError(f"Duplicate migration version {version}.")
        seen.add(version)
        if not (migration.description or "").strip():
            raise MigrationError(f"Migration {version} has no description.")
        if len(migration.description) > _MAX_DESCRIPTION:
            raise MigrationError(
                f"Migration {version} description is longer than {_MAX_DESCRIPTION}."
            )
        if not callable(migration.upgrade):
            raise MigrationError(f"Migration {version} has no upgrade callable.")
    return ordered


def _resolve(migrations: Iterable[Migration] | None) -> tuple[Migration, ...]:
    return _validate(MIGRATIONS if migrations is None else tuple(migrations))


def applied_versions(engine: Engine) -> dict[str, str]:
    """Return ``{version: applied_at}`` without writing anything.

    A database that predates this module simply has no tracking table yet; that
    is reported as "nothing applied" rather than created as a side effect of a
    status check.
    """
    if not inspect(engine).has_table(MIGRATIONS_TABLE):
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT version, applied_at FROM {MIGRATIONS_TABLE} ORDER BY version")
        )
        return {str(row[0]): str(row[1]) for row in rows}


def pending_migrations(
    engine: Engine, migrations: Iterable[Migration] | None = None
) -> list[Migration]:
    """Migrations that have not been recorded as applied, in version order."""
    done = applied_versions(engine)
    return [item for item in _resolve(migrations) if item.version not in done]


def ensure_tracking_table(connection: Connection) -> None:
    connection.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} ("
            "version VARCHAR(32) PRIMARY KEY, "
            f"description VARCHAR({_MAX_DESCRIPTION}) NOT NULL, "
            "applied_at DATETIME NOT NULL)"
        )
    )


def _ddl_engine(engine: Engine) -> tuple[Engine, bool]:
    """Return ``(runner, owned)`` — a connection where DDL is transactional.

    Python's sqlite3 driver only opens an implicit transaction for DML, so an
    ``ALTER TABLE`` would autocommit and a half-applied migration could not be
    rolled back. SQLAlchemy's documented remedy is to switch the driver to
    manual transaction control and emit ``BEGIN`` ourselves.

    This is deliberately a *private* engine used only while migrating, never
    the one that serves requests: changing transaction control for the whole
    app would alter locking behaviour for every student submission.

    Foreign keys are left at the SQLite default (off) on this connection so a
    migration can use the standard table-rebuild procedure inside its
    transaction. ``PRAGMA foreign_key_check`` runs afterwards instead, and a
    violation stops startup.
    """
    if not engine.url.get_backend_name().startswith("sqlite"):
        return engine, False
    if engine.url.database in (None, "", ":memory:"):
        # A second engine would open a *different* empty in-memory database.
        # Nothing durable is at stake there, so migrate on the given engine.
        return engine, False

    runner = create_engine(
        engine.url.render_as_string(hide_password=False),
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(runner, "connect")
    def _manual_transactions(dbapi_connection, _record):  # noqa: ANN001
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    @event.listens_for(runner, "begin")
    def _emit_begin(connection):  # noqa: ANN001
        connection.exec_driver_sql("BEGIN")

    return runner, True


def _check_foreign_keys(runner: Engine) -> None:
    with runner.connect() as connection:
        violations = connection.execute(text("PRAGMA foreign_key_check")).all()
    if violations:
        raise MigrationError(
            f"The migrations committed but left {len(violations)} foreign-key "
            "violation(s). Startup is stopped; inspect the database before serving."
        )


def apply_migrations(
    engine: Engine, migrations: Iterable[Migration] | None = None
) -> list[str]:
    """Apply every pending migration and return the versions that ran.

    Each migration gets its own transaction: the schema change and its tracking
    row commit together or not at all. A failure raises :class:`MigrationError`
    and leaves the database exactly as it was before that migration.
    """
    ordered = _resolve(migrations)
    runner, owned = _ddl_engine(engine)
    try:
        return _apply_on(runner, ordered)
    finally:
        if owned:
            runner.dispose()


def _apply_on(runner: Engine, ordered: Sequence[Migration]) -> list[str]:
    with runner.begin() as connection:
        ensure_tracking_table(connection)

    applied: list[str] = []
    for migration in ordered:
        try:
            with runner.begin() as connection:
                already = connection.execute(
                    text(
                        f"SELECT version FROM {MIGRATIONS_TABLE} WHERE version = :version"
                    ),
                    {"version": migration.version},
                ).first()
                if already is not None:
                    continue
                migration.upgrade(connection)
                connection.execute(
                    text(
                        f"INSERT INTO {MIGRATIONS_TABLE} (version, description, applied_at) "
                        "VALUES (:version, :description, :applied_at)"
                    ),
                    {
                        "version": migration.version,
                        "description": migration.description,
                        "applied_at": datetime.now(timezone.utc)
                        .replace(tzinfo=None, microsecond=0)
                        .isoformat(sep=" "),
                    },
                )
        except MigrationError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface one clear message
            raise MigrationError(
                f"Migration {migration.version} ({migration.description}) failed: {exc}"
            ) from exc
        applied.append(migration.version)
    if applied and runner.url.get_backend_name().startswith("sqlite"):
        _check_foreign_keys(runner)
    return applied


def migration_status(
    engine: Engine, migrations: Iterable[Migration] | None = None
) -> list[dict[str, str]]:
    """Read-only rows for the CLI: one per known migration, plus any orphan."""
    done = applied_versions(engine)
    rows = [
        {
            "version": item.version,
            "description": item.description,
            "state": "applied" if item.version in done else "pending",
            "applied_at": done.get(item.version, ""),
        }
        for item in _resolve(migrations)
    ]
    known = {item.version for item in _resolve(migrations)}
    for version, stamp in sorted(done.items()):
        if version not in known:
            rows.append(
                {
                    "version": version,
                    "description": "(recorded, but not in this codebase)",
                    "state": "unknown",
                    "applied_at": stamp,
                }
            )
    return rows