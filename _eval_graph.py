"""Full top-level cross-reference graph for src/llm_evaluator.py."""
import ast
from collections import defaultdict
from pathlib import Path

SRC = Path("src/llm_evaluator.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

defined = {}
order = []
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined[node.name] = node
        order.append(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                defined[t.id] = node
                order.append(t.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        defined[node.target.id] = node
        order.append(node.target.id)

def refs_of(node):
    loads = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            loads.add(sub.id)
    return loads & set(defined) - {k for k in defined} | (loads & set(defined))

print("=== refs to other top-level names (def -> its refs) ===")
for name in order:
    node = defined[name]
    r = sorted(refs_of(node) - {name})
    if r:
        print(f"  {name}: {r}")

# reverse index: who is referenced BY others (excluding self)
rev = defaultdict(list)
for name in order:
    node = defined[name]
    for other in refs_of(node) - {name}:
        rev[other].append(name)
print("\n=== referenced-by (name -> consumers) for alias/state names ===")
watch = ["_ROUTER", "_ROUTER_READY", "_PACER", "_AUDIT_SIDECAR", "_client",
         "_first_working_stage", "_STAGES", "_RESPONSE_FORMAT", "_JSON_OBJECT_FORMAT",
         "EVAL_PAUSE_SECONDS", "SYSTEM_PROMPT", "Rubric"]
for w in watch:
    print(f"  {w}: {sorted(rev.get(w, []))}")

# module-level Expr statements
print("\n=== module-level Expr statements ===")
for node in TREE.body:
    if isinstance(node, ast.Expr) and not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
        print(f"  L{node.lineno}-{node.end_lineno}: {ast.dump(node.value)[:160]}")

# assigns whose value references other defs (aliases / computed)
print("\n=== Assign values referencing defs ===")
for node in TREE.body:
    targets = []
    if isinstance(node, ast.Assign):
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        targets = [node.target.id]
    if not targets:
        continue
    r = sorted(refs_of(node))
    if r:
        print(f"  L{node.lineno} {targets} <- {r}")
