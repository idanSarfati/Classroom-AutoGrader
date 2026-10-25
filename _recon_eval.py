"""Recon for splitting src/llm_evaluator.py: node map + import map + seam usage."""
import ast
from pathlib import Path

SRC = Path("src/llm_evaluator.py").read_text(encoding="utf-8")
LINES = SRC.splitlines()
TREE = ast.parse(SRC)

print(f"total lines: {len(LINES)}")
print("\n=== top-level nodes ===")
import_block = []
for i, node in enumerate(TREE.body):
    span = (node.lineno, node.end_lineno)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        kind = f"{type(node).__name__}:{node.name}"
    elif isinstance(node, ast.ImportFrom):
        kind = "ImportFrom: " + ", ".join(a.name for a in node.names)
        import_block.append(span)
    elif isinstance(node, ast.Import):
        kind = "Import: " + ", ".join(a.name for a in node.names)
        import_block.append(span)
    elif isinstance(node, ast.Assign):
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        kind = "Assign: " + ", ".join(targets)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        kind = f"AnnAssign: {node.target.id}"
    elif isinstance(node, ast.Expr):
        kind = "Expr(docstring)" if isinstance(node.value, ast.Constant) else "Expr"
    else:
        kind = type(node).__name__
    print(f"  L{span[0]:>4}-{span[1]:<4} {kind}")

print("\n=== module docstring span ===")
if TREE.body and isinstance(TREE.body[0], ast.Expr) and isinstance(TREE.body[0].value, ast.Constant):
    print(f"  L{TREE.body[0].lineno}-{TREE.body[0].end_lineno}")

print("\n=== import block extent ===")
if import_block:
    print(f"  L{import_block[0][0]}-{import_block[-1][1]}")

# free names per top-level def/class -> potential seams / cross refs
SEAM_CANDIDATES = {"_ROUTER", "_ROUTER_READY", "_call_completion", "_client",
                   "_create_completion", "_first_working_stage", "_get_client",
                   "_pacer", "_parse_result", "EVAL_PAUSE_SECONDS", "_PACER",
                   "_AUDIT_SIDECAR", "LLMEvaluationError", "StudentGradeRequest"}

print("\n=== seam/global usage per node ===")
defined = {}
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined[node.name] = (node.lineno, node.end_lineno)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                defined[t.id] = (node.lineno, node.end_lineno)

for node in TREE.body:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        continue
    loads = {n.id for n in ast.walk(node)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    used = sorted(loads & SEAM_CANDIDATES)
    if used:
        print(f"  {node.name}: {used}")

print("\n=== consumers: who imports src.llm_evaluator ===")
import re
pat = re.compile(r"(?:from src\.llm_evaluator import|import src\.llm_evaluator|from src import llm_evaluator)")
for f in sorted(list(Path(".").glob("*.py")) + list(Path("src").rglob("*.py")) + list(Path("tests").rglob("*.py"))):
    if f.name in ("llm_evaluator.py", "_gen_ui.py"):
        continue
    text = f.read_text(encoding="utf-8", errors="replace")
    for m in pat.finditer(text):
        line = text[: m.start()].count("\n") + 1
        snippet = text.splitlines()[line - 1].strip()
        print(f"  {f}:{line}: {snippet}")
