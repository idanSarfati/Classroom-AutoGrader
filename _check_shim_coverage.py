"""Coverage check: every name app.py defines must survive as a shim export."""
import ast
from pathlib import Path

SRC = Path("app.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

defined = {}
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined[node.name] = type(node).__name__
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                defined[t.id] = "Assign"
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        defined[node.target.id] = "AnnAssign"

DONE = {
    "_LiveProgress",
    "doc_url", "_normalize_text", "_text_tokens", "_config_display_path",
    "_read_config_title", "_score_config_for_assignment",
    "suggest_config_for_assignment", "get_all_config_files",
    "_register_retry_notifier", "_cached_classroom_service",
    "_render_google_auth_error", "_handle_google_error", "get_classroom_service",
    "DAILY_CHECK_TASK_NAME", "_CATCHUP_STATE", "_schtasks_catchup",
    "_detached_catchup", "trigger_daily_check_catchup",
    "_ensure_today_report_catchup",
    # LAYOUT names:
    "render_evaluate_tab", "_run_evaluation_action", "_render_quota_notice",
    "_render_audit_rollup",
    "render_daily_reports_tab", "_render_daily_audit_section",
    "_looks_like_daily_check", "_load_release_report", "render_release_tab",
    "_render_publish_options", "_entry_audit_badge", "_render_report_audit_warning",
    "_render_editable_entries", "_persist_report_edits", "_run_release_action",
    "_load_courses", "_restore_courses", "_load_course_work", "_restore_course_work",
    "_snapshot_pending_groups", "_restore_pending_groups", "_load_pending_dashboard",
    "_build_dashboard_resolver", "_run_dry_run_action", "_dry_run_result_key",
    "_render_inline_dry_run", "_render_dry_run_assignment", "_render_dry_run_results",
    "render_dashboard_tab",
}
KEPT = {"main", "logger", "CONFIG_DIR"}

missing_from_layout = sorted(set(DONE) - set(defined))
uncovered = sorted(n for n in defined if n not in DONE and n not in KEPT)

print("names listed for extraction but NOT defined in app.py:", missing_from_layout or "none")
print("\napp.py definitions NOT covered by shim export:")
for n in uncovered:
    print(f"  {n}  ({defined[n]})")
if not uncovered:
    print("  (none - shim export is complete)")

# Does any test reference app.<name> for an uncovered name? Cheap heuristic:
# search tests for `app.<name>` / `app_module.<name>` attribute reads.
import re
attr_pat = re.compile(r"\b(?:app|app_module|mod|workflow_app|dashboard_app|daily_app)\.(\w+)")
refs = set()
for f in Path("tests").rglob("*.py"):
    for m in attr_pat.finditer(f.read_text(encoding="utf-8", errors="replace")):
        refs.add(m.group(1))
bad = sorted(refs & set(uncovered) & set(defined))
print("\nuncovered names REFERENCED by tests:", bad or "none")
