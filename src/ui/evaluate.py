"""Evaluate tab: pick a course/assignment, load its rubric, run a paced grading batch, and save results back to Classroom.

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
    _handle_google_error,
)
from src.ui.dashboard import (
    _load_course_work,
    _load_courses,
    _restore_course_work,
    _restore_courses,
)
from src.ui.helpers import (
    _config_display_path,
    _render_quota_notice,
    get_all_config_files,
    suggest_config_for_assignment,
)
from src.ui.progress import (
    _LiveProgress,
)
from src.seams import seam_fn, st_proxy

logger = logging.getLogger(__name__)

def render_evaluate_tab(service: ClassroomService) -> None:
    st.subheader("Evaluate Submissions")
    st.caption(
        "Select a course, coursework assignment, and rubric JSON to automatically evaluate "
        "turned-in student Google Docs."
    )

    try:
        # Cached: in Streamlit every widget click re-runs this script, and an
        # uncached list_courses() here used to cost a network round trip per
        # dropdown interaction. _load_courses() returns a pickle-safe dict
        # snapshot (st.cache_data pickles it); rebuild live Course objects here.
        courses = _restore_courses(_load_courses())
    except Exception as exc:  # noqa: BLE001 - auth vs API error decided by message
        _handle_google_error(exc, "Failed to fetch Google Classroom courses")
        return

    if not courses:
        st.warning("No active courses found in your Google Classroom account.")
        return

    course_labels = [
        f"{c.name}" + (f" ({c.section})" if c.section else "")
        for c in courses
    ]
    selected_course_idx = st.selectbox(
        "Select Active Course",
        range(len(courses)),
        format_func=lambda i: course_labels[i],
        key="eval_course",
    )
    selected_course: Course = courses[selected_course_idx]

    try:
        assignments = _restore_course_work(_load_course_work(selected_course.id))
    except Exception as exc:  # noqa: BLE001 - auth vs API error decided by message
        _handle_google_error(exc, "Failed to fetch assignments")
        return

    if not assignments:
        st.info(f"No published coursework assignments found for {selected_course.name}.")
        return

    assignment_labels = [
        f"{a.title}"
        + (f" (Due: {a.due_date})" if a.due_date else "")
        for a in assignments
    ]
    # Key includes course id so the widget resets when the course changes
    # (prevents Streamlit reusing a stale index that points at the wrong coursework id).
    selected_assignment_idx = st.selectbox(
        "Select Coursework Assignment",
        range(len(assignments)),
        format_func=lambda i: assignment_labels[i],
        key=f"eval_assignment_{selected_course.id}",
    )
    selected_assignment: CourseWork = assignments[selected_assignment_idx]

    config_files = get_all_config_files()
    if not config_files:
        st.error(
            "No rubric configuration JSON files found in `assignments/`, "
            "`config/` or `configs/`."
        )
        return

    config_labels = [_config_display_path(c) for c in config_files]

    # Smart association order: explicit mapping -> fuzzy title match -> first file.
    mapped = resolve_mapped_config(selected_assignment.id)
    suggestion, suggestion_score, suggestion_reason = suggest_config_for_assignment(
        selected_assignment.title, config_files
    )
    default_config_index = 0
    if mapped is not None:
        for i, path in enumerate(config_files):
            if path.resolve() == mapped.resolve():
                default_config_index = i
                break
        st.caption(f"📌 Remembered mapping: `{config_labels[default_config_index]}`")
    elif suggestion is not None:
        default_config_index = config_files.index(suggestion)
        st.caption(
            f"✨ Suggested rubric: `{_config_display_path(suggestion)}` "
            f"({suggestion_reason}, confidence {suggestion_score:.0%}) — "
            "pre-selected, you can change it below."
        )
    else:
        st.caption("No confident rubric match found — please pick the rubric manually.")

    selected_config_name = st.selectbox(
        "Select Assignment Configuration (Rubric)",
        config_labels,
        index=default_config_index,
        help="Pre-selects the remembered mapping, or the best title match, if available.",
    )
    selected_config_file = config_files[config_labels.index(selected_config_name)]
    if (
        mapped is None
        and suggestion is not None
        and selected_config_file.resolve() == suggestion.resolve()
    ):
        st.caption(
            "Auto-matched by assignment title — the choice will be remembered "
            "for this assignment after you run the evaluation."
        )

    try:
        rubric = load_rubric(selected_config_file)
    except Exception as exc:
        st.error(f"Failed to parse configuration file '{selected_config_name}': {exc}")
        return

    # Exempt (broken) tasks are skipped by the grader, so it is stated up front to
    # avoid "8 tasks defined" vs 7 graded rows confusion.
    if isinstance(rubric, AssignmentConfig):
        config = rubric
        exempt_count = sum(1 for task in config.tasks if task.exempt)
        exempt_note = f", {exempt_count} exempt (skipped)" if exempt_count else ""
        st.write(
            f"**Rubric Loaded:** `{config.assignment_id}` - *{config.title}* "
            f"({len(config.tasks)} tasks defined{exempt_note})"
        )
    else:
        config = rubric
        exempt_count = 0
        exempt_note = ""
        task_counts = sum(len(s.questions) for s in rubric.sections)
        st.write(
            f"**Rubric Loaded:** `{rubric.assignment_title}` - *{rubric.topic}*\n"
            f"({task_counts} questions across {len(rubric.sections)} sections, "
            f"weights {', '.join(str(s.weight) for s in rubric.sections)})"
        )

    if st.button("Run Evaluation", type="primary", width="stretch"):
        _run_evaluation_action(service, selected_course, selected_assignment, selected_config_file, config)

def _run_evaluation_action(
    service: ClassroomService,
    course: Course,
    assignment: CourseWork,
    config_file: Path,
    config: AssignmentConfig | AssignmentSpec,
) -> None:
    try:
        save_assignment_mapping(assignment.id, config_file)
    except Exception as exc:
        logger.warning("Could not persist assignment mapping: %s", exc)

    # --- Fetch submissions (same logic as the CLI) ---
    logger.info(
        "Evaluate: course=%s assignment=%s (%s)",
        course.id, assignment.id, assignment.title,
    )
    with st.spinner("Fetching turned-in student submissions..."):
        try:
            submissions = service.get_submissions(course.id, assignment.id)
        except Exception as exc:  # noqa: BLE001 - auth vs API error decided by message
            _handle_google_error(exc, "Failed to fetch submissions")
            return

    turned_in = [s for s in submissions if (s.state or "").strip().upper() == "TURNED_IN"]
    if not turned_in:
        st.info("No 'TURNED_IN' submissions found for this assignment.")
        return

    st.info(f"Grading {len(turned_in)} turned-in submission(s) using `{settings.groq_model}`...")

    live = _LiveProgress(len(turned_in))
    rows: list[GradingRow] = []

    # --- Fetch documents first (fast, sequential Google reads) ---
    # The old loop interleaved "read doc -> grade -> sleep 2s -> read doc",
    # so every student paid the pacing sleep even though the sleep exists to
    # protect the *LLM* per-minute budget, not Google Docs. Fetching all docs
    # up front leaves only the LLM calls for the parallel batch below.
    doc_texts: dict[str, str] = {}
    gradable: list = []
    try:
        for idx, sub in enumerate(turned_in, start=1):
            if not sub.doc_id:
                live.begin(idx, sub.student_name, "no Google Doc attached - skipped")
                rows.append(GradingRow(sub, error=UNSUPPORTED_ATTACHMENT_MSG))
                live.complete(idx, f"{sub.student_name} - skipped (no document)")
                continue
            live.begin(idx, sub.student_name, "reading document")
            try:
                doc_texts[sub.submission_id] = service.extract_doc_text(sub.doc_id)
            except ClassroomServiceError as exc:
                rows.append(GradingRow(sub, error=f"Document extraction failed: {exc}"))
                live.complete(idx, f"{sub.student_name} - document extraction failed")
                continue
            gradable.append(sub)
            live.complete(idx, f"{sub.student_name} - document ready")
    finally:
        pass

    # --- Grade in parallel (slow LLM waits overlap) ---
    # grade_students_batch spreads students over up to 3 workers; the shared
    # router round-robins Groq keys across them, so concurrent students land
    # on different keys/tiers instead of queueing on one. paced_pause is passed
    # so each new evaluation is spaced out, preventing a burst of concurrent
    # requests from exceeding Groq's per-minute output-token budget (which
    # truncates responses mid-JSON).
    if gradable:
        import threading as _threading

        live.label(f"Scoring {len(gradable)} submission(s) in parallel...")
        progress_lock = _threading.Lock()
        finished_names: list[str] = []

        def _on_one(outcome: object, done: int, total: int) -> None:
            name = getattr(outcome, "student_name", "?")
            with progress_lock:
                finished_names.append(str(name))
                shown = list(finished_names)
            try:
                live.fraction(done / max(total, 1))
                live.label(
                    f"Scoring... {done}/{total} done"
                    + (f" (latest: {shown[-1]})" if shown else "")
                )
            except Exception:
                logger.debug("batch progress paint failed", exc_info=True)

        batch_requests = [
            StudentGradeRequest(
                student_name=sub.student_name,
                raw_doc_text=doc_texts[sub.submission_id],
                late_penalty_points=penalty_for(
                    sub,
                    assignment,
                    settings.late_penalty_points,
                    first_submitted_at=sub.first_submitted_at,
                ),
            )
            for sub in gradable
        ]
        by_name = {sub.student_name: sub for sub in gradable}
        try:
            batch_outcomes = grade_students_batch(
                batch_requests, config, on_result=_on_one, pause=paced_pause
            )
        except Exception as exc:  # noqa: BLE001 - never lose a whole batch
            logger.exception("Parallel grading batch failed: %s", exc)
            batch_outcomes = []
            for req in batch_requests:
                from src.llm_evaluator import StudentGradeOutcome as _Outcome

                batch_outcomes.append(
                    _Outcome(student_name=req.student_name, error=f"LLM evaluation failed: {exc}")
                )
        for outcome in batch_outcomes:
            sub = by_name.get(outcome.student_name)
            if sub is None:
                continue
            if outcome.result is not None:
                rows.append(
                    GradingRow(sub, result=outcome.result, audit=last_audit_for(outcome.result))
                )
            elif outcome.quota_exhausted:
                rows.append(GradingRow(sub, error=outcome.error or "Daily quota exhausted."))
            else:
                rows.append(GradingRow(sub, error=outcome.error or "Evaluation failed"))
        try:
            live.complete(len(turned_in), "Scoring finished")
        except Exception:
            logger.debug("batch finish label failed", exc_info=True)
        live.notice(_fallback_notice())
    try:
        live.clear()
    except Exception:
        logger.debug("progress clear failed", exc_info=True)

    try:
        csv_path = write_mashov_csv(rows, course.name, assignment.title)
        report_path = write_evaluation_report(rows, course, assignment, config)
        st.success(
            f"Evaluation complete! Saved report to `{report_path.as_posix()}` "
            f"and Mashov CSV to `{csv_path.as_posix()}`."
        )
    except Exception as exc:
        st.error(f"Error saving evaluation exports: {exc}")

    table_data = []
    for r in rows:
        if r.result is not None:
            score_val = r.result.score
            status_summary = _tasks_summary(r.result)
            notes = r.result.feedback_hebrew
        else:
            score_val = None
            status_summary = "ERROR"
            notes = r.error or "Evaluation failed"
        table_data.append({
            "Student Name": r.submission.student_name,
            "Score": score_val if score_val is not None else "-",
            "Status Summary": status_summary,
            "Audit": audit_summary(r),
            "Notes / Feedback": notes,
        })

    df = coerce_score_column(pd.DataFrame(table_data))
    st.dataframe(df, width="stretch")
    _render_audit_rollup(rows)
    _render_quota_notice()

def _render_audit_rollup(rows: list) -> None:
    """Audit totals for a graded batch, then the findings that need a human.

    The audit exists to shrink the teacher's review load, so the panel leads
    with one number - how many grades are clean / repaired / suspicious - and
    only lists the individual problems when there are any.
    """
    verdicts = [getattr(r, "audit", None) for r in rows if r.result is not None]
    if not any(verdicts):
        return
    stats = summarize_verdicts(verdicts)
    if not stats.get("total"):
        return

    st.markdown("#### 🔍 Audit")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Audited", stats["total"])
    c2.metric("Clean", stats["clean"])
    c3.metric("Auto-fixed", stats["autofixed"])
    c4.metric("Needs review", stats["needs_review"])
    if stats.get("corrected_points"):
        st.caption(
            f"Audit corrected {stats['corrected_points']} point(s) of score math "
            "(deduction sums that disagreed with the final score)."
        )
    if stats.get("flags"):
        st.caption(
            "Flags raised: "
            + ", ".join(f"`{flag}` × {count}" for flag, count in stats["flags"].items())
        )

    flagged = [
        r for r in rows
        if r.result is not None and getattr(r.audit, "needs_review", False)
    ]
    if not flagged:
        st.success("No grade needed manual attention after the audit pass.")
        return

    st.warning(
        f"{len(flagged)} grade(s) need a human look before releasing. "
        "Fix the cause (rubric, feedback, or task statuses) and re-grade, or "
        "edit the score in **Release Grades & Feedback** if the grade is right."
    )
    with st.expander(f"⚠️ Findings ({len(flagged)})"):
        for r in flagged:
            st.markdown(f"**{r.submission.student_name}** — {r.audit.summary()}")
            for finding in r.audit.findings:
                st.write(f"- `{finding.flag}` {finding.message}")

# ----------------------------------------------------------------------
# Seam wiring
# ----------------------------------------------------------------------
# Tests monkeypatch these on the ``app`` facade. The bodies above reference
# them as plain globals; Python resolves globals at call time, so rebinding
# them here to facade-resolving proxies honours a patch without touching
# any call site. UI-proxied seams are imported (already proxies) - not rewrapped.
grade_students_batch = seam_fn("app", "grade_students_batch", grade_students_batch)
load_rubric = seam_fn("app", "load_rubric", load_rubric)
paced_pause = seam_fn("app", "paced_pause", paced_pause)
resolve_mapped_config = seam_fn("app", "resolve_mapped_config", resolve_mapped_config)
save_assignment_mapping = seam_fn("app", "save_assignment_mapping", save_assignment_mapping)
st = st_proxy("app", st)
