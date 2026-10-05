"""Unit tests for the pending-submission dashboard and the DRY_RUN pipeline.

Run from the project root::

    python -m pytest tests/test_dashboard_dry_run.py -q

Pure unit tests: no network, no credentials, no API key. They pin the two
promises the teacher actually relies on:

* the dashboard reports exactly the submissions that are ``TURNED_IN`` but not
  yet ``RETURNED``, grouped per assignment;
* the automated dry-run **only ever reads**. Every mutating method on
  ``ClassroomService`` is blocked before it can be called, the full pipeline
  is run against a fake that records any write attempt, and the local report +
  CSV that come out of it are shown on screen rather than published.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.dashboard as dashboard  # noqa: E402
from src.classroom_service import ClassroomServiceError  # noqa: E402
from src.dashboard import (  # noqa: E402
    CLASSROOM_WRITE_METHODS,
    DRY_RUN,
    DryRunWriteViolation,
    PendingGroup,
    ReadOnlyClassroomService,
    collect_pending_submissions,
    dry_run_rows,
    is_pending_submission,
    pending_rows,
    run_dry_run_evaluation,
    summarize_pending,
)
from src.models import (  # noqa: E402
    AssignmentConfig,
    Course,
    CourseWork,
    GradingResult,
    StudentSubmission,
    TaskDefinition,
    TaskEvaluation,
)

ROOT = Path(__file__).resolve().parent.parent
LESSON_1 = ROOT / "assignments" / "lesson_1.json"

SUBMITTED_AT = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def make_submission(
    student_id: str = "u1",
    name: str = "Noa",
    state: str = "TURNED_IN",
    doc_id: Optional[str] = "doc-1",
    **kwargs: Any,
) -> StudentSubmission:
    return StudentSubmission(
        student_id=student_id,
        student_name=name,
        submission_id=f"sub-{student_id}",
        state=state,
        doc_id=doc_id,
        submitted_at=kwargs.pop("submitted_at", SUBMITTED_AT),
        **kwargs,
    )


def make_course(course_id: str = "c1", name: str = "פייתון א") -> Course:
    return Course(id=course_id, name=name)


def make_work(work_id: str = "w1", title: str = "lesson_1") -> CourseWork:
    return CourseWork(id=work_id, title=title)


def make_config() -> AssignmentConfig:
    return AssignmentConfig(
        assignment_id="lesson_1",
        title="Lesson 1",
        tasks=[TaskDefinition(id=1, name="Task 1", description="Print hello")],
    )


def make_result(name: str, score: int = 88) -> GradingResult:
    return GradingResult(
        student_name=name,
        score=score,
        feedback_hebrew="עבודה טובה, שפרו את השם של המשתנה.",
        task_evaluations=[
            TaskEvaluation(
                task_name="Task 1",
                student_answer_found="print(1)",
                status="CORRECT",
                notes="ok",
            )
        ],
    )


class FakeClassroomService:
    """In-memory stand-in that *records* any write attempt.

    Read methods answer from the fixture data. Write methods only append to
    ``write_calls`` and return normally, so a dry-run that somehow reached one
    of them fails a ``write_calls == []`` assertion rather than blowing up with
    an exception that would mask which test broke.
    """

    def __init__(
        self,
        courses: tuple[Course, ...] = (),
        works: Optional[dict[str, list[CourseWork]]] = None,
        submissions: Optional[dict[str, list[StudentSubmission]]] = None,
        docs: Optional[dict[str, str]] = None,
        failing_docs: Optional[set[str]] = None,
    ) -> None:
        self.courses = list(courses)
        self.works = works or {}
        self.submissions = submissions or {}
        self.docs = docs or {}
        self.failing_docs = failing_docs or set()
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []

    # -- reads -------------------------------------------------------------

    def list_courses(self) -> list[Course]:
        self.read_calls.append("list_courses")
        return list(self.courses)

    def list_course_work(self, course_id: str) -> list[CourseWork]:
        self.read_calls.append(f"list_course_work:{course_id}")
        return list(self.works.get(course_id, []))

    def get_submissions(
        self, course_id: str, course_work_id: str
    ) -> list[StudentSubmission]:
        self.read_calls.append(f"get_submissions:{course_work_id}")
        return list(self.submissions.get(course_work_id, []))

    def extract_doc_text(self, doc_id: str) -> str:
        self.read_calls.append(f"extract_doc_text:{doc_id}")
        if doc_id in self.failing_docs:
            raise ClassroomServiceError(
                f"Could not read document '{doc_id}': HTTP 403 forbidden"
            )
        return self.docs.get(doc_id, "print(1)")

    # -- writes (recorded, never raised) -----------------------------------

    def _record(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.write_calls.append(name)
        return {}

    def create_course_work_draft(self, *args: Any, **kwargs: Any):
        return self._record("create_course_work_draft")

    def update_submission_grade(self, *args: Any, **kwargs: Any):
        return self._record("update_submission_grade")

    def return_student_submission(self, *args: Any, **kwargs: Any):
        return self._record("return_student_submission")

    def publish_feedback_comment(self, *args: Any, **kwargs: Any):
        return self._record("publish_feedback_comment")

    def _create_comment(self, *args: Any, **kwargs: Any):
        return self._record("_create_comment")

    def _create_anchored_comment(self, *args: Any, **kwargs: Any):
        return self._record("_create_anchored_comment")

    def _delete_comment_quietly(self, *args: Any, **kwargs: Any):
        return self._record("_delete_comment_quietly")


def two_pending_service() -> FakeClassroomService:
    """One course, two assignments: one pending, one already returned."""
    course = make_course()
    waiting = make_work("w1", title="lesson_1")
    finished = make_work("w2", title="lesson_2")
    return FakeClassroomService(
        courses=(course,),
        works={"c1": [waiting, finished]},
        submissions={
            "w1": [
                make_submission("u1", "Noa", state="TURNED_IN"),
                make_submission("u2", "Dana", state="RETURNED"),
                make_submission("u3", "Yael", state="TURNED_IN"),
            ],
            "w2": [make_submission("u4", "Ori", state="RETURNED")],
        },
        docs={"doc-1": "print(1)"},
    )


# --------------------------------------------------------------------------- #
# 1. What counts as "waiting for review"
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "state,expected",
    [
        ("TURNED_IN", True),
        ("turned_in", True),  # the API is case-sensitive, we are not
        ("TURNED_IN ", True),
        ("RETURNED", False),  # graded and handed back
        ("NEW", False),  # never submitted
        ("CREATED", False),
        ("RECLAIMED_BY_STUDENT", False),  # took the work back
        ("", False),
    ],
)
def test_only_turned_in_counts_as_pending(state: str, expected: bool) -> None:
    assert is_pending_submission(make_submission(state=state)) is expected


def test_pending_submissions_are_grouped_by_assignment() -> None:
    """Only the assignment still waiting shows up, with only its own students."""
    groups = collect_pending_submissions(two_pending_service())

    assert [group.assignment.id for group in groups] == ["w1"]
    group = groups[0]
    assert group.count == 2
    assert [row["Student"] for row in pending_rows(group)] == ["Noa", "Yael"]
    # The already-returned student must not appear anywhere.
    assert "Dana" not in [row["Student"] for row in pending_rows(group)]


def test_assignments_with_nothing_pending_are_omitted() -> None:
    """An empty section per assignment would bury the ones needing attention."""
    service = two_pending_service()
    groups = collect_pending_submissions(service)
    assert len(groups) == 1
    assert groups[0].assignment.title != "lesson_2"


def test_course_scope_narrows_the_scan() -> None:
    """Selecting one class must not scan (or bill for) every other one."""
    first = FakeClassroomService(
        courses=(make_course("c1", "A"), make_course("c2", "B")),
        works={
            "c1": [make_work("w1")],
            "c2": [make_work("w2")],
        },
        submissions={
            "w1": [make_submission("u1", "Noa")],
            "w2": [make_submission("u2", "Dana")],
        },
    )

    groups = collect_pending_submissions(first, course_ids=("c2",))

    assert [group.course.id for group in groups] == ["c2"]
    assert [row["Student"] for row in pending_rows(groups[0])] == ["Dana"]
    assert "list_course_work:w1" not in first.read_calls


def test_summary_counts_distinct_courses_assignments_and_students() -> None:
    groups = [
        PendingGroup(
            course=make_course("c1"),
            assignment=make_work("w1"),
            submissions=(
                make_submission("u1", "Noa", is_late=True),
                make_submission("u2", "Dana", late_state="LATE"),
                make_submission("u3", "Yael"),
            ),
        ),
        PendingGroup(
            course=make_course("c1"),
            assignment=make_work("w2"),
            submissions=(make_submission("u4", "Ori"),),
        ),
        PendingGroup(
            course=make_course("c2"),
            assignment=make_work("w3"),
            submissions=(make_submission("u5", "Ziv"),),
        ),
    ]

    summary = summarize_pending(groups)

    assert summary.course_count == 2
    assert summary.assignment_count == 3
    assert summary.submission_count == 5
    assert summary.late_count == 2


def test_pending_rows_expose_the_submission_and_lateness_verdict() -> None:
    group = PendingGroup(
        course=make_course(),
        assignment=make_work(),
        submissions=(
            make_submission("u1", "Noa", late_state="ON_TIME"),
            make_submission("u2", "Dana", is_late=True),
            make_submission("u3", "Yael", doc_id=None),
        ),
    )

    rows = pending_rows(group)

    assert rows[0]["Late"] == "On time"
    assert rows[1]["Late"] == "⚠️ Late"
    assert rows[0]["Submitted"] == "2026-10-01 08:30"
    assert rows[2]["Attachment"] == "No extractable doc"
    # The doc id travels with the row so the UI can build a link.
    assert rows[0]["doc_id"] == "doc-1"


def test_collect_never_reaches_a_write_method() -> None:
    """Even the *scan* goes through the read-only facade."""
    service = two_pending_service()
    collect_pending_submissions(service)
    assert service.write_calls == []


# --------------------------------------------------------------------------- #
# 2. The safety rule: dry-run can never write to Google Classroom
# --------------------------------------------------------------------------- #


def test_the_dry_run_pipeline_is_flagged_as_dry_run() -> None:
    """The module-level contract every caller reads before doing anything."""
    assert DRY_RUN is True
    assert dashboard.DRY_RUN is True


def test_every_classroom_write_method_is_blocked() -> None:
    """Grade, comment, return and coursework creation all refuse to run.

    Driven off :data:`CLASSROOM_WRITE_METHODS` so the list itself is what gets
    pinned: forgetting to block a new write method fails here.
    """
    fake = FakeClassroomService()
    reader = ReadOnlyClassroomService(fake)

    assert CLASSROOM_WRITE_METHODS, "the blocklist must not be empty"
    for name in sorted(CLASSROOM_WRITE_METHODS):
        # The fake really does implement it, so this is not a false pass.
        assert hasattr(fake, name), f"fake is missing {name}"
        with pytest.raises(DryRunWriteViolation) as excinfo:
            getattr(reader, name)()
        assert name in str(excinfo.value)
        # And nothing ever reached the underlying service.
        assert fake.write_calls == []


def test_read_methods_still_work_through_the_facade() -> None:
    """Blocking writes must not break the reads the dashboard depends on."""
    service = two_pending_service()
    reader = ReadOnlyClassroomService(service)

    assert [course.id for course in reader.list_courses()] == ["c1"]
    assert [work.id for work in reader.list_course_work("c1")] == ["w1", "w2"]
    assert reader.extract_doc_text("doc-1") == "print(1)"
    assert service.read_calls, "the reads must reach the real service"


def test_wrapping_an_already_wrapped_service_is_harmless() -> None:
    service = two_pending_service()
    once = ReadOnlyClassroomService(service)
    twice = ReadOnlyClassroomService(once)

    assert twice._service is service
    assert repr(twice).startswith("ReadOnlyClassroomService(")


def test_the_blocklist_covers_the_real_service() -> None:
    """A write method added to ``ClassroomService`` must be listed here too.

    The release workflow knows every one of them by name; anything missing
    from :data:`CLASSROOM_WRITE_METHODS` would be reachable from a dry-run.
    """
    from src.classroom_service import ClassroomService

    known = {
        "create_course_work_draft",
        "update_submission_grade",
        "return_student_submission",
        "publish_feedback_comment",
    }
    assert known <= CLASSROOM_WRITE_METHODS
    for name in CLASSROOM_WRITE_METHODS:
        assert callable(getattr(ClassroomService, name, None)), name


def test_missing_private_attribute_does_not_recurse() -> None:
    """``__getattr__`` must not spin forever on the wrapped service itself.

    Bypassing ``__init__`` (as unpickling can) leaves ``_service`` unset, and
    ``__getattr__`` is then entered for that very name.
    """
    orphan = ReadOnlyClassroomService.__new__(ReadOnlyClassroomService)
    with pytest.raises(AttributeError):
        orphan._service


# --------------------------------------------------------------------------- #
# 3. The automated dry-run pipeline
# --------------------------------------------------------------------------- #

from src.fetch_submissions import UNSUPPORTED_ATTACHMENT_MSG  # noqa: E402


def _resolver(config_path: Optional[Path] = LESSON_1):
    """Stand-in for the app's remembered-mapping/fuzzy-title resolution."""

    def _resolve(assignment: CourseWork):
        if config_path is None:
            return None
        return config_path, make_config()

    return _resolve


def fake_evaluate(
    student_name: str,
    doc_text: str,
    assignment: AssignmentConfig,
    late_penalty_points: int = 0,
) -> GradingResult:
    """Stand-in for the Groq call: never touches the network."""
    return make_result(student_name, score=88)


def test_dry_run_only_grades_pending_work_and_never_writes(
    tmp_path, monkeypatch
) -> None:
    """End-to-end: scan -> grade, with every write attempt recorded."""
    monkeypatch.setattr(dashboard, "evaluate_student_submission", fake_evaluate)
    service = two_pending_service()
    groups = collect_pending_submissions(service)

    graded: list[str] = []

    def spy(name, doc_text, assignment, late_penalty_points=0):
        graded.append(name)
        return make_result(name)

    results = run_dry_run_evaluation(
        service, groups, _resolver(), directory=tmp_path, evaluate=spy
    )

    # Only the two TURNED_IN students - the returned one is never graded.
    assert graded == ["Noa", "Yael"]
    assert len(results) == 1
    # ...and nothing at all was pushed to Classroom.
    assert service.write_calls == []


def test_dry_run_writes_the_report_and_csv_locally(tmp_path, monkeypatch) -> None:
    """Reports are generated on disk; ``exports/``-style, never uploaded."""
    monkeypatch.setattr(dashboard, "evaluate_student_submission", fake_evaluate)
    service = two_pending_service()
    groups = collect_pending_submissions(service)

    results = run_dry_run_evaluation(
        service, groups, _resolver(), directory=tmp_path
    )

    report = results[0].report_path
    csv_path = results[0].csv_path
    assert report is not None and report.is_file()
    assert csv_path is not None and csv_path.is_file()
    # Both stayed inside the directory the caller asked for.
    assert report.parent == tmp_path
    assert csv_path.parent == tmp_path

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["course_work_title"] == "lesson_1"
    assert [entry["result"]["score"] for entry in payload["entries"]] == [88, 88]
    assert payload["csv_file"] == "lesson_1.csv"

    # UTF-8-SIG keeps the Hebrew header readable in Excel/Mashov.
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "שם התלמיד" in csv_text
    assert "Noa" in csv_text and "Yael" in csv_text
    assert service.write_calls == []


def test_dry_run_surfaces_predicted_scores_and_feedback(tmp_path) -> None:
    """What the teacher reviews on screen: score, statuses, Hebrew feedback."""
    service = two_pending_service()
    groups = collect_pending_submissions(service)

    results = run_dry_run_evaluation(
        service, groups, _resolver(), directory=tmp_path, evaluate=fake_evaluate
    )

    rows = dry_run_rows(results[0])
    assert [row["Score"] for row in rows] == [88, 88]
    assert rows[0]["Tasks"] == "CORRECT: 1"
    assert rows[0]["Feedback (Hebrew)"].startswith("עבודה טובה")
    assert rows[0]["Error"] == ""
    assert results[0].evaluated == 2
    assert results[0].failed == 0


def test_an_assignment_without_a_rubric_is_skipped_with_a_reason(tmp_path) -> None:
    """Never guess a rubric: skip loudly instead of grading against nothing."""
    service = two_pending_service()
    groups = collect_pending_submissions(service)

    results = run_dry_run_evaluation(
        service,
        groups,
        _resolver(None),
        directory=tmp_path,
        evaluate=fake_evaluate,
    )

    assert len(results) == 1
    assert results[0].skipped_reason
    assert "rubric" in results[0].skipped_reason
    assert results[0].rows == []
    assert results[0].report_path is None
    assert results[0].csv_path is None
    assert service.write_calls == []


def test_a_submission_without_a_doc_is_recorded_not_graded(tmp_path) -> None:
    """Unsupported attachments become an error row, not a crash or a guess."""
    service = FakeClassroomService(
        courses=(make_course(),),
        works={"c1": [make_work()]},
        submissions={"w1": [make_submission("u1", "Noa", doc_id=None)]},
    )
    groups = collect_pending_submissions(service)
    graded: list[str] = []

    def spy(name, doc_text, assignment, late_penalty_points=0):
        graded.append(name)
        return make_result(name)

    results = run_dry_run_evaluation(
        service, groups, _resolver(), directory=tmp_path, evaluate=spy
    )

    assert graded == [], "a document-less submission cannot be graded"
    assert results[0].rows[0].error == UNSUPPORTED_ATTACHMENT_MSG
    assert results[0].failed == 1
    # No student graded -> no empty report JSON, but the CSV still records why.
    assert results[0].report_path is None
    assert results[0].csv_path is not None
    assert service.write_calls == []


def test_a_document_that_cannot_be_read_is_reported_per_student(tmp_path) -> None:
    """One 403 must not abort the batch - the other students still get graded."""
    service = FakeClassroomService(
        courses=(make_course(),),
        works={"c1": [make_work()]},
        submissions={
            "w1": [
                make_submission("u1", "Noa", doc_id="bad-doc"),
                make_submission("u2", "Dana", doc_id="good-doc"),
            ]
        },
        docs={"good-doc": "print(1)"},
        failing_docs={"bad-doc"},
    )
    groups = collect_pending_submissions(service)

    results = run_dry_run_evaluation(
        service, groups, _resolver(), directory=tmp_path, evaluate=fake_evaluate
    )

    outcome = results[0]
    assert outcome.rows[0].error.startswith("Document extraction failed")
    assert outcome.rows[1].result is not None
    assert outcome.evaluated == 1
    assert outcome.failed == 1
    assert service.write_calls == []


def test_progress_reaches_one_hundred_percent(tmp_path) -> None:
    service = two_pending_service()
    groups = collect_pending_submissions(service)
    seen: list[float] = []

    run_dry_run_evaluation(
        service,
        groups,
        _resolver(),
        directory=tmp_path,
        evaluate=fake_evaluate,
        on_progress=seen.append,
    )

    assert seen
    assert seen[-1] == 1.0
    assert all(0.0 <= value <= 1.0 for value in seen)


def test_pause_runs_between_students_but_never_after_the_last(tmp_path) -> None:
    """Groq meters output tokens per minute; the last student must not wait."""
    service = two_pending_service()
    groups = collect_pending_submissions(service)
    pending_total = sum(group.count for group in groups)

    pauses: list[int] = []
    run_dry_run_evaluation(
        service,
        groups,
        _resolver(),
        directory=tmp_path,
        evaluate=fake_evaluate,
        pause=lambda: pauses.append(1),
    )

    assert pending_total == 2
    assert len(pauses) == pending_total - 1


def test_the_pipeline_refuses_to_run_when_dry_run_is_off(
    tmp_path, monkeypatch
) -> None:
    """The guard that keeps a future refactor from becoming a publish path."""
    monkeypatch.setattr(dashboard, "DRY_RUN", False)
    service = two_pending_service()
    groups = collect_pending_submissions(service)

    with pytest.raises(RuntimeError, match="DRY_RUN=False"):
        run_dry_run_evaluation(
            service,
            groups,
            _resolver(),
            directory=tmp_path,
            evaluate=fake_evaluate,
        )
    assert service.write_calls == []


# --------------------------------------------------------------------------- #
# 4. The Streamlit wiring: the button really runs the read-only pipeline
# --------------------------------------------------------------------------- #


class _Widget:
    """Container-ish widget: accepts anything, usable as a context manager."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Column(_Widget):
    def __init__(self, stub: "DashboardStub") -> None:
        self._stub = stub

    def metric(self, label, value=None, **kwargs):
        self._stub.metrics.append((label, value))


class DashboardStub:
    """Just enough of ``st`` to render the Dashboard headlessly."""

    def __init__(
        self,
        buttons: tuple[str, ...] = (),
        select_value: int = 0,
        checkbox_value: bool = False,
    ) -> None:
        self.buttons = set(buttons)
        self.select_value = select_value
        self.checkbox_value = checkbox_value
        self.messages: list[str] = []
        self.metrics: list[tuple] = []
        self.dataframes: list[Any] = []
        self.session_state: dict[str, Any] = {}

    def columns(self, spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        return [_Column(self) for _ in range(count)]

    def selectbox(self, label, options, **kwargs):
        return self.select_value

    def checkbox(self, label, **kwargs):
        return self.checkbox_value

    def button(self, label, **kwargs):
        return label in self.buttons

    def dataframe(self, data, **kwargs):
        self.dataframes.append(data)

    def expander(self, *args, **kwargs):
        return _Widget()

    def progress(self, *args, **kwargs):
        return _Widget()

    def empty(self, *args, **kwargs):
        return _Widget()

    def __getattr__(self, name):
        def _emit(*args, **kwargs):
            self.messages.append(f"{name}:{args[0] if args else ''}")

        return _emit


@pytest.fixture
def dashboard_app(monkeypatch):
    """Import ``app`` with Streamlit replaced by a recording stub."""
    import app as app_module

    stub = DashboardStub()
    monkeypatch.setattr(app_module, "st", stub)
    return app_module, stub


def _two_pending_groups():
    return collect_pending_submissions(two_pending_service())


def test_dashboard_renders_the_pending_summary(dashboard_app, monkeypatch) -> None:
    """The home view shows what is waiting, grouped and counted."""
    app_module, stub = dashboard_app
    groups = _two_pending_groups()
    monkeypatch.setattr(
        app_module, "_load_pending_dashboard", lambda course_ids: tuple(groups)
    )

    app_module.render_dashboard_tab(two_pending_service())

    metrics = dict(stub.metrics)
    assert metrics["Assignments awaiting review"] == 1
    assert metrics["Submissions turned in"] == 2
    assert metrics["Flagged late"] == 0
    # The safety banner is on screen before anything else.
    assert any("DRY RUN" in message for message in stub.messages)
    # The pending table for the assignment was rendered.
    assert stub.dataframes
    assert list(stub.dataframes[0]["Student"]) == ["Noa", "Yael"]


def test_dashboard_button_runs_the_dry_run_and_writes_nothing(
    dashboard_app, monkeypatch, tmp_path
) -> None:
    """Pressing the button grades locally; the fake records zero writes."""
    app_module, stub = dashboard_app
    monkeypatch.setattr(stub, "buttons", {"🧪 Run Automated Dry-Run"})
    groups = _two_pending_groups()
    monkeypatch.setattr(
        app_module, "_load_pending_dashboard", lambda course_ids: tuple(groups)
    )
    # Keep the scan/report inside the test: no real mapping file, no real
    # exports/ folder, no Groq call, no sleeping between students.
    monkeypatch.setattr(app_module, "resolve_mapped_config", lambda work_id: None)
    monkeypatch.setattr(app_module, "EVAL_PAUSE_SECONDS", 0)
    monkeypatch.setattr(dashboard, "evaluate_student_submission", fake_evaluate)

    original = app_module.run_dry_run_evaluation

    def _into_tmp(*args, **kwargs):
        kwargs["directory"] = tmp_path
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "run_dry_run_evaluation", _into_tmp)

    service = two_pending_service()
    app_module.render_dashboard_tab(service)

    # The critical assertion: not one mutating call reached the service.
    assert service.write_calls == []
    # The report really was produced, locally.
    assert list(tmp_path.glob("*.json")), "a report JSON must be written"
    assert list(tmp_path.glob("*.csv")), "a Mashov CSV must be written"
    # ...and the UI says so.
    assert any("Nothing was pushed" in message for message in stub.messages)


def test_the_dashboard_dry_run_never_fires_on_its_own(
    dashboard_app, monkeypatch
) -> None:
    """Without the button (and with the auto option off) nothing is spent."""
    app_module, stub = dashboard_app
    groups = _two_pending_groups()
    monkeypatch.setattr(
        app_module, "_load_pending_dashboard", lambda course_ids: tuple(groups)
    )
    called: list[tuple] = []
    monkeypatch.setattr(
        app_module,
        "run_dry_run_evaluation",
        lambda *args, **kwargs: called.append(args) or [],
    )

    app_module.render_dashboard_tab(two_pending_service())

    assert called == [], "a dry-run must be a deliberate action by default"


def test_the_auto_option_wires_up_the_dry_run(dashboard_app, monkeypatch) -> None:
    """``DASHBOARD_AUTO_DRY_RUN=true`` opts in without pressing the button."""
    app_module, stub = dashboard_app
    monkeypatch.setattr(stub, "checkbox_value", True)
    groups = _two_pending_groups()
    monkeypatch.setattr(
        app_module, "_load_pending_dashboard", lambda course_ids: tuple(groups)
    )
    called: list[tuple] = []
    monkeypatch.setattr(
        app_module,
        "run_dry_run_evaluation",
        lambda *args, **kwargs: called.append(args) or [],
    )

    app_module.render_dashboard_tab(two_pending_service())

    assert len(called) == 1
    # Re-running with the same scan must not spend the tokens a second time.
    called.clear()
    app_module.render_dashboard_tab(two_pending_service())
    assert called == []


def test_run_dry_run_action_reports_an_empty_queue(dashboard_app) -> None:
    """No pending work -> a friendly message, not an error or a scan."""
    app_module, stub = dashboard_app

    results = app_module._run_dry_run_action(two_pending_service(), [], [LESSON_1])

    assert results == []
    assert any(
        "no pending submissions" in message for message in stub.messages
    )


def test_run_dry_run_action_needs_a_rubric_before_grading(dashboard_app) -> None:
    """Without rubric JSON the dry-run refuses instead of guessing."""
    app_module, stub = dashboard_app

    results = app_module._run_dry_run_action(
        two_pending_service(), _two_pending_groups(), []
    )

    assert results == []
    assert any(message.startswith("error:") for message in stub.messages)







