"""Phase-2 probe: seam usage sites + full evaluator map. Output -> _refactor_probe_out.txt."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "_refactor_probe_out.txt"

APP_SEAMS = [
    "st", "has_report_for_today", "_CATCHUP_STATE", "_schtasks_catchup",
    "_detached_catchup", "trigger_daily_check_catchup", "_ensure_today_report_catchup",
    "list_daily_reports", "load_daily_report", "resolve_mapped_config",
    "EVAL_PAUSE_SECONDS", "run_dry_run_evaluation", "_load_pending_dashboard",
    "_delete_report_artifacts", "_load_courses", "_load_course_work",
    "grade_students_batch", "evaluate_student_submission", "paced_pause",
    "load_rubric", "save_assignment_mapping", "get_all_config_files",
]

EVAL_SEAMS = [
    "_ROUTER", "_ROUTER_READY", "_client", "_get_client", "_create_completion",
    "_call_completion", "_parse_result", "_first_working_stage", "_pacer",
    "EVAL_PAUSE_SECONDS", "evaluate_student_submission", "grade_students_batch",
    "paced_pause", "_serve_decision", "_note_rate_limited", "_rate_limit_wait",
    "_parse_payload", "_serve_from_router", "_router", "_client_for",
]


def usages(path: Path, names: list[str], header: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    print(f"----- {header}: usage sites in {path.name} -----")
    for name in names:
        pat = re.compile(rf"(?<![\w.]){re.escape(name)}(?![\w])")
        hits = [i + 1 for i, ln in enumerate(lines) if pat.search(ln)]
        print(f"{name}: {hits}")
    print()


def eval_nodes() -> None:
    path = ROOT / "src" / "llm_evaluator.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    print(f"===== src/llm_evaluator.py top-level (total {len(src.splitlines())}) =====")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign):
            name = ",".join(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            name = "import: " + ", ".join((a.asname or a.name) for a in node.names)
        else:
            name = type(node).__name__
        print(f"{node.lineno}-{node.end_lineno}: {name}")
    print()


def all_setattr_targets() -> None:
    print("===== every monkeypatch.setattr first-arg + attr (all tests) =====")
    pat = re.compile(r"monkeypatch\.setattr\(\s*([A-Za-z_][\w.]*)\s*,\s*['\"]([A-Za-z_]\w*)['\"]")
    pairs: dict[str, set[str]] = {}
    for test in sorted((ROOT / "tests").glob("*.py")):
        text = test.read_text(encoding="utf-8")
        for m in pat.finditer(text):
            line = text[: m.start()].count("\n") + 1
            pairs.setdefault(m.group(1), set()).add(f"{m.group(2)}@{test.name}:{line}")
    for var in sorted(pairs):
        print(f"{var}:")
        for item in sorted(pairs[var]):
            print(f"    {item}")
    print()


def alias_imports() -> None:
    print("===== module alias imports in tests =====")
    pat = re.compile(
        r"^\s*(?:import|from)\s+(?:app|src(?:\.\w+)*|llm_evaluator)\b.*",
        re.MULTILINE,
    )
    seen: dict[str, set[str]] = {}
    for test in sorted((ROOT / "tests").glob("*.py")):
        text = test.read_text(encoding="utf-8")
        for m in pat.finditer(text):
            stmt = m.group(0).strip()
            seen.setdefault(stmt, set()).add(test.name)
    for stmt in sorted(seen):
        print(f"{stmt}   <- {', '.join(sorted(seen[stmt]))}")
    print()


if __name__ == "__main__":
    with open(OUT, "w", encoding="utf-8") as fh:
        import contextlib

        with contextlib.redirect_stdout(fh):
            eval_nodes()
            usages(ROOT / "app.py", APP_SEAMS, "app seams")
            usages(ROOT / "src" / "llm_evaluator.py", EVAL_SEAMS, "eval seams")
            all_setattr_targets()
            alias_imports()
    print("written", OUT)
