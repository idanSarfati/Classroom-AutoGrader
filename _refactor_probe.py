"""Temporary introspection helper for the app.py / llm_evaluator.py refactor.

Prints:
1. Top-level AST nodes (line ranges) of app.py and src/llm_evaluator.py.
2. All monkeypatch.setattr / mock.patch seams targeting app / llm_evaluator aliases.
3. All attribute accesses on app / llm_evaluator aliases inside tests.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent


def top_nodes(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    total = len(src.splitlines())
    print(f"===== {path} (total {total} lines) =====")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            name = ",".join(targets)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            name = "import: " + ", ".join(
                (a.asname or a.name) for a in node.names
            )
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            name = "docstring/comment"
        else:
            name = type(node).__name__
        print(f"{node.lineno}-{node.end_lineno}: {name}")
    print()


# --- seams -------------------------------------------------------------------

APP_ALIASES = {"app", "app_module", "workflow_app", "dashboard_app", "daily_app", "mod"}
EVAL_ALIASES = {
    "evaluator", "llm", "llm_evaluator", "src_llm", "mod_eval", "ev",
    "evaluator_module", "llm_module",
}

SETATTR_RE = re.compile(
    r"monkeypatch\.setattr\(\s*([A-Za-z_][\w.]*)\s*,\s*['\"]([A-Za-z_]\w*)['\"]"
)
PATCH_RE = re.compile(
    r"(?:mock\.)?patch\(\s*['\"]((?:app|src\.llm_evaluator)(?:\.[A-Za-z_]\w*)+)['\"]"
)
ASSIGN_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*[^=]")


def seams() -> None:
    print("===== monkeypatch seams on app =====")
    app_seams: dict[str, list[str]] = {}
    eval_seams: dict[str, list[str]] = {}
    for test in sorted((ROOT / "tests").glob("*.py")):
        text = test.read_text(encoding="utf-8")
        for m in SETATTR_RE.finditer(text):
            var, attr = m.group(1), m.group(2)
            line = text[: m.start()].count("\n") + 1
            if var in APP_ALIASES:
                app_seams.setdefault(attr, []).append(f"{test.name}:{line}")
            elif var in EVAL_ALIASES:
                eval_seams.setdefault(attr, []).append(f"{test.name}:{line}")
        for m in PATCH_RE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            print(f"  mock.patch {m.group(1)} @ {test.name}:{line}")
    for attr, where in sorted(app_seams.items()):
        print(f"  app.{attr}  <- {', '.join(where)}")
    print("\n===== monkeypatch seams on src.llm_evaluator =====")
    for attr, where in sorted(eval_seams.items()):
        print(f"  eval.{attr}  <- {', '.join(where)}")
    print()


def attribute_uses() -> None:
    print("===== attribute reads on app / llm_evaluator aliases in tests =====")
    uses: dict[str, set[str]] = {"app": set(), "eval": set()}
    # Find alias bindings first, per file.
    alias_re = re.compile(
        r"import\s+app(?:\s+as\s+(\w+))?|import\s+src\.llm_evaluator(?:\s+as\s+(\w+))?"
        r"|from\s+src\s+import\s+llm_evaluator(?:\s+as\s+(\w+))?"
    )
    for test in sorted((ROOT / "tests").glob("*.py")):
        text = test.read_text(encoding="utf-8")
        app_aliases: set[str] = {"app"}
        eval_aliases: set[str] = {"llm_evaluator"}
        for line in text.splitlines():
            m = re.match(r"\s*import\s+app\s+as\s+(\w+)", line)
            if m:
                app_aliases.add(m.group(1))
            m = re.match(r"\s*import\s+app\b", line)
            if m:
                app_aliases.add("app")
            m = re.match(r"\s*import\s+src\.llm_evaluator\s+as\s+(\w+)", line)
            if m:
                eval_aliases.add(m.group(1))
            m = re.match(r"\s*from\s+src\s+import\s+llm_evaluator\s+as\s+(\w+)", line)
            if m:
                eval_aliases.add(m.group(1))
        for alias in app_aliases:
            for m in re.finditer(rf"\b{alias}\.([A-Za-z_]\w*)", text):
                uses["app"].add(m.group(1))
        for alias in eval_aliases:
            for m in re.finditer(rf"\b{alias}\.([A-Za-z_]\w*)", text):
                uses["eval"].add(m.group(1))
    print("app attrs:", json.dumps(sorted(uses["app"])))
    print()
    print("eval attrs:", json.dumps(sorted(uses["eval"])))
    print()


if __name__ == "__main__":
    top_nodes(ROOT / "app.py")
    top_nodes(ROOT / "src" / "llm_evaluator.py")
    seams()
    attribute_uses()
