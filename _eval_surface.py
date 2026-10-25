"""Collect every attribute accessed on llm_evaluator aliases in tests/ and src/."""
import ast
from pathlib import Path
from collections import defaultdict

ALIASES = {"evaluator", "llm", "src_llm", "mod_eval", "ev", "evaluator_module", "llm_module"}
attrs = defaultdict(set)

def scan(path, aliases):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except Exception as e:
        print(f"SKIP {path}: {e}")
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in aliases:
                attrs[node.attr].add(str(path))

for f in sorted(Path("tests").rglob("*.py")):
    scan(f, ALIASES)
# also: names imported FROM src.llm_evaluator anywhere
imports = defaultdict(set)
imp_pat_names = defaultdict(set)
import_names = defaultdict(set)
for f in sorted(list(Path("tests").rglob("*.py")) + list(Path("src").rglob("*.py")) + list(Path(".").glob("*.py"))):
    if str(f).startswith("_") or f.name == "llm_evaluator.py":
        continue
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
    except Exception:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.llm_evaluator":
            for a in node.names:
                import_names[a.name].add(str(f))

print("=== attribute access on evaluator aliases ===")
for a in sorted(attrs):
    print(f"  {a}: {sorted(attrs[a])}")
print("\n=== from src.llm_evaluator import NAME ===")
for a in sorted(import_names):
    print(f"  {a}: {sorted(import_names[a])}")
