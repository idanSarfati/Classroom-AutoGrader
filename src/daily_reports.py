"""Headless helpers for the "Daily Reports" Streamlit tab.

Lists ``exports/daily_checks/*/`` runs (newest first), loads one
``daily_check.json`` safely, flattens per-assignment rows for display.
No Streamlit import, no network - unit-tested (tests/test_daily_reports.py).
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DAILY_CHECKS_DIR = PROJECT_ROOT / "exports" / "daily_checks"
DAILY_CHECK_JSON = "daily_check.json"
DAILY_CHECK_SUMMARY = "summary.txt"


@dataclass
class DailyReportMeta:
    run_id: str = ""
    directory: Optional[Path] = None
    json_path: Optional[Path] = None
    summary_path: Optional[Path] = None
    generated_at: str = ""
    mode: str = ""
    project_id: Optional[str] = None
    course_count: int = 0
    coursework_count: int = 0
    submission_count: int = 0
    pending_count: int = 0
    late_pending_count: int = 0
    returned_count: int = 0
    error_count: int = 0
    load_error: Optional[str] = None


@dataclass
class DailyReportDetail(DailyReportMeta):
    courses: list = field(default_factory=list)
    assignment_rows: list = field(default_factory=list)
    pending_rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def audit(self) -> dict:
        """The grade-audit block of this run, if the run produced one.

        Written by ``src.daily_check`` (see ``DailyCheckResult.audit``) and
        absent in runs older than the audit, or when it was skipped with
        ``--no-audit``.
        """
        block = self.raw.get("audit")
        return block if isinstance(block, dict) else {}

    @property
    def audit_findings(self) -> list:
        rows = self.audit.get("findings")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    @property
    def audit_rows(self) -> list:
        rows = self.audit.get("rows")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _parse_stamp(name: str) -> Optional[datetime]:
    try:
        return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def has_report_for_today(root: Optional[Path] = None, now: Optional[datetime] = None) -> bool:
    """True when a *loadable* ``daily_check.json`` for today's date exists.

    This is the freshness probe behind the catch-up behaviour: the 06:00
    scheduled task runs with ``StartWhenAvailable`` (and a boot trigger), and
    both that script and the Streamlit dashboard ask this question to decide
    whether a fallback run is needed. Only folder names matter for the date
    (``YYYY-MM-DD_HH-MM-SS``, see ``src.daily_check.report_dir_for_now``);
    today's folder must contain a readable JSON object to count - a broken
    or half-written report does not satisfy the check, so a catch-up run can
    still replace it.
    """
    base = DEFAULT_DAILY_CHECKS_DIR if root is None else root
    if not base.is_dir():
        return False
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    for child in base.iterdir():
        if not child.is_dir() or not child.name.startswith(day + "_"):
            continue
        jp = child / DAILY_CHECK_JSON
        if not jp.is_file():
            continue
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return True
    return False



def _fill_meta(meta: DailyReportMeta, data: dict) -> None:
    meta.generated_at = str(data.get("generated_at", "") or "")
    meta.mode = str(data.get("mode", "") or "")
    meta.project_id = data.get("project_id")
    for key in ("course_count", "coursework_count", "submission_count", "pending_count", "late_pending_count", "returned_count"):
        try:
            setattr(meta, key, int(data.get(key, 0) or 0))
        except (TypeError, ValueError):
            setattr(meta, key, 0)
    errs = data.get("errors") or []
    meta.error_count = len(errs) if isinstance(errs, list) else 0
def list_daily_reports(root: Optional[Path] = None) -> list:
    """Saved daily-check runs, newest first (bad JSON tolerated)."""
    base = DEFAULT_DAILY_CHECKS_DIR if root is None else root
    if not base.is_dir():
        return []
    metas: list = []
    kids = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for child in kids:
        jp = child / DAILY_CHECK_JSON
        sp = child / DAILY_CHECK_SUMMARY
        meta = DailyReportMeta(run_id=child.name, directory=child, json_path=jp if jp.is_file() else None, summary_path=sp if sp.is_file() else None)
        if jp.is_file():
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                meta.load_error = f"Could not read {jp.name}: {exc}"
            else:
                _fill_meta(meta, data if isinstance(data, dict) else {})
        else:
            meta.load_error = f"No {DAILY_CHECK_JSON} here."
        metas.append(meta)
    metas.sort(key=lambda m: _parse_stamp(m.run_id) or datetime.min, reverse=True)
    return metas


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def load_daily_report(path: Path) -> DailyReportDetail:
    """Load one daily_check.json into display rows (never raises)."""
    run_dir = path if path.is_dir() else path.parent
    jp = path if path.is_file() else run_dir / DAILY_CHECK_JSON
    detail = DailyReportDetail(run_id=run_dir.name, directory=run_dir, json_path=jp if jp.is_file() else None, summary_path=(run_dir / DAILY_CHECK_SUMMARY))
    if detail.summary_path is not None and not detail.summary_path.is_file():
        detail.summary_path = None
    try:
        raw = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        detail.load_error = f"Could not read {jp}: {exc}"
        return detail
    if not isinstance(raw, dict):
        detail.load_error = f"Report {jp} is not a JSON object."
        return detail
    detail.raw = raw
    _fill_meta(detail, raw)
    detail.errors = [str(m) for m in (raw.get("errors") or []) if str(m).strip()]
    for course in raw.get("courses") or []:
        if not isinstance(course, dict):
            continue
        detail.courses.append(course)
        for item in course.get("coursework") or []:
            if not isinstance(item, dict):
                continue
            counts = item.get("submission_counts") or {}
            if not isinstance(counts, dict):
                counts = {}
            names = item.get("pending_students") or []
            waiting = ", ".join(str(n) for n in names[:8]) + (f" (+{len(names) - 8} more)" if len(names) > 8 else "") if names else "-"
            row = {"Course": str(course.get("course_name", "") or ""), "Assignment": str(item.get("title", "") or ""), "Due": str(item.get("due_date", "") or "-") or "-", "Total": _as_int(item.get("total_submissions")), "Pending": _as_int(item.get("pending_count")), "Late": _as_int(item.get("late_pending_count")), "Returned": _as_int(counts.get("RETURNED")), "States": ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "-", "Waiting students": waiting, "Error": str(item.get("error", "") or "")}
            detail.assignment_rows.append(row)
            if _as_int(item.get("pending_count")):
                detail.pending_rows.append(row)
    return detail

