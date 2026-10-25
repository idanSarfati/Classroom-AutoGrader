"""Tests for the Release tab's report loading + dropdown filtering.

The bug this pins down: the "Release Grades & Feedback" tab used to list every
``*.json`` under ``exports/`` (via ``_list_report_files``), which included the
read-only daily-check scans in ``exports/daily_checks/*/daily_check.json``.
Selecting one crashed the tab with a raw Pydantic dump - "8 validation errors
for EvaluationReport ... created_at / course_id / entries ..." - because a daily
check is a course-wide submission snapshot, not a graded report.

Two independent guards now stop that:

* the dropdown is fed by ``report_audit.find_report_files``, which skips the
  daily_checks tree and every non-report JSON, so a daily check can never be
  selected; and
* ``app._load_release_report`` turns *any* incompatible file (daily check,
  material dump, truncated JSON) into a short, human notice instead of raising,
  so a stray file that still reaches the loader is answered gracefully.

Run from the project root::

    python -m pytest tests/test_release_report_loading.py -q

No network, no credentials, no Streamlit runtime: the helpers are pure, and the
``app`` module is imported the same way ``test_ui_progress.py`` imports it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.daily_reports import DAILY_CHECK_JSON  # noqa: E402
from src.models import (  # noqa: E402
    DeductionItem,
    EvaluationReport,
    GradingResult,
    ReportEntry,
    TaskEvaluation,
)
from src.report_audit import find_report_files  # noqa: E402

import app as app_module  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
def _ev(name: str, status: str = "CORRECT") -> TaskEvaluation:
    return TaskEvaluation(task_name=name, student_answer_found="x", status=status, notes="n")


def _grading() -> GradingResult:
    return GradingResult(
        student_name="Noa",
        score=90,
        feedback_hebrew="עבודה טובה.",
        deduction_breakdown=[DeductionItem(task_name="T2", points_deducted=10, reason="gap")],
        task_evaluations=[_ev("T1"), _ev("T2", "PARTIALLY_CORRECT")],
    )


def _write_report(root: Path, name: str = "20261010-120000.json") -> Path:
    report = EvaluationReport(
        created_at="2026-10-10T12:00:00",
        model="qwen",
        course_id="c1",
        course_name="Class A",
        course_work_id="w1",
        course_work_title="2.1 | HW",
        assignment_config_id="l1",
        entries=[
            ReportEntry(
                student_id="u0",
                student_name="Noa",
                submission_id="s0",
                doc_id="d0",
                result=_grading(),
            )
        ],
    )
    target = root / name
    target.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    return target


def _daily_check_payload(**over) -> dict:
    base = {
        "generated_at": "2026-10-14T06:28:50+03:00",
        "mode": "read-only/simulation",
        "dry_run": True,
        "project_id": "classroom-autograder-509507",
        "course_count": 1,
        "coursework_count": 1,
        "submission_count": 4,
        "pending_count": 2,
        "courses": [{"course_id": "c", "course_name": "Math", "coursework": []}],
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# 1. Dropdown filtering: a daily check never appears as a releasable report
# --------------------------------------------------------------------------- #
def test_dropdown_excludes_daily_check_summaries(tmp_path):
    """The list the Release dropdown shows must not contain daily_check.json."""
    _write_report(tmp_path)
    checks = tmp_path / "daily_checks" / "2026-10-14_06-29-49"
    checks.mkdir(parents=True)
    (checks / DAILY_CHECK_JSON).write_text(json.dumps(_daily_check_payload()), encoding="utf-8")
    # A material dump in the same tree must also stay out.
    (tmp_path / "coursework_materials.json").write_text(
        json.dumps({"courses": [{"id": "x", "assignments": []}]}), encoding="utf-8"
    )

    found = find_report_files(tmp_path)

    assert [p.name for p in found] == ["20261010-120000.json"]
    assert all(DAILY_CHECK_JSON not in p.parts for p in found)


def test_real_exports_tree_lists_only_reports():
    """Smoke test against the real exports/ layout: no daily_checks file leaks in.

    Guarded so the assertion is meaningful whether or not the repo's exports/
    directory happens to exist in a given checkout.
    """
    from src.main import EXPORTS_DIR

    if not EXPORTS_DIR.is_dir():
        return
    found = find_report_files(EXPORTS_DIR)
    assert found, "expected at least one saved report under exports/"
    for path in found:
        assert "daily_checks" not in path.parts
        assert path.name != DAILY_CHECK_JSON


# --------------------------------------------------------------------------- #
# 2. _looks_like_daily_check: recognise the scan by name, place, and shape
# --------------------------------------------------------------------------- #
def test_daily_check_detected_by_name(tmp_path):
    path = tmp_path / DAILY_CHECK_JSON
    path.write_text(json.dumps(_daily_check_payload()), encoding="utf-8")
    assert app_module._looks_like_daily_check(path, _daily_check_payload())


def test_daily_check_detected_by_location(tmp_path):
    """Even a renamed file inside daily_checks/ is treated as a daily check."""
    run = tmp_path / "daily_checks" / "2026-10-14_06-29-49"
    run.mkdir(parents=True)
    path = run / "snapshot.json"
    path.write_text("{}", encoding="utf-8")
    assert app_module._looks_like_daily_check(path, {})


def test_daily_check_detected_by_shape(tmp_path):
    """A daily check moved out of its folder is still caught by its keys."""
    path = tmp_path / "moved.json"
    assert app_module._looks_like_daily_check(path, _daily_check_payload())


def test_material_dump_is_not_a_daily_check(tmp_path):
    """coursework_materials.json has 'courses' but none of the scan markers."""
    path = tmp_path / "coursework_materials.json"
    data = {"courses": [{"id": "x", "name": "C", "assignments": []}]}
    assert not app_module._looks_like_daily_check(path, data)


def test_evaluation_report_is_not_a_daily_check(tmp_path):
    path = _write_report(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert not app_module._looks_like_daily_check(path, raw)


# --------------------------------------------------------------------------- #
# 3. _load_release_report: graceful notices instead of a Pydantic traceback
# --------------------------------------------------------------------------- #
def test_loads_a_valid_report(tmp_path):
    path = _write_report(tmp_path)
    report, notice = app_module._load_release_report(path)
    assert report is not None and notice is None
    assert report.course_name == "Class A"
    assert report.entries[0].student_name == "Noa"


def test_daily_check_yields_a_friendly_notice(tmp_path):
    """Selecting a daily check returns guidance, never raises."""
    run = tmp_path / "daily_checks" / "2026-10-14_06-29-49"
    run.mkdir(parents=True)
    path = run / DAILY_CHECK_JSON
    path.write_text(json.dumps(_daily_check_payload()), encoding="utf-8")

    report, notice = app_module._load_release_report(path)

    assert report is None
    assert notice is not None
    assert "Daily Check Summary" in notice
    assert "not an Evaluation Report" in notice
    assert "Daily Reports" in notice
    # The old crash text must be gone.
    assert "validation error" not in notice.lower()
    assert "created_at" not in notice


def test_unrelated_json_yields_a_notice_not_a_traceback(tmp_path):
    """A material dump that somehow reaches the loader is answered kindly."""
    path = tmp_path / "coursework_materials.json"
    path.write_text(json.dumps({"courses": [{"id": "x"}]}), encoding="utf-8")
    report, notice = app_module._load_release_report(path)
    assert report is None
    assert notice is not None
    assert "not a valid Evaluation Report" in notice
    # It must be a short, single-line notice - never the raw multi-line Pydantic
    # dump (which carried a field list, echoed input values, and a doc URL).
    assert "\n" not in notice
    assert "errors.pydantic.dev" not in notice
    assert "For further information" not in notice


def test_truncated_json_is_reported_not_raised(tmp_path):
    path = tmp_path / "20261010-120000.json"
    path.write_text("{not valid json", encoding="utf-8")
    report, notice = app_module._load_release_report(path)
    assert report is None
    assert "JSON" in notice

