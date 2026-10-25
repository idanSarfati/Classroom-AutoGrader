"""Print a top-level boundary map for the two monoliths (temporary)."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

FILES = ["app.py", "src/llm_evaluator.py"]
min_line = int(sys.argv[1]) if len(sys.argv) > 1 else 0

for f in FILES:
    p = Path(f)
    src = p.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=f)
    print(f"===== {f} ({len(src.splitlines())} lines) =====")
    for node in tree.body:
        if getattr(node, "lineno", 0) < min_line:
            continue
        span = f"{getattr(node, 'lineno', '?'):>5}-{getattr(node, 'end_lineno', '?'):<5}"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            print(f"{span} {type(node).__name__:12} {node.name}")
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            print(f"{span} {'Assign':12} {', '.join(targets) or ast.unparse(node.targets)[:60]}")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            print(f"{span} {'AnnAssign':12} {node.target.id}")
        else:
            try:
                preview = ast.unparse(node)[:80]
            except Exception:
                preview = "?"
            print(f"{span} {type(node).__name__:12} {preview}")
