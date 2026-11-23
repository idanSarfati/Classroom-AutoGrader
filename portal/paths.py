"""Filesystem locations used by the portal.

Everything the portal writes lives under ``portal/`` (database, generated
secret key, logs). The project root is only ever *read*.
"""
from __future__ import annotations

from pathlib import Path

PORTAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PORTAL_ROOT.parent

TEMPLATES_DIR = PORTAL_ROOT / "templates"
STATIC_DIR = PORTAL_ROOT / "static"
DATA_DIR = PORTAL_ROOT / "data"

# Imported Classroom media (slide images, PDFs, DOCX). Files are stored by
# SHA-256 and served through authenticated routes, never as public static
# files. The directory is git-ignored.
CONTENT_DIR = PORTAL_ROOT / "content"

# Rubric files are teacher-authored inputs that already exist in the project.
# The portal may bind assignments to them, but only below these roots - never
# to an arbitrary path such as ``.env`` or ``credentials.json``.
RUBRIC_ROOTS: tuple[Path, ...] = (
    PROJECT_ROOT / "assignments",
    PROJECT_ROOT / "configs",
)
