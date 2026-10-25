"""Tests for the Daily Reports reader (no network, no Streamlit).

Run: python -m pytest tests/test_daily_reports.py -q
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.daily_reports import has_report_for_today, list_daily_reports, load_daily_report


def _write_run(root: Path, name: str, payload: dict) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "daily_check.json").write_text(json.dumps(payload), encoding="utf-8")
    (d / "summary.txt").write_text("summary", encoding="utf-8")
    return d


def _payload(**over):
    base = {"generated_at": "2026-10-11T06:00:00+03:00", "mode": "read-only", "project_id": "classroom-autograder-509507", "course_count": 1, "coursework_count": 1, "submission_count": 4, "pending_count": 2, "late_pending_count": 1, "returned_count": 1, "errors": [], "courses": [{"course_id": "c", "course_name": "Math", "coursework": [{"course_id": "c", "course_name": "Math", "coursework_id": "w", "title": "HW1", "due_date": "2026-10-14", "submission_counts": {"NEW": 1, "RETURNED": 1, "TURNED_IN": 2}, "total_submissions": 4, "pending_count": 2, "pending_students": ["A", "B"], "late_pending_count": 1}]}]}
    base.update(over)
    return base


def test_lists_newest_first(tmp_path):
    _write_run(tmp_path, "2026-10-09_06-00-00", _payload())
    _write_run(tmp_path, "2026-10-11_06-00-00", _payload())
    metas = list_daily_reports(tmp_path)
    assert [m.run_id for m in metas] == ["2026-10-11_06-00-00", "2026-10-09_06-00-00"]
    assert metas[0].pending_count == 2 and metas[0].load_error is None


def test_missing_and_broken_reports_tolerated(tmp_path):
    empty = tmp_path / "2026-10-10_06-00-00"
    empty.mkdir()
    bad = tmp_path / "2026-10-11_06-00-00"
    bad.mkdir()
    (bad / "daily_check.json").write_text("{nope", encoding="utf-8")
    metas = list_daily_reports(tmp_path)
    assert len(metas) == 2 and all(m.load_error for m in metas)
    assert list_daily_reports(tmp_path / "nope") == []


def test_load_flattens_assignment_rows(tmp_path):
    d = _write_run(tmp_path, "2026-10-11_06-00-00", _payload())
    detail = load_daily_report(d / "daily_check.json")
    assert detail.load_error is None and len(detail.assignment_rows) == 1
    row = detail.assignment_rows[0]
    assert row["Course"] == "Math" and row["Pending"] == 2 and row["Returned"] == 1
    assert row["States"] == "NEW: 1, RETURNED: 1, TURNED_IN: 2"
    assert len(detail.pending_rows) == 1 and detail.errors == []


def test_load_collects_errors(tmp_path):
    d = _write_run(tmp_path, "2026-10-11_06-00-00", _payload(errors=["boom"]))
    detail = load_daily_report(d)
    assert detail.errors == ["boom"] and detail.error_count == 1


# --------------------------------------------------------------------------- #
# has_report_for_today: the freshness probe behind the catch-up behaviour
# --------------------------------------------------------------------------- #
def test_has_report_for_today_true_for_loadable_run(tmp_path):
    _write_run(tmp_path, "2026-10-13_06-53-44", _payload())
    assert has_report_for_today(tmp_path, now=datetime(2026, 10, 13, 9, 0)) is True


def test_has_report_for_today_false_when_only_yesterday_exists(tmp_path):
    _write_run(tmp_path, "2026-10-12_06-00-00", _payload())
    assert has_report_for_today(tmp_path, now=datetime(2026, 10, 13, 9, 0)) is False


def test_has_report_for_today_false_for_broken_or_missing_json(tmp_path):
    # A folder for today whose JSON is corrupt does NOT count: the catch-up
    # run must still be allowed to replace it.
    broken = tmp_path / "2026-10-13_06-00-00"
    broken.mkdir()
    (broken / "daily_check.json").write_text("{nope", encoding="utf-8")
    # And a folder without any JSON at all doesn't count either.
    (tmp_path / "2026-10-13_07-00-00").mkdir()
    assert has_report_for_today(tmp_path, now=datetime(2026, 10, 13, 9, 0)) is False


def test_has_report_for_today_false_when_dir_missing(tmp_path):
    assert has_report_for_today(tmp_path / "nope", now=datetime(2026, 10, 13, 9, 0)) is False
