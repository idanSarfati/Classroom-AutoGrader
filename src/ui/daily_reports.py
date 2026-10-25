"""Daily Reports tab: list past daily-check reports, render today's report with its audit rollup, and offer the one-click catch-up.

Extracted verbatim from :mod:`app` (the former monolithic app.py).
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

from src.ui.catchup import (
    _CATCHUP_STATE,
    trigger_daily_check_catchup,
)
from src.seams import seam_fn, st_proxy

logger = logging.getLogger(__name__)

def render_daily_reports_tab() -> None:
    """Daily Reports: browse saved read-only daily-check scans (local files).

    Reads ``exports/daily_checks/*/daily_check.json`` written by
    ``python -m src.daily_check --once`` (or the 06:00 scheduled task) and
    shows headline metrics, per-assignment submission states, the waiting
    students, and any warnings. Purely local: makes zero Google API calls and
    can never publish anything.

    When today's report is missing (e.g. the machine was powered off at
    06:00), the tab falls back to the latest snapshot on disk, explains the
    gap, and offers a one-click read-only catch-up run.
    """
    st.subheader("📅 Daily Reports")
    st.caption(
        "Saved results of the read-only daily background scan "
        f"(`{DEFAULT_DAILY_CHECKS_DIR.as_posix()}`). "
        "Each run lists every assignment's submission states; "
        "publishing still happens only in **Release Grades & Feedback**."
    )
    st.info(
        "🔒 **Read-only history** — these files are snapshots for review. "
        "Nothing here pushes grades, comments, or returns.",
        icon="🛡️",
    )

    metas = list_daily_reports()

    # Freshness banner + fallback trigger. `has_report_for_today` is a pure
    # local-filesystem probe; when today's folder is missing (machine was off
    # at 06:00, task not registered yet, ...) the tab shows the latest
    # snapshot it *does* have and offers to run the read-only check now.
    today_ok = has_report_for_today()
    if not today_ok:
        latest_id = metas[0].run_id if metas else None
        st.warning(
            "No daily report for **today** yet. "
            + (f"Showing the latest saved snapshot ({latest_id}) below. " if latest_id else "")
            + "The 06:00 task is registered with `StartWhenAvailable`, so it "
            "catches up automatically after boot/wake — you can also run the "
            "read-only check right now:",
            icon="⏳",
        )
        # DAILY CHECK TEMPORARILY DISABLED - manual catch-up button hidden.
        # Re-enable by restoring the st.button block below.
        # if st.button("🚀 Run today's read-only check now", key="daily_reports_catchup"):
        #     ok, status = trigger_daily_check_catchup()
        #     (st.success if ok else st.error)(status)
        #     if ok:
        #         st.caption("The scan runs in the background; refresh in a minute to see today's report.")
        # elif _CATCHUP_STATE["attempted"] and _CATCHUP_STATE["status"]:
        #     # Surface what the automatic startup catch-up did (once per process).
        #     st.caption(f"Automatic catch-up: {_CATCHUP_STATE['status']}")
    if not metas:
        st.info(
            "No daily reports yet. Run `python -m src.daily_check --once` once, "
            "or wait for the 06:00 scheduled task "
            "(`ClassroomAutoGrader-DailyCheck`) to produce the first one."
        )
        return

    labels = []
    for meta in metas:
        headline = (
            f"{meta.run_id} — {meta.pending_count} pending / "
            f"{meta.submission_count} seen"
        )
        if meta.load_error:
            headline += " ⚠️"
        labels.append(headline)
    # Default to the newest *loadable* snapshot: a broken/half-written folder
    # at the top must not blank the tab when older good data exists.
    default_index = next(
        (i for i, m in enumerate(metas) if not m.load_error and m.json_path is not None),
        0,
    )
    selected = st.selectbox(
        "Daily run",
        range(len(labels)),
        index=default_index,
        format_func=lambda i: labels[i],
        key="daily_reports_run",
    )
    meta = metas[selected]
    if meta.load_error and meta.json_path is None:
        st.warning(meta.load_error)
        return

    detail = load_daily_report(meta.json_path or meta.directory)
    if detail.load_error:
        st.error(detail.load_error)
        return

    st.markdown(
        f"**Generated:** {detail.generated_at or detail.run_id} &nbsp;|&nbsp; "
        f"**Project:** `{detail.project_id or 'unknown'}` &nbsp;|&nbsp; "
        f"**Mode:** {detail.mode or 'read-only/simulation'}"
    )
    t1, t2, t3, t4, t5 = st.columns(5)
    t1.metric("Courses", detail.course_count)
    t2.metric("Assignments", detail.coursework_count)
    t3.metric("Submissions seen", detail.submission_count)
    t4.metric("Pending review", detail.pending_count, delta=f"{detail.late_pending_count} late" if detail.late_pending_count else None)
    t5.metric("Returned", detail.returned_count)

    if detail.pending_rows:
        st.markdown("#### ⏳ Waiting for review")
        st.dataframe(
            pd.DataFrame(detail.pending_rows),
            width="stretch",
            hide_index=True,
        )
    else:
        st.success("🎉 No pending submissions in this run — all handed back.")

    st.markdown("#### 📚 All assignments (submission states)")
    course_names = ["All courses"] + sorted({r["Course"] for r in detail.assignment_rows if r["Course"]})
    picked = st.selectbox("Course filter", course_names, key="daily_reports_course")
    rows = detail.assignment_rows
    if picked != "All courses":
        rows = [r for r in rows if r["Course"] == picked]
    only_pending = st.checkbox("Show only assignments with pending work", value=False, key="daily_reports_only_pending")
    if only_pending:
        rows = [r for r in rows if r["Pending"]]
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info("No assignments match this filter.")

    _render_daily_audit_section(detail)

    if detail.errors:
        with st.expander(f"⚠️ Warnings/errors ({len(detail.errors)})"):
            for message in detail.errors:
                st.write(f"- {message}")

    if detail.summary_path is not None:
        try:
            summary_text = detail.summary_path.read_text(encoding="utf-8")
        except OSError:
            summary_text = ""
        if summary_text.strip():
            with st.expander("📄 Raw summary.txt"):
                st.code(summary_text, language="text")

def _render_daily_audit_section(detail) -> None:
    """Show this run's grade-audit verdicts: what needs a human, and why.

    The audit is the part of the daily check that answers "are the grades I
    haven't released yet actually right?" - and it is split on purpose:
    per-grade problems are listed by student (that is real work), while
    systemic notes are rolled up as rubric/prompt advice (one fix, not many).
    """
    audit = detail.audit
    stats = audit.get("stats") if isinstance(audit.get("stats"), dict) else {}
    total = int(stats.get("total", 0) or 0)
    if not total:
        st.caption(
            "🔍 No graded reports to audit in this run (audit covers saved "
            "evaluation reports; it costs no tokens)."
        )
        return

    st.markdown("#### 🔍 Grade audit")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Grades audited", total)
    c2.metric("Clean", int(stats.get("clean", 0) or 0))
    c3.metric("Auto-fixed", int(stats.get("autofixed", 0) or 0))
    c4.metric("Needs review", int(stats.get("needs_review", 0) or 0))

    findings = detail.audit_findings
    if findings:
        st.warning(
            f"{len(findings)} grade(s) need a human look. Nothing was changed "
            "or released — fix the cause and re-grade, or adjust the score "
            "in **Release Grades & Feedback**."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Student": f.get("Student", ""),
                        "Assignment": f.get("Assignment", ""),
                        "Score": f.get("Score"),
                        "Status": f.get("Status", ""),
                        "Flags": f.get("Flags", ""),
                        "Report": f.get("Report", ""),
                    }
                    for f in findings
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        with st.expander(f"🧾 Audit detail ({len(findings)})"):
            for item in findings:
                st.markdown(
                    f"**{item.get('Student', '')}** — {item.get('Assignment', '')} "
                    f"· {item.get('Status', '')}"
                )
                for finding in item.get("Findings") or []:
                    if isinstance(finding, dict):
                        st.write(
                            f"- `{finding.get('flag', '')}` "
                            f"{finding.get('message', '')}"
                        )
    else:
        st.success("Every audited grade is self-consistent.")

    systemic = stats.get("systemic") or {}
    if systemic:
        st.caption(
            "Rubric/prompt advice (same for the whole class — fix once): "
            + ", ".join(
                f"`{flag}` × {count}" for flag, count in systemic.items()
            )
        )

    if detail.summary_path is not None:
        try:
            summary_text = detail.summary_path.read_text(encoding="utf-8")
        except OSError:
            summary_text = ""
        if summary_text.strip():
            with st.expander("📄 Raw summary.txt"):
                st.code(summary_text, language="text")

# ----------------------------------------------------------------------
# Seam wiring
# ----------------------------------------------------------------------
# Tests monkeypatch these on the ``app`` facade. The bodies above reference
# them as plain globals; Python resolves globals at call time, so rebinding
# them here to facade-resolving proxies honours a patch without touching
# any call site. UI-proxied seams are imported (already proxies) - not rewrapped.
has_report_for_today = seam_fn("app", "has_report_for_today", has_report_for_today)
list_daily_reports = seam_fn("app", "list_daily_reports", list_daily_reports)
load_daily_report = seam_fn("app", "load_daily_report", load_daily_report)
st = st_proxy("app", st)
