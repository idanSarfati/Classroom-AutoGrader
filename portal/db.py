"""SQLAlchemy engine/session setup for the portal's own SQLite database."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for portal tables (all prefixed ``portal_``)."""


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    kwargs: dict = {"future": True, "echo": echo, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        # FastAPI runs sync handlers in a thread pool, so the SQLite connection
        # may be opened on a different thread than the one that created it.
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        _enable_sqlite_foreign_keys(engine)
    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL lets autosave writes from many students overlap with the
            # grading worker instead of serialising on one writer lock.
            cursor.execute("PRAGMA journal_mode=WAL")
            # A short busy timeout lets the single worker and web requests
            # coexist without spurious "database is locked" errors.
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


def init_db(engine: Engine) -> None:
    """Create missing tables, then apply any pending schema migration.

    ``create_all`` never alters a table that already exists, so changes to live
    tables are expressed as ordered migrations in ``portal.migrations``. With
    no migrations registered this is exactly the old behaviour plus an empty
    tracking table.
    """
    from portal import models  # noqa: F401  - register mappers
    from portal.migrations import apply_migrations

    Base.metadata.create_all(engine)
    apply_migrations(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit/rollback wrapper for scripts and the future worker."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
