"""Streamlit Web UI for Classroom AutoGrader.

Provides a clean four-tab interface wrapping our grading and release backend:
  1. Dashboard: home view - scan the active courses, show which submissions
     are turned in but not yet returned (i.e. waiting for review), grouped by
     assignment, plus an automated **dry-run** that computes predicted scores
     and feedback for review. The dry-run is structurally read-only
     (``DRY_RUN = True``) and can never push grades or comments back.
  2. Evaluate Submissions: pick course, assignment, config -> evaluate turned-in docs -> export CSV + JSON report.
  3. Release Grades & Feedback: choose saved report -> preview students/scores -> push grades, private comments, doc feedback, return submissions -> prune released files.
  4. Daily Reports: browse saved read-only daily-check scans from
     ``exports/daily_checks/`` (written by ``python -m src.daily_check``
     or the 06:00 scheduled task) - metrics, per-assignment submission
     states, waiting students, warnings. Local files only, zero API calls.

Transient network failures (a dropped socket, Windows ``WinError 10053``) are
retried inside :mod:`src.api_retry`; :func:`_register_retry_notifier` surfaces
those retries in the UI so a long pause never looks like a frozen app.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from config.settings import settings
from src import config_match
from src.api_retry import add_retry_notifier, clear_retry_notifiers
from src.classroom_service import (
    ClassroomService,
    ClassroomServiceError,
    is_auth_failure,
)
from src.dashboard import (
    DRY_RUN,
    DryRunAssignmentResult,
    PendingGroup,
    coerce_score_column,
    collect_pending_submissions,
    dry_run_rows,
    pending_rows,
    run_dry_run_evaluation,
    summarize_pending,
)
from src.daily_reports import (
    DAILY_CHECK_JSON,
    DEFAULT_DAILY_CHECKS_DIR,
    has_report_for_today,
    list_daily_reports,
    load_daily_report,
)
from src.fetch_submissions import UNSUPPORTED_ATTACHMENT_MSG
from src.late_policy import penalty_for
from src.grade_audit import AuditVerdict, summarize_verdicts
from src.report_audit import DAILY_CHECKS_DIRNAME, _shorten, find_report_files
from src.llm_evaluator import (
    EVAL_PAUSE_SECONDS,
    LLMEvaluationError,
    StudentGradeRequest,
    evaluate_student_submission,
    grade_students_batch,
    last_audit_for,
    paced_pause,
)
from src.llm_router import DailyQuotaExhausted
from src.main import (
    EXPORTS_DIR,
    GradingRow,
    _delete_report_artifacts,
    _tasks_summary,
    _fallback_notice,
    audit_summary,
    deductions_summary,
    load_rubric,
    mashov_csv_name,
    report_directory,
    resolve_mapped_config,
    save_assignment_mapping,
    write_evaluation_report,
    write_mashov_csv,
)
from src.report_label import label_from_json
from src.models import (
    AssignmentConfig,
    AssignmentSpec,
    SpecQuestion,
    SpecSection,
    Course,
    CourseWork,
    EvaluationReport,
    StudentSubmission,
)

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent / "config"

# ---------------------------------------------------------------------------
# Backward-compatible re-exports
# ---------------------------------------------------------------------------
# app.py is now a thin facade: every name the old monolith defined lives in a
# single-responsibility module under src/ui/ and is re-exported here, so
# ``import app`` and ``app.<name>`` (including test monkeypatching of seams)
# keep working unchanged.
from src.ui.auth import (
    _cached_classroom_service,
    _handle_google_error,
    _register_retry_notifier,
    _render_google_auth_error,
    get_classroom_service,
)
from src.ui.catchup import (
    DAILY_CHECK_TASK_NAME,
    _CATCHUP_STATE,
    _detached_catchup,
    _ensure_today_report_catchup,
    _schtasks_catchup,
    trigger_daily_check_catchup,
)
from src.ui.daily_reports import (
    _render_daily_audit_section,
    render_daily_reports_tab,
)
from src.ui.dashboard import (
    _build_dashboard_resolver,
    _dry_run_result_key,
    _load_course_work,
    _load_courses,
    _load_pending_dashboard,
    _render_dry_run_assignment,
    _render_dry_run_results,
    _render_inline_dry_run,
    _restore_course_work,
    _restore_courses,
    _restore_pending_groups,
    _run_dry_run_action,
    _snapshot_pending_groups,
    render_dashboard_tab,
)
from src.ui.evaluate import (
    _render_audit_rollup,
    _run_evaluation_action,
    render_evaluate_tab,
)
from src.ui.helpers import (
    _config_display_path,
    _normalize_text,
    _read_config_title,
    _render_quota_notice,
    _score_config_for_assignment,
    _text_tokens,
    doc_url,
    get_all_config_files,
    suggest_config_for_assignment,
)
from src.ui.progress import (
    _LiveProgress,
)
from src.ui.release import (
    _entry_audit_badge,
    _load_release_report,
    _looks_like_daily_check,
    _persist_report_edits,
    _render_editable_entries,
    _render_publish_options,
    _render_report_audit_warning,
    _run_release_action,
    render_release_tab,
)

def main() -> None:
    st.set_page_config(
        page_title="Classroom AutoGrader",
        page_icon="🎓",
        layout="wide",
    )
    st.title("🎓 Google Classroom AutoGrader")

    # Surface automatic API retries (dropped socket / 5xx) in the UI so a
    # brief pause never looks like a frozen app.
    _register_retry_notifier()

    # DAILY CHECK TEMPORARILY DISABLED (re-enable by restoring the call below).
    # The 06:00 Windows scheduled task has been unregistered, and this startup
    # catch-up is intentionally skipped until you ask to turn it back on.
    # _ensure_today_report_catchup()

    with st.sidebar:
        st.caption("Google connection")
        st.caption(
            "Writing feedback into a student's document needs the "
            "`documents` write scope. If feedback publishing fails with a "
            "403, reconnect after running `python src/auth.py`. If Google "
            "reports `invalid_grant` (token expired or revoked), delete "
            "`token.json` first, then re-authorize and reconnect."
        )
        if st.button("🔄 Reconnect Google", width="stretch"):
            _cached_classroom_service.clear()
            try:
                _load_courses.clear()
            except Exception:  # noqa: BLE001 - cache may not exist yet
                logger.debug("course cache clear skipped", exc_info=True)
            try:
                _load_course_work.clear()
            except Exception:  # noqa: BLE001 - cache may not exist yet
                logger.debug("coursework cache clear skipped", exc_info=True)
            try:
                _load_pending_dashboard.clear()
            except Exception:  # noqa: BLE001 - cache may not exist yet
                logger.debug("pending dashboard cache clear skipped", exc_info=True)
            st.rerun()
        st.divider()
        st.caption("Safety")
        st.caption(
            f"Env `DRY_RUN`: **{'on' if settings.dry_run else 'off'}** · "
            f"Dashboard dry-run: **DRY_RUN = {DRY_RUN}**. The Dashboard "
            "evaluates and displays predictions only; grades, comments and "
            "returns happen solely in **Release Grades & Feedback**."
        )

    # The Google service can fail (expired token, missing scope, bad
    # credentials) - and it must never take the whole UI down with it. The
    # tabs are therefore always rendered; only the three Google-backed tabs
    # need the service. "📅 Daily Reports" reads local files exclusively
    # (``exports/daily_checks/``), so it stays fully usable - the tab a
    # teacher needs precisely to check whether the 06:00 automation ran.
    try:
        service: Optional[ClassroomService] = get_classroom_service()
        service_error: Optional[Exception] = None
    except Exception as exc:
        service = None
        service_error = exc

    def _service_ready() -> bool:
        """Show the connection error inside the calling tab, if any."""
        if service is None:
            if service_error is not None and is_auth_failure(service_error):
                _render_google_auth_error(service_error)
            else:
                st.error(
                    f"Failed to initialize Google Classroom service: {service_error}\n\n"
                    "Please check that credentials or authentication tokens exist "
                    "(run `python src/auth.py`), then reload."
                )
            return False
        return True

    tab0, tab1, tab2, tab3 = st.tabs(
        ["🏠 Dashboard", "Evaluate Submissions", "Release Grades & Feedback", "📅 Daily Reports"]
    )

    with tab0:
        if _service_ready():
            render_dashboard_tab(service)

    with tab1:
        if _service_ready():
            render_evaluate_tab(service)

    with tab2:
        if _service_ready():
            render_release_tab(service)

    with tab3:
        # Local files only - never gated behind the Google connection.
        render_daily_reports_tab()


if __name__ == "__main__":
    main()
