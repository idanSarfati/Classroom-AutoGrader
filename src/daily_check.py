"""Daily automated background check (read-only / simulation mode ONLY).
Part 1/4: header + constants. See end of file for usage.

The scan itself never writes to Google Classroom: it records submission states
and counts only. Grading is ON by default: pending submissions additionally run
through the LLM and save **releasable** ``EvaluationReport`` JSON files *locally*
(via the same read-only pipeline the Dashboard dry-run uses) - it still never
publishes a grade, comment, or return. Publishing stays a deliberate human act
in the Release tab. Pass ``--no-grade`` for a pure scan (no LLM cost). After each successful run the
previous day's scan folders and stale ``source == "daily_check"`` reports are
pruned (``--no-prune`` skips that), so only today's snapshot stays on disk.
"""
from __future__ import annotations
import argparse
import json
import logging
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.classroom_service import ClassroomService, ClassroomServiceError  # noqa: E402
from src.config_match import build_config_resolver  # noqa: E402
from src.daily_reports import has_report_for_today  # noqa: E402
from src.dashboard import (  # noqa: E402
    DRY_RUN,
    PENDING_STATE,
    ReadOnlyClassroomService,
    collect_pending_submissions,
    is_late_submission,
    is_reviewable_coursework,
    run_dry_run_evaluation,
)
# The same adaptive inter-student pause every other batch loop uses (Evaluate
# tab, CLI, dashboard dry-run): this nightly --grade run was the one caller
# firing back-to-back completions at Groq's per-minute output-token bucket.
from src.llm_evaluator import paced_pause  # noqa: E402

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "exports" / "daily_checks"
# Folder name of the scan snapshots under exports/ - step 2 of the retention
# prune must skip it (step 1 owns those folders).
DAILY_SCAN_DIRNAME = DEFAULT_OUTPUT_DIR.name
EXPECTED_PROJECT_ID = "classroom-autograder-509507"
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
READ_ONLY_MODE_LABEL = "read-only/simulation (never pushes grades, comments, or returns)"
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Console fallbacks for the emoji the audit summary uses (see ``_console_safe``).
_ASCII_MARKERS = (
    ("\u2705", "[ok]"),
    ("\U0001f527", "[fixed]"),
    ("\u26a0\ufe0f", "[review]"),
    ("\u26a0", "[review]"),
    ("\u2139\ufe0f", "[note]"),
    ("\u2139", "[note]"),
)
@dataclass
class CourseWorkCheck:
    course_id: str = ""
    course_name: str = ""
    coursework_id: str = ""
    title: str = ""
    state: Optional[str] = None
    work_type: Optional[str] = None
    due_date: Optional[str] = None
    reviewable: bool = True
    skipped_reason: Optional[str] = None
    submission_counts: dict = field(default_factory=dict)
    total_submissions: int = 0
    pending_count: int = 0
    pending_students: list = field(default_factory=list)
    late_pending_count: int = 0
    error: Optional[str] = None


@dataclass
class CourseCheck:
    course_id: str = ""
    course_name: str = ""
    section: Optional[str] = None
    coursework: list = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class DailyCheckResult:
    generated_at: str = ""
    mode: str = READ_ONLY_MODE_LABEL
    dry_run: bool = True
    project_id: Optional[str] = None
    courses: list = field(default_factory=list)
    course_count: int = 0
    coursework_count: int = 0
    submission_count: int = 0
    pending_count: int = 0
    late_pending_count: int = 0
    returned_count: int = 0
    errors: list = field(default_factory=list)
    audit: dict = field(default_factory=dict)
    # Opt-in --grade mode: releasable EvaluationReport JSON files written locally
    # (never published). Empty unless --grade ran and produced reports.
    graded_reports: list = field(default_factory=list)
    graded_count: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def read_credentials_project_id(path: Optional[Path] = None) -> Optional[str]:
    cand = path or CREDENTIALS_FILE
    try:
        data = json.loads(cand.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        inner = data.get("installed") or data.get("web") or {}
        if isinstance(inner, dict) and inner.get("project_id"):
            return str(inner["project_id"])
        if isinstance(data.get("project_id"), str):
            return str(data["project_id"])
    return None


def verify_project_id(expected: str) -> Optional[str]:
    actual = read_credentials_project_id()
    if actual is None:
        logger.warning("No project_id in %s; continuing.", CREDENTIALS_FILE)
        return None
    if expected and actual != expected:
        logger.warning("project '%s' != expected '%s'; read-only run continues.", actual, expected)
    else:
        logger.info("Verified Google Cloud project '%s'.", actual)
    return actual


def build_read_only_service() -> ReadOnlyClassroomService:
    if not DRY_RUN:
        raise RuntimeError("Daily check requires DRY_RUN=True.")
    reader = ReadOnlyClassroomService(ClassroomService())
    try:
        reader.update_submission_grade("c", "w", "s", 100)  # type: ignore
    except Exception as exc:
        from src.dashboard import DryRunWriteViolation
        if not isinstance(exc, DryRunWriteViolation):
            raise RuntimeError(f"Guard misbehaving: {exc}")
    else:
        raise RuntimeError("Guard failed to block grade write!")
    return reader


def _norm_state(raw: dict) -> str:
    return (raw.get("state") or "UNKNOWN").strip().upper() or "UNKNOWN"

def run_daily_check(service: Any, course_ids=None, include_drafts: bool = False) -> DailyCheckResult:
    if not DRY_RUN:
        raise RuntimeError("run_daily_check needs DRY_RUN=True.")
    if isinstance(service, ReadOnlyClassroomService):
        reader = service
    else:
        reader = ReadOnlyClassroomService(service)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    result = DailyCheckResult(generated_at=generated_at, dry_run=bool(DRY_RUN))
    result.project_id = read_credentials_project_id()
    try:
        courses = reader.list_courses()
    except ClassroomServiceError as exc:
        result.errors.append(f"list_courses failed: {exc}")
        return result
    if course_ids is not None:
        wanted = set(course_ids)
        courses = [c for c in courses if c.id in wanted]
    for course in courses:
        cc = CourseCheck(course_id=course.id, course_name=course.name, section=course.section)
        result.courses.append(cc)
        try:
            works = reader.list_course_work(course.id)
        except ClassroomServiceError as exc:
            cc.error = str(exc)
            result.errors.append(f"[{course.name}] list_course_work failed: {exc}")
            continue
        if include_drafts:
            try:
                drafts = reader.list_course_work_items(course.id, states=("DRAFT",))
            except Exception as exc:
                result.errors.append(f"[{course.name}] drafts failed: {exc}")
                drafts = []
            for raw in drafts or []:
                cc.coursework.append(CourseWorkCheck(course_id=course.id, course_name=course.name, coursework_id=str(raw.get("id", "")), title=str(raw.get("title", "Untitled")), state="DRAFT", reviewable=False, skipped_reason="Draft: never scanned."))
        for work in works:
            due = work.due_date.isoformat() if work.due_date else None
            item = CourseWorkCheck(course_id=course.id, course_name=course.name, coursework_id=work.id, title=work.title, state=work.state, work_type=work.work_type, due_date=due)
            cc.coursework.append(item)
            if not is_reviewable_coursework(work):
                item.reviewable = False
                item.skipped_reason = "Not a published assignment; no query made."
                continue
            try:
                raws = reader._raw_submissions(course.id, work.id)
            except ClassroomServiceError as exc:
                item.error = str(exc)
                result.errors.append(f"[{course.name}/{work.title}] list failed: {exc}")
                continue
            counts = Counter(_norm_state(r) for r in raws)
            item.submission_counts = dict(sorted(counts.items()))
            item.total_submissions = len(raws)
            pend = [r for r in raws if (r.get("state") or "").strip().upper() == PENDING_STATE]
            item.pending_count = len(pend)
            for raw in pend:
                try:
                    sub = reader._submission_from_raw(raw)
                except ClassroomServiceError as exc:
                    result.errors.append(f"[{course.name}/{work.title}] row failed: {exc}")
                    continue
                item.pending_students.append(sub.student_name)
                if is_late_submission(sub):
                    item.late_pending_count += 1
    result.course_count = len(result.courses)
    result.coursework_count = sum(len(c.coursework) for c in result.courses)
    result.submission_count = sum(i.total_submissions for c in result.courses for i in c.coursework)
    result.pending_count = sum(i.pending_count for c in result.courses for i in c.coursework)
    result.late_pending_count = sum(i.late_pending_count for c in result.courses for i in c.coursework)
    result.returned_count = sum(i.submission_counts.get("RETURNED", 0) for c in result.courses for i in c.coursework)
    return result


def grade_pending_submissions(
    service: Any,
    course_ids=None,
    output_dir: Optional[Path] = None,
    evaluate=None,
    on_status=None,
) -> dict:
    """Grade pending work and save releasable reports (ON by default).

    This reuses the Dashboard's read-only evaluation pipeline
    (:func:`src.dashboard.run_dry_run_evaluation`) unchanged, so the safety
    guarantees are identical and already proven:

    * the service is wrapped in :class:`ReadOnlyClassroomService`, which raises
      before any grade/comment/return call, and
    * the pipeline refuses to run unless ``DRY_RUN`` is True.

    The only differences from the on-screen dry-run are the ``source`` tag
    (``'daily_check'``) and the destination: reports land in the normal
    ``exports/<Course>/<Assignment>/`` layout so they natively appear in the
    Release dropdown. When ``output_dir`` is given (tests point it at a temp
    dir) the reports go there instead. Nothing is ever published here.

    Returns a small summary dict (report paths + counts) that ``run_once`` folds
    into the daily-check JSON. Never raises for a per-assignment failure - the
    pipeline records those on its result - but a whole-run error is captured and
    returned rather than aborting the scan.
    """
    if not DRY_RUN:
        raise RuntimeError("grade mode requires DRY_RUN=True (never publish).")
    summary = {
        "graded_reports": [],
        "graded_count": 0,
        "errors": [],
        # Groups that produced NO releasable report and would otherwise vanish
        # from the Release tab with no explanation. Split by cause so the log
        # can say *why* (no rubric vs. every student errored), never just "fewer
        # reports than groups".
        "no_rubric": [],
        "grade_failures": [],
        "groups_seen": 0,
    }
    try:
        groups = collect_pending_submissions(service, course_ids=course_ids)
    except Exception as exc:  # noqa: BLE001 - a scan failure must not abort
        summary["errors"].append(f"collect pending failed: {exc}")
        return summary
    if not groups:
        return summary
    try:
        results = run_dry_run_evaluation(
            service,
            groups,
            build_config_resolver(),
            directory=output_dir,
            evaluate=evaluate,
            source="daily_check",
            on_status=on_status,
            # Groq meters output tokens per minute: pace this background batch
            # exactly like the on-screen dry-run, so a nightly grade run never
            # fires back-to-back completions at the OTPM bucket (HTTP 429).
            pause=paced_pause,
        )
    except Exception as exc:  # noqa: BLE001 - keep the scan alive
        summary["errors"].append(f"grading failed: {exc}")
        return summary
    for outcome in results:
        if outcome.report_path is not None:
            summary["graded_reports"].append(str(outcome.report_path))
        if outcome.save_error:
            summary["errors"].append(
                f"[{outcome.assignment.title}] save failed: {outcome.save_error}"
            )
    summary["graded_count"] = len(summary["graded_reports"])
    return summary


def report_dir_for_now(root: Optional[Path] = None) -> Path:
    base = DEFAULT_OUTPUT_DIR if root is None else root
    return base / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

report_directory_for_now = report_dir_for_now


def prune_previous_reports(
    keep_dir: Path,
    root: Optional[Path] = None,
    keep_graded_reports: Optional[Sequence[str]] = None,
    exports_root: Optional[Path] = None,
) -> dict:
    """Delete yesterday's (and older) daily-check artifacts, keep today's.

    Removal scope - daily-check outputs only, never manual work:

    * every ``<root>/<YYYY-MM-DD_HH-MM-SS>/`` scan folder except ``keep_dir``
      (the run that just finished), and
    * stale ``source == "daily_check"`` EvaluationReport JSONs in the standard
      ``exports/<Course>/<Assignment>/`` layout, plus each report's companion
      Mashov CSV (resolved from the report's own ``csv_file`` field, only ever
      beside the report itself). Reports written by *this* run (``keep_graded_-
      reports``) and anything manual (``source`` unset/``dashboard``/``None``)
      are never touched.

    Runs strictly *after* the new report was written, so a crash mid-prune can
    never leave the teacher with no snapshot at all. Best-effort by design: a
    locked or half-written file is skipped with a warning, never an abort.
    Returns ``{"pruned_scan_dirs": [...], "pruned_reports": [...], "skipped": [...]}``
    (all strings) for the log line.

    ``exports_root`` is a test seam: when given, step 2 prunes there instead of
    the real ``exports/`` tree, so tests can prove the scope without touching
    real teacher data.
    """
    base = DEFAULT_OUTPUT_DIR if root is None else Path(root)
    outcome: dict = {"pruned_scan_dirs": [], "pruned_reports": [], "skipped": []}
    try:
        keep_resolved = keep_dir.resolve()
    except OSError:
        keep_resolved = keep_dir
    fresh = {str(p) for p in (keep_graded_reports or [])}

    # 1. Older scan folders (daily_check.json + summary.txt snapshots).
    try:
        siblings = [p for p in base.iterdir() if p.is_dir()]
    except OSError as exc:
        logger.warning("Retention prune skipped (cannot list %s): %s", base, exc)
        return outcome
    for folder in sorted(siblings):
        try:
            same = folder.resolve() == keep_resolved
        except OSError:
            same = folder == keep_dir
        if same:
            continue
        try:
            shutil.rmtree(folder)
            outcome["pruned_scan_dirs"].append(str(folder))
        except OSError as exc:
            logger.warning("Retention prune: could not remove %s: %s", folder, exc)
            outcome["skipped"].append(str(folder))

    # 2. Stale auto-graded EvaluationReports in the standard exports/ layout.
    # Lazy import: src.main owns the exports root and must never be pulled in
    # at module import time (it drags the CLI with it).
    if exports_root is None:
        try:
            from src.main import EXPORTS_DIR

            exports_root = Path(EXPORTS_DIR)
        except Exception as exc:  # noqa: BLE001 - pruning is best-effort
            logger.warning("Retention prune: cannot resolve exports dir: %s", exc)
            return outcome
    try:
        exports_resolved = Path(exports_root).resolve()
    except OSError:
        return outcome
    for report_path in sorted(exports_resolved.rglob("*.json")):
        if str(report_path) in fresh:
            continue
        if DAILY_SCAN_DIRNAME in report_path.parts:
            continue  # scan snapshots belong to step 1, not here
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # unreadable: not ours to judge, leave it alone
        if not isinstance(data, dict) or data.get("source") != "daily_check":
            continue  # manual / dry-run / foreign JSON: never touch
        try:
            report_path.unlink()
            outcome["pruned_reports"].append(str(report_path))
        except OSError as exc:
            logger.warning("Retention prune: could not remove %s: %s", report_path, exc)
            outcome["skipped"].append(str(report_path))
            continue
        csv_name = data.get("csv_file")
        if csv_name:
            sibling = report_path.parent / Path(str(csv_name)).name
            try:
                if sibling.is_file() and sibling != report_path:
                    sibling.unlink()
                    outcome["pruned_reports"].append(str(sibling))
            except OSError as exc:
                logger.warning("Retention prune: could not remove %s: %s", sibling, exc)
                outcome["skipped"].append(str(sibling))
    return outcome


def _console_safe(text: str) -> str:
    """Make a summary printable on the legacy Windows console (cp1255).

    Student and course names are Hebrew; on the default Hebrew code page an
    emoji (or any unencodable glyph) raises ``UnicodeEncodeError`` and would
    abort a scheduled run *after* the report had already been written. The
    markers are therefore downgraded to ASCII when stdout cannot encode them.
    """
    try:
        text.encode(sys.stdout.encoding or "ascii")
        return text
    except (UnicodeEncodeError, LookupError):
        pass
    for emoji, ascii_mark in _ASCII_MARKERS:
        text = text.replace(emoji, ascii_mark)
    return text.encode("ascii", "replace").decode("ascii")


def render_summary(result: DailyCheckResult) -> str:
    lines = [
        "Classroom AutoGrader - daily check (READ-ONLY, simulation only)",
        f"Generated: {result.generated_at}",
        f"Mode: {result.mode}",
        f"Google Cloud project: {result.project_id or 'unknown'}",
        "",
        f"Courses scanned:      {result.course_count}",
        f"Coursework scanned:   {result.coursework_count}",
        f"Submissions seen:     {result.submission_count}",
        f"Pending review:       {result.pending_count} (late: {result.late_pending_count})",
        f"Already returned:     {result.returned_count}",
        "",
    ]
    if result.pending_count:
        lines.append("Pending by assignment:")
        for course in result.courses:
            for item in course.coursework:
                if item.pending_count:
                    names = ", ".join(item.pending_students[:8])
                    lines.append(f"  - {course.course_name} / {item.title}: {item.pending_count} waiting [{names}]")
        lines.append("")
    if result.graded_reports:
        lines.append(f"Graded locally (--grade, ready to release): {len(result.graded_reports)} report(s)")
        for path in result.graded_reports[:20]:
            lines.append(f"  * {path}")
        lines.append("")
    if result.errors:
        lines.append(f"Warnings ({len(result.errors)}):")
        lines.extend(f"  ! {m}" for m in result.errors[:20])
        lines.append("")
    lines.append("No grades pushed, no comments posted, no submissions returned.")
    audit_block = format_audit_summary_from(result.audit)
    if audit_block:
        lines.append(audit_block)
    return "\n".join(lines)


def format_audit_summary_from(audit: dict) -> str:
    """Render the stored audit block back into the summary text."""
    if not audit:
        return ""
    try:
        from src.report_audit import ReportsAuditResult

        rebuilt = ReportsAuditResult()
        rebuilt.report_count = int(audit.get("report_count", 0) or 0)
        rebuilt.audited_count = int(audit.get("audited_count", 0) or 0)
        rebuilt.skipped_count = int(audit.get("skipped_count", 0) or 0)
        rebuilt.error_count = int(audit.get("error_count", 0) or 0)
        rebuilt.stats = dict(audit.get("stats") or {})
        rebuilt.rows = list(audit.get("rows") or [])
        rebuilt.findings = list(audit.get("findings") or [])
        rebuilt.errors = list(audit.get("errors") or [])
        from src.report_audit import format_audit_summary

        return format_audit_summary(rebuilt)
    except Exception as exc:  # noqa: BLE001 - a report must never fail on this
        logger.debug("Could not render audit summary: %s", exc)
        return ""


def audit_stored_reports(limit: Optional[int] = None) -> dict:
    """Re-audit every saved report locally: no LLM call, no writes.

    Returns ``{}`` when nothing could be audited, so a missing/failed audit
    never disturbs the Classroom scan itself.
    """
    try:
        from src.report_audit import audit_saved_reports

        return audit_saved_reports(limit=limit).to_dict()
    except Exception as exc:  # noqa: BLE001 - auditing is best-effort by design
        logger.warning("Grade audit skipped: %s", exc)
        return {}


def write_report(result: DailyCheckResult, directory: Path) -> tuple:
    directory.mkdir(parents=True, exist_ok=True)
    jp = directory / "daily_check.json"
    jp.write_text(result.to_json() + "\n", encoding="utf-8")
    sp = directory / "summary.txt"
    sp.write_text(render_summary(result) + "\n", encoding="utf-8")
    logger.info("Report written to %s", jp)
    return jp, sp

def parse_time_of_day(value: str) -> tuple:
    m = _TIME_RE.match(value.strip())
    if not m:
        raise ValueError(f"Time must be HH:MM (24h), got {value!r}.")
    return int(m.group(1)), int(m.group(2))


def seconds_until_next_run(now: datetime, hour: int, minute: int) -> float:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_once(course_ids=None, output_dir: Optional[Path] = None, include_drafts: bool = False, require_project: str = EXPECTED_PROJECT_ID, audit_reports: bool = True, skip_if_today_done: bool = False, grade: bool = True, prune: bool = True, exports_root: Optional[Path] = None) -> tuple:
    if skip_if_today_done and has_report_for_today(output_dir):
        # Idempotent catch-up: the boot/startup trigger and the dashboard's
        # fallback both fire the same read-only check, and a machine that was
        # off at 06:00 can produce several triggers in one morning. Skipping
        # here guarantees at most one report folder per calendar day.
        logger.info("Today's daily report already exists; skipping this run.")
        return None, None
    if require_project:
        verify_project_id(require_project)
    # One read-only service for the whole run: the scan and the grading share
    # it, so the write-blocking guarantee covers both.
    service = build_read_only_service()
    result = run_daily_check(service, course_ids=course_ids, include_drafts=include_drafts)
    if grade:
        # Default: grade the pending work and save releasable EvaluationReport
        # JSON locally (never published - the Release tab is the only publish
        # path). Reports deliberately go to the *standard* exports/ layout
        # (NOT this scan's daily_checks/ folder, which the Release dropdown
        # skips) so they show up there tagged [Daily Check]. Hence output_dir
        # is not forwarded here. Pass grade=False / --no-grade for a pure
        # scan (no LLM cost).
        grade_summary = grade_pending_submissions(service, course_ids=course_ids)
        result.graded_reports = grade_summary["graded_reports"]
        result.graded_count = grade_summary["graded_count"]
        result.errors.extend(grade_summary["errors"])
    if audit_reports:
        # Local-only re-check of grades that have not been released yet. Zero
        # tokens, zero writes: it reads the report JSON and re-does the math.
        result.audit = audit_stored_reports()
    directory = report_dir_for_now(output_dir)
    json_path, _ = write_report(result, directory)
    if prune:
        # Single-day retention: yesterday's scan folders and stale
        # source=="daily_check" reports go away now that today's snapshot
        # (plus this run's fresh graded reports) is safely on disk. Manual
        # reports are never touched; --no-prune skips this. exports_root is a
        # test seam so unit tests can prove the scope in a temp dir.
        pruned = prune_previous_reports(
            directory,
            root=output_dir,
            keep_graded_reports=result.graded_reports,
            exports_root=exports_root,
        )
        n_dirs = len(pruned["pruned_scan_dirs"])
        n_reports = len(pruned["pruned_reports"])
        if n_dirs or n_reports or pruned["skipped"]:
            logger.info(
                "Retention prune: removed %d scan folder(s) and %d report file(s)%s.",
                n_dirs,
                n_reports,
                f" ({len(pruned['skipped'])} skipped)" if pruned["skipped"] else "",
            )
    print(_console_safe(render_summary(result)))
    print(f"\nReport: {json_path}")
    return result, directory


def run_daemon(at_time: str = "06:00", course_ids=None, output_dir: Optional[Path] = None, include_drafts: bool = False, require_project: str = EXPECTED_PROJECT_ID, audit_reports: bool = True, grade: bool = True, prune: bool = True) -> None:
    hour, minute = parse_time_of_day(at_time)
    logger.info("Daemon: daily at %02d:%02d local. Ctrl+C stops.", hour, minute)
    while True:
        now = datetime.now()
        wait = seconds_until_next_run(now, hour, minute)
        nxt = now + timedelta(seconds=wait)
        logger.info("Next check %s (in %.1fh).", nxt.strftime("%Y-%m-%d %H:%M"), wait / 3600)
        try:
            end = time.monotonic() + wait
            while True:
                rem = end - time.monotonic()
                if rem <= 0:
                    break
                time.sleep(min(60.0, rem))
            run_once(course_ids=course_ids, output_dir=output_dir, include_drafts=include_drafts, require_project=require_project, audit_reports=audit_reports, grade=grade, prune=prune)
        except KeyboardInterrupt:
            logger.info("Daemon stopped.")
            raise
        except Exception:
            logger.exception("Check failed; retrying next slot.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Daily Classroom check (READ-ONLY: scans coursework+submissions, writes local report, never publishes). Usage: python -m src.daily_check --once | --daemon --time 06:00")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", dest="once", action="store_true", default=True, help="Single scan now (default).")
    mode.add_argument("--daemon", dest="daemon", action="store_true", help="Background: run daily at --time.")
    p.add_argument("--time", default="06:00", help="Daily time for --daemon as HH:MM local.")
    p.add_argument("--course-id", dest="course_ids", action="append", default=None)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--include-drafts", action="store_true")
    p.add_argument("--skip-if-today-done", dest="skip_if_today_done", action="store_true", help="Exit without scanning when today's report already exists (makes boot/catch-up triggers idempotent).")
    p.add_argument("--require-project", default=EXPECTED_PROJECT_ID)
    p.add_argument("--no-audit", dest="audit_reports", action="store_false", default=True, help="Skip the local re-audit of saved evaluation reports.")
    p.add_argument("--grade", "--no-grade", dest="grade", action=argparse.BooleanOptionalAction, default=True, help="Grade pending submissions with the LLM and save releasable EvaluationReport JSON locally (still never publishes - review in the Release tab). ON by default; --no-grade gives a pure scan with no LLM cost.")
    p.add_argument("--prune", "--no-prune", dest="prune", action=argparse.BooleanOptionalAction, default=True, help="After a successful run, delete previous days' scan folders and stale auto-graded (source == 'daily_check') reports, keeping manual reports untouched. ON by default; --no-prune keeps everything.")
    p.add_argument("--log-level", default=getattr(settings, "log_level", "INFO"))
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=str(args.log_level).upper(), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        if args.daemon:
            run_daemon(at_time=args.time, course_ids=args.course_ids, output_dir=args.output_dir, include_drafts=args.include_drafts, require_project=args.require_project, audit_reports=args.audit_reports, grade=args.grade, prune=args.prune)
        else:
            result = run_once(course_ids=args.course_ids, output_dir=args.output_dir, include_drafts=args.include_drafts, require_project=args.require_project, audit_reports=args.audit_reports, skip_if_today_done=args.skip_if_today_done, grade=args.grade, prune=args.prune)
            if result[0] is None:
                # Skipped because today's report already exists - a success,
                # not a failure (exit code 0 keeps Task Scheduler happy).
                return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except ClassroomServiceError as exc:
        print(f"Classroom API: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Daily check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

