"""Streamlit Web UI for Classroom AutoGrader.

Provides a clean two-tab interface wrapping our grading and release backend:
  1. Evaluate Submissions: pick course, assignment, config -> evaluate turned-in docs -> export CSV + JSON report.
  2. Release Grades & Feedback: choose saved report -> preview students/scores -> push grades, private comments, doc feedback, return submissions -> prune released files.

Transient network failures (a dropped socket, Windows ``WinError 10053``) are
retried inside :mod:`src.api_retry`; :func:`_register_retry_notifier` surfaces
those retries in the UI so a long pause never looks like a frozen app.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
import time

from config.settings import settings
from src.api_retry import add_retry_notifier, clear_retry_notifiers
from src.classroom_service import ClassroomService, ClassroomServiceError
from src.fetch_submissions import UNSUPPORTED_ATTACHMENT_MSG
from src.late_policy import penalty_for
from src.llm_evaluator import (
    EVAL_PAUSE_SECONDS,
    LLMEvaluationError,
    evaluate_student_submission,
)
from src.main import (
    ASSIGNMENTS_DIR,
    EXPORTS_DIR,
    GradingRow,
    _delete_report_artifacts,
    _list_report_files,
    _tasks_summary,
    deductions_summary,
    load_assignment_config,
    mashov_csv_name,
    report_directory,
    resolve_mapped_config,
    save_assignment_mapping,
    write_evaluation_report,
    write_mashov_csv,
)
from src.models import AssignmentConfig, Course, CourseWork, EvaluationReport, StudentSubmission

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


def doc_url(doc_id: Optional[str]) -> Optional[str]:
    """Direct Google Docs view URL for a stored Drive/Docs id."""
    if not doc_id or not str(doc_id).strip():
        return None
    return f"https://docs.google.com/document/d/{str(doc_id).strip()}/edit"


def _normalize_text(value: str) -> str:
    """Lowercase, accent-stripped text for fuzzy Hebrew/Latin matching."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _text_tokens(value: str) -> set[str]:
    return set(re.findall(r"[\w\u0590-\u05FF]+", _normalize_text(value)))


def _config_display_path(path: Path) -> str:
    try:
        return path.relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        return path.as_posix()


def _read_config_title(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if isinstance(data, dict):
        title = data.get("title", "")
        return str(title) if title else ""
    return ""


def _score_config_for_assignment(
    config_path: Path, assignment_title: str
) -> tuple[float, str]:
    """Score a rubric file against a Classroom assignment title (0..1 + reason)."""
    assignment_norm = _normalize_text(assignment_title)
    stem = config_path.stem  # e.g. lesson_2_worksheet
    stem_norm = _normalize_text(stem)
    rubric_title = _read_config_title(config_path)
    rubric_norm = _normalize_text(rubric_title)

    # 1. Exact assignment_id token inside the assignment title ("lesson_2", "lesson2").
    compact_stem = stem_norm.replace("_", "").replace("-", "").replace(" ", "")
    compact_title = assignment_norm.replace("_", "").replace("-", "").replace(" ", "")
    if compact_stem and compact_stem in compact_title:
        return 1.0, f"assignment id '{stem}' found in assignment title"

    # 2. Same explicit task/lesson number ("2" in "lesson_2" vs "שיעור 2").
    stem_number = (re.findall(r"\d+", stem_norm) or [None])[0]
    title_numbers = set(re.findall(r"\d+", assignment_norm))
    if stem_number and stem_number in title_numbers:
        return 0.85, f"matching number '{stem_number}' in title"

    # 3. Token overlap between rubric file title and assignment title.
    config_tokens = _text_tokens(f"{stem} {rubric_title}")
    assignment_tokens = _text_tokens(assignment_title)
    overlap = config_tokens & assignment_tokens
    if overlap and assignment_tokens:
        score = len(overlap) / max(len(assignment_tokens), 1)
        if score >= 0.2:
            return (
                min(0.8, score),
                f"shared words: {', '.join(sorted(overlap))}",
            )

    # 4. Fallback: raw string similarity on both stem and rubric title.
    candidates = [c for c in (rubric_norm, stem_norm) if c]
    best = max(
        (SequenceMatcher(None, assignment_norm, c).ratio() for c in candidates),
        default=0.0,
    )
    if best >= 0.6:
        return best * 0.7, "similar title text"
    return 0.0, ""


def suggest_config_for_assignment(
    assignment_title: str, config_files: list[Path]
) -> tuple[Optional[Path], float, str]:
    """Pick the best rubric file for an assignment title, with score + reason."""
    best_path: Optional[Path] = None
    best_score = 0.0
    best_reason = ""
    for config_path in config_files:
        score, reason = _score_config_for_assignment(config_path, assignment_title)
        if score > best_score:
            best_path, best_score, best_reason = config_path, score, reason
    return best_path, best_score, best_reason


def get_all_config_files() -> list[Path]:
    """Find assignment config JSON files in assignments/ and config/ directories."""
    configs: list[Path] = []
    seen_names: set[str] = set()

    for directory in (ASSIGNMENTS_DIR, CONFIG_DIR):
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.json")):
            if file.name in {"assignment_mapping.json", "credentials.json", "token.json"}:
                continue
            if file.name not in seen_names:
                configs.append(file)
                seen_names.add(file.name)
    return configs


@st.cache_resource(show_spinner="Connecting to Google Classroom...")
def _cached_classroom_service() -> ClassroomService:
    """Build (and cache) the ClassroomService with the current OAuth grant."""
    return ClassroomService()


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
        courses = service.list_courses()
    except ClassroomServiceError as exc:
        st.error(f"Failed to fetch Google Classroom courses: {exc}")
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
        assignments = service.list_course_work(selected_course.id)
    except ClassroomServiceError as exc:
        st.error(f"Failed to fetch assignments: {exc}")
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
        st.error("No rubric configuration JSON files found in `assignments/` or `config/`.")
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
        config: AssignmentConfig = load_assignment_config(selected_config_file)
    except Exception as exc:
        st.error(f"Failed to parse configuration file '{selected_config_name}': {exc}")
        return

    # Exempt (broken) tasks are skipped by the grader, so state it up front to
    # avoid "8 tasks defined" vs 7 graded rows confusion.
    exempt_count = sum(1 for task in config.tasks if task.exempt)
    exempt_note = f", {exempt_count} exempt (skipped)" if exempt_count else ""
    st.write(
        f"**Rubric Loaded:** `{config.assignment_id}` - *{config.title}* "
        f"({len(config.tasks)} tasks defined{exempt_note})"
    )

    if st.button("Run Evaluation", type="primary", use_container_width=True):
        _run_evaluation_action(service, selected_course, selected_assignment, selected_config_file, config)


def _run_evaluation_action(
    service: ClassroomService,
    course: Course,
    assignment: CourseWork,
    config_file: Path,
    config: AssignmentConfig,
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
        except ClassroomServiceError as exc:
            st.error(f"Failed to fetch submissions: {exc}")
            return

    turned_in = [s for s in submissions if (s.state or "").strip().upper() == "TURNED_IN"]
    if not turned_in:
        st.info("No 'TURNED_IN' submissions found for this assignment.")
        return

    st.info(f"Grading {len(turned_in)} turned-in submission(s) using `{settings.groq_model}`...")

    progress_bar = st.progress(0)
    status_text = st.empty()
    rows: list[GradingRow] = []

    for idx, sub in enumerate(turned_in, start=1):
        status_text.text(f"Evaluating {idx}/{len(turned_in)}: {sub.student_name}...")
        if not sub.doc_id:
            rows.append(GradingRow(sub, error=UNSUPPORTED_ATTACHMENT_MSG))
        else:
            try:
                doc_text = service.extract_doc_text(sub.doc_id)
            except ClassroomServiceError as exc:
                rows.append(GradingRow(sub, error=f"Document extraction failed: {exc}"))
                progress_bar.progress(idx / len(turned_in))
                continue

            try:
                res = evaluate_student_submission(
                    sub.student_name,
                    doc_text,
                    config,
                    late_penalty_points=penalty_for(
                        sub, assignment, settings.late_penalty_points
                    ),
                )
                rows.append(GradingRow(sub, result=res))
            except LLMEvaluationError as exc:
                rows.append(GradingRow(sub, error=f"LLM evaluation failed: {exc}"))
            # Groq meters output tokens per minute: pause between students so
            # sequential completions do not burst the OTPM budget (HTTP 429).
            if idx < len(turned_in):
                time.sleep(EVAL_PAUSE_SECONDS)

        progress_bar.progress(idx / len(turned_in))

    status_text.empty()
    progress_bar.empty()

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
            "Notes / Feedback": notes,
        })

    df = pd.DataFrame(table_data)
    st.dataframe(df, use_container_width=True)


def render_release_tab(service: ClassroomService) -> None:
    st.subheader("Release Grades & Feedback")
    st.caption(
        "Publish finalized grades and feedback comments to Google Docs, and return submissions "
        "to students. Fully released reports are automatically pruned."
    )

    reports = _list_report_files(EXPORTS_DIR)
    if not reports:
        st.info("No saved evaluation reports found in `exports/`. Run an evaluation in Tab 1 first.")
        return

    report_labels = {}
    for r_path in reports:
        mtime = datetime.fromtimestamp(r_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        try:
            rel_path = r_path.relative_to(EXPORTS_DIR).as_posix()
        except ValueError:
            rel_path = r_path.as_posix()
        report_labels[f"{rel_path} ({mtime})"] = r_path

    selected_label = st.selectbox("Select Saved Evaluation Report", list(report_labels.keys()))
    selected_report_path = report_labels[selected_label]

    try:
        report = EvaluationReport.model_validate(
            json.loads(selected_report_path.read_text(encoding="utf-8"))
        )
    except Exception as exc:
        st.error(f"Failed to read or parse report file: {exc}")
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
            use_container_width=True,
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
            use_container_width=True,
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
                "Feedback": entry.result.feedback_hebrew,
                "Submission": doc_url(entry.doc_id) or "",
                "Remove ❌": False,
            }
            for entry in report.entries
        ]
    )
    edited_df = st.data_editor(
        df,
        use_container_width=True,
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
    st.dataframe(pd.DataFrame(results_data), use_container_width=True)

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

    with st.sidebar:
        st.caption("Google connection")
        st.caption(
            "Writing feedback into a student's document needs the "
            "`documents` write scope. If feedback publishing fails with a "
            "403, reconnect after running `python src/auth.py`."
        )
        if st.button("🔄 Reconnect Google", use_container_width=True):
            _cached_classroom_service.clear()
            st.rerun()

    try:
        service = get_classroom_service()
    except Exception as exc:
        st.error(
            f"Failed to initialize Google Classroom service: {exc}\n\n"
            "Please check that credentials or authentication tokens exist."
        )
        return

    tab1, tab2 = st.tabs(["Evaluate Submissions", "Release Grades & Feedback"])

    with tab1:
        render_evaluate_tab(service)

    with tab2:
        render_release_tab(service)


if __name__ == "__main__":
    main()

