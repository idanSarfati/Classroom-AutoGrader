"""Rubric paths must survive the move from the Windows laptop to a server."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from portal.scripts.normalize_rubric_paths import (
    STATUS_EMPTY,
    STATUS_PORTABLE,
    STATUS_REWRITE,
    STATUS_UNRESOLVED,
    normalize_database,
    portable_path,
)

WINDOWS_ROOT = r"C:\Users\Idan Sarfati\Desktop\projects\Clasroom AutoGrader"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "assignments").mkdir()
    (tmp_path / "configs").mkdir()
    (tmp_path / "assignments" / "lesson_3.json").write_text(
        '{"tasks": []}', encoding="utf-8"
    )
    (tmp_path / "configs" / "units_1_2_spec.json").write_text(
        '{"units": []}', encoding="utf-8"
    )
    return tmp_path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "portal.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE portal_assignments ("
        "id INTEGER PRIMARY KEY, "
        "rubric_path TEXT, "
        "rubric_sha256 TEXT)"
    )
    connection.commit()
    connection.close()
    return path


def test_windows_absolute_path_becomes_project_relative(project: Path) -> None:
    assert (
        portable_path(WINDOWS_ROOT + r"\assignments\lesson_3.json", project)
        == "assignments/lesson_3.json"
    )
    assert (
        portable_path(WINDOWS_ROOT + r"\configs\units_1_2_spec.json", project)
        == "configs/units_1_2_spec.json"
    )


def test_portable_paths_survive_untouched(project: Path) -> None:
    assert (
        portable_path("assignments/lesson_3.json", project)
        == "assignments/lesson_3.json"
    )
    assert (
        portable_path("configs/units_1_2_spec.json", project)
        == "configs/units_1_2_spec.json"
    )


def test_unknown_files_and_traversal_are_rejected(project: Path) -> None:
    assert portable_path("", project) is None
    assert portable_path(None, project) is None
    assert portable_path(WINDOWS_ROOT + r"\assignments\missing.json", project) is None
    # A path that escapes the allowed directory must not be made portable,
    # even when the target file exists.
    (project / "secret.txt").write_text("x", encoding="utf-8")
    assert (
        portable_path(
            WINDOWS_ROOT + r"\configs\..\..\secret.txt", project
        )
        is None
    )


def test_apply_rewrites_only_resolvable_rows(project: Path, tmp_path: Path) -> None:
    db = _database(tmp_path)
    good_sha = _sha(project / "assignments" / "lesson_3.json")
    connection = sqlite3.connect(db)
    connection.executemany(
        "INSERT INTO portal_assignments (id, rubric_path, rubric_sha256) "
        "VALUES (?, ?, ?)",
        [
            (1, WINDOWS_ROOT + r"\assignments\lesson_3.json", good_sha),
            (2, "assignments/lesson_3.json", good_sha),
            (3, WINDOWS_ROOT + r"\assignments\missing.json", "0" * 64),
            (4, "", None),
        ],
    )
    connection.commit()
    connection.close()

    preview = normalize_database(db, apply=False, project_root=project)
    assert [change.status for change in preview] == [
        STATUS_REWRITE,
        STATUS_PORTABLE,
        STATUS_UNRESOLVED,
        STATUS_EMPTY,
    ]
    assert preview[0].sha_matches is True
    with sqlite3.connect(db) as check:
        stored = check.execute(
            "SELECT rubric_path FROM portal_assignments WHERE id = 1"
        ).fetchone()[0]
    assert stored == WINDOWS_ROOT + r"\assignments\lesson_3.json"

    applied = normalize_database(db, apply=True, project_root=project)
    assert applied[0].status == STATUS_REWRITE
    with sqlite3.connect(db) as check:
        rows = dict(
            check.execute("SELECT id, rubric_path FROM portal_assignments")
        )
    assert rows[1] == "assignments/lesson_3.json"
    assert rows[2] == "assignments/lesson_3.json"
    assert rows[3] == WINDOWS_ROOT + r"\assignments\missing.json"
    assert rows[4] == ""


def test_hash_mismatch_is_reported_but_path_is_still_fixed(
    project: Path, tmp_path: Path
) -> None:
    db = _database(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO portal_assignments (id, rubric_path, rubric_sha256) "
        "VALUES (1, ?, ?)",
        (WINDOWS_ROOT + r"\assignments\lesson_3.json", "0" * 64),
    )
    connection.commit()
    connection.close()

    changes = normalize_database(db, apply=False, project_root=project)
    assert changes[0].status == STATUS_REWRITE
    assert changes[0].sha_matches is False


def test_missing_database_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        normalize_database(tmp_path / "absent.db")
