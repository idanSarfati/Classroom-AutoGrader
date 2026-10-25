"""Map which seam names each top-level node in a facade references (temporary)."""
from __future__ import annotations

import ast
from pathlib import Path

# Names tests (or the facade itself) monkeypatch on the facade module.
APP_SEAMS = {
    "st", "has_report_for_today", "_CATCHUP_STATE", "_schtasks_catchup",
    "_detached_catchup", "trigger_daily_check_catchup", "_ensure_today_report_catchup",
    "list_daily_reports", "load_daily_report", "resolve_mapped_config",
    "EVAL_PAUSE_SECONDS", "run_dry_run_evaluation", "_load_pending_dashboard",
    "_delete_report_artifacts", "_load_courses", "_load_course_work",
    "grade_students_batch", "evaluate_student_submission", "paced_pause",
    "load_rubric", "save_assignment_mapping", "get_all_config_files",
}

EVAL_SEAMS = {
    "_ROUTER", "_ROUTER_READY", "_client", "_get_client", "_create_completion",
    "_call_completion", "_parse_result", "_first_working_stage", "_pacer",
    "EVAL_PAUSE_SECONDS", "evaluate_student_submission", "grade_students_batch",
    "paced_pause", "_serve_decision", "_note_rate_limited", "_rate_limit_wait",
    "_parse_payload", "_serve_from_router", "_router", "_client_for",
    "_AUDIT_SIDECAR", "last_audit_for", "_run_audit",
}


def scan(path: str, seams: set[str]) -> None:
    src = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=path)
    print(f"===== {path} =====")
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        used: set[str] = set()
        assigned: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                if isinstance(sub.ctx, ast.Load):
                    if sub.id in seams:
                        used.add(sub.id)
                elif isinstance(sub.ctx, ast.Store):
                    assigned.add(sub.id)
            elif isinstance(sub, ast.Global):
                assigned.update(sub.names)
        tag = node.name
        if used:
            print(f"  {tag:32} USES: {', '.join(sorted(used))}")
        else:
            print(f"  {tag:32} (clean)")
        # Flag seam names assigned inside the node (global writes -> mutable seams).
        writes = assigned & seams
        if writes:
            print(f"  {'':32} WRITES: {', '.join(sorted(writes))}")


scan("app.py", APP_SEAMS)
print()
scan("src/llm_evaluator.py", EVAL_SEAMS)
