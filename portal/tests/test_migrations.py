"""The schema migration runner: ordering, atomicity, idempotence and the CLI.

Every test builds its own temporary SQLite file. Nothing here touches
``portal/data/portal.db``, and the CLI is always given an explicit
``--database-url`` so it never falls back to the configured live database.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from portal.db import build_engine, init_db
from portal.migrations import (
    MIGRATIONS_TABLE,
    Migration,
    MigrationError,
    applied_versions,
    apply_migrations,
    migration_status,
    pending_migrations,
)
from portal.scripts import migrate as migrate_cli


@pytest.fixture
def engine(tmp_path):
    value = build_engine(f"sqlite:///{(tmp_path / 'portal.db').as_posix()}")
    try:
        yield value
    finally:
        value.dispose()


def _existing_table(engine) -> None:
    """A table that predates the migration, the way create_all leaves one."""
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE portal_lessons (id INTEGER PRIMARY KEY, title VARCHAR(160))")
        )
        connection.execute(
            text("INSERT INTO portal_lessons (id, title) VALUES (1, 'שיעור ישן')")
        )


def _add_summary(connection) -> None:
    connection.execute(text("ALTER TABLE portal_lessons ADD COLUMN summary TEXT DEFAULT ''"))


def _columns(engine, table="portal_lessons") -> set[str]:
    return {row["name"] for row in inspect(engine).get_columns(table)}


# ------------------------------------------------------------------ registry


def test_the_shipped_registry_applies_on_startup(engine):
    """The real registry runs end to end on a fresh database.

    A fresh database is built by ``create_all`` *with* the new columns already
    present, so 0001 and 0002 must notice that and still record themselves.
    This is the path every new deployment and every test database takes.
    """
    from portal.migrations import MIGRATIONS

    assert [migration.version for migration in MIGRATIONS] == ["0001", "0002"]
    init_db(engine)
    assert inspect(engine).has_table(MIGRATIONS_TABLE)
    assert set(applied_versions(engine)) == {"0001", "0002"}
    assert {
        "exam_config",
        "opens_at",
        "closes_at",
        "exam_release",
    } <= _columns(engine, "portal_assignments")
    assert pending_migrations(engine) == []
    assert apply_migrations(engine) == []


def test_the_shipped_0002_adds_the_release_column_to_an_old_table(engine):
    """The live database predates the column; every old row starts hidden."""
    from portal.migrations.m0002_exam_release import upgrade

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE portal_assignments "
                "(id INTEGER PRIMARY KEY, title VARCHAR(200))"
            )
        )
        connection.execute(
            text("INSERT INTO portal_assignments (id, title) VALUES (1, 'מבחן')")
        )

    with engine.begin() as connection:
        upgrade(connection)

    assert "exam_release" in _columns(engine, "portal_assignments")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT exam_release FROM portal_assignments WHERE id = 1")
            ).scalar_one()
            == "held"
        )

    # ``create_all`` may already have built the column: a second run must be
    # harmless rather than adding it twice.
    with engine.begin() as connection:
        upgrade(connection)


def test_status_does_not_create_the_tracking_table(engine):
    _existing_table(engine)
    assert applied_versions(engine) == {}
    assert migration_status(engine, ()) == []
    assert not inspect(engine).has_table(MIGRATIONS_TABLE)


# ----------------------------------------------------------------- applying


def test_a_migration_alters_an_existing_table_and_keeps_its_rows(engine):
    _existing_table(engine)
    migration = Migration("0001", "הוספת תקציר לשיעור", _add_summary)

    assert pending_migrations(engine, [migration]) == [migration]
    assert apply_migrations(engine, [migration]) == ["0001"]

    assert "summary" in _columns(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT title, summary FROM portal_lessons WHERE id = 1")
        ).one()
    assert row[0] == "שיעור ישן" and row[1] == ""
    recorded = applied_versions(engine)
    assert set(recorded) == {"0001"} and recorded["0001"]
    assert pending_migrations(engine, [migration]) == []


def test_applying_twice_runs_the_migration_only_once(engine):
    _existing_table(engine)
    calls: list[str] = []

    def upgrade(connection):
        calls.append("ran")
        _add_summary(connection)

    migration = Migration("0001", "פעם אחת", upgrade)
    assert apply_migrations(engine, [migration]) == ["0001"]
    assert apply_migrations(engine, [migration]) == []
    assert calls == ["ran"]


def test_migrations_run_in_version_order_not_list_order(engine):
    order: list[str] = []

    def step(version):
        def upgrade(connection):
            order.append(version)
            connection.execute(
                text(f"CREATE TABLE marker_{version} (id INTEGER PRIMARY KEY)")
            )
        return upgrade

    migrations = [
        Migration("0003", "שלישית", step("0003")),
        Migration("0001", "ראשונה", step("0001")),
        Migration("0002", "שנייה", step("0002")),
    ]
    assert apply_migrations(engine, migrations) == ["0001", "0002", "0003"]
    assert order == ["0001", "0002", "0003"]


def test_a_failing_migration_rolls_back_and_stops_the_run(engine):
    _existing_table(engine)

    def broken(connection):
        connection.execute(text("ALTER TABLE portal_lessons ADD COLUMN summary TEXT"))
        connection.execute(text("CREATE TABLE half_done (id INTEGER PRIMARY KEY)"))
        raise RuntimeError("נכשל באמצע")

    later = Migration("0002", "לא אמורה לרוץ", lambda connection: order.append("later"))
    order: list[str] = []
    migrations = [Migration("0001", "שבורה", broken), later]

    with pytest.raises(MigrationError) as excinfo:
        apply_migrations(engine, migrations)
    assert "0001" in str(excinfo.value) and "נכשל באמצע" in str(excinfo.value)

    assert "summary" not in _columns(engine)
    assert not inspect(engine).has_table("half_done")
    assert applied_versions(engine) == {}
    assert order == []
    # The rollback left the database usable, so a fixed migration can run.
    assert apply_migrations(engine, [Migration("0001", "מתוקנת", _add_summary)]) == ["0001"]
    assert "summary" in _columns(engine)


def test_a_migration_that_raises_migration_error_keeps_its_message(engine):
    def refuse(connection):
        raise MigrationError("הסכמה לא במצב הצפוי")

    with pytest.raises(MigrationError, match="הסכמה לא במצב הצפוי"):
        apply_migrations(engine, [Migration("0001", "מסרבת", refuse)])


def test_a_migration_leaving_orphan_rows_stops_startup(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE child (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES parent(id))"
            )
        )
        connection.execute(text("INSERT INTO parent (id) VALUES (1)"))

    def orphan(connection):
        # Foreign keys are off on the migration connection so a table rebuild
        # is possible; the post-run check is what catches a bad backfill.
        connection.execute(text("INSERT INTO child (id, parent_id) VALUES (1, 999)"))

    with pytest.raises(MigrationError, match="foreign-key"):
        apply_migrations(engine, [Migration("0001", "שורה יתומה", orphan)])
    # The change itself committed; the check is what refuses to serve it.
    assert set(applied_versions(engine)) == {"0001"}
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all()


def test_a_clean_migration_passes_the_foreign_key_check(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE child (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES parent(id))"
            )
        )
        connection.execute(text("INSERT INTO parent (id) VALUES (1)"))

    def valid(connection):
        connection.execute(text("INSERT INTO child (id, parent_id) VALUES (1, 1)"))

    assert apply_migrations(engine, [Migration("0001", "תקין", valid)]) == ["0001"]


# --------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "migration",
    [
        Migration("1", "גרסה קצרה", _add_summary),
        Migration("00001", "גרסה ארוכה", _add_summary),
        Migration("abcd", "גרסה לא מספרית", _add_summary),
        Migration("", "בלי גרסה", _add_summary),
        Migration("0001", "", _add_summary),
        Migration("0001", "   ", _add_summary),
        Migration("0001", "x" * 301, _add_summary),
        Migration("0001", "בלי פונקציה", None),
    ],
)
def test_malformed_migrations_are_refused_before_any_write(engine, migration):
    with pytest.raises(MigrationError):
        apply_migrations(engine, [migration])
    assert not inspect(engine).has_table(MIGRATIONS_TABLE)


def test_duplicate_versions_are_refused(engine):
    with pytest.raises(MigrationError, match="Duplicate"):
        apply_migrations(
            engine,
            [
                Migration("0001", "ראשונה", _add_summary),
                Migration("0001", "כפולה", _add_summary),
            ],
        )
    assert not inspect(engine).has_table(MIGRATIONS_TABLE)


def test_a_recorded_version_missing_from_the_codebase_is_reported(engine):
    _existing_table(engine)
    apply_migrations(engine, [Migration("0001", "הוסרה מאוחר יותר", _add_summary)])
    rows = migration_status(engine, [])
    assert rows == [
        {
            "version": "0001",
            "description": "(recorded, but not in this codebase)",
            "state": "unknown",
            "applied_at": rows[0]["applied_at"],
        }
    ]
    assert pending_migrations(engine, []) == []


def test_an_in_memory_database_migrates_on_the_same_engine():
    memory = build_engine("sqlite://")
    try:
        with memory.begin() as connection:
            connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY)"))
        migration = Migration(
            "0001",
            "זיכרון",
            lambda connection: connection.execute(
                text("ALTER TABLE sample ADD COLUMN note TEXT DEFAULT ''")
            ),
        )
        assert apply_migrations(memory, [migration]) == ["0001"]
        assert "note" in _columns(memory, "sample")
        assert set(applied_versions(memory)) == {"0001"}
    finally:
        memory.dispose()


# ------------------------------------------------------------------- wiring


def test_startup_applies_registered_migrations(engine, monkeypatch):
    _existing_table(engine)
    monkeypatch.setattr(
        "portal.migrations.MIGRATIONS",
        (Migration("0001", "רצה בהפעלה", _add_summary),),
    )
    init_db(engine)
    assert "summary" in _columns(engine)
    assert set(applied_versions(engine)) == {"0001"}


def test_the_app_fixture_applies_the_shipped_migration(app):
    with app.state.session_factory() as db:
        rows = db.execute(text(f"SELECT version FROM {MIGRATIONS_TABLE}")).all()
    assert [row[0] for row in rows] == ["0001", "0002"]


# ---------------------------------------------------------------------- CLI


def _url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_cli_status_is_read_only(engine, capsys):
    _existing_table(engine)
    assert migrate_cli.main(["--database-url", _url(engine)]) == 0
    out = capsys.readouterr().out
    assert "0001" in out and "pending" in out
    assert not inspect(engine).has_table(MIGRATIONS_TABLE)


def test_cli_dry_run_lists_pending_without_applying(engine, capsys, monkeypatch):
    _existing_table(engine)
    monkeypatch.setattr(
        "portal.migrations.MIGRATIONS",
        (Migration("0001", "הוספת תקציר", _add_summary),),
    )
    assert migrate_cli.main(["--database-url", _url(engine), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "0001" in out and "pending" in out and "1 migration(s) would be applied" in out
    assert "summary" not in _columns(engine)
    assert not inspect(engine).has_table(MIGRATIONS_TABLE)


def test_cli_apply_then_reports_up_to_date(engine, capsys, monkeypatch):
    _existing_table(engine)
    monkeypatch.setattr(
        "portal.migrations.MIGRATIONS",
        (Migration("0001", "הוספת תקציר", _add_summary),),
    )
    assert migrate_cli.main(["--database-url", _url(engine), "--apply"]) == 0
    assert "applied 0001" in capsys.readouterr().out
    assert "summary" in _columns(engine)

    assert migrate_cli.main(["--database-url", _url(engine), "--apply"]) == 0
    assert "No pending migrations" in capsys.readouterr().out


def test_cli_reports_a_failed_migration_with_exit_code_1(engine, capsys, monkeypatch):
    _existing_table(engine)

    def broken(connection):
        raise RuntimeError("שגיאה")

    monkeypatch.setattr(
        "portal.migrations.MIGRATIONS", (Migration("0001", "שבורה", broken),)
    )
    assert migrate_cli.main(["--database-url", _url(engine), "--apply"]) == 1
    assert "migration problem" in capsys.readouterr().out
    assert applied_versions(engine) == {}


def test_cli_rejects_apply_and_dry_run_together(engine):
    with pytest.raises(SystemExit) as excinfo:
        migrate_cli.main(["--database-url", _url(engine), "--apply", "--dry-run"])
    assert excinfo.value.code == 2