"""Inventory of EVERY monkeypatch.setattr / setattr pair in tests (any target)."""
import ast
from pathlib import Path
from collections import defaultdict

pairs = defaultdict(set)  # attr -> {target-expr}
plain = defaultdict(set)  # alias.attr -> sites (plain assignment)

for f in sorted(Path("tests").rglob("*.py")):
    tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Attribute):
                name = fn.attr
            elif isinstance(fn, ast.Name):
                name = fn.id
            if name in ("setattr",) and len(node.args) >= 2:
                tgt, attr = node.args[0], node.args[1]
                if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                    def expr(e):
                        if isinstance(e, ast.Name):
                            return e.id
                        if isinstance(e, ast.Attribute):
                            base = expr(e.value)
                            return f"{base}.{e.attr}" if base else e.attr
                        if isinstance(e, ast.Constant):
                            return repr(e.value)
                        if isinstance(e, ast.Call):
                            return expr(e.func) + "(...)"
                        return type(e).__name__
                    pairs[attr.value].add(f"{expr(tgt)}  [{f.name}:{node.lineno}]")
            # monkeypatch.setattr with keyword attr=
            if isinstance(fn, ast.Attribute) and fn.attr == "setattr" and not node.args:
                kw = {k.arg: k.value for k in node.keywords}
                if "name" in kw and isinstance(kw["name"], ast.Constant):
                    tgt = kw.get("target") or kw.get("obj")
                    def expr2(e):
                        if e is None: return "?"
                        if isinstance(e, ast.Name): return e.id
                        if isinstance(e, ast.Attribute):
                            b = expr2(e.value); return f"{b}.{e.attr}" if b else e.attr
                        return type(e).__name__
                    pairs[kw["name"].value].add(f"{expr2(tgt)}  [{f.name}:{node.lineno}]")
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                base = t.value.id
                if base in ("app", "app_module", "mod", "workflow_app", "dashboard_app",
                            "daily_app", "evaluator", "llm", "src_llm", "mod_eval", "ev",
                            "evaluator_module", "llm_module"):
                    plain[f"{base}.{t.attr}"].add(f.name)

print("=== monkeypatch.setattr / setattr targets (attr -> sites), evaluator-relevant ===")
EVAL_ALIASES = {"evaluator", "llm", "src_llm", "mod_eval", "ev", "evaluator_module",
                "llm_module", "src.llm_evaluator"}
APP_ALIASES = {"app", "app_module", "mod", "workflow_app", "dashboard_app", "daily_app"}
for attr in sorted(pairs):
    sites = sorted(pairs[attr])
    ev = [s for s in sites if s.split("  [")[0] in EVAL_ALIASES]
    ap = [s for s in sites if s.split("  [")[0] in APP_ALIASES]
    other = [s for s in sites if s not in ev and s not in ap]
    tag = []
    if ev: tag.append(f"EVAL:{ev}")
    if ap: tag.append(f"APP:{ap}")
    if other: tag.append(f"OTHER:{other}")
    print(f"  {attr}: {' | '.join(tag)}")

print("\n=== plain assignments to alias.attr ===")
for k in sorted(plain):
    print(f"  {k}: {sorted(plain[k])}")
