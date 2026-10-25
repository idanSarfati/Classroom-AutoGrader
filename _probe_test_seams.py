"""Report every monkeypatched attribute on the app/evaluator facades in tests."""
import re
from pathlib import Path

APP_ALIASES = {"mod", "app_module", "workflow_app", "dashboard_app", "daily_app", "app"}
EVAL_ALIASES = {"evaluator", "llm", "src_llm", "mod_eval", "ev", "evaluator_module", "llm_module"}

pat = re.compile(r"monkeypatch\.setattr\(\s*([\w\.]+)\s*,\s*['\"](\w+)['\"]")
pat2 = re.compile(r"setattr\(\s*([\w\.]+)\s*,\s*['\"](\w+)['\"]")

app_names, eval_names = {}, {}
for f in Path("tests").rglob("*.py"):
    text = f.read_text(encoding="utf-8", errors="replace")
    for m in list(pat.finditer(text)) + list(pat2.finditer(text)):
        target, attr = m.group(1), m.group(2)
        leaf = target.split(".")[-1]
        line = text[: m.start()].count("\n") + 1
        if leaf in APP_ALIASES:
            app_names.setdefault(attr, []).append(f"{f.name}:{line}")
        elif leaf in EVAL_ALIASES:
            eval_names.setdefault(attr, []).append(f"{f.name}:{line}")

print("=== app facade seams ===")
for k in sorted(app_names):
    print(f"  {k}  ({len(app_names[k])}x) {app_names[k][:3]}")
print("\n=== evaluator facade seams ===")
for k in sorted(eval_names):
    print(f"  {k}  ({len(eval_names[k])}x) {eval_names[k][:3]}")
