"""Tests for the daily report-audit layer (src/report_audit.py).

No network, no LLM: reports are written to a tmp_path and re-audited from disk.
Run: python -m pytest tests/test_report_audit.py -q
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.grade_audit import FLAG_DEDUCTION_SUM_MISMATCH, audit_grading_result
from src.models import (
    AssignmentConfig,
    DeductionItem,
    EvaluationReport,
    GradingResult,
    ReportEntry,
    TaskDefinition,
    TaskEvaluation,
)
from src.report_audit import (
    audit_report_file,
    audit_saved_reports,
    find_report_files,
    format_audit_summary,
    looks_like_report,
)


def _ev(name: str, status: str = "CORRECT") -> TaskEvaluation:
    return TaskEvaluation(task_name=name, student_answer_found="x", status=status, notes="n")


def _rubric() -> AssignmentConfig:
    return AssignmentConfig(
        assignment_id="l1",
        title="L1",
        tasks=[
            TaskDefinition(id=1, name="T1", description="d1"),
            TaskDefinition(id=2, name="T2", description="d2"),
        ],
    )


def _clean() -> GradingResult:
    return GradingResult(
        student_name="Clean",
        score=95,
        feedback_hebrew="עבודה טובה.",
        deduction_breakdown=[
            DeductionItem(task_name="T2", points_deducted=5, reason="gap")
        ],
        task_evaluations=[_ev("T1"), _ev("T2", "PARTIALLY_CORRECT")],
    )


def _bad_math() -> GradingResult:
    """Score does not equal 100 minus the itemised deductions."""
    return GradingResult(
        student_name="Bad",
        score=100,
        feedback_hebrew="x",
        deduction_breakdown=[
            DeductionItem(task_name="T2", points_deducted=8, reason="gap")
        ],
        task_evaluations=[_ev("T1"), _ev("T2", "PARTIALLY_CORRECT")],
    )


def _write_report(
    root: Path,
    name: str = "20261010-120000.json",
    results: list | None = None,
    audited: bool = True,
) -> Path:
    results = results or [_clean()]
    entries = []
    for i, res in enumerate(results):
        entries.append(
            ReportEntry(
                student_id=f"u{i}",
                student_name=res.student_name,
                submission_id=f"s{i}",
                doc_id=f"d{i}",
                result=res,
                audit=audit_grading_result(res, _rubric(), autofix=False).to_dict()
                if audited
                else None,
            )
        )
    report = EvaluationReport(
        created_at="2026-10-10T12:00:00",
        model="qwen",
        course_id="c1",
        course_name="Class A",
        course_work_id="w1",
        course_work_title="2.1 | HW",
        assignment_config_id="l1",
        entries=entries,
    )
    target = root / name
    target.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    return target


def test_finds_only_report_shaped_json(tmp_path):
    _write_report(tmp_path)
    (tmp_path / "coursework_materials.json").write_text(
        json.dumps([{"id": 1}]), encoding="utf-8"
    )
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    found = find_report_files(tmp_path)
    assert [p.name for p in found] == ["20261010-120000.json"]


def test_looks_like_report_rejects_lists(tmp_path):
    listy = tmp_path / "materials.json"
    listy.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert not looks_like_report(listy)
    real = _write_report(tmp_path)
    assert looks_like_report(real)


def test_audit_reads_grades_back_and_separates_severity(tmp_path):
    _write_report(tmp_path, results=[_clean(), _bad_math()])
    result = audit_saved_reports(root=tmp_path, assignments_dir=tmp_path)
    assert result.report_count == 1 and result.audited_count == 1 and not result.errors
    stats = result.stats
    assert stats["total"] == 2
    assert stats["needs_review"] == 1
    assert stats["clean"] == 1
    assert FLAG_DEDUCTION_SUM_MISMATCH in stats["flags"]
    assert [f["Student"] for f in result.findings] == ["Bad"]


def test_audit_detects_problem_the_stored_verdict_missed(tmp_path):
    """A hand-edited score must be caught even though the report said clean."""
    path = _write_report(tmp_path, results=[_clean()])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"][0]["result"]["score"] = 10  # now the math does not close
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    audited = audit_report_file(path, assignments_dir=tmp_path)
    entry = audited.entries[0]
    assert entry.needs_review
    assert entry.repaired_now  # found now, absent from the stored verdict


def test_audit_never_mutates_the_report_on_disk(tmp_path):
    path = _write_report(tmp_path, results=[_bad_math()])
    before = path.read_text(encoding="utf-8")
    audit_saved_reports(root=tmp_path, assignments_dir=tmp_path, apply_fixes=False)
    assert path.read_text(encoding="utf-8") == before


def test_unreadable_report_is_recorded_not_raised(tmp_path):
    (tmp_path / "20261111-111111.json").write_text("{oops", encoding="utf-8")
    result = audit_saved_reports(root=tmp_path, assignments_dir=tmp_path)
    assert result.error_count == 1 and result.audited_count == 0
    assert any("20261111" in e for e in result.errors)


def test_empty_exports_dir_is_quiet(tmp_path):
    result = audit_saved_reports(root=tmp_path, assignments_dir=tmp_path)
    assert result.report_count == 0 and result.stats.get("total", 0) == 0
    assert "No stored evaluation reports" in format_audit_summary(result)


def test_format_summary_lists_blocking_and_systemic_separately(tmp_path):
    _write_report(tmp_path, results=[_bad_math()])
    result = audit_saved_reports(root=tmp_path, assignments_dir=tmp_path)
    text = format_audit_summary(result)
    assert "needing a human look" in text
    assert "Bad" in text
    assert "GRADE AUDIT" in text
