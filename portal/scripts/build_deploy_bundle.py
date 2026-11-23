"""Pack everything a Linux server needs to run the portal, into one archive.

The portal is a plain Python application: the project tree, a SQLAlchemy
database, a signing key and the imported Classroom blobs. This script collects
exactly those pieces and leaves behind the things that must not travel:

* backups (``portal.db.bak-*``), logs and the public-URL file;
* roster/mapping CSVs and reports (private, and not needed on the server);
* the local ``.venv``, caches and ``__pycache__`` directories.

The database is copied with ``VACUUM INTO``, which produces a consistent
snapshot while the local server keeps running. The snapshot - never the live
database - then has its rubric paths rewritten by
:mod:`portal.scripts.normalize_rubric_paths`, because the live values are
Windows absolute paths that do not exist on Linux.

The archive root is ``clasroom-autograder/`` and it includes the teacher's
``.env`` (API keys). Treat the resulting file as a secret: it is written under
the git-ignored ``portal/data/_tmp/``.

Usage::

    python -m portal.scripts.build_deploy_bundle              # first install
    python -m portal.scripts.build_deploy_bundle --code-only  # later updates

``--code-only`` leaves out ``portal/data`` entirely, so extracting an update
over the running server cannot replace its live database.
"""
from __future__ import annotations

import argparse
import io
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from portal.paths import DATA_DIR, PROJECT_ROOT
from portal.scripts.normalize_rubric_paths import (
    STATUS_UNRESOLVED,
    normalize_database,
)

ARCHIVE_ROOT = "clasroom-autograder"

# Project directories that make up the runnable application. ``src/`` and
# ``config/`` are the grading engine the portal imports read-only; the rubric
# files are resolved from ``assignments/`` and ``configs/``.
TREE_DIRS = ("portal", "src", "config", "configs", "assignments")
TREE_FILES = ("requirements.txt", ".env")

# Portal data that must travel: the snapshot and the two credentials the
# portal itself uses (session secret and the portal's copy of the Google
# token). Everything else in portal/data stays on the laptop.
DATA_FILES = ("secret_key", "google_token.json")

EXCLUDED_DIR_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}
)
EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def _iter_tree_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        if root == PROJECT_ROOT / "portal":
            # Data is handled below; other portal subdirectories are included.
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] == "data":
                continue
        files.append(path)
    return files


def _archive_mode(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Give every entry a deliberate Unix mode.

    Windows has no POSIX execute bit, so archives built there otherwise land
    on the server as world-writable files with no executable script. Secrets
    stay owner-only; scripts and the database keep the modes the server
    expects before ``setup_server.sh`` re-applies permissions.
    """
    name = info.name.rsplit("/", 1)[-1]
    if name in ("secret_key", "google_token.json", ".env"):
        info.mode = 0o600
    elif name.endswith(".sh"):
        info.mode = 0o755
    else:
        info.mode = 0o644
    return info


def snapshot_database(db_path: Path, target: Path) -> None:
    """Write a consistent copy of ``db_path`` to ``target``."""
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    if target.exists():
        target.unlink()
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        # VACUUM INTO takes a filename literal; doubling quotes keeps a path
        # containing an apostrophe valid.
        literal = str(target).replace("'", "''")
        connection.execute(f"VACUUM INTO '{literal}'")
    finally:
        connection.close()

    check = sqlite3.connect(target)
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        raise RuntimeError(f"snapshot failed integrity check: {verdict}")


def _counts(db_path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(db_path)
    try:
        assignments = connection.execute(
            "SELECT COUNT(*) FROM portal_assignments"
        ).fetchone()[0]
        students = connection.execute(
            "SELECT COUNT(*) FROM portal_users WHERE role = 'student'"
        ).fetchone()[0]
    finally:
        connection.close()
    return assignments, students


def build(out: Path, *, db_path: Path | None = None, code_only: bool = False) -> Path:
    db_path = db_path or (DATA_DIR / "portal.db")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise FileExistsError(f"{out} already exists; pass --force to replace")

    workdir = DATA_DIR / "_tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot = workdir / "bundle-portal.db"
    if code_only and snapshot.exists():
        snapshot.unlink()
    if not code_only:
        snapshot_database(db_path, snapshot)

    changes = [] if code_only else normalize_database(snapshot, apply=True)
    unresolved = [change for change in changes if change.status == STATUS_UNRESOLVED]
    if unresolved:
        ids = ", ".join(str(change.assignment_id) for change in unresolved)
        raise RuntimeError(
            "rubric paths outside assignments/ and configs/ cannot be made "
            f"portable; assignment ids: {ids}"
        )
    rewrote = sum(1 for change in changes if change.needs_write)
    verified = sum(1 for change in changes if change.sha_matches is True)
    mismatched = sum(1 for change in changes if change.sha_matches is False)

    manifest_lines = [
        f"Built: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Source: {PROJECT_ROOT}",
        "Contents: code only (the server's database is not replaced)"
        if code_only
        else "Contents: code + database snapshot (first install)",
    ]
    if not code_only:
        assignments, students = _counts(snapshot)
        manifest_lines += [
            f"Assignments: {assignments}",
            f"Student accounts: {students}",
            f"Rubric paths made portable: {rewrote}",
            f"Rubric hashes verified: {verified}",
            f"Rubric hash mismatches: {mismatched}",
        ]
    manifest_lines += [
        "",
        "Extract into /opt/clasroom-autograder and read "
        "portal/docs/deploy-linux.md.",
        "",
    ]
    manifest = "\n".join(manifest_lines)

    files: list[tuple[Path, str]] = []
    for name in TREE_DIRS:
        root = PROJECT_ROOT / name
        if not root.is_dir():
            raise FileNotFoundError(f"missing project directory: {root}")
        for path in _iter_tree_files(root):
            files.append((path, f"{ARCHIVE_ROOT}/{path.relative_to(PROJECT_ROOT).as_posix()}"))
    for name in TREE_FILES:
        path = PROJECT_ROOT / name
        if path.is_file():
            files.append((path, f"{ARCHIVE_ROOT}/{name}"))

    data_files: list[tuple[Path, str]] = []
    if not code_only:
        data_files.append((snapshot, f"{ARCHIVE_ROOT}/portal/data/portal.db"))
        for name in DATA_FILES:
            path = DATA_DIR / name
            if path.is_file():
                data_files.append((path, f"{ARCHIVE_ROOT}/portal/data/{name}"))

    with tarfile.open(out, "w:gz") as archive:
        for path, arcname in files + data_files:
            archive.add(
                path, arcname=arcname, recursive=False, filter=_archive_mode
            )
        manifest_bytes = manifest.encode("utf-8")
        info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/DEPLOY-INFO.txt")
        info.size = len(manifest_bytes)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        archive.addfile(info, io.BytesIO(manifest_bytes))

    snapshot.unlink(missing_ok=True)
    return out


def main(argv: list[str] | None = None) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="archive path (default: portal/data/_tmp/portal-deploy-<date>.tar.gz)",
    )
    parser.add_argument(
        "--code-only",
        action="store_true",
        help=(
            "ship code without the database, secret key or Google token; use "
            "this for updates so the server's live data is never overwritten"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing archive",
    )
    args = parser.parse_args(argv)
    if args.out is None:
        kind = "portal-code" if args.code_only else "portal-deploy"
        args.out = DATA_DIR / "_tmp" / f"{kind}-{stamp}.tar.gz"
    out = args.out
    if out.exists() and args.force:
        out.unlink()
    try:
        result = build(out, code_only=args.code_only)
    except (FileExistsError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 2
    size_mb = result.stat().st_size / (1024 * 1024)
    print(f"bundle: {result}  ({size_mb:.1f} MB)")
    print("copy it to the server, then follow portal/docs/deploy-linux.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
