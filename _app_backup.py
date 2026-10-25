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


def _register_retry_notifier() -> None:
    """Show automatic API retries in the UI instead of a silent stall.

    Called once per script run. The notifiers are module-level in
    :mod:`src.api_retry`, so the previous run's Streamlit widgets are dropped
    first to avoid writing into stale containers.
    """
    clear_retry_notifiers()

    def _on_retry(message: str) -> None:
        logger.info(message)
        st.warning(message, icon="🔄")

    add_retry_notifier(_on_retry)


class _LiveProgress:
    """Live progress bar + status line for a long, blocking Streamlit batch.

    Streamlit only repaints the widgets it is told about, and a grading batch
    blocks the script repeatedly: the Google fetch, each Groq call, and the
    paced pause after every student. Two habits keep that from reading as a
    freeze:

    * **paint before the work, not after** - the bar names the student being
      graded *now* instead of trailing one behind, and every phase boundary
      (reading document / scoring / scored) produces a fresh delta; and
    * **tick through the waits** - :func:`src.llm_evaluator.paced_pause` is
      handed :meth:`tick`, so a backoff that stretches to 30s after a 429 shows
      a live countdown and its reason instead of holding one dead frame for the
      whole wait.

    It also surfaces the fallback-provider notice the moment the router leaves
    Groq, so the teacher sees which model is grading while the batch is still
    running rather than only in the summary.
    """

    # Streamlit truncates visually, and a status line is not the place for an
    # essay - the full text is always in the log/report anyway.
    _MAX_LABEL = 140

    def __init__(self, total: int) -> None:
        self.total = max(int(total), 0)
        self._fraction = 0.0
        self._label = "Starting..."
        self._suffix = ""
        self._notice_text: Optional[str] = None
        self._bar = st.progress(0.0, text=self._label)
        self._notice = st.empty()
        self._status = None
        try:
            _status_fn = getattr(st, "status", None)
            if callable(_status_fn):
                self._status = _status_fn("Running dry-run...", expanded=True)
        except Exception:
            self._status = None

    # -- painting ------------------------------------------------------------
    def _text(self) -> str:
        return f"{self._label} {self._suffix}".strip()[: self._MAX_LABEL]

    def _sync_status(self, text: str) -> None:
        status = getattr(self, "_status", None)
        if status is None:
            return
        try:
            update = getattr(status, "update", None)
            if callable(update):
                update(label=text, expanded=True)
        except Exception:
            logger.debug("status sync failed", exc_info=True)

    def _paint(self) -> None:
        value = min(max(self._fraction, 0.0), 1.0)
        text = self._text()
        self._sync_status(text)
        try:
            self._bar.progress(value, text=text)
        except Exception:
            # A dead widget must never abort a batch that is otherwise fine.
            logger.debug("progress paint failed", exc_info=True)

    def label(self, text: str) -> None:
        """Point the bar at a new phase, keeping the completed fraction."""
        self._label = text
        self._suffix = ""
        self._paint()

    def fraction(self, value: float) -> None:
        """Set the completed fraction directly (used by the dry-run callback)."""
        try:
            self._fraction = float(value)
        except (TypeError, ValueError):
            return
        self._paint()

    def begin(self, index: int, name: str, action: str) -> None:
        """Start student ``index``: bar moves to the leading edge of that slice."""
        self._fraction = (index - 1) / self.total if self.total else 0.0
        self.label(f"[{index}/{self.total}] {name} - {action}")

    def complete(self, index: int, note: str) -> None:
        """Close student ``index``: bar advances and the outcome is stated."""
        self._fraction = index / self.total if self.total else 1.0
        self.label(f"[{index}/{self.total}] {note}")

    def pacing(self) -> None:
        """Rewrite the label's tail so the wait does not read as more scoring.

        ``"... - scoring"`` becomes ``"... - pacing"``; without this the screen
        would keep claiming it is scoring for the whole pause that follows the
        score.
        """
        head = self._label.split(" - ", 1)[0]
        self.label(f"{head} - pacing")

    def tick(self, remaining: float, total: float, reason: str) -> None:
        """Recount a paced pause. Called by ``paced_pause(on_tick=...)``.

        The countdown rides on the *current* label rather than replacing it, so
        the screen keeps saying which student is being waited on while also
        saying why (``pacing`` vs ``rate-limit backoff``).
        """
        try:
            left = float(remaining)
        except (TypeError, ValueError):
            return
        if left <= 0:
            self._suffix = ""
        else:
            self._suffix = f"waiting {math.ceil(left)}s ({reason})"
        self._paint()

    def notice(self, text: Optional[str]) -> None:
        """Show the fallback-provider warning once, and only when it changes."""
        if not text or text == self._notice_text:
            return
        self._notice_text = text
        try:
            self._notice.markdown(f"⚡ {text}")
        except Exception:
            logger.debug("notice paint failed", exc_info=True)

    def clear(self) -> None:
        """Drop the transient widgets so the summary below owns the screen."""
        status = getattr(self, "_status", None)
        if status is not None:
            try:
                update = getattr(status, "update", None)
                if callable(update):
                    update(label="Dry-run finished.", expanded=False)
            except Exception:
                logger.debug("status finish failed", exc_info=True)
        for widget in (self._bar, self._notice):
            try:
                widget.empty()
            except Exception:
                logger.debug("progress cleanup failed", exc_info=True)


def doc_url(doc_id: Optional[str]) -> Optional[str]:
    """Direct Google Docs view URL for a stored Drive/Docs id."""
    if not doc_id or not str(doc_id).strip():
        return None
    return f"https://docs.google.com/document/d/{str(doc_id).strip()}/edit"


def _normalize_text(value: str) -> str:
    """Lowercase, accent-stripped text for fuzzy Hebrew/Latin matching."""
    return config_match.normalize_text(value)


def _text_tokens(value: str) -> set[str]:
    return config_match.text_tokens(value)


def _config_display_path(path: Path) -> str:
    try:
        return path.relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        return path.as_posix()


def _read_config_title(path: Path) -> str:
    return config_match.read_config_title(path)


def _score_config_for_assignment(
    config_path: Path, assignment_title: str
) -> tuple[float, str]:
    """Score a rubric file against a Classroom assignment title (0..1 + reason)."""
    return config_match.score_config_for_assignment(config_path, assignment_title)


def suggest_config_for_assignment(
    assignment_title: str, config_files: list[Path]
) -> tuple[Optional[Path], float, str]:
    """Pick the best rubric file for an assignment title, with score + reason."""
    return config_match.suggest_config_for_assignment(assignment_title, config_files)


def get_all_config_files() -> list[Path]:
    """Find rubric JSON files in assignments/, config/ and configs/ directories."""
    return config_match.list_config_files()



@st.cache_resource(show_spinner="Connecting to Google Classroom...")
def _cached_classroom_service() -> ClassroomService:
    """Build (and cache) the ClassroomService with the current OAuth grant."""
    return ClassroomService()


def _render_google_auth_error(exc: BaseException) -> None:
    """Show a Google auth failure with a one-click recovery path.

    A revoked/expired OAuth grant (``invalid_grant``) surfaces lazily - the
    saved ``token.json`` looks fine at startup and only the first real API
    call triggers the refresh Google rejects. The message alone is not
    enough: the dead grant also sits in ``st.cache_resource`` (the service)
    and ``st.cache_data`` (courses / coursework / pending scan), so without
    clearing those caches a fresh ``token.json`` would never even be read.
    This panel therefore clears every Google-backed cache entry before
    asking for a rerun.
    """
    st.error(
        "🔑 Your Google authorization has expired or was revoked "
        f"(`{type(exc).__name__}`).\n\n"
        "1. Delete `token.json` from the project folder (or run "
        "`python src/auth.py` and approve the consent screen in the browser).\n"
        "2. Press **Reconnect Google** below, then reload.",
        icon="🔑",
    )
    with st.expander("Technical details"):
        st.code(f"{type(exc).__name__}: {exc}")
    if st.button("🔄 Reconnect Google", key=f"reconnect_{id(exc)}"):
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


def _handle_google_error(exc: BaseException, fallback: str) -> bool:
    """Render ``exc`` and return True when it was an auth failure.

    Catches by message (via :func:`is_auth_failure`) rather than by class so
    it also works when Streamlit's hot-reload re-imported
    :mod:`src.classroom_service` - the ``except ClassroomServiceError`` in the
    tab bodies then holds a stale class object and would miss the live one.
    Non-auth errors fall back to a plain ``st.error`` with ``fallback``.
    """
    if is_auth_failure(exc):
        _render_google_auth_error(exc)
        return True
    st.error(f"{fallback}: {exc}")
    return False


def get_classroom_service() -> ClassroomService:
    """Return the cached service, rebuilding it when its OAuth grant is stale.

    ``st.cache_resource`` keeps the instance - and therefore the Credentials
    captured when it was built - for the lifetime of the Streamlit server
    process. ``auth.get_google_credentials`` is only called once, inside
    ``ClassroomService.__init__``, so its scope-change detection never runs
    again: a server started before the ``documents`` write scope was added
    keeps using the old token forever and every Doc write fails, while the CLI
    (which builds a fresh service on each run) works fine. The grant is
    therefore re-validated on every script run and the cache is dropped when
    it no longer covers everything we need.
    """
    service = _cached_classroom_service()
    missing = service.missing_scopes
    if not missing:
        return service

    st.warning(
        "The saved Google authorization is missing required scope(s): "
        + ", ".join(missing)
        + ". Reconnecting..."
    )
    _cached_classroom_service.clear()
    service = _cached_classroom_service()
    still_missing = service.missing_scopes
    if still_missing:
        st.error(
            "Google did not grant the scope(s) we need: "
            + ", ".join(still_missing)
            + ". Run `python src/auth.py`, approve the extra permissions in "
            "the browser, then reload this page."
        )
    return service

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
                    sub, assignment, settings.late_penalty_points
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


def _render_quota_notice() -> None:
    """Warn, in the app, when the batch had to leave Groq for the fallback.

    The router switches provider silently by design - stalling on an exhausted
    daily quota is worse than finishing on another model - but the teacher has
    to know which model graded their students. Same message as the CLI prints.
    """
    notice = _fallback_notice()
    if not notice:
        return
    st.warning(f"⚡ {notice}")


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


# ---------------------------------------------------------------------------
# Daily-check catch-up (keeps the "📅 Daily Reports" tab fresh)
# ---------------------------------------------------------------------------
DAILY_CHECK_TASK_NAME = "ClassroomAutoGrader-DailyCheck"

# One record per Streamlit *server process*: the automatic catch-up below fires
# at most once - not on every widget-triggered script rerun. (Streamlit reruns
# ``main()`` on each interaction, so a plain module-level flag is what makes
# "once at startup" actually mean once.)
_CATCHUP_STATE: dict = {"attempted": False, "ok": False, "status": ""}


def _schtasks_catchup(task_name: str = DAILY_CHECK_TASK_NAME) -> tuple:
    """Ask Windows Task Scheduler to run the daily check right now.

    Returns ``(ok, message)``. The registered task is the same read-only
    ``src.daily_check`` job the 06:00 trigger uses (and it carries
    ``--skip-if-today-done``, so this can never double-scan). Any failure -
    task not registered, non-Windows host, ``schtasks`` unavailable - comes
    back as ``(False, ...)`` so the caller can fall back. Never raises.
    """
    if os.name != "nt":
        return False, "Task Scheduler is only available on Windows."
    try:
        proc = subprocess.run(
            ["schtasks", "/Run", "/TN", task_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"schtasks failed: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err or f"schtasks exited with code {proc.returncode}."
    return True, f"scheduled task '{task_name}' started."


def _detached_catchup() -> tuple:
    """Run ``python -m src.daily_check --once`` as a detached background job.

    Fallback for when the scheduled task is not registered (or we are not on
    Windows). Detached so the Streamlit UI never blocks on the Classroom scan;
    ``--skip-if-today-done`` keeps it idempotent with the scheduled task.
    Returns ``(ok, message)``; never raises.
    """
    py = sys.executable or "python"
    root = Path(__file__).resolve().parent
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [py, "-m", "src.daily_check", "--once", "--skip-if-today-done"],
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except (OSError, ValueError) as exc:
        return False, f"could not start the background check: {exc}"
    return True, f"background read-only check started with `{py}`."


def trigger_daily_check_catchup(allow_direct_run: bool = True) -> tuple:
    """Make sure today's daily report exists, running the read-only check now
    if it does not. Prefers the registered scheduled task; with
    ``allow_direct_run`` also falls back to a detached
    ``python -m src.daily_check``. Returns ``(ok, message)``; never raises -
    a broken catch-up must not take the dashboard down.

    ``allow_direct_run=False`` is used by the *automatic* startup check: the
    scheduled task is safe to poke (idempotent, read-only, registered by
    ``Register-DailyCheckTask.ps1``), but quietly spawning Google-scanning
    processes on hosts without that task (e.g. Streamlit Community Cloud)
    would waste API quota on every server boot, so the automatic path stops
    after schtasks and leaves the explicit button as the manual fallback.
    """
    try:
        if has_report_for_today():
            return True, "Today's daily report already exists - nothing to run."
        ok, msg = _schtasks_catchup()
        if ok:
            return True, msg
        if not allow_direct_run:
            return False, msg
        ok, msg2 = _detached_catchup()
        if ok:
            return True, f"{msg} Started a direct run instead: {msg2}"
        return False, f"{msg} {msg2}"
    except Exception as exc:  # noqa: BLE001 - UI must survive anything here
        logger.warning("Daily-check catch-up failed: %s", exc)
        return False, f"catch-up failed: {exc}"


def _ensure_today_report_catchup() -> tuple:
    """Startup freshness check: trigger the fallback at most once per server
    process when today's report is missing (e.g. the machine was powered off
    at 06:00 and StartWhenAvailable has not fired yet). Only pokes the
    registered scheduled task - never spawns extra scans by itself. Returns
    the recorded ``(ok, status message)``; never raises.
    """
    state = _CATCHUP_STATE
    if state["attempted"]:
        return state["ok"], state["status"]
    state["attempted"] = True
    try:
        if has_report_for_today():
            state["ok"], state["status"] = True, ""
        else:
            state["ok"], state["status"] = trigger_daily_check_catchup(allow_direct_run=False)
            logger.info("Daily report missing for today; catch-up: %s", state["status"])
    except Exception as exc:  # noqa: BLE001 - startup must never crash the UI
        state["ok"], state["status"] = False, f"catch-up check failed: {exc}"
        logger.warning("Daily report catch-up skipped: %s", exc)
    return state["ok"], state["status"]


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
        if st.button("🚀 Run today's read-only check now", key="daily_reports_catchup"):
            ok, status = trigger_daily_check_catchup()
            (st.success if ok else st.error)(status)
            if ok:
                st.caption("The scan runs in the background; refresh in a minute to see today's report.")
        elif _CATCHUP_STATE["attempted"] and _CATCHUP_STATE["status"]:
            # Surface what the automatic startup catch-up did (once per process).
            st.caption(f"Automatic catch-up: {_CATCHUP_STATE['status']}")
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

    # Startup freshness check for the Daily Reports automation: if today's
    # report is missing (machine was off at 06:00 and the StartWhenAvailable
    # catch-up hasn't fired yet), trigger the read-only check in the
    # background - once per server process, never fatal to the UI.
    _ensure_today_report_catchup()

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

