"""Phase 2.5: import Google Classroom content into the portal.

Read-only boundary
------------------
:mod:`portal.ingestion.classroom_source` is the only module in the ingestion
package that imports ``src.*`` (plus :mod:`portal.ingestion.google_auth`,
which only borrows the existing OAuth client configuration). Everything else
works on plain, normalized snapshots, so the importer can be tested without a
network or a Google account and no raw Drive payload can reach a template.

Pipeline
--------
``classroom_source`` -> ``types`` -> ``normalize`` / ``converter`` / ``deck``
-> ``importer`` -> portal database + ``portal/content/blobs``.

Nothing here writes to ``src/``, ``app.py``, ``config/`` or the existing
tests, and no Drive id or Google URL is ever stored on a student-facing row.
"""
