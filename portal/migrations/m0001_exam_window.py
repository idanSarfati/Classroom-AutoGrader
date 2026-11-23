"""0001 - the exam window on ``portal_assignments``.

``exam_config`` names a definition in ``portal/exams``; ``opens_at`` and
``closes_at`` are the enforced sitting window. Ordinary homework leaves all
three empty, so nothing about existing assignments changes.

The column check matters: ``init_db`` runs ``create_all`` first, so on a brand
new database these columns already exist and there is nothing to add. On the
live database the table is old and the columns are missing. Both paths must
succeed, and both must end with the version recorded.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

DESCRIPTION = "Add exam_config, opens_at and closes_at to portal_assignments"

# (column, DDL type + default). A constant default is required: SQLite cannot
# add a NOT NULL column without one, and existing homework rows must keep
# working with no exam set.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("exam_config", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("opens_at", "DATETIME"),
    ("closes_at", "DATETIME"),
)


def upgrade(connection: Connection) -> None:
    existing = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(portal_assignments)"))
    }
    if not existing:
        # create_all did not run for this table. Nothing sensible to alter.
        raise RuntimeError("portal_assignments does not exist")
    for column, declaration in _COLUMNS:
        if column in existing:
            continue
        connection.execute(
            text(f"ALTER TABLE portal_assignments ADD COLUMN {column} {declaration}")
        )