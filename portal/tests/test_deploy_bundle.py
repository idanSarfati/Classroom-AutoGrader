"""The deploy bundle must carry the data on first install and never on update."""
from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from portal.scripts import build_deploy_bundle as bundle


@pytest.fixture
def fake_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    for name in ("portal", "grading_engine", "configs", "assignments"):
        (root / name).mkdir(parents=True)
        (root / name / "module.py").write_text("x = 1\n", encoding="utf-8")
    (root / "portal" / "deploy").mkdir()
    (root / "portal" / "deploy" / "setup_server.sh").write_text(
        "#!/usr/bin/env bash\n", encoding="utf-8"
    )
    (root / "requirements.txt").write_text("x\n", encoding="utf-8")
    (root / ".env").write_text("GROQ_API_KEY=test\n", encoding="utf-8")
    (root / "assignments" / "lesson_1.json").write_text(
        '{"tasks": []}', encoding="utf-8"
    )

    data = root / "portal" / "data"
    data.mkdir()
    (data / "secret_key").write_text("secret", encoding="utf-8")
    (data / "google_token.json").write_text("{}", encoding="utf-8")
    (data / "portal.db.bak-old").write_text("backup", encoding="utf-8")
    (root / "portal" / "content" / "blobs").mkdir(parents=True)
    (root / "portal" / "content" / "blobs" / "abc").write_bytes(b"blob")

    db = data / "portal.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE portal_assignments (id INTEGER PRIMARY KEY, "
        "rubric_path TEXT, rubric_sha256 TEXT)"
    )
    connection.execute(
        "CREATE TABLE portal_users (id INTEGER PRIMARY KEY, role TEXT)"
    )
    connection.execute(
        "INSERT INTO portal_assignments VALUES (1, ?, NULL)",
        (str(root / "assignments" / "lesson_1.json"),),
    )
    connection.execute("INSERT INTO portal_users VALUES (1, 'student')")
    connection.commit()
    connection.close()

    monkeypatch.setattr(bundle, "PROJECT_ROOT", root)
    monkeypatch.setattr(bundle, "DATA_DIR", data)
    return root


def _names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tar:
        return tar.getnames()


def test_first_install_carries_snapshot_and_secrets(
    fake_project: Path, tmp_path: Path
) -> None:
    out = bundle.build(tmp_path / "deploy.tar.gz")
    names = _names(out)
    assert "clasroom-autograder/portal/data/portal.db" in names
    assert "clasroom-autograder/portal/data/secret_key" in names
    assert "clasroom-autograder/portal/data/google_token.json" in names
    assert "clasroom-autograder/.env" in names
    assert not any(".bak-old" in name for name in names)
    assert "clasroom-autograder/portal/deploy/setup_server.sh" in names
    assert "clasroom-autograder/portal/content/blobs/abc" in names

    with tarfile.open(out) as tar:
        modes = {
            member.name.rsplit("/", 1)[-1]: member.mode
            for member in tar.getmembers()
        }
        snapshot = tar.extractfile(
            "clasroom-autograder/portal/data/portal.db"
        ).read()
    assert modes["secret_key"] == 0o600
    assert modes[".env"] == 0o600
    assert modes["setup_server.sh"] == 0o755
    assert modes["module.py"] == 0o644
    assert snapshot

    # The recorded Windows path is replaced by the portable form.
    snapshot_db = tmp_path / "snapshot.db"
    snapshot_db.write_bytes(snapshot)
    connection = sqlite3.connect(snapshot_db)
    stored = connection.execute(
        "SELECT rubric_path FROM portal_assignments WHERE id = 1"
    ).fetchone()[0]
    connection.close()
    assert stored == "assignments/lesson_1.json"


def test_code_only_bundle_never_ships_data(
    fake_project: Path, tmp_path: Path
) -> None:
    out = bundle.build(tmp_path / "update.tar.gz", code_only=True)
    names = _names(out)
    assert not any("/portal/data/" in name for name in names)
    assert "clasroom-autograder/grading_engine/module.py" in names
    assert "clasroom-autograder/portal/deploy/setup_server.sh" in names
    assert "clasroom-autograder/requirements.txt" in names
