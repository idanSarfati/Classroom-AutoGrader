"""Generate the remaining src/ui modules + app.py shim from app.py (temporary).

Extracts top-level function/class bodies VERBATIM by line range and wraps each
in a generated module with:
  * the original import block (so every stdlib/third-party/src name resolves);
  * static imports for app-defined names that moved to sibling ui modules;
  * seam-wiring that rebinds monkeypatchable names to facade-resolving proxies.

Prints a report of every generated module, the seams it wires, and any
unclassified free name (so mistakes surface before pytest does).
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path("app.py")
SRC = APP.read_text(encoding="utf-8")
LINES = SRC.splitlines()
TREE = ast.parse(SRC, filename="app.py")

IMPORT_BLOCK_START = 21   # 'from __future__ ...'
IMPORT_BLOCK_END = 104    # last import line


def node_span(node):
    decs = [d.lineno for d in getattr(node, "decorator_list", [])]
    start = min(decs + [node.lineno])
    return start, node.end_lineno


def slice_lines(start, end):
    return "\n".join(LINES[start - 1 : end])


# Top-level defined names -> source span.
defined_span: dict[str, tuple[int, int]] = {}
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined_span[node.name] = node_span(node)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                defined_span[t.id] = node_span(node)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        defined_span[node.target.id] = node_span(node)

# Imported names -> source module (for static re-import).
imported_from: dict[str, str] = {}
for node in TREE.body:
    if isinstance(node, ast.ImportFrom):
        mod = ("." * node.level) + (node.module or "")
        for a in node.names:
            if a.name != "*":
                imported_from[a.asname or a.name] = mod
    elif isinstance(node, ast.Import):
        for a in node.names:
            imported_from[a.asname or a.name.split(".")[0]] = a.name

IMPORTED_NAMES = set(imported_from)
DEFINED_NAMES = set(defined_span)

# Which ui module each app-defined name moved to.
DEFINED_TARGET = {}
for n in ["_register_retry_notifier", "_cached_classroom_service", "_render_google_auth_error",
          "_handle_google_error", "get_classroom_service"]:
    DEFINED_TARGET[n] = "auth"
for n in ["doc_url", "_normalize_text", "_text_tokens", "_config_display_path",
          "_read_config_title", "_score_config_for_assignment", "suggest_config_for_assignment",
          "get_all_config_files",
          "_render_quota_notice"]:   # shared by evaluate+dashboard -> leaf helper (breaks the cycle)
    DEFINED_TARGET[n] = "helpers"
DEFINED_TARGET["_LiveProgress"] = "progress"
for n in ["DAILY_CHECK_TASK_NAME", "_CATCHUP_STATE", "_schtasks_catchup", "_detached_catchup",
          "trigger_daily_check_catchup", "_ensure_today_report_catchup"]:
    DEFINED_TARGET[n] = "catchup"
for n in ["render_evaluate_tab", "_run_evaluation_action", "_render_audit_rollup"]:
    DEFINED_TARGET[n] = "evaluate"
for n in ["render_daily_reports_tab", "_render_daily_audit_section"]:
    DEFINED_TARGET[n] = "daily_reports"
for n in ["_looks_like_daily_check", "_load_release_report", "render_release_tab", "_render_publish_options",
          "_entry_audit_badge", "_render_report_audit_warning", "_render_editable_entries",
          "_persist_report_edits", "_run_release_action"]:
    DEFINED_TARGET[n] = "release"
for n in ["_load_courses", "_restore_courses", "_load_course_work", "_restore_course_work",
          "_snapshot_pending_groups", "_restore_pending_groups", "_load_pending_dashboard",
          "_build_dashboard_resolver", "_run_dry_run_action", "_dry_run_result_key",
          "_render_inline_dry_run", "_render_dry_run_assignment", "_render_dry_run_results",
          "render_dashboard_tab"]:
    DEFINED_TARGET[n] = "dashboard"

# Seam classification.
#  ST: the 'st' attribute proxy.
#  UI: seam whose real impl lives in a sibling ui module and is ALREADY a proxy
#      there -> import it directly, do NOT re-wrap.
#  EXT: seam whose real impl is an app-level import -> rebind to a proxy with
#       the imported real function as default.
UI_PROXIED = {
    "_load_courses": "dashboard", "_load_course_work": "dashboard",
    "_load_pending_dashboard": "dashboard",
    "_CATCHUP_STATE": "catchup", "trigger_daily_check_catchup": "catchup",
    "get_all_config_files": "helpers",
}
EXT_SEAMS = {
    "has_report_for_today", "list_daily_reports", "load_daily_report",
    "resolve_mapped_config", "load_rubric", "save_assignment_mapping",
    "_delete_report_artifacts", "run_dry_run_evaluation",
    "grade_students_batch", "evaluate_student_submission", "paced_pause",
}
ALL_SEAMS = EXT_SEAMS | set(UI_PROXIED) | {"st"}

# Module layout: ui module -> (docstring, app-defined names to include).
LAYOUT = {
    "evaluate": (
        "Evaluate tab: pick a course/assignment, load its rubric, run a paced "
        "grading batch, and save results back to Classroom.",
        ["render_evaluate_tab", "_run_evaluation_action",
         "_render_audit_rollup"],
    ),
    "daily_reports": (
        "Daily Reports tab: list past daily-check reports, render today's "
        "report with its audit rollup, and offer the one-click catch-up.",
        ["render_daily_reports_tab", "_render_daily_audit_section"],
    ),
    "release": (
        "Release tab: preview a report, edit entries inline, publish grades + "
        "feedback comments to Classroom, and clean up report artifacts.",
        ["_looks_like_daily_check", "_load_release_report", "render_release_tab",
         "_render_publish_options", "_entry_audit_badge", "_render_report_audit_warning",
         "_render_editable_entries", "_persist_report_edits", "_run_release_action"],
    ),
    "dashboard": (
        "Dashboard tab: cache Google course/coursework/pending-submission "
        "loaders, run the read-only dry-run pipeline, and render pending groups.",
        ["_load_courses", "_restore_courses", "_load_course_work", "_restore_course_work",
         "_snapshot_pending_groups", "_restore_pending_groups", "_load_pending_dashboard",
         "_build_dashboard_resolver", "_run_dry_run_action", "_dry_run_result_key",
         "_render_inline_dry_run", "_render_dry_run_assignment", "_render_dry_run_results",
         "render_dashboard_tab"],
    ),
}


def free_names(nodes) -> set[str]:
    loads: set[str] = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                loads.add(sub.id)
    return loads


def nodes_for(names: list[str]):
    by_name = {}
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            by_name[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    by_name[t.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            by_name[node.target.id] = node
    return [by_name[n] for n in names]


def build_module(mod: str, doc: str, names: list[str]) -> str:
    nodes = nodes_for(names)
    refs = free_names(nodes)
    seams = sorted(refs & ALL_SEAMS)

    # cross-module ui imports: (a) non-seam app-defined names from siblings,
    # (b) UI-proxied seams defined in a *different* sibling.
    cross: dict[str, set[str]] = {}
    unclassified: set[str] = set()
    for name in sorted(refs & DEFINED_NAMES):
        if name in names or name in ALL_SEAMS:
            continue
        target = DEFINED_TARGET.get(name)
        if target and target != mod:
            cross.setdefault(f"src.ui.{target}", set()).add(name)
        elif not target:
            unclassified.add(name)
    for name in seams:
        if name in UI_PROXIED and UI_PROXIED[name] != mod:
            cross.setdefault(f"src.ui.{UI_PROXIED[name]}", set()).add(name)

    out: list[str] = []
    out.append(f'"""{doc}')
    out.append("")
    out.append("Extracted verbatim from :mod:`app` (the former monolithic app.py).")
    out.append('"""')
    out.append("")
    out.append(slice_lines(IMPORT_BLOCK_START, IMPORT_BLOCK_END))
    out.append("")
    for srcmod in sorted(cross):
        out.append(f"from {srcmod} import (")
        for n in sorted(cross[srcmod]):
            out.append(f"    {n},")
        out.append(")")
    needs = []
    if any(n != "st" and not (n in UI_PROXIED and UI_PROXIED[n] != mod) for n in seams):
        needs.append("seam_fn")
    if "st" in seams:
        needs.append("st_proxy")
    if needs:
        out.append(f"from src.seams import {', '.join(needs)}")
    out.append("")
    out.append("logger = logging.getLogger(__name__)")
    out.append("")
    out.append("\n\n".join(slice_lines(*defined_span[n]) for n in names))
    out.append("")
    out.append("# " + "-" * 70)
    out.append("# Seam wiring")
    out.append("# " + "-" * 70)
    out.append("# Tests monkeypatch these on the ``app`` facade. The bodies above reference")
    out.append("# them as plain globals; Python resolves globals at call time, so rebinding")
    out.append("# them here to facade-resolving proxies honours a patch without touching")
    out.append("# any call site. UI-proxied seams are imported (already proxies) - not rewrapped.")
    for name in seams:
        if name == "st":
            out.append('st = st_proxy("app", st)')
        elif name in UI_PROXIED and UI_PROXIED[name] != mod:
            continue
        else:
            out.append(f'{name} = seam_fn("app", "{name}", {name})')
    text = "\n".join(out) + "\n"
    return text, seams, unclassified


DONE_NAMES = {
    "progress": ["_LiveProgress"],
    "helpers": ["doc_url", "_normalize_text", "_text_tokens", "_config_display_path",
                "_read_config_title", "_score_config_for_assignment",
                "suggest_config_for_assignment", "get_all_config_files",
                "_render_quota_notice"],
    "auth": ["_register_retry_notifier", "_cached_classroom_service",
             "_render_google_auth_error", "_handle_google_error", "get_classroom_service"],
    "catchup": ["DAILY_CHECK_TASK_NAME", "_CATCHUP_STATE", "_schtasks_catchup",
                "_detached_catchup", "trigger_daily_check_catchup",
                "_ensure_today_report_catchup"],
}


def build_shim() -> str:
    # Re-export every moved name from its ui module.
    reexport: dict[str, list[str]] = {}
    for tgt, nlist in DONE_NAMES.items():
        reexport[f"src.ui.{tgt}"] = list(nlist)
    for tgt, (_doc, nlist) in LAYOUT.items():
        reexport.setdefault(f"src.ui.{tgt}", []).extend(nlist)

    out: list[str] = []
    out.append(slice_lines(1, 19).rstrip())  # original module docstring
    out.append("")
    out.append(slice_lines(IMPORT_BLOCK_START, IMPORT_BLOCK_END))
    out.append("")
    out.append(slice_lines(106, 108).rstrip())  # logger + CONFIG_DIR
    out.append("")
    out.append("# ---------------------------------------------------------------------------")
    out.append("# Backward-compatible re-exports")
    out.append("# ---------------------------------------------------------------------------")
    out.append("# app.py is now a thin facade: every name the old monolith defined lives in a")
    out.append("# single-responsibility module under src/ui/ and is re-exported here, so")
    out.append("# ``import app`` and ``app.<name>`` (including test monkeypatching of seams)")
    out.append("# keep working unchanged.")
    for srcmod in sorted(reexport):
        nlist = sorted(set(reexport[srcmod]))
        out.append(f"from {srcmod} import (")
        for n in nlist:
            out.append(f"    {n},")
        out.append(")")
    out.append("")
    out.append(slice_lines(2300, 2396).rstrip())  # main()
    out.append("")
    out.append("")
    out.append('if __name__ == "__main__":')
    out.append("    main()")
    return "\n".join(out) + "\n"


def main() -> None:
    ui_dir = Path("src/ui")
    ui_dir.mkdir(parents=True, exist_ok=True)
    problems = 0
    for mod, (doc, names) in LAYOUT.items():
        text, seams, unclassified = build_module(mod, doc, names)
        (ui_dir / f"{mod}.py").write_text(text, encoding="utf-8")
        try:
            compile(text, f"{mod}.py", "exec")
        except SyntaxError as e:
            print(f"SYNTAX ERROR in {mod}.py: {e}")
            problems += 1
        print(f"{mod}.py  ({len(text.splitlines())} lines)  seams={seams}")
        if unclassified:
            print(f"   !! unclassified app-defined refs: {sorted(unclassified)}")
    # src/ui/__init__.py
    (ui_dir / "__init__.py").write_text(
        '"""Single-responsibility UI modules extracted from the former app.py monolith."""\n',
        encoding="utf-8",
    )
    # app.py shim
    shim = build_shim()
    Path("app.py").write_text(shim, encoding="utf-8")
    try:
        compile(shim, "app.py", "exec")
    except SyntaxError as e:
        print(f"SYNTAX ERROR in app.py shim: {e}")
        problems += 1
    print(f"app.py shim ({len(shim.splitlines())} lines)")
    print("OK" if problems == 0 else f"{problems} SYNTAX PROBLEMS")


if __name__ == "__main__":
    main()



