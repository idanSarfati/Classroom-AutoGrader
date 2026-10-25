"""Daily-report catch-up helpers extracted from :mod:`app`.

Read-only helpers that make sure today's daily-check report exists, preferring
the registered Windows scheduled task and falling back to a detached
``python -m src.daily_check`` run. Never raises - a broken catch-up must not
take the dashboard down.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from src.daily_reports import DEFAULT_DAILY_CHECKS_DIR, has_report_for_today

from src.seams import seam_dict, seam_fn

# Project root is three levels up from this file: <root>/src/ui/catchup.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DAILY_CHECK_TASK_NAME = "ClassroomAutoGrader-DailyCheck"

# One record per Streamlit *server process*: the automatic catch-up below fires
# at most once - not on every widget-triggered script rerun. (Streamlit reruns
# ``main()`` on each interaction, so a plain module-level flag is what makes
# "once at startup" actually mean once.)
_CATCHUP_STATE: dict = {"attempted": False, "ok": False, "status": ""}

logger = logging.getLogger(__name__)


def _schtasks_catchup(task_name: str = DAILY_CHECK_TASK_NAME) -> tuple:
    """Ask Windows Task Scheduler to run the daily check right now.

    Returns ``(ok, message)``. The registered task is the same read-only
    ``src.daily_check`` job the 06:00 trigger uses (and it carries
    ``--skip-if-today-done``, so this can never double-scan). Any failure -
    task not registered, non-Windows host, ``schtasks`` unavailable - comes
    back as ``(False, ...)`` so the caller can fall back. Never raises.
    """
    if os.name != "nt":
        return False, "Task Scheduler is only available on Windows."
    try:
        proc = subprocess.run(
            ["schtasks", "/Run", "/TN", task_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"schtasks failed: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err or f"schtasks exited with code {proc.returncode}."
    return True, f"scheduled task '{task_name}' started."


def _detached_catchup() -> tuple:
    """Run ``python -m src.daily_check --once`` as a detached background job.

    Fallback for when the scheduled task is not registered (or we are not on
    Windows). Detached so the Streamlit UI never blocks on the Classroom scan;
    ``--skip-if-today-done`` keeps it idempotent with the scheduled task.
    Returns ``(ok, message)``; never raises.
    """
    py = sys.executable or "python"
    root = PROJECT_ROOT
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [py, "-m", "src.daily_check", "--once", "--skip-if-today-done"],
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except (OSError, ValueError) as exc:
        return False, f"could not start the background check: {exc}"
    return True, f"background read-only check started with `{py}`."


def trigger_daily_check_catchup(allow_direct_run: bool = True) -> tuple:
    """Make sure today's daily report exists, running the read-only check now
    if it does not. Prefers the registered scheduled task; with
    ``allow_direct_run`` also falls back to a detached
    ``python -m src.daily_check``. Returns ``(ok, message)``; never raises -
    a broken catch-up must not take the dashboard down.

    ``allow_direct_run=False`` is used by the *automatic* startup check: the
    scheduled task is safe to poke (idempotent, read-only, registered by
    ``Register-DailyCheckTask.ps1``), but quietly spawning Google-scanning
    processes on hosts without that task (e.g. Streamlit Community Cloud)
    would waste API quota on every server boot, so the automatic path stops
    after schtasks and leaves the explicit button as the manual fallback.
    """
    try:
        if has_report_for_today():
            return True, "Today's daily report already exists - nothing to run."
        ok, msg = _schtasks_catchup()
        if ok:
            return True, msg
        if not allow_direct_run:
            return False, msg
        ok, msg2 = _detached_catchup()
        if ok:
            return True, f"{msg} Started a direct run instead: {msg2}"
        return False, f"{msg} {msg2}"
    except Exception as exc:  # noqa: BLE001 - UI must survive anything here
        logger.warning("Daily-check catch-up failed: %s", exc)
        return False, f"catch-up failed: {exc}"


def _ensure_today_report_catchup() -> tuple:
    """Startup freshness check: trigger the fallback at most once per server
    process when today's report is missing (e.g. the machine was powered off
    at 06:00 and StartWhenAvailable has not fired yet). Only pokes the
    registered scheduled task - never spawns extra scans by itself. Returns
    the recorded ``(ok, status message)``; never raises.
    """
    state = _CATCHUP_STATE
    if state["attempted"]:
        return state["ok"], state["status"]
    state["attempted"] = True
    try:
        if has_report_for_today():
            state["ok"], state["status"] = True, ""
        else:
            state["ok"], state["status"] = trigger_daily_check_catchup(
                allow_direct_run=False
            )
            logger.info("Daily report missing for today; catch-up: %s", state["status"])
    except Exception as exc:  # noqa: BLE001 - startup must never crash the UI
        state["ok"], state["status"] = False, f"catch-up check failed: {exc}"
        logger.warning("Daily report catch-up skipped: %s", exc)
    return state["ok"], state["status"]


# ---------------------------------------------------------------------------
# Seam wiring
# ---------------------------------------------------------------------------
# Tests monkeypatch these names on the ``app`` facade (``app._schtasks_catchup``,
# ``app.has_report_for_today``, ``app._CATCHUP_STATE``, ...). The bodies above
# reference them as plain module globals, and Python resolves globals at *call*
# time - so rebinding them here to facade-resolving proxies keeps every call
# site above untouched while still honouring a patch applied to ``app``.
#
# Each proxy captures the real implementation/dict as its default and falls
# back to it when ``app`` is not loaded (or when ``app`` re-exports the proxy
# itself, which the ``value is self`` guard inside the proxy detects).
_schtasks_catchup = seam_fn("app", "_schtasks_catchup", _schtasks_catchup)
_detached_catchup = seam_fn("app", "_detached_catchup", _detached_catchup)
has_report_for_today = seam_fn("app", "has_report_for_today", has_report_for_today)
trigger_daily_check_catchup = seam_fn(
    "app", "trigger_daily_check_catchup", trigger_daily_check_catchup
)
_CATCHUP_STATE = seam_dict("app", "_CATCHUP_STATE", _CATCHUP_STATE)