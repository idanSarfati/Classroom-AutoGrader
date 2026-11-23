"""Rewrite stored rubric paths so they work on any machine.

``portal_assignments.rubric_path`` is written when the teacher publishes an
assignment and holds the path the grader will open. On the teacher's Windows
laptop that path is absolute::

    C:\\Users\\...\\Clasroom AutoGrader\\assignments\\lesson_3.json

The file itself lives *inside* ``assignments/`` or ``configs/``, and
:func:`portal.grading.adapter.load_rubric` resolves relative paths against the
project root and confines them to those two directories. The portable form is
therefore the path inside the root::

    assignments/lesson_3.json

This script rewrites only the stored strings. The rubric files keep their
bytes, so the SHA-256 fingerprint recorded at publish time stays valid. It is
dry-run by default; ``--apply`` writes. The deploy bundle builder
(:mod:`portal.scripts.build_deploy_bundle`) runs it on the database *snapshot*
so the live database is never touched by a deployment.

Usage::

    python -m portal.scripts.normalize_rubric_paths
    python -m portal.scripts.normalize_rubric_paths --apply
    python -m portal.scripts.normalize_rubric_paths --db portal/data/_tmp/snapshot.db --apply
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from portal.paths import DATA_DIR, PROJECT_ROOT

# Directories the adapter accepts (mirrors portal.paths.RUBRIC_ROOTS without
# importing the grading stack, so this runs on a bare snapshot).
RUBRIC_DIRS = ("assignments", "configs")

STATUS_EMPTY = "empty"
STATUS_PORTABLE = "portable"
STATUS_REWRITE = "rewrite"
STATUS_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class PathChange:
    """One assignment row and what should happen to its rubric path."""

    assignment_id: int
    old: str
    new: str | None
    status: str
    sha_matches: bool | None = None

    @property
    def needs_write(self) -> bool:
        return self.status == STATUS_REWRITE and self.new is not None


def portable_path(value: str | None, project_root: Path = PROJECT_ROOT) -> str | None:
    """Return the project-relative path, or ``None`` when it cannot be resolved.

    Any prefix is discarded: the first path component named ``assignments`` or
    ``configs`` starts the portable path, which must then exist under
    ``project_root``. Parent traversal (``..``) and empty segments are
    rejected, so a crafted value cannot point outside the allowed roots.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    for index, part in enumerate(parts):
        if part not in RUBRIC_DIRS:
            continue
        candidate = "/".join(parts[index:])
        if (project_root / candidate).is_file():
            return candidate
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_row(
    assignment_id: int,
    old: str | None,
    sha256: str | None,
    project_root: Path = PROJECT_ROOT,
) -> PathChange:
    raw = (old or "").strip()
    if not raw:
        return PathChange(assignment_id, "", None, STATUS_EMPTY)
    portable = portable_path(raw, project_root)
    if portable is None:
        return PathChange(assignment_id, raw, None, STATUS_UNRESOLVED)
    expected = (sha256 or "").strip().lower()
    actual = _sha256(project_root / portable) if expected else None
    sha_ok = None if expected == "" else (actual == expected)
    status = STATUS_PORTABLE if portable == raw else STATUS_REWRITE
    return PathChange(assignment_id, raw, portable, status, sha_ok)


def normalize_database(
    db_path: Path | str,
    *,
    apply: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> list[PathChange]:
    """Inspect every assignment; rewrite the paths when ``apply`` is true.

    The caller owns the file: pass the database to change, not a shared one.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    changes: list[PathChange] = []
    connection = sqlite3.connect(path)
    try:
        try:
            cursor = connection.execute(
                "SELECT id, rubric_path, rubric_sha256 "
                "FROM portal_assignments ORDER BY id"
            )
        except sqlite3.OperationalError as exc:
            raise RuntimeError(f"not a portal database: {path}: {exc}") from exc
        for assignment_id, old, sha in cursor.fetchall():
            change = inspect_row(assignment_id, old, sha, project_root)
            changes.append(change)
            if apply and change.needs_write:
                connection.execute(
                    "UPDATE portal_assignments SET rubric_path = ? WHERE id = ?",
                    (change.new, assignment_id),
                )
        if apply:
            connection.commit()
    finally:
        connection.close()
    return changes


def _summarise(changes: list[PathChange], *, applied: bool, db_path: Path) -> int:
    counts = {
        STATUS_EMPTY: 0,
        STATUS_PORTABLE: 0,
        STATUS_REWRITE: 0,
        STATUS_UNRESOLVED: 0,
    }
    for change in changes:
        counts[change.status] += 1
    mode = "applied" if applied else "dry run"
    print(f"{db_path} ({mode})")
    for change in changes:
        if change.status in (STATUS_EMPTY, STATUS_PORTABLE):
            continue
        target = change.new if change.new else "<unresolved>"
        print(f"  id {change.assignment_id}: {change.old} -> {target}")
        if change.sha_matches is False:
            print(
                "    warning: the file's SHA-256 no longer matches the value "
                "recorded at publish; publishing again is required before "
                "this assignment can be graded."
            )
    print(
        f"{counts[STATUS_EMPTY]} without a rubric, "
        f"{counts[STATUS_PORTABLE]} already portable, "
        f"{counts[STATUS_REWRITE]} to rewrite, "
        f"{counts[STATUS_UNRESOLVED]} unresolved."
    )
    if not applied and counts[STATUS_REWRITE]:
        print("Run again with --apply to write the portable paths.")
    if counts[STATUS_UNRESOLVED] or any(
        change.sha_matches is False for change in changes
    ):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        type=Path,
        default=DATA_DIR / "portal.db",
        help="SQLite database to inspect (default: portal/data/portal.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the portable paths; without this flag nothing changes",
    )
    args = parser.parse_args(argv)
    try:
        changes = normalize_database(args.db, apply=args.apply)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return _summarise(changes, applied=args.apply, db_path=args.db)


if __name__ == "__main__":
    raise SystemExit(main())
