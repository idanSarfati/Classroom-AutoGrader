"""Dashboard tab: cache Google course/coursework/pending-submission loaders, run the read-only dry-run pipeline, and render pending groups.

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
    _cached_classroom_service,
    _handle_google_error,
    _render_google_auth_error,
)
from src.ui.helpers import (
    _config_display_path,
    _render_quota_notice,
    doc_url,
    get_all_config_files,
    suggest_config_for_assignment,
)
from src.ui.progress import (
    _LiveProgress,
)
from src.seams import seam_fn, st_proxy

logger = logging.getLogger(__name__)

@st.cache_data(ttl=300, show_spinner="Loading Google Classroom courses...")
def _load_courses() -> tuple[dict[str, Any], ...]:
    """Active courses, cached across reruns.

    Streamlit re-executes the whole script on every widget interaction, so an
    uncached ``list_courses()`` in a tab cost a Google round trip per dropdown
    click. Caching makes those reruns instant while the TTL still picks up a
    class archived (or created) today.

    Returns a plain-dict snapshot, never live ``Course`` objects.
    ``st.cache_data`` pickles what it stores, and a Pydantic instance built by
    a *previous* import does not survive Streamlit's hot-reload re-import of
    :mod:`src.models`: ``pickle`` raises ``Can't pickle <class
    'src.models.Course'>: it's not the same object as src.models.Course``,
    which crashed the Evaluate tab on first render. ``model_dump(mode="json")``
    keeps the cached payload free of any class reference (and turns
    ``date``/``datetime`` into ISO strings that round-trip exactly through
    ``model_validate``). ``render_evaluate_tab`` rebuilds the domain objects
    via :func:`_restore_courses` - the same snapshot/restore contract
    :func:`_snapshot_pending_groups` already gives the Dashboard tab.
    """
    return tuple(
        course.model_dump(mode="json")
        for course in _cached_classroom_service().list_courses()
    )

def _restore_courses(items: Iterable[dict[str, Any] | Course]) -> list[Course]:
    """Rebuild ``Course`` objects from a :func:`_load_courses` snapshot.

    Accepts both fresh dict snapshots and already-built ``Course`` objects, so
    a caller (or test) that passes live models through unchanged keeps working.
    """
    return [
        item if isinstance(item, Course) else Course.model_validate(item)
        for item in items
    ]

@st.cache_data(ttl=300, show_spinner="Loading assignments...")
def _load_course_work(course_id: str) -> tuple[dict[str, Any], ...]:
    """Published coursework for one course, cached across reruns.

    Plain-dict snapshot for the same pickle/hot-reload reason as
    :func:`_load_courses`; ``render_evaluate_tab`` rebuilds the ``CourseWork``
    objects via :func:`_restore_course_work`.
    """
    return tuple(
        work.model_dump(mode="json")
        for work in _cached_classroom_service().list_course_work(course_id)
    )

def _restore_course_work(
    items: Iterable[dict[str, Any] | CourseWork],
) -> list[CourseWork]:
    """Rebuild ``CourseWork`` objects from a :func:`_load_course_work` snapshot.

    Accepts both fresh dict snapshots and already-built ``CourseWork`` objects.
    """
    return [
        item if isinstance(item, CourseWork) else CourseWork.model_validate(item)
        for item in items
    ]

def _snapshot_pending_groups(
    groups: Iterable[PendingGroup],
) -> tuple[dict[str, Any], ...]:
    """Plain-data snapshot of a pending scan - safe for ``st.cache_data``.

    ``st.cache_data`` pickles its return value, and Streamlit's hot-reload
    re-imports :mod:`src.models` on every edit. An instance built by the
    *previous* import then fails ``pickle`` with ``Can't pickle <class
    'src.models.Course'>: it's not the same object as src.models.Course``,
    crashing the tab. Serializing to primitives keeps the cached payload free
    of any class reference, so it always pickles - and unpickles - cleanly
    across reloads. ``mode="json"`` also turns ``date``/``datetime`` fields
    into ISO strings that round-trip exactly through ``model_validate``.
    """
    return tuple(
        {
            "course": group.course.model_dump(mode="json"),
            "assignment": group.assignment.model_dump(mode="json"),
            "submissions": [
                submission.model_dump(mode="json")
                for submission in group.submissions
            ],
        }
        for group in groups
    )

def _restore_pending_groups(
    items: Iterable[dict[str, Any] | PendingGroup],
) -> list[PendingGroup]:
    """Inverse of :func:`_snapshot_pending_groups`, for the dashboard tab.

    Accepts both fresh dict snapshots and already-built ``PendingGroup``
    objects: ``st.session_state`` survives code reloads, so groups saved by an
    earlier session - or injected directly by tests - must keep rendering
    instead of being mis-parsed as dicts.
    """
    restored: list[PendingGroup] = []
    for item in items:
        if not isinstance(item, dict):
            # Already a PendingGroup (this session's state, or a test stub):
            # pass it through untouched.
            restored.append(item)
            continue
        restored.append(
            PendingGroup(
                course=Course.model_validate(item["course"]),
                assignment=CourseWork.model_validate(item["assignment"]),
                submissions=tuple(
                    StudentSubmission.model_validate(submission)
                    for submission in item["submissions"]
                ),
            )
        )
    return restored

@st.cache_data(ttl=300, show_spinner="Scanning Google Classroom for pending submissions...")
def _load_pending_dashboard(
    course_ids: Optional[tuple[str, ...]],
) -> tuple[dict[str, Any], ...]:
    """Cached snapshot of turned-in-but-unreturned submissions.

    The scan is an N+1 fan-out (courses -> coursework -> submissions), so it
    is deliberately called only when there is nothing to show - the caller
    keeps the result in ``st.session_state`` and this cache is the second
    layer behind it. The course filter is applied in memory, so switching it
    never triggers a rescan. **🔄 Refresh** clears both layers on purpose.

    The return value is a plain-dict snapshot (see
    :func:`_snapshot_pending_groups`), never live ``PendingGroup`` objects:
    ``st.cache_data`` must pickle what it stores, and Pydantic model
    instances do not survive Streamlit's hot-reload class re-import.
    ``render_dashboard_tab`` rebuilds the domain objects via
    :func:`_restore_pending_groups`.
    """
    return _snapshot_pending_groups(
        collect_pending_submissions(
            _cached_classroom_service(), course_ids=course_ids
        )
    )

def _build_dashboard_resolver(config_files: list[Path]):
    """Return ``(assignment) -> (config_path, config) | None`` for the dry-run.

    Reuses the Evaluate tab's two-step resolution: the remembered mapping for
    that coursework first, then a fuzzy title match. Anything that cannot be
    resolved - or fails to parse - yields ``None``, so the dry-run skips that
    assignment with a visible reason instead of grading against the wrong
    rubric.
    """

    def _resolve(
        assignment: CourseWork,
    ) -> Optional[tuple[Path, AssignmentConfig | AssignmentSpec]]:
        path = resolve_mapped_config(assignment.id)
        if path is None:
            suggestion, _score, _reason = suggest_config_for_assignment(
                assignment.title, config_files
            )
            path = suggestion
        if path is None:
            return None
        try:
            return path, load_rubric(path)
        except (OSError, ValueError) as exc:
            logger.warning("Dry-run could not load rubric %s: %s", path, exc)
            return None

    return _resolve

def _run_dry_run_action(
    service: ClassroomService,
    groups: list[PendingGroup],
    config_files: list[Path],
    directory: Optional[Path] = None,
) -> list[DryRunAssignmentResult]:
    """Run the automated dry-run and render the predicted scores.

    Read-only by construction: :func:`src.dashboard.run_dry_run_evaluation`
    wraps ``service`` in a read-only facade that raises before any grade,
    comment, or return call, so there is no code path out of here that could
    publish anything. ``directory`` exists so tests can point the local report
    at a temp folder instead of the real ``exports/`` tree.
    """
    if not groups:
        st.info("Nothing to evaluate - there are no pending submissions.")
        return []
    if not config_files:
        st.error(
            "No rubric configuration JSON files found in `assignments/`, "
            "`config/` or `configs/`."
        )
        return []

    pending_total = sum(group.count for group in groups)
    st.info(
        f"🧪 Dry-run: evaluating **{pending_total}** submission(s) across "
        f"**{len(groups)}** assignment(s) with `{settings.groq_model}` "
        f"(DRY_RUN = {DRY_RUN}). Predicted scores only - nothing is sent to "
        "Google Classroom."
    )

    live = _LiveProgress(pending_total)

    def _on_status(message: str) -> None:
        live.label(message)
        # The router can leave Groq on any single request, so re-check on every
        # beat: the teacher should see the switch as it happens, not at the end.
        live.notice(_fallback_notice())

    def _on_progress(value: float) -> None:
        live.fraction(value)

    def _pause() -> None:
        # Adaptive pacing: the shared pacer stretches after each 429 and
        # relaxes on a clean streak, so a throttled batch slows down instead of
        # hammering the rate limit. `on_tick` turns that wait into an on-screen
        # countdown - without it a 30s backoff is a dead, frozen frame.
        live.pacing()
        paced_pause(on_tick=live.tick)

    try:
        results = run_dry_run_evaluation(
            service,
            groups,
            _build_dashboard_resolver(config_files),
            directory=directory,
            source="dashboard",
            on_status=_on_status,
            on_progress=_on_progress,
            pause=_pause,
        )
    except Exception as exc:  # noqa: BLE001 - auth vs API error decided by message
        if is_auth_failure(exc):
            _render_google_auth_error(exc)
        else:
            st.error(f"Google Classroom request failed during the dry-run: {exc}")
        try:
            live.label(f"Dry-run stopped: {exc}")
        except Exception:
            logger.debug("dry-run error label failed", exc_info=True)
        return []
    except LLMEvaluationError as exc:
        st.error(f"Groq evaluation failed: {exc}")
        try:
            live.label(f"Dry-run stopped: {exc}")
        except Exception:
            logger.debug("dry-run error label failed", exc_info=True)
        return []

    try:
        live.complete(pending_total, "Dry-run finished - predictions below")
    except Exception:
        logger.debug("dry-run finish label failed", exc_info=True)

    _render_dry_run_results(results)
    _render_quota_notice()
    return results

def _dry_run_result_key(group: PendingGroup) -> str:
    """Session-state key holding one assignment's inline dry-run result.

    Keyed by course *and* coursework id so two classes running the same
    worksheet ("lesson_1") keep separate predictions.
    """
    return f"dryrun_{group.course.id}_{group.assignment.id}"

def _render_inline_dry_run(
    service: ClassroomService, group: PendingGroup
) -> None:
    """Per-assignment "Run Dry-Run" control, rendered inside its own section.

    The click runs the very same read-only pipeline as the bulk button
    (:func:`_run_dry_run_action` -> :func:`src.dashboard.run_dry_run_evaluation`,
    which holds the service behind ``ReadOnlyClassroomService`` and refuses to
    run unless ``DRY_RUN`` is True) - scoped to just this one group.

    Streamlit re-runs the whole script on every interaction, so the outcome is
    stored in ``st.session_state`` and rendered from there afterwards. Without
    that, the predictions would vanish on the next click - or, worse, be
    recomputed and spend Groq tokens again. Re-running and clearing are always
    explicit.
    """
    key = _dry_run_result_key(group)
    stored = st.session_state.get(key)
    triggered = False

    if stored is None:
        if st.button(
            "▶ Run Dry-Run for this assignment",
            key=f"{key}_run",
            type="primary",
            width="stretch",
            help=(
                "Grades the submissions listed above with the Groq engine and "
                f"shows the predicted scores here. Read-only: nothing is sent "
                f"to Google Classroom (DRY_RUN = {DRY_RUN})."
            ),
        ):
            triggered = True
    else:
        st.markdown("🧪 **Dry-run predictions** — not published")
        _render_dry_run_assignment(stored)
        rerun_col, clear_col, _ = st.columns([1, 1, 2])
        with rerun_col:
            triggered = st.button(
                "🔁 Re-run",
                key=f"{key}_rerun",
                width="stretch",
                help="Grades again with the current model and rubric.",
            )
        with clear_col:
            if st.button(
                "🗑 Clear",
                key=f"{key}_clear",
                width="stretch",
                help="Hide these predictions. The saved report is untouched.",
            ):
                st.session_state.pop(key, None)
                st.rerun()

    if triggered:
        results = _run_dry_run_action(service, [group], get_all_config_files())
        if results:
            st.session_state[key] = results[0]
        # Render from the cache on the next pass, so the panel and the stored
        # result can never drift apart.
        st.rerun()

def _render_dry_run_assignment(result: DryRunAssignmentResult) -> None:
    """Predicted scores, deductions and feedback for one assignment.

    Shared by the bulk "Run Automated Dry-Run" button and by the inline
    per-assignment button, so both paths render byte-identical output.
    """
    label = f"{result.course.name} — {result.assignment.title}"
    if result.skipped_reason:
        st.warning(f"**{label}**: {result.skipped_reason}")
        return

    artifacts = [
        f"{kind} `{_config_display_path(path)}`"
        for kind, path in (
            ("report", result.report_path),
            ("CSV", result.csv_path),
        )
        if path is not None
    ]
    st.markdown(
        f"**{label}**" + (f" · {' · '.join(artifacts)}" if artifacts else "")
    )
    if result.save_error:
        st.error(f"Could not save local artifacts for {label}: {result.save_error}")

    rows = dry_run_rows(result)
    st.dataframe(
        coerce_score_column(pd.DataFrame(rows)).drop(columns=["doc_id"], errors="ignore"),
        width="stretch",
        hide_index=True,
    )
    links = [
        (row["Student"], doc_url(row["doc_id"])) for row in rows if row.get("doc_id")
    ]
    links = [(name, url) for name, url in links if url]
    if links:
        with st.expander(f"📄 Student documents ({len(links)})"):
            for name, url in links:
                st.markdown(f"- [{name}]({url})")

def _render_dry_run_results(results: list[DryRunAssignmentResult]) -> None:
    """Show every assignment's predictions - nothing is published."""
    st.subheader("Dry-Run Results (predicted — not published)")
    st.caption(
        "Review the scores and feedback below. Publishing any of it is a "
        "separate, deliberate step in **Release Grades & Feedback**."
    )

    evaluated = sum(result.evaluated for result in results)
    failed = sum(result.failed for result in results)
    skipped = sum(1 for result in results if result.skipped_reason)

    for result in results:
        _render_dry_run_assignment(result)

    parts = [f"{evaluated} predicted score(s) computed"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} assignment(s) skipped (no rubric)")
    summary = "; ".join(parts)
    if failed or skipped:
        st.warning(f"Dry-run finished: {summary}.")
    else:
        st.success(f"Dry-run finished: {summary}.")
    st.success(
        "🔒 Nothing was pushed to Google Classroom - no grades, no comments, "
        "no returns."
    )

def render_dashboard_tab(service: ClassroomService) -> None:
    """Home view: what is waiting for review, plus the automated dry-run.

    Deliberately lazy. Streamlit executes *every* tab body on every script
    run, and the pending-submission scan is an N+1 fan-out over the Classroom
    API (courses -> coursework -> submissions). Running it eagerly meant the
    page sat behind a spinner before a single widget could appear - and with
    no socket timeout a stalled connection left it there forever.

    So the scan only happens when the teacher asks for it, its result is kept
    in ``st.session_state``, and the course filter is a pure in-memory slice:
    after the first scan, switching courses and every later rerun cost **zero**
    Google calls. Refresh is the explicit escape hatch, and it deliberately
    bypasses the cache.
    """
    st.subheader("Pending Submissions Dashboard")

    st.warning(
        "🔒 **DRY RUN** — this tab only computes predicted scores and feedback "
        "for your review. It never pushes grades, comments, or returns to "
        "Google Classroom. Publishing is always a deliberate, manual step in "
        "**Release Grades & Feedback**.",
        icon="🛡️",
    )

    refresh_col, auto_col, mode_col = st.columns([1, 2, 2])
    with refresh_col:
        refresh_clicked = st.button("🔄 Refresh", width="stretch")
    with auto_col:
        auto_dry_run = st.checkbox(
            "Run dry-run automatically when the list is refreshed",
            value=settings.dashboard_auto_dry_run,
            key="dashboard_auto_dry_run_checked",
            help=(
                "Off by default: a dry-run spends Groq tokens, so starting one "
                "should stay a deliberate choice. It is read-only either way."
            ),
        )
    with mode_col:
        st.caption(
            f"Env `DRY_RUN`: **{'on' if settings.dry_run else 'off'}** · "
            f"Dashboard pipeline: **DRY_RUN = {DRY_RUN}** (always read-only)"
        )

    # ---- Data: read from Google only when there is nothing cached --------
    groups = st.session_state.get("dashboard_groups")
    scan_requested = False

    if refresh_clicked:
        # Drop both cache layers so the scan below really re-reads Google.
        _load_pending_dashboard.clear()
        st.session_state["dashboard_scan"] = (
            st.session_state.get("dashboard_scan", 0) + 1
        )
        st.session_state["dashboard_groups"] = None
        groups = None
        scan_requested = True

    if groups is None and not scan_requested:
        st.info(
            "Nothing has been fetched from Google Classroom yet, so the app "
            "starts instantly. Press **Scan** to look for submissions that "
            "are waiting for review."
        )
        if st.button(
            "🔍 Scan for pending submissions",
            type="primary",
            width="stretch",
        ):
            scan_requested = True
        else:
            # First visit: stop here, before any Google API call is made.
            return

    if scan_requested:
        try:
            with st.spinner(
                "Scanning Google Classroom for pending submissions..."
            ):
                # The cache hands back plain dicts (pickle-safe across
                # hot reloads); rebuild the domain objects for the tab.
                groups = _restore_pending_groups(
                    _load_pending_dashboard(None)
                )
        except Exception as exc:  # noqa: BLE001 - auth vs API error decided by message
            _handle_google_error(exc, "Failed to scan Google Classroom")
            return
        st.session_state["dashboard_groups"] = groups
    else:
        groups = _restore_pending_groups(groups)

    # ---- Course filter: an in-memory slice, never a second API call -----
    # Every listed course is one that actually has pending work, so the
    # dropdown can be built from the scan result without listing courses
    # again just to populate a picker.
    courses: list[Course] = []
    for group in groups:
        if all(existing.id != group.course.id for existing in courses):
            courses.append(group.course)

    scope: Optional[str] = None
    if courses:
        labels = ["All courses"] + [
            f"{course.name}" + (f" · {course.section}" if course.section else "")
            for course in courses
        ]
        selected = st.selectbox(
            "Course",
            range(len(labels)),
            format_func=lambda index: labels[index],
            key="dashboard_course_scope",
        )
        if selected != 0:
            scope = courses[selected - 1].id
            groups = [
                group for group in groups if group.course.id == scope
            ]

    summary = summarize_pending(groups)

    tile1, tile2, tile3, tile4 = st.columns(4)
    tile1.metric("Courses with pending work", summary.course_count)
    tile2.metric("Assignments awaiting review", summary.assignment_count)
    tile3.metric("Submissions turned in", summary.submission_count)
    tile4.metric("Flagged late", summary.late_count)

    if not groups:
        st.success(
            "🎉 All caught up — every turned-in submission has been returned."
        )
        return

    st.markdown("#### Waiting for review")
    for group in groups:
        due = group.assignment.due_datetime or group.assignment.due_date
        due_text = f" · due {due:%Y-%m-%d}" if due else ""
        with st.expander(
            f"📝 {group.assignment.title} — {group.count} awaiting review",
        ):
            st.caption(f"Course: {group.course.name}{due_text}")
            st.dataframe(
                pd.DataFrame(pending_rows(group)).drop(
                    columns=["doc_id"], errors="ignore"
                ),
                width="stretch",
                hide_index=True,
            )
            # Inline dry-run for this assignment only - the same read-only
            # pipeline as the bulk button, scoped to this group.
            _render_inline_dry_run(service, group)

    st.divider()
    action_col, _hint = st.columns([1, 1])
    with action_col:
        clicked = st.button(
            "🧪 Run Automated Dry-Run",
            type="primary",
            width="stretch",
        )
    st.caption(
        "Fetches the submissions above, grades them with the Groq engine, and "
        "shows the predicted scores + Hebrew feedback below. Reports are saved "
        "locally under `exports/` only."
    )

    # The auto option must not re-spend Groq tokens on every widget rerun:
    # it fires only when the scan itself changed (Refresh, or a different
    # course scope / number of pending submissions).
    scan_key = (
        st.session_state.get("dashboard_scan", 0),
        scope,
        summary.submission_count,
    )
    last_auto_key = st.session_state.get("dashboard_last_auto_key")
    auto_due = auto_dry_run and last_auto_key != scan_key
    if clicked or auto_due:
        config_files = get_all_config_files()
        _run_dry_run_action(service, groups, config_files)
        # Remember the *attempt*, not just a successful one: a failed or empty
        # run must not re-spend Groq tokens on every widget rerun. The next
        # automatic run is triggered by an explicit Refresh (or by pressing
        # the button), exactly as the checkbox promises.
        st.session_state["dashboard_last_auto_key"] = scan_key

# ----------------------------------------------------------------------
# Seam wiring
# ----------------------------------------------------------------------
# Tests monkeypatch these on the ``app`` facade. The bodies above reference
# them as plain globals; Python resolves globals at call time, so rebinding
# them here to facade-resolving proxies honours a patch without touching
# any call site. UI-proxied seams are imported (already proxies) - not rewrapped.
_load_pending_dashboard = seam_fn("app", "_load_pending_dashboard", _load_pending_dashboard)
load_rubric = seam_fn("app", "load_rubric", load_rubric)
paced_pause = seam_fn("app", "paced_pause", paced_pause)
resolve_mapped_config = seam_fn("app", "resolve_mapped_config", resolve_mapped_config)
run_dry_run_evaluation = seam_fn("app", "run_dry_run_evaluation", run_dry_run_evaluation)
st = st_proxy("app", st)
