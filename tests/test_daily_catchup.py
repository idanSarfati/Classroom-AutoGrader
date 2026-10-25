"""Tests for the Daily Reports catch-up wiring (no network, no real tasks).

Covers:
  * app.trigger_daily_check_catchup / _ensure_today_report_catchup logic
    with schtasks and subprocess fully stubbed out.
  * The banner + manual button rendered by render_daily_reports_tab when
    today's report is missing.

Run: python -m pytest tests/test_daily_catchup.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def app_module(monkeypatch):
    """Import ``app`` with Streamlit replaced by a recording stub."""
    import app as app_module

    stub = _StubStreamlit()
    monkeypatch.setattr(app_module, "st", stub)
    # Every test controls freshness explicitly; the real exports/ dir of the
    # developer machine must never leak into assertions.
    monkeypatch.setattr(app_module, "has_report_for_today", lambda: False)
    # Reset the once-per-process catch-up state between tests.
    monkeypatch.setattr(
        app_module,
        "_CATCHUP_STATE",
        {"attempted": False, "ok": False, "status": ""},
    )
    return app_module, stub


class _StubStreamlit:
    """Minimal ``st`` stand-in: records calls, no widgets that matter."""

    def __init__(self, buttons=()):
        self.buttons = set(buttons)
        self.messages: list[str] = []

    def button(self, label, key=None, **kwargs):
        self.messages.append(f"button:{label}" + (f"#{key}" if key else ""))
        return label in self.buttons

    def selectbox(self, label, options, **kwargs):
        self.messages.append(f"selectbox:{label}")
        return 0

    def checkbox(self, label, **kwargs):
        return False

    def columns(self, spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)

        class _Col:
            def metric(self, *a, **k):
                pass

        return [_Col() for _ in range(count)]

    def expander(self, *args, **kwargs):
        return _NullCtx()

    def dataframe(self, *args, **kwargs):
        self.messages.append("dataframe")

    def __getattr__(self, name):
        def _emit(*args, **kwargs):
            self.messages.append(f"{name}:{args[0] if args else ''}")

        return _emit


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------------------- #
# trigger_daily_check_catchup: schtasks first, direct run only on demand
# --------------------------------------------------------------------------- #
def test_trigger_is_noop_when_todays_report_exists(app_module, monkeypatch):
    mod, _ = app_module
    monkeypatch.setattr(mod, "has_report_for_today", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("must not launch anything when today's report exists")

    monkeypatch.setattr(mod, "_schtasks_catchup", _boom)
    monkeypatch.setattr(mod, "_detached_catchup", _boom)
    ok, msg = mod.trigger_daily_check_catchup()
    assert ok is True and "already exists" in msg


def test_trigger_prefers_the_scheduled_task(app_module, monkeypatch):
    mod, _ = app_module
    monkeypatch.setattr(mod, "_schtasks_catchup", lambda *a, **k: (True, "task started."))
    monkeypatch.setattr(
        mod, "_detached_catchup", lambda *a, **k: pytest.fail("no direct run when task works")
    )
    ok, msg = mod.trigger_daily_check_catchup()
    assert ok is True and msg == "task started."


def test_trigger_falls_back_to_detached_run_when_task_missing(app_module, monkeypatch):
    mod, _ = app_module
    monkeypatch.setattr(mod, "_schtasks_catchup", lambda *a, **k: (False, "task not registered."))
    monkeypatch.setattr(mod, "_detached_catchup", lambda *a, **k: (True, "direct run started."))
    ok, msg = mod.trigger_daily_check_catchup()
    assert ok is True and "direct run started" in msg


def test_automatic_path_never_spawns_a_direct_run(app_module, monkeypatch):
    """The startup check only pokes schtasks - no surprise Google scans."""
    mod, _ = app_module
    monkeypatch.setattr(mod, "_schtasks_catchup", lambda *a, **k: (False, "task not registered."))
    monkeypatch.setattr(
        mod, "_detached_catchup", lambda *a, **k: pytest.fail("auto path must not spawn scans")
    )
    ok, msg = mod.trigger_daily_check_catchup(allow_direct_run=False)
    assert ok is False and "task not registered" in msg


def test_trigger_survives_a_raising_helper(app_module, monkeypatch):
    mod, _ = app_module

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mod, "_schtasks_catchup", _boom)
    ok, msg = mod.trigger_daily_check_catchup()
    assert ok is False and "kaboom" in msg


# --------------------------------------------------------------------------- #
# _ensure_today_report_catchup: once per process, never fatal
# --------------------------------------------------------------------------- #
def test_startup_catchup_runs_once_per_process(app_module, monkeypatch):
    mod, _ = app_module
    calls = []
    monkeypatch.setattr(
        mod, "trigger_daily_check_catchup", lambda **k: calls.append(k) or (True, "started")
    )

    assert mod._ensure_today_report_catchup() == (True, "started")
    assert mod._ensure_today_report_catchup() == (True, "started")
    assert calls == [{"allow_direct_run": False}], "second call must reuse the recorded state"


def test_startup_catchup_silent_when_report_exists(app_module, monkeypatch):
    mod, _ = app_module
    monkeypatch.setattr(mod, "has_report_for_today", lambda: True)
    monkeypatch.setattr(
        mod,
        "trigger_daily_check_catchup",
        lambda **k: pytest.fail("must not trigger when today's report exists"),
    )
    assert mod._ensure_today_report_catchup() == (True, "")


def test_startup_catchup_never_raises(app_module, monkeypatch):
    mod, _ = app_module

    def _boom(**k):
        raise RuntimeError("no scheduler here")

    monkeypatch.setattr(mod, "trigger_daily_check_catchup", _boom)
    ok, status = mod._ensure_today_report_catchup()
    assert ok is False and "no scheduler here" in status


# --------------------------------------------------------------------------- #
# The tab itself: banner + button when today's report is missing
# --------------------------------------------------------------------------- #
def _fake_meta(run_id):
    """A minimal DailyReportMeta lookalike that loads cleanly."""
    from src.daily_reports import DailyReportMeta

    meta = DailyReportMeta(
        run_id=run_id, directory=Path(run_id), json_path=Path(run_id) / "daily_check.json"
    )
    meta.generated_at = "2026-10-12T06:00:00+03:00"
    meta.mode = "read-only"
    return meta


def test_tab_warns_and_offers_catchup_when_today_missing(app_module, monkeypatch):
    mod, stub = app_module
    # One older snapshot so the tab has something to display.
    older = _fake_meta("2026-10-12_06-00-00")
    monkeypatch.setattr(mod, "list_daily_reports", lambda *a, **k: [older])
    monkeypatch.setattr(
        mod, "load_daily_report", lambda p: _fake_detail()
    )

    mod.render_daily_reports_tab()

    assert any(m.startswith("warning:") and "No daily report for" in m for m in stub.messages)
    # Daily check temporarily disabled: the catch-up button must not be rendered.
    assert not any("button:🚀 Run today's read-only check now" in m for m in stub.messages)


def test_tab_catchup_button_triggers_the_check(app_module, monkeypatch):
    mod, stub = app_module
    # Daily check temporarily disabled: the button no longer exists, so
    # rendering the tab must not attempt to trigger a catch-up run.
    older = _fake_meta("2026-10-12_06-00-00")
    monkeypatch.setattr(mod, "list_daily_reports", lambda *a, **k: [older])
    monkeypatch.setattr(mod, "load_daily_report", lambda p: _fake_detail())
    launched = []
    monkeypatch.setattr(
        mod,
        "trigger_daily_check_catchup",
        lambda **k: launched.append(k) or (True, "scheduled task started."),
    )

    mod.render_daily_reports_tab()

    assert launched == []


def test_tab_has_no_warning_when_report_is_fresh(app_module, monkeypatch):
    mod, stub = app_module
    monkeypatch.setattr(mod, "has_report_for_today", lambda: True)
    today = _fake_meta(datetime.now().strftime("%Y-%m-%d") + "_06-00-00")
    monkeypatch.setattr(mod, "list_daily_reports", lambda *a, **k: [today])
    monkeypatch.setattr(mod, "load_daily_report", lambda p: _fake_detail())

    mod.render_daily_reports_tab()

    assert not any(m.startswith("warning:") and "No daily report" in m for m in stub.messages)


def _fake_detail():
    from src.daily_reports import DailyReportDetail

    detail = DailyReportDetail(
        run_id="2026-10-12_06-00-00",
        directory=Path("x"),
        json_path=Path("x") / "daily_check.json",
    )
    detail.generated_at = "2026-10-12T06:00:00+03:00"
    detail.mode = "read-only"
    return detail
