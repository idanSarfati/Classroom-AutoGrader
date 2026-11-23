"""Phase 7 importers: school directory (אלפון) and Classroom history.

The package is intentionally separate from :mod:`portal.ingestion`:

* ``portal.ingestion`` pulls *content* from Google Classroom at the teacher's
  request and stores snapshots;
* ``portal.imports`` reads *files the school already exports* (the alephon
  workbook and a Classroom grade CSV) and produces plan objects that a CLI can
  preview before any database write.

Nothing in this package talks to Google, and nothing outside ``portal/`` is
imported or modified.
"""
from __future__ import annotations

__all__ = [
    "matching",
    "sources",
]
