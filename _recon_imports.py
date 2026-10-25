"""Recon: parse app.py imports into a name->module map; check for app back-imports.

Temporary. Prints:
  * every top-level name app.py imports, mapped to its source module;
  * any src/ module that imports ``app`` (circularity risk for the split).
"""
from __future__ import annotations

import ast
from pathlib import Path

app_src = Path("app.py").read_text(encoding="utf-8")
tree = ast.parse(app_src, filename="app.py")

print("===== app.py imported-name -> source-module map =====")
name_to_mod: dict[str, str] = {}
for node in tree.body:
    if isinstance(node, ast.ImportFrom):
        mod = ("." * node.level) + (node.module or "")
        for a in node.names:
            if a.name == "*":
                continue
            local = a.asname or a.name
            name_to_mod[local] = f"{mod}.{a.name}" if a.asname else mod
            print(f"  {local:32} <- {mod}" + (f"  (as {a.asname})" if a.asname else ""))
    elif isinstance(node, ast.Import):
        for a in node.names:
            local = a.asname or a.name.split(".")[0]
            name_to_mod[local] = a.name
            print(f"  {local:32} <- {a.name}")

print()
print("===== names DEFINED at top level in app.py =====")
defined = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined.append(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                defined.append(t.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        defined.append(node.target.id)
print("  " + ", ".join(defined))

print()
print("===== src/ modules that import app (circularity check) =====")
found = False
for p in sorted(Path("src").rglob("*.py")):
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if "import app" in txt or "from app " in txt or "from app import" in txt:
        print(f"  {p}  <-- imports app!")
        found = True
if not found:
    print("  (none - safe to split)")
