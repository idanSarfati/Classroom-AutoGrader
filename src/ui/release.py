"""Release tab: preview a report, edit entries inline, publish grades + feedback comments to Classroom, and clean up report artifacts.

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

from src.ui.auth import (
    _render_google_auth_error,
)
from src.ui.helpers import (
    doc_url,
)
from src.seams import seam_fn, st_proxy

logger = logging.getLogger(__name__)

def _looks_like_daily_check(path: Path, data: object) -> bool:
    """True when a saved JSON is a daily-check scan, not a graded report.

    The background daily check writes ``exports/daily_checks/*/daily_check.json``
    (see ``src.daily_check`` / ``src.daily_reports``). Those files are read-only
    snapshots for the **Daily Reports** tab: they hold per-course submission
    counts, not the fields :class:`EvaluationReport` requires, so feeding one to
    the Release tab would only raise a wall of Pydantic "field required" errors.

    Detection is by name/location first (exact and cheap), then by JSON shape as
    a fallback for a file that was renamed or hand-moved out of that folder.
    """
    if path.name == DAILY_CHECK_JSON or DAILY_CHECKS_DIRNAME in path.parts:
        return True
    # A daily-check summary is keyed by "generated_at"/"mode"/"submission_count";
    # a graded report is keyed by "entries"/"course_name" (cf.
    # report_audit.looks_like_report). Bare "courses" is deliberately not enough:
    # the coursework-materials dump uses that key too, so it must not be
    # mistaken for a daily check.
    if isinstance(data, dict) and "entries" not in data:
        return any(
            key in data for key in ("generated_at", "mode", "submission_count")
        )
    return False

def _load_release_report(path: Path) -> tuple[Optional[EvaluationReport], Optional[str]]:
    """Read a saved report for the Release tab without ever raising.

    Returns ``(report, None)`` on success, or ``(None, notice)`` when the file is
    not a releasable :class:`EvaluationReport` - a daily-check summary, an
    unrelated JSON dump, or a truncated file. ``notice`` is a short, human
    sentence for the UI: a daily check is answered with guidance pointing at the
    **Daily Reports** tab, and any other problem keeps the raw Pydantic dump
    out of the way by condensing it through ``report_audit._shorten``.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError covers json.JSONDecodeError
        return None, f"Could not read this file as JSON: {_shorten(exc)}"

    if _looks_like_daily_check(path, raw):
        return None, (
            "This file is a **Daily Check Summary**, not an Evaluation Report. "
            "Please view it in the **📅 Daily Reports** tab."
        )

    try:
        return EvaluationReport.model_validate(raw), None
    except ValidationError as exc:
        return None, (
            "This file is not a valid Evaluation Report, so there is nothing to "
            f"release from it. ({_shorten(exc)})"
        )

def render_release_tab(service: ClassroomService) -> None:
    st.subheader("Release Grades & Feedback")
    st.caption(
        "Publish finalized grades and feedback comments to Google Docs, and return submissions "
        "to students. Fully released reports are automatically pruned."
    )

    # Only genuine EvaluationReports are offered here. find_report_files() skips
    # the daily_checks/ tree and every non-report JSON under exports/ (material
    # dumps, stray files), so a daily-check summary can never be selected by
    # mistake - and if one still slips through, _load_release_report() below
    # explains it instead of dumping a Pydantic traceback.
    reports = find_report_files(EXPORTS_DIR)
    if not reports:
        st.info("No saved evaluation reports found in `exports/`. Run an evaluation in Tab 1 first.")
        return

    # Clean, bidi-safe labels: "[tag] [class] assignment | date" built from each
    # report's own metadata instead of a raw exports/ path (whose Hebrew course
    # names carry invisible bidi controls that render garbled in the dropdown).
    # A file that can't be parsed for a label falls back to its relative path so
    # it still appears - selecting it then surfaces the graceful notice below.
    report_labels = {}
    for r_path in reports:
        label = label_from_json(r_path)
        if not label:
            try:
                rel_path = r_path.relative_to(EXPORTS_DIR).as_posix()
            except ValueError:
                rel_path = r_path.as_posix()
            mtime = datetime.fromtimestamp(r_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            label = f"{rel_path} ({mtime})"
        # Two runs can share a class/assignment/minute; keep keys unique.
        base, suffix = label, 2
        while label in report_labels:
            label = f"{base} #{suffix}"
            suffix += 1
        report_labels[label] = r_path

    selected_label = st.selectbox("Select Saved Evaluation Report", list(report_labels.keys()))
    selected_report_path = report_labels[selected_label]

    report, notice = _load_release_report(selected_report_path)
    if report is None:
        st.warning(notice, icon="🗂️")
        return

    st.markdown(
        f"**Course:** {report.course_name} &nbsp;|&nbsp; "
        f"**Assignment:** {report.course_work_title} &nbsp;|&nbsp; "
        f"**Generated:** {report.created_at.strftime('%Y-%m-%d %H:%M')} &nbsp;|&nbsp; "
        f"**Model:** `{report.model}`"
    )

    if not report.entries:
        st.warning("This report contains no graded student entries.")
        return

    st.info(
        "Review each student below. You can edit **Score** and **Feedback**, open the "
        "📄 submission directly, or tick **Remove ❌** to drop a row before publishing."
    )
    edited_report, removed_names = _render_editable_entries(selected_report_path, report)

    if removed_names:
        st.warning(
            f"Marked for removal ({len(removed_names)}): "
            + ", ".join(removed_names)
            + ". They will be dropped from the report JSON and Mashov CSV on save/publish."
        )

    options = _render_publish_options(selected_report_path)

    save_col, publish_col = st.columns(2)
    with save_col:
        if st.button(
            "💾 Save Changes",
            width="stretch",
            key=f"save_{selected_report_path.as_posix()}",
        ):
            _persist_report_edits(selected_report_path, edited_report)
            st.success(
                f"Saved {len(edited_report.entries)} student(s) to report JSON + Mashov CSV"
                + (f" (removed {len(removed_names)})." if removed_names else ".")
            )
            st.rerun()
    with publish_col:
        if st.button(
            "Publish Grades & Feedback",
            type="primary",
            width="stretch",
            key=f"publish_{selected_report_path.as_posix()}",
        ):
            _persist_report_edits(selected_report_path, edited_report)
            if not edited_report.entries:
                st.warning(
                    "All student rows were removed — saved the empty report "
                    "(JSON + CSV) and skipped publishing."
                )
            else:
                _run_release_action(
                    service, selected_report_path, edited_report, options
                )

def _render_publish_options(report_path: Path) -> dict[str, bool]:
    """Checkboxes choosing what the release step actually does per student.

    Google Classroom only lets a Developer Console project modify coursework
    that the *same* OAuth client created. When an assignment was made in the
    Classroom UI, pushing the grade fails with HTTP 403 - but the student's
    document is still perfectly writable, so the teacher can uncheck
    "Publish grades" and the Hebrew feedback is still left as a comment for
    the student to read.
    """
    st.markdown("**Publish options**")
    opt1, opt2 = st.columns(2)
    with opt1:
        publish_grades = st.checkbox(
            "Publish grades to Classroom",
            value=True,
            key=f"opt_grades_{report_path.as_posix()}",
            help=(
                "Set the score on the Classroom submission. Fails with HTTP 403 "
                "for coursework not created by this project's OAuth client."
            ),
        )
    with opt2:
        post_comment = st.checkbox(
            "Leave a comment on the Google Doc 💬",
            value=True,
            key=f"opt_comment_{report_path.as_posix()}",
            help=(
                "Post the Hebrew feedback as a Drive comment on the student's "
                "document, anchored as a bubble next to their work. The "
                "document body is never modified, and re-publishing updates "
                "the existing comment instead of adding another one."
            ),
        )
    return_submissions = st.checkbox(
        "Return submissions to students",
        value=True,
        key=f"opt_return_{report_path.as_posix()}",
        help=(
            "Release the submission so students can see it. Skipped "
            "automatically for any student whose grade failed, so nobody is "
            "returned ungraded."
        ),
    )
    if not publish_grades and not post_comment:
        st.warning(
            "Every delivery option is off — this will only return submissions."
        )
    return {
        "publish_grades": publish_grades,
        "post_comment": post_comment,
        "return_submissions": return_submissions,
    }

def _entry_audit_badge(entry) -> str:
    """Audit badge for one report entry ('-' when it was never audited)."""
    verdict = AuditVerdict.from_dict(getattr(entry, "audit", None))
    if verdict.original_score is None and not verdict.findings:
        return "-"
    return verdict.badge()

def _render_report_audit_warning(report: EvaluationReport) -> None:
    """Warn, before anything is published, about grades the audit flagged.

    Reading the verdict back out of the saved report is what makes this useful:
    the audit ran while grading, possibly days ago, and this is the last gate
    before students see the number.
    """
    verdicts = [
        AuditVerdict.from_dict(getattr(entry, "audit", None))
        for entry in report.entries
    ]
    if not any(v.findings or v.autofixed for v in verdicts):
        return
    stats = summarize_verdicts(verdicts)
    if not stats.get("total"):
        return

    flagged = [
        entry
        for entry in report.entries
        if AuditVerdict.from_dict(entry.audit).needs_review
    ]
    if flagged:
        st.warning(
            f"🔍 Audit: {stats['needs_review']} of {stats['total']} grade(s) in this "
            "report need a human look — check the **Audit** column before releasing."
        )
        with st.expander(f"⚠️ Audit findings ({len(flagged)})"):
            for entry in flagged:
                verdict = AuditVerdict.from_dict(entry.audit)
                st.markdown(f"**{entry.student_name}** — {verdict.summary()}")
                for finding in verdict.findings:
                    st.write(f"- `{finding.flag}` {finding.message}")
    elif stats.get("autofixed"):
        st.info(
            f"🔍 Audit repaired the score math for {stats['autofixed']} grade(s) "
            f"({stats['corrected_points']} point(s) total); the rest is clean."
        )

def _render_editable_entries(
    report_path: Path, report: EvaluationReport
) -> tuple[EvaluationReport, list[str]]:
    """Editable Score/Feedback table with doc links + row deletion.

    Returns the edited report (removals already dropped) and the removed names.
    """
    df = pd.DataFrame(
        [
            {
                "submission_id": entry.submission_id,
                "Student": entry.student_name,
                "Score": entry.result.score,
                "Deductions": deductions_summary(entry.result),
                "Tasks": _tasks_summary(entry.result),
                # The verdict was saved with the grade, so a batch graded days
                # ago still carries its own confidence into the release step.
                "Audit": _entry_audit_badge(entry),
                "Feedback": entry.result.feedback_hebrew,
                "Submission": doc_url(entry.doc_id) or "",
                "Remove ❌": False,
            }
            for entry in report.entries
        ]
    )
    _render_report_audit_warning(report)
    edited_df = st.data_editor(
        df,
        width="stretch",
        # "dynamic" enables the native row-deletion UI (row ⋮ menu) in addition
        # to our explicit Remove checkbox, so injected/unwanted rows can be dropped.
        num_rows="dynamic",
        hide_index=True,
        key=f"grade_editor_{report_path.as_posix()}",
        column_config={
            "submission_id": None,  # internal key, keep hidden
            "Student": st.column_config.TextColumn("Student", disabled=True),
            "Score": st.column_config.NumberColumn(
                "Score", min_value=0, max_value=100, step=1, required=True
            ),
            "Tasks": st.column_config.TextColumn("Tasks", disabled=True),
            "Audit": st.column_config.TextColumn(
                "Audit",
                disabled=True,
                help=(
                    "Result of the deterministic audit pass: ✅ clean, "
                    "🔧 score math repaired automatically, or ⚠️ needs a human look."
                ),
            ),
            "Deductions": st.column_config.TextColumn(
                "Deductions",
                disabled=True,
                help=(
                    "Why points were taken off: task, points deducted and the "
                    "pedagogical reason. Read-only — edit the Score/Feedback "
                    "columns instead."
                ),
                width="large",
            ),
            "Feedback": st.column_config.TextColumn("Feedback", width="large"),
            "Submission": st.column_config.LinkColumn(
                "Submission",
                display_text="Open Doc 📄",
                help="Direct link to the student's Google Doc submission.",
            ),
            "Remove ❌": st.column_config.CheckboxColumn(
                "Remove ❌",
                help="Tick to drop this student from the report JSON and Mashov CSV on save/publish.",
                default=False,
            ),
        },
    )

    by_submission_id = {entry.submission_id: entry for entry in report.entries}
    edited_entries = []
    removed_names: list[str] = []
    for _, row in edited_df.iterrows():
        sub_id = str(row.get("submission_id") or "")
        entry = by_submission_id.get(sub_id)
        if entry is None:
            continue  # ignore manually added blank rows from dynamic mode
        if bool(row.get("Remove ❌", False)):
            removed_names.append(entry.student_name)
            continue
        updated = entry.model_copy(deep=True)
        try:
            updated.result.score = int(float(row["Score"]))
        except (TypeError, ValueError):
            pass  # keep original score if the edit is not a valid number
        updated.result.feedback_hebrew = str(row["Feedback"] or "")
        edited_entries.append(updated)
    # Native row deletions (row ⋮ → Delete) show up as missing submission_ids.
    kept_ids = {
        str(row.get("submission_id") or "")
        for _, row in edited_df.iterrows()
        if str(row.get("submission_id") or "") in by_submission_id
        and not bool(row.get("Remove ❌", False))
    }
    for entry in report.entries:
        if entry.submission_id not in kept_ids and entry.student_name not in removed_names:
            removed_names.append(entry.student_name)
    return report.model_copy(update={"entries": edited_entries}), removed_names

def _persist_report_edits(report_path: Path, report: EvaluationReport) -> None:
    """Save UI edits back to the report JSON and its companion Mashov CSV."""
    try:
        report_path.write_text(
            report.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        st.warning(f"Could not save edits to report file: {exc}")
        return
    # Keep the downloadable/published CSV consistent with the edited report.
    directory = report_path.parent
    expected_csv = report.csv_file or mashov_csv_name(report.course_work_title)
    if directory.resolve() != report_directory(
        report.course_name, report.course_work_title
    ).resolve():
        directory = report_directory(report.course_name, report.course_work_title)
    rows = [
        GradingRow(
            submission=StudentSubmission(
                student_id=entry.student_id,
                student_name=entry.student_name,
                submission_id=entry.submission_id,
                doc_id=entry.doc_id,
                state="TURNED_IN",
            ),
            result=entry.result,
        )
        for entry in report.entries
    ]
    try:
        write_mashov_csv(
            rows,
            report.course_name,
            report.course_work_title,
            directory=directory,
        )
        if report.csv_file and report.csv_file != expected_csv:
            stale = directory / report.csv_file
            if stale.exists() and stale.name != expected_csv:
                stale.unlink()
    except OSError as exc:
        st.warning(f"Edits saved to the report, but the Mashov CSV was not updated: {exc}")

def _run_release_action(
    service: ClassroomService,
    report_path: Path,
    report: EvaluationReport,
    options: Optional[dict[str, bool]] = None,
) -> None:
    """Deliver grades and feedback to every student in ``report``.

    Each delivery channel is independently optional and independently
    failure-tolerant. In particular a Classroom grade failure (HTTP 403 when
    the coursework belongs to another Developer Console project) does **not**
    abort the run: the Hebrew feedback is still left as a comment on the
    student's document, so the student is never left with nothing.
    """
    options = options or {}
    publish_grades = options.get("publish_grades", True)
    post_comment = options.get("post_comment", True)
    return_submissions = options.get("return_submissions", True)

    # Fail fast on foreign coursework: Classroom only lets the Developer
    # Console project that created an item patch/return its submissions, so
    # checking associatedWithDeveloper once beats a per-student 403 storm.
    # Comments still run below, so feedback is not lost for such items.
    grades_writable: bool | None = None
    if publish_grades or return_submissions:
        try:
            grades_writable = service.is_coursework_writable(
                report.course_id, report.course_work_id
            )
        except Exception as exc:  # noqa: BLE001 - auth surfaces as reconnect panel
            if is_auth_failure(exc):
                _render_google_auth_error(exc)
                return
            grades_writable = None
        if grades_writable is False:
            st.warning(
                "This assignment was not created by this Developer Console "
                "project (OAuth client), so Google Classroom will refuse grade "
                "updates and returns with HTTP 403 @ProjectPermissionDenied. "
                "Grades/returns are skipped; feedback comments below still run. "
                "To enable grading, create the assignment through this same "
                "OAuth client - e.g. `python -m src.migrate_class_assignments "
                '--course "<name>" --apply` - then grade the clone.'
            )
            publish_grades = False
            return_submissions = False

    total = len(report.entries)
    unreleased: list[tuple[str, str]] = []
    progress_bar = st.progress(0)
    status_text = st.empty()

    results_data = []

    for idx, entry in enumerate(report.entries, start=1):
        status_text.text(f"Releasing [{idx}/{total}]: {entry.student_name}...")
        student_status_parts = []
        has_error = False
        feedback = entry.result.feedback_hebrew.strip()

        # 1. Grade on the Classroom submission (optional; may be restricted).
        grade_published = not publish_grades
        if publish_grades:
            try:
                service.update_submission_grade(
                    report.course_id,
                    report.course_work_id,
                    entry.submission_id,
                    float(entry.result.score),
                )
                grade_published = True
                student_status_parts.append("Grade: Success")
            except ClassroomServiceError as exc:
                unreleased.append((entry.student_name, f"grade failed: {exc}"))
                student_status_parts.append(f"Grade: Failed ({exc})")
        else:
            student_status_parts.append("Grade: Skipped")

        # 2. Leave the feedback as a Drive comment on the student's document.
        #    Independent of the grade result, so a restricted assignment still
        #    delivers the Hebrew feedback where the student will read it. The
        #    document body is never modified.
        if not post_comment:
            student_status_parts.append("Comment: Skipped (option off)")
        elif entry.doc_id and feedback:
            try:
                outcome = service.publish_feedback_comment(
                    entry.doc_id, feedback, score=entry.result.score
                )
                action = str(outcome.get("action", "created"))
                cleaned = int(outcome.get("duplicatesRemoved", 0) or 0)
                note = f"Comment: {action.title()}"
                if cleaned:
                    note += f" (cleaned {cleaned} duplicate(s))"
                student_status_parts.append(note)
            except ClassroomServiceError as exc:
                unreleased.append((entry.student_name, f"comment failed: {exc}"))
                student_status_parts.append(f"Comment: Failed ({exc})")
        else:
            student_status_parts.append("Comment: Skipped (no doc/feedback)")

        # 3. Return the submission so the student can see it.
        if not return_submissions:
            student_status_parts.append("Return: Skipped (option off)")
        elif not grade_published:
            # Never release a submission whose grade did not land.
            student_status_parts.append("Return: Skipped (grade not published)")
        else:
            try:
                service.return_student_submission(
                    report.course_id, report.course_work_id, entry.submission_id
                )
                student_status_parts.append("Return: Success")
            except ClassroomServiceError as exc:
                unreleased.append((entry.student_name, f"return failed: {exc}"))
                student_status_parts.append(f"Return: Failed ({exc})")

        if unreleased and unreleased[-1][0] == entry.student_name:
            has_error = True

        results_data.append({
            "Student Name": entry.student_name,
            "Score": entry.result.score,
            "Outcome": "FAILED" if has_error else "SUCCESS",
            "Details": ", ".join(student_status_parts),
        })
        progress_bar.progress(idx / total)

    status_text.empty()
    progress_bar.empty()

    st.subheader("Release Results")
    st.dataframe(pd.DataFrame(results_data), width="stretch")

    if unreleased:
        st.warning(
            f"Release completed with issues for {len(unreleased)} student action(s). "
            "The report and CSV have been preserved for inspection and retry."
        )
        for s_name, reason in unreleased:
            st.error(f"{s_name}: {reason}")
    else:
        removed = _delete_report_artifacts(report_path, report)
        st.success(
            "All selected actions completed successfully for every student! "
            "Report artifacts have been cleaned up."
        )
        if removed:
            with st.expander("Removed Artifacts"):
                for r in removed:
                    st.write(f"- `{r.as_posix()}`")

# ----------------------------------------------------------------------
# Seam wiring
# ----------------------------------------------------------------------
# Tests monkeypatch these on the ``app`` facade. The bodies above reference
# them as plain globals; Python resolves globals at call time, so rebinding
# them here to facade-resolving proxies honours a patch without touching
# any call site. UI-proxied seams are imported (already proxies) - not rewrapped.
_delete_report_artifacts = seam_fn("app", "_delete_report_artifacts", _delete_report_artifacts)
st = st_proxy("app", st)
