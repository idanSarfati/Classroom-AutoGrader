"""Tests for the read-only daily background check (no network/credentials).

Run: python -m pytest tests/test_daily_check.py -q
Covers: read-only guarantees, aggregation, scheduler math, report writing.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.daily_check as dc
from src.dashboard import CLASSROOM_WRITE_METHODS, DRY_RUN, DryRunWriteViolation
from src.models import Course, CourseWork, StudentSubmission


class FakeService:
    def __init__(self, courses=(), works=None, raw=None):
        self.courses = list(courses)
        self.works = dict(works or {})
        self.raw = dict(raw or {})
        self.write_attempts: list[str] = []

    def list_courses(self):
        return list(self.courses)

    def list_course_work(self, course_id):
        return list(self.works.get(course_id, []))

    def list_course_work_items(self, course_id, states=("DRAFT",)):
        return []

    def _raw_submissions(self, course_id, work_id):
        return [dict(r) for r in self.raw.get(work_id, [])]

    def _submission_from_raw(self, raw):
        late = (raw.get("lateState") or "").upper() == "LATE"
        return StudentSubmission(student_id=raw.get("userId", "u"), student_name=f"Student-{raw.get('userId', 'u')}", submission_id=raw.get("id", "s"), state=raw.get("state", "TURNED_IN"), doc_id="doc", submitted_at=None, late_state=raw.get("lateState"), is_late=True if late else None)

    def __getattr__(self, name):
        if name in CLASSROOM_WRITE_METHODS:
            def _blocked(*a, **k):
                self.write_attempts.append(name)
                raise DryRunWriteViolation(f"blocked {name}")
            return _blocked
        raise AttributeError(name)


def _raw(sid, state, late=None):
    r = {"id": sid, "userId": sid, "state": state}
    if late:
        r["lateState"] = late
    return r


def test_dry_run_constant_is_true():
    assert DRY_RUN is True


def test_every_write_method_blocked_through_daily_wrapper():
    from src.dashboard import ReadOnlyClassroomService
    fake = FakeService()
    reader = ReadOnlyClassroomService(fake)
    for name in CLASSROOM_WRITE_METHODS:
        with pytest.raises(DryRunWriteViolation):
            getattr(reader, name)()
    assert fake.write_attempts == []


def test_run_daily_check_aggregates_without_writing(tmp_path):
    svc = FakeService(
        courses=[Course(id="c1", name="Math"), Course(id="c2", name="Code")],
        works={"c1": [CourseWork(id="w1", title="HW1", state="PUBLISHED", work_type="ASSIGNMENT")], "c2": [CourseWork(id="w2", title="Quiz", state="PUBLISHED", work_type="MULTIPLE_CHOICE_QUESTION")]},
        raw={"w1": [_raw("u1", "TURNED_IN"), _raw("u2", "TURNED_IN", "LATE"), _raw("u3", "RETURNED"), _raw("u4", "NEW")]},
    )
    res = dc.run_daily_check(svc)
    assert res.course_count == 2
    assert res.submission_count == 4
    assert res.pending_count == 2
    assert res.late_pending_count == 1
    assert res.returned_count == 1
    assert svc.write_attempts == []
    hw = res.courses[0].coursework[0]
    assert hw.submission_counts == {"NEW": 1, "RETURNED": 1, "TURNED_IN": 2}
    assert hw.pending_students == ["Student-u1", "Student-u2"]
    quiz = res.courses[1].coursework[0]
    assert quiz.reviewable is False and quiz.total_submissions == 0


def test_run_daily_check_never_pushes_with_writable_service():
    svc = FakeService(courses=[Course(id="c", name="C")], works={"c": [CourseWork(id="w", title="T", state="PUBLISHED", work_type="ASSIGNMENT")]}, raw={"w": [_raw("u", "TURNED_IN")]})
    res = dc.run_daily_check(svc)
    assert res.pending_count == 1 and svc.write_attempts == []


def test_course_failure_does_not_abort_run():
    class Boom(FakeService):
        def list_course_work(self, course_id):
            if course_id == "bad":
                from src.classroom_service import ClassroomServiceError
                raise ClassroomServiceError("boom")
            return super().list_course_work(course_id)
    svc = Boom(courses=[Course(id="bad", name="Bad"), Course(id="ok", name="Ok")], works={"ok": [CourseWork(id="w", title="T", state="PUBLISHED", work_type="ASSIGNMENT")]}, raw={"w": [_raw("u", "TURNED_IN")]})
    res = dc.run_daily_check(svc)
    assert res.pending_count == 1 and len(res.errors) == 1 and "Bad" in res.errors[0]


@pytest.mark.parametrize("now,hour,minute,expected_h", [("2026-10-11 05:00", 6, 0, 1.0), ("2026-10-11 07:00", 6, 0, 23.0), ("2026-10-11 06:00", 6, 0, 24.0)])
def test_seconds_until_next_run(now, hour, minute, expected_h):
    assert dc.seconds_until_next_run(datetime.fromisoformat(now), hour, minute) == pytest.approx(expected_h * 3600)


def test_parse_time_of_day():
    assert dc.parse_time_of_day("06:00") == (6, 0)
    with pytest.raises(ValueError):
        dc.parse_time_of_day("25:00")


def test_write_report_creates_json_and_summary(tmp_path):
    svc = FakeService(courses=[Course(id="c", name="C")], works={"c": [CourseWork(id="w", title="T", state="PUBLISHED", work_type="ASSIGNMENT")]}, raw={"w": [_raw("u", "TURNED_IN")]})
    res = dc.run_daily_check(svc)
    jp, sp = dc.write_report(res, tmp_path / "run")
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert data["pending_count"] == 1 and data["dry_run"] is True and "read-only" in data["mode"]
    assert "READ-ONLY" in sp.read_text(encoding="utf-8")


def test_credentials_project_id_matches(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"installed": {"project_id": "classroom-autograder-509507"}}), encoding="utf-8")
    assert dc.read_credentials_project_id(p) == "classroom-autograder-509507"


# --------------------------------------------------------------------------- #
# --skip-if-today-done: idempotent catch-up for boot/StartWhenAvailable runs
# --------------------------------------------------------------------------- #
def _fake_service():
    return FakeService(
        courses=[Course(id="c", name="C")],
        works={"c": [CourseWork(id="w", title="T", state="PUBLISHED", work_type="ASSIGNMENT")]},
        raw={"w": [_raw("u", "TURNED_IN")]},
    )


def test_skip_if_today_done_skips_when_report_exists(tmp_path, monkeypatch):
    from datetime import datetime as _dt

    day = _dt.now().strftime("%Y-%m-%d")
    run_dir = tmp_path / f"{day}_06-00-00"
    run_dir.mkdir()
    (run_dir / "daily_check.json").write_text("{}", encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("must not build a service or scan when skipping")

    monkeypatch.setattr(dc, "build_read_only_service", _boom)
    result, directory = dc.run_once(output_dir=tmp_path, skip_if_today_done=True)
    assert result is None and directory is None


def test_skip_if_today_done_runs_when_today_missing(tmp_path, monkeypatch):
    # Only *yesterday's* report exists -> the catch-up must still scan.
    from datetime import datetime as _dt, timedelta

    yesterday = (_dt.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    old_dir = tmp_path / f"{yesterday}_06-00-00"
    old_dir.mkdir()
    (old_dir / "daily_check.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(dc, "build_read_only_service", lambda: _fake_service())
    result, directory = dc.run_once(output_dir=tmp_path, audit_reports=False, skip_if_today_done=True)
    assert result is not None and result.pending_count == 1
    assert (directory / "daily_check.json").is_file()


def test_cli_skip_if_today_done_exits_zero_without_scanning(tmp_path, monkeypatch):
    from datetime import datetime as _dt

    day = _dt.now().strftime("%Y-%m-%d")
    run_dir = tmp_path / f"{day}_06-00-00"
    run_dir.mkdir()
    (run_dir / "daily_check.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(dc, "build_read_only_service", lambda: pytest.fail("must not scan"))
    rc = dc.main(["--once", "--skip-if-today-done", "--output-dir", str(tmp_path)])
    assert rc == 0  # a skip is a success - Task Scheduler must see 0


# --------------------------------------------------------------------------- #
# --grade: pending work is graded locally into releasable EvaluationReports
# --------------------------------------------------------------------------- #
def _grade_service():
    """One course + one published assignment with a single pending submission."""
    from datetime import datetime, timezone

    submitted = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)
    sub = StudentSubmission(
        student_id="u1",
        student_name="Noa",
        submission_id="sub-u1",
        state="TURNED_IN",
        doc_id="doc-1",
        submitted_at=submitted,
    )

    class GradeFake(FakeService):
        def get_pending_submissions(self, course_id, course_work_id):
            return [sub]

        def get_submissions(self, course_id, course_work_id):
            return [sub]

        def extract_doc_text(self, doc_id):
            return "print(1)"

    return GradeFake(
        courses=[Course(id="c1", name="Course A")],
        works={
            "c1": [
                CourseWork(
                    id="w1",
                    title="lesson_1",
                    state="PUBLISHED",
                    work_type="ASSIGNMENT",
                )
            ]
        },
        raw={"w1": [_raw("u1", "TURNED_IN")]},
    )


def _grade_evaluate(student_name, doc_text, config, late_penalty_points=0):
    from src.models import GradingResult, TaskEvaluation

    return GradingResult(
        student_name=student_name,
        score=88,
        feedback_hebrew="Good.",
        task_evaluations=[
            TaskEvaluation(
                task_name="Task 1",
                student_answer_found="print(1)",
                status="CORRECT",
                notes="ok",
            )
        ],
    )


def test_grade_mode_saves_releasable_report(tmp_path, monkeypatch):
    """The --grade contract: reports validate and show up for release."""
    from src.models import AssignmentConfig, EvaluationReport, TaskDefinition

    cfg = AssignmentConfig(
        assignment_id="lesson_1",
        title="Lesson 1",
        tasks=[TaskDefinition(id=1, name="Task 1", description="Print hello")],
    )
    monkeypatch.setattr(dc, "build_config_resolver", lambda: (lambda a: (None, cfg)))
    monkeypatch.setattr(
        dc, "resolve_mapped_config", lambda work_id: None, raising=False
    )

    service = _grade_service()
    summary = dc.grade_pending_submissions(
        service,
        output_dir=tmp_path,
        evaluate=_grade_evaluate,
    )
    assert summary["graded_count"] == 1
    assert summary["errors"] == []
    (report_file,) = summary["graded_reports"]

    # 1. The saved file is a genuine EvaluationReport - the Release loader
    #    validates it with zero errors (this was the original crash).
    report = EvaluationReport.model_validate_json(
        Path(report_file).read_text(encoding="utf-8")
    )
    assert report.source == "daily_check"
    assert len(report.entries) == 1

    # 2. The release dropdown lists it via its report-audit + label helpers.
    from src import report_label
    from src.report_audit import find_report_files

    found = [str(p) for p in find_report_files(tmp_path)]
    assert report_file in found
    label = report_label.label_from_json(Path(report_file))
    assert label is not None and label.startswith("[Daily Check] ")

    # 3. And nothing was ever published to Classroom.
    assert service.write_attempts == []


def test_grade_mode_passes_a_pacing_pause_between_students(monkeypatch):
    """Groq meters output tokens per minute: the background batch must pace.

    The on-screen dry-run pauses between students; the nightly --grade run
    goes through the same pipeline and must not be the one caller firing
    back-to-back completions at a 1000-OTPM bucket (HTTP 429 ERROR rows).
    """
    captured: dict = {}

    def fake_run(service, groups, resolver, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(dc, "collect_pending_submissions", lambda *a, **k: [object()])
    monkeypatch.setattr(dc, "build_config_resolver", lambda: (lambda a: None))
    monkeypatch.setattr(dc, "run_dry_run_evaluation", fake_run)

    summary = dc.grade_pending_submissions(object())

    assert summary["errors"] == []
    assert callable(captured.get("pause"))



