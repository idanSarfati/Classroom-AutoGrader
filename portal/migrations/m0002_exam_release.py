"""0002 - the exam result release state on ``portal_assignments``.

An exam is marked the moment it is handed in, but the student must not see the
score or the paper until the teacher releases it. ``exam_release`` is one of
``held`` (default), ``scores`` (the number only) or ``full`` (score, feedback,
per-question detail and the student's own paper).

The safe default is deliberate: existing rows - and every row a future deploy
adds - start hidden, so a class that sits early cannot leak the paper to a
class that sits days later. Homework ignores the column entirely.

The column check matters: ``init_db`` runs ``create_all`` first, so on a brand
new database the column already exists and there is nothing to add. On the
live database the table is old and the column is missing. Both paths must
succeed, and both must end with the version recorded.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

DESCRIPTION = "Add exam_release to portal_assignments (held/scores/full)"

_COLUMN = "exam_release"
_DECLARATION = "VARCHAR(16) NOT NULL DEFAULT 'held'"


def upgrade(connection: Connection) -> None:
    existing = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(portal_assignments)"))
    }
    if not existing:
        # create_all did not run for this table. Nothing sensible to alter.
        raise RuntimeError("portal_assignments does not exist")
    if _COLUMN not in existing:
        connection.execute(
            text(
                f"ALTER TABLE portal_assignments ADD COLUMN {_COLUMN} {_DECLARATION}"
            )
        )
    # An empty string is not one of the three states, and reading it as "held"
    # is only a fallback. Make the stored value explicit.
    connection.execute(
        text(
            "UPDATE portal_assignments SET exam_release = 'held' "
            "WHERE exam_release IS NULL OR exam_release = ''"
        )
    )
