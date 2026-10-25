"""Map cross-module import edges among src/ui/*.py and detect cycles."""
import ast
from pathlib import Path

edges: dict[str, set[str]] = {}
for f in sorted(Path("src/ui").glob("*.py")):
    if f.name == "__init__.py":
        continue
    tree = ast.parse(f.read_text(encoding="utf-8"))
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src.ui."):
            deps.add(node.module.split(".")[-1])
        if isinstance(node, ast.ImportFrom) and node.level >= 1:  # relative imports
            print(f"{f.name}: relative import level={node.level}")
    edges[f.stem] = deps

for mod, deps in sorted(edges.items()):
    print(f"{mod} -> {sorted(deps) or '(none)'}")

# naive cycle detection
def find_cycles():
    seen = []
    def dfs(m, path):
        if m in path:
            seen.append(path[path.index(m):] + [m])
            return
        for d in sorted(edges.get(m, ())):
            dfs(d, path + [m])
    for m in sorted(edges):
        dfs(m, [])
    return seen

cycles = find_cycles()
if cycles:
    print("\nCYCLES FOUND:")
    for c in cycles:
        print("  " + " -> ".join(c))
else:
    print("\nno cycles")

# also: do the already-created modules (auth/helpers/progress/catchup) import ui siblings?
print("\nall from-imports in each ui module:")
for f in sorted(Path("src/ui").glob("*.py")):
    tree = ast.parse(f.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src.ui"):
            print(f"  {f.name}:{node.lineno}: from {node.module} import {', '.join(a.name for a in node.names)}")

# tests importing src.ui directly?
print("\ntests referencing src.ui:")
for f in sorted(Path("tests").rglob("*.py")):
    text = f.read_text(encoding="utf-8", errors="replace")
    if "src.ui" in text or "src import ui" in text:
        for i, line in enumerate(text.splitlines(), 1):
            if "src.ui" in line:
                print(f"  {f.name}:{i}: {line.strip()}")
