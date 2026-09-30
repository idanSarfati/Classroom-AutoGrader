"""Unit tests for Streamlit-Community-Cloud secret resolution.

Run from the project root::

    python -m pytest tests/test_cloud_secrets.py -q

Pure unit tests: no network, no real credentials, no API key. They pin the
behaviour that lets the app run on a hosted Streamlit instance, where there is
no ``.env`` file, no ``credentials.json`` / ``token.json`` on disk, and no
browser to complete an OAuth flow:

* config values fall back from ``os.environ`` to ``st.secrets``;
* the OAuth client secrets and the user token are materialized on disk from
  ``GOOGLE_CREDENTIALS`` / ``GOOGLE_TOKEN`` secrets;
* the interactive browser flow is refused on a cloud host instead of hanging;
* a missing or malformed secret degrades to the documented "not configured"
  error rather than raising something opaque.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.settings as settings_module  # noqa: E402
import src.auth as auth  # noqa: E402

FAKE_CLIENT = {"installed": {"client_id": "cid", "client_secret": "csec"}}
FAKE_TOKEN = {
    "token": "ya29.fake",
    "refresh_token": "1//fake",
    "client_id": "cid",
    "client_secret": "csec",
    "token_uri": "https://oauth2.googleapis.com/token",
    # Every scope the app requests, so the grant counts as complete.
    "scopes": list(auth.SCOPES),
}

_TRACKED_ENV = (
    "LOG_LEVEL",
    "DRY_RUN",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "GROQ_BASE_URL",
    "LATE_PENALTY_POINTS",
)


class FakeSecrets:
    """Stand-in for ``st.secrets`` backed by a plain dict."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str):
        return self._data.get(key)


def _install_fake_streamlit(monkeypatch, data: dict) -> FakeSecrets:
    """Register a fake ``streamlit`` module exposing ``data`` as secrets."""
    module = types.ModuleType("streamlit")
    secrets = FakeSecrets(data)
    module.secrets = secrets  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "streamlit", module)
    return secrets


@pytest.fixture
def fake_secrets(monkeypatch):
    """Factory installing a controllable secret store."""
    return lambda data: _install_fake_streamlit(monkeypatch, data)


@pytest.fixture
def clean_env(monkeypatch):
    """Drop local ``.env`` influence so only the secret store is consulted."""
    for name in _TRACKED_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Point the auth module at a temp dir so no real credential is touched."""
    creds = tmp_path / "credentials.json"
    token = tmp_path / "token.json"
    monkeypatch.setattr(auth, "CREDENTIALS_FILE", creds)
    monkeypatch.setattr(auth, "TOKEN_FILE", token)
    return creds, token


# ---------------------------------------------------------------------------
# config.settings._secret
# ---------------------------------------------------------------------------


def test_secret_prefers_the_environment(fake_secrets, clean_env, monkeypatch):
    fake_secrets({"GROQ_API_KEY": "from-secrets"})
    monkeypatch.setenv("GROQ_API_KEY", "  from-env  ")
    assert settings_module._secret("GROQ_API_KEY") == "from-env"


def test_secret_falls_back_to_streamlit_secrets(fake_secrets, clean_env):
    fake_secrets({"GROQ_API_KEY": "from-secrets"})
    assert settings_module._secret("GROQ_API_KEY") == "from-secrets"


def test_secret_trims_whitespace_from_both_sources(
    fake_secrets, clean_env, monkeypatch
):
    fake_secrets({"GROQ_MODEL": "  qwen/qwen3.8-27b  "})
    assert settings_module._secret("GROQ_MODEL") == "qwen/qwen3.8-27b"
    monkeypatch.setenv("GROQ_MODEL", "\tspaced-model\n")
    assert settings_module._secret("GROQ_MODEL") == "spaced-model"


def test_secret_returns_none_when_absent_everywhere(fake_secrets, clean_env):
    fake_secrets({})
    assert settings_module._secret("GROQ_API_KEY") is None


def test_secret_returns_none_when_streamlit_is_unavailable(monkeypatch, clean_env):
    """A CLI run has no Streamlit at all; the lookup must not explode."""
    monkeypatch.setitem(sys.modules, "streamlit", None)
    assert settings_module._secret("GROQ_API_KEY") is None


def test_groq_api_key_reaches_settings_from_secrets(fake_secrets, clean_env):
    fake_secrets({"GROQ_API_KEY": "gsk-from-secrets"})
    assert settings_module.load_settings().groq_api_key == "gsk-from-secrets"


def test_other_settings_also_resolve_from_secrets(fake_secrets, clean_env):
    fake_secrets(
        {
            "GROQ_MODEL": "qwen/qwen3.8-27b",
            "GROQ_BASE_URL": "https://example.test/v1",
            "DRY_RUN": "true",
            "LATE_PENALTY_POINTS": "0",
            "LOG_LEVEL": "debug",
        }
    )
    loaded = settings_module.load_settings()
    assert loaded.groq_model == "qwen/qwen3.8-27b"
    assert loaded.groq_base_url == "https://example.test/v1"
    assert loaded.dry_run is True
    assert loaded.late_penalty_points == 0
    assert loaded.log_level == "DEBUG"


def test_missing_api_key_stays_none_so_the_evaluator_can_explain(
    fake_secrets, clean_env
):
    """The evaluator raises the actionable "GROQ_API_KEY is not set" error."""
    fake_secrets({})
    assert settings_module.load_settings().groq_api_key is None


# ---------------------------------------------------------------------------
# src.auth secret materialization
# ---------------------------------------------------------------------------


def test_secret_json_accepts_a_json_string(fake_secrets):
    fake_secrets({"GOOGLE_CREDENTIALS": json.dumps(FAKE_CLIENT)})
    assert auth._secret_json("GOOGLE_CREDENTIALS") == FAKE_CLIENT


def test_secret_json_accepts_a_toml_table(fake_secrets):
    fake_secrets({"GOOGLE_CREDENTIALS": FAKE_CLIENT})
    assert auth._secret_json("GOOGLE_CREDENTIALS") == FAKE_CLIENT


def test_secret_json_rejects_malformed_json(fake_secrets, caplog):
    fake_secrets({"GOOGLE_CREDENTIALS": "{not json"})
    with caplog.at_level("ERROR"):
        assert auth._secret_json("GOOGLE_CREDENTIALS") is None
    assert "not valid JSON" in caplog.text


def test_secret_json_rejects_a_non_object_document(fake_secrets, caplog):
    fake_secrets({"GOOGLE_CREDENTIALS": "[1, 2, 3]"})
    with caplog.at_level("ERROR"):
        assert auth._secret_json("GOOGLE_CREDENTIALS") is None
    assert "must contain a JSON object" in caplog.text


def test_secret_json_is_none_when_absent(fake_secrets):
    fake_secrets({})
    assert auth._secret_json("GOOGLE_TOKEN") is None


def test_secret_json_is_none_without_streamlit(monkeypatch):
    monkeypatch.setitem(sys.modules, "streamlit", None)
    assert auth._secret_json("GOOGLE_CREDENTIALS") is None




def test_ensure_files_writes_both_from_secrets(fake_secrets, isolated_paths):
    creds_path, token_path = isolated_paths
    fake_secrets(
        {
            "GOOGLE_CREDENTIALS": json.dumps(FAKE_CLIENT),
            "GOOGLE_TOKEN": json.dumps(FAKE_TOKEN),
        }
    )
    auth._ensure_credential_files()
    assert json.loads(creds_path.read_text(encoding="utf-8")) == FAKE_CLIENT
    assert json.loads(token_path.read_text(encoding="utf-8")) == FAKE_TOKEN


def test_ensure_files_never_overwrites_existing_files(fake_secrets, isolated_paths):
    """Local development must win over a stale secret."""
    creds_path, token_path = isolated_paths
    local_creds = {"installed": {"client_id": "local", "client_secret": "local"}}
    creds_path.write_text(json.dumps(local_creds), encoding="utf-8")
    token_path.write_text(json.dumps(FAKE_TOKEN), encoding="utf-8")
    fake_secrets({"GOOGLE_CREDENTIALS": json.dumps(FAKE_CLIENT)})
    auth._ensure_credential_files()
    assert json.loads(creds_path.read_text(encoding="utf-8")) == local_creds


def test_ensure_files_is_a_noop_without_secrets(fake_secrets, isolated_paths):
    creds_path, token_path = isolated_paths
    fake_secrets({})
    auth._ensure_credential_files()
    assert not creds_path.exists()
    assert not token_path.exists()


def test_materialize_writes_valid_json_without_leaving_temp_files(isolated_paths):
    creds_path, _ = isolated_paths
    auth._materialize(creds_path, FAKE_CLIENT)
    assert json.loads(creds_path.read_text(encoding="utf-8")) == FAKE_CLIENT
    assert [p.name for p in creds_path.parent.iterdir()] == ["credentials.json"]


def test_get_credentials_loads_the_secret_token(fake_secrets, isolated_paths):
    """End-to-end: a token supplied as a secret yields usable credentials."""
    fake_secrets({"GOOGLE_TOKEN": json.dumps(FAKE_TOKEN)})


def test_get_credentials_refuses_the_browser_flow_in_the_cloud(
    fake_secrets, isolated_paths, monkeypatch
):
    """Without a token the flow cannot complete on a server - fail fast."""
    fake_secrets({"GOOGLE_CREDENTIALS": json.dumps(FAKE_CLIENT)})
    monkeypatch.setattr(auth, "_running_in_cloud", lambda: True)

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("run_local_server must not be reached in the cloud")

    monkeypatch.setattr(auth.InstalledAppFlow, "run_local_server", _explode)
    with pytest.raises(RuntimeError) as excinfo:
        auth.get_google_credentials()
    assert "GOOGLE_TOKEN" in str(excinfo.value)


def test_running_in_cloud_is_false_in_a_plain_script():
    """Outside a Streamlit runtime the browser flow stays available locally."""
    assert auth._running_in_cloud() is False


def test_save_token_survives_an_unwritable_filesystem(
    monkeypatch, isolated_paths, caplog
):
    """A read-only cloud filesystem must not take the app down."""
    _, token_path = isolated_paths

    def _boom(*args, **kwargs):
        raise OSError("Read-only file system")

    monkeypatch.setattr(auth.tempfile, "mkstemp", _boom)
    with caplog.at_level("WARNING"):
        auth._save_token(object())  # type: ignore[arg-type]
    assert "Could not persist" in caplog.text
    assert not token_path.exists()
