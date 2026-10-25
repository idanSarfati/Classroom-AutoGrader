import importlib, sys, traceback
mods = ["src.seams", "src.ui.helpers", "src.ui.progress", "src.ui.auth", "src.ui.catchup",
        "src.ui.dashboard", "src.ui.evaluate", "src.ui.release", "src.ui.daily_reports", "app"]
for m in mods:
    try:
        importlib.import_module(m)
        print(f"OK   {m}")
    except Exception as e:
        print(f"FAIL {m}: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
import app
print("app exports:", len([n for n in dir(app) if not n.startswith('__')]))
