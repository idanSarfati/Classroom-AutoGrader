"""Standalone validation of the seam-proxy mechanism (temporary).

Mimics what test_daily_catchup.py does: register a fake ``app`` facade in
sys.modules, monkeypatch its seams, and confirm the moved src.ui.catchup code
routes its calls to the patched versions (not the real implementations).
"""
from __future__ import annotations

import sys
import types

# Import the facade-dependent module FIRST so we can register "app" after.
import src.ui.catchup as catchup

# Build a fake "app" facade module carrying patched seams.
fake_app = types.ModuleType("app")
fake_app.st = object()
fake_app.has_report_for_today = lambda: True  # patched: report exists
fake_app._CATCHUP_STATE = {"attempted": False, "ok": False, "status": ""}


def _boom(*a, **k):
    raise AssertionError("must not launch when today's report exists")


fake_app._schtasks_catchup = _boom
fake_app._detached_catchup = _boom

sys.modules["app"] = fake_app

failures = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


# 1. trigger_daily_check_catchup must see the patched has_report_for_today=True
ok, msg = catchup.trigger_daily_check_catchup()
check("trigger sees patched has_report_for_today=True (no launch)", ok is True and "already exists" in msg)

# 2. _ensure_today_report_catchup must see patched _CATCHUP_STATE (fresh dict)
ok, status = catchup._ensure_today_report_catchup()
check("ensure sees patched _CATCHUP_STATE (ok=True, status='')", ok is True and status == "")
check("ensure mutated the FACADE dict", fake_app._CATCHUP_STATE["attempted"] is True)

# 3. A second call is a no-op because the facade dict is now attempted=True
fake_app.trigger_daily_check_catchup = lambda **k: (_ for _ in ()).throw(AssertionError("must not re-trigger"))
ok2, status2 = catchup._ensure_today_report_catchup()
check("ensure short-circuits on attempted=True", ok2 is True and status2 == "")

# 4. Route through schtasks when report missing
fake_app.has_report_for_today = lambda: False
# Reset the trigger seam to the REAL implementation (step 3 left an exploder).
fake_app.trigger_daily_check_catchup = catchup.trigger_daily_check_catchup.default
fake_app._schtasks_catchup = lambda *a, **k: (True, "task started.")
ok3, msg3 = catchup.trigger_daily_check_catchup()
check("trigger prefers patched schtasks", ok3 is True and "task started." in msg3)

# 5. Fall back to detached when schtasks fails
fake_app._schtasks_catchup = lambda *a, **k: (False, "no task")
fake_app._detached_catchup = lambda: (True, "detached started.")
ok4, msg4 = catchup.trigger_daily_check_catchup(allow_direct_run=True)
check("trigger falls back to patched detached", ok4 is True and "detached started." in msg4)

# 6. Never raises when the patched trigger explodes (ensure path)
fake_app.has_report_for_today = lambda: False
fake_app._CATCHUP_STATE = {"attempted": False, "ok": False, "status": ""}


def _explode(**k):
    raise RuntimeError("no scheduler here")


fake_app.trigger_daily_check_catchup = _explode
ok5, status5 = catchup._ensure_today_report_catchup()
check("ensure survives patched trigger exception", ok5 is False and "no scheduler here" in status5)

print()
if failures:
    print(f"{len(failures)} FAILURES")
    sys.exit(1)
print("ALL SEAM CHECKS PASSED")
