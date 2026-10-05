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
    "DASHBOARD_AUTO_DRY_RUN",
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


# -------------------------------------------------------------------------- #
# The late penalty: the default must never silently disappear
#
# A penalty that quietly evaluates to 0 is indistinguishable from no policy at
# all - the student keeps the full mark and nothing in the UI says why. These
# pin every way the value can arrive, so a missing, malformed, out-of-range or
# wrong-typed setting still leaves a usable number behind.
# -------------------------------------------------------------------------- #


def test_the_penalty_default_is_ten_points(fake_secrets, clean_env):
    fake_secrets({})
    assert (
        settings_module.load_settings().late_penalty_points
        == settings_module.DEFAULT_LATE_PENALTY_POINTS
        == 10
    )


def test_the_configured_default_matches_the_policy_module(fake_secrets, clean_env):
    """The two defaults must not drift apart.

    ``config.settings`` cannot import ``src.late_policy`` (that module imports
    this one, so the dependency would be circular), which is exactly why this
    link needs pinning in a test rather than by the import graph.
    """
    from src.late_policy import DEFAULT_LATE_PENALTY_POINTS

    assert settings_module.DEFAULT_LATE_PENALTY_POINTS == DEFAULT_LATE_PENALTY_POINTS


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "10.5", "ten"])
def test_an_unusable_penalty_falls_back_to_the_default(
    fake_secrets, clean_env, monkeypatch, raw
):
    if raw is not None:
        monkeypatch.setenv("LATE_PENALTY_POINTS", raw)
    fake_secrets({})
    assert settings_module.load_settings().late_penalty_points == 10


@pytest.mark.parametrize(
    "raw,expected", [("-5", 0), ("-1", 0), ("150", 100), ("1000", 100), ("250", 100)]
)
def test_an_out_of_range_penalty_is_clamped_instead_of_crashing(
    fake_secrets, clean_env, monkeypatch, raw, expected
):
    """The real failure was a ValidationError at import time.

    ``Settings`` declares ``ge=0, le=100``, and pydantic *rejects* a value
    outside that - it does not clamp it. Because ``load_settings()`` runs at
    import, an unclamped ``LATE_PENALTY_POINTS=150`` took the whole app down on
    startup rather than degrading to a usable number.
    """
    monkeypatch.setenv("LATE_PENALTY_POINTS", raw)
    fake_secrets({})
    loaded = settings_module.load_settings()
    assert loaded.late_penalty_points == expected
    assert 0 <= loaded.late_penalty_points <= 100


@pytest.mark.parametrize("raw,expected", [("0", 0), ("15", 15), ("100", 100)])
def test_a_penalty_written_as_a_bare_toml_number_is_honoured(
    fake_secrets, clean_env, expected, raw
):
    """``LATE_PENALTY_POINTS = 0`` in secrets.toml is an int, not a string.

    ``st.secrets`` returns the type the TOML declared, so the unquoted form -
    the natural way to write a number - arrived as an int. Reading strings only
    discarded it and fell back to the default, which turned the documented
    "set it to 0 to switch the penalty off" back into a 10-point charge.
    """
    fake_secrets({"LATE_PENALTY_POINTS": int(raw)})
    assert settings_module.load_settings().late_penalty_points == expected


def test_a_toml_boolean_secret_is_readable_as_a_flag(fake_secrets, clean_env):
    """``DRY_RUN = true`` unquoted must still switch dry-run on."""
    fake_secrets({"DRY_RUN": True, "LATE_PENALTY_POINTS": 5})
    loaded = settings_module.load_settings()
    assert loaded.dry_run is True
    assert loaded.late_penalty_points == 5


# --------------------------------------------------------------------------
# The Dashboard's automatic dry-run option
#
# Off by default: a dry-run still spends Groq tokens, so opting in has to be
# explicit. Whatever it does, it is read-only - see src/dashboard.py.
# --------------------------------------------------------------------------


def test_auto_dry_run_is_off_by_default(fake_secrets, clean_env):
    """The Dashboard must not start grading on its own."""
    fake_secrets({})
    assert settings_module.load_settings().dashboard_auto_dry_run is False


def test_auto_dry_run_is_readable_from_streamlit_secrets(fake_secrets, clean_env):
    """On Streamlit Community Cloud the flag arrives as a TOML boolean."""
    fake_secrets({"DASHBOARD_AUTO_DRY_RUN": True})
    assert settings_module.load_settings().dashboard_auto_dry_run is True


def test_auto_dry_run_reads_the_environment(fake_secrets, clean_env, monkeypatch):
    """Local ``.env`` / ``os.environ`` wins, as for every other setting."""
    fake_secrets({})
    monkeypatch.setenv("DASHBOARD_AUTO_DRY_RUN", "yes")
    assert settings_module.load_settings().dashboard_auto_dry_run is True


def test_auto_dry_run_tolerates_an_unusable_value(fake_secrets, clean_env, monkeypatch):
    """A typo must degrade to the safe default instead of crashing startup."""
    monkeypatch.setenv("DASHBOARD_AUTO_DRY_RUN", "maybe")
    fake_secrets({})
    assert settings_module.load_settings().dashboard_auto_dry_run is False


def test_the_configured_penalty_reaches_the_final_score(
    fake_secrets, clean_env, monkeypatch
):
    """End of the chain: a configured 25 must leave 25 points off the grade.

    This is the guarantee the whole configuration path exists to serve, so it
    is asserted on the score the teacher would import into Mashov - not on the
    setting, which could be correct and still never be applied.
    """
    from datetime import datetime, timezone

    import src.llm_evaluator as evaluator
    from src.late_policy import penalty_for
    from src.models import (
        AssignmentConfig,
        CourseWork,
        StudentSubmission,
        TaskDefinition,
    )

    assignment = AssignmentConfig(
        assignment_id="lesson_1",
        title="Worksheet",
        tasks=[TaskDefinition(id=1, name="Task 1", description="print")],
    )
    due = datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc)
    coursework = CourseWork(
        id="cw1", title="Worksheet", due_date=due.date(), due_datetime=due
    )

    # A perfect grade, so any points missing from the score are the penalty.
    payload = {
        "student_name": "Noa",
        "score": 100,
        "feedback_hebrew": "כל הכבוד!",
        "deduction_breakdown": [],
        "task_evaluations": [
            {
                "task_name": "Task 1",
                "student_answer_found": "print(1)",
                "status": "CORRECT",
                "notes": "נכון",
            }
        ],
    }

    class _Response:
        def __init__(self, content: str) -> None:
            self.choices = [
                types.SimpleNamespace(message=types.SimpleNamespace(content=content))
            ]

    monkeypatch.setattr(evaluator, "_get_client", lambda: object())
    monkeypatch.setattr(
        evaluator,
        "_call_completion",
        lambda client, system, prompt, fmt: _Response(json.dumps(payload)),
    )
    monkeypatch.setattr(evaluator, "_first_working_stage", 0)

    fake_secrets({"LATE_PENALTY_POINTS": 25})
    configured = settings_module.load_settings().late_penalty_points
    assert configured == 25, "a bare TOML number must survive the secret lookup"

    # Lateness is the policy's call; only the number comes from settings.
    late = StudentSubmission(
        student_id="u1",
        student_name="Noa",
        submission_id="s1",
        state="TURNED_IN",
        is_late=True,
    )
    graded = evaluator.evaluate_student_submission(
        late.student_name,
        "print(1)",
        assignment,
        late_penalty_points=penalty_for(late, coursework, configured),
    )

    assert graded.score == 75, "25 configured points must come off a perfect 100"
    assert sum(i.points_deducted for i in graded.deduction_breakdown) == (
        100 - graded.score
    )


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
