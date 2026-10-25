"""Report exact usage/patch sites for the tricky *value* seams (temporary)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

VALUE_SEAMS = [
    "EVAL_PAUSE_SECONDS", "_CATCHUP_STATE", "_ROUTER", "_ROUTER_READY",
    "_client", "_PACER", "_AUDIT_SIDECAR", "_first_working_stage", "_pacer",
]


def uses(path: str) -> None:
    src = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=path)
    # Map top-level node -> its name for context.
    spans = []
    for node in tree.body:
        name = getattr(node, "name", None) or type(node).__name__
        spans.append((node.lineno, getattr(node, "end_lineno", node.lineno), name))
    print(f"===== SOURCE {path} =====")
    for vn in VALUE_SEAMS:
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == vn:
                ctx = type(node.ctx).__name__
                owner = next((n for a, b, n in spans if a <= node.lineno <= b), "?")
                hits.append(f"L{node.lineno}:{ctx}@{owner}")
            elif isinstance(node, ast.Attribute) and node.attr == vn:
                owner = next((n for a, b, n in spans if a <= node.lineno <= b), "?")
                hits.append(f"L{node.lineno}:attr@{owner}")
        if hits:
            print(f"  {vn:22} {len(hits):2}  {'; '.join(hits[:14])}")


def patches(path: str) -> None:
    src = Path(path).read_text(encoding="utf-8")
    print(f"===== TEST PATCHES {path} =====")
    for i, line in enumerate(src.splitlines(), 1):
        if "monkeypatch.setattr" in line or "patch(" in line:
            m = re.search(r'setattr\(\s*[\w.]+,\s*["\'](\w+)["\']', line)
            name = m.group(1) if m else line.strip()
            if name in VALUE_SEAMS or any(s in line for s in VALUE_SEAMS):
                print(f"  L{i}: {line.strip()}")


uses("src/llm_evaluator.py")
uses("app.py")
for p in sorted(Path("tests").glob("*.py")):
    patches(str(p).replace("\\", "/"))
