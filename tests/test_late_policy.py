"""Unit tests for the late-submission policy.

Run from the project root::

    python -m pytest tests/test_late_policy.py -q

Pure unit tests: no network, no credentials, no API key. They pin the rule that
a submission handed in after the deadline is charged a fixed penalty, that the
charge is visible in the score, the deduction breakdown *and* the Hebrew
feedback, and - just as important - that work which is **not** late is never
touched.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.classroom_service as classroom_service  # noqa: E402
import src.llm_evaluator as evaluator  # noqa: E402
from src.late_policy import (  # noqa: E402
    DEFAULT_LATE_PENALTY_POINTS,
    LATE_PENALTY_TASK_NAME,
    apply_late_penalty,
    is_late,
    late_penalty_sentence,
    penalty_for,
)
from src.models import (  # noqa: E402
    AssignmentConfig,
    CourseWork,
    DeductionItem,
    GradingResult,
    StudentSubmission,
    TaskDefinition,
    TaskEvaluation,
)

DUE = datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc)


def coursework(due: datetime | None = DUE) -> CourseWork:
    return CourseWork(
        id="cw1",
        title="Worksheet",
        due_date=due.date() if due else None,
        due_datetime=due,
    )


def submission(
    *,
    late_state: str | None = None,
    is_late: bool | None = None,
    submitted_at: datetime | None = None,
    name: str = "Noa",
) -> StudentSubmission:
    return StudentSubmission(
        student_id="u1",
        student_name=name,
        submission_id="s1",
        state="TURNED_IN",
        doc_id="doc-1",
        late_state=late_state,
        is_late=is_late,
        submitted_at=submitted_at,
    )


def result(score: int = 95, feedback: str = "מעולה!") -> GradingResult:
    return GradingResult(
        student_name="Noa",
        score=score,
        feedback_hebrew=feedback,
        deduction_breakdown=[
            DeductionItem(
                task_name="Task 1",
                points_deducted=100 - score,
                reason="minor gap",
            )
        ],
        task_evaluations=[
            TaskEvaluation(
                task_name="Task 1",
                student_answer_found="print(1)",
                status="CORRECT",
                notes="ok",
            )
        ],
    )


# --------------------------------------------------------------------------- #
# 1. Which submissions count as late
# --------------------------------------------------------------------------- #


def test_classroom_late_state_marks_a_submission_late():
    assert is_late(submission(late_state="LATE"), coursework()) is True


@pytest.mark.parametrize("state", ["ON_TIME", "NEEDS_GRADING"])
def test_every_non_late_classroom_state_is_respected(state):
    assert is_late(submission(late_state=state), coursework()) is False


def test_the_boolean_late_flag_is_used_when_there_is_no_state():
    assert is_late(submission(is_late=True), coursework()) is True
    assert is_late(submission(is_late=False), coursework()) is False


def test_classroom_state_wins_over_the_boolean_flag():
    """Classroom's own verdict already accounts for grace periods."""
    assert is_late(submission(late_state="ON_TIME", is_late=True), coursework()) is False
    assert is_late(submission(late_state="LATE", is_late=False), coursework()) is True


def test_timestamps_decide_when_classroom_reports_nothing():
    late = submission(submitted_at=DUE + timedelta(minutes=1))
    on_time = submission(submitted_at=DUE - timedelta(hours=1))
    assert is_late(late, coursework()) is True
    assert is_late(on_time, coursework()) is False


def test_submitted_exactly_on_the_deadline_is_on_time():
    assert is_late(submission(submitted_at=DUE), coursework()) is False


def test_timestamp_fallback_is_logged(caplog):
    """The fallback is an approximation, so it must leave a trace."""
    with caplog.at_level("WARNING", logger="src.late_policy"):
        is_late(submission(submitted_at=DUE + timedelta(days=1)), coursework())
    assert "falling back to timestamps" in caplog.text


def test_no_signal_at_all_is_treated_as_on_time(caplog):
    """A missing signal must never invent a penalty on a student's grade."""
    with caplog.at_level("WARNING", logger="src.late_policy"):
        assert is_late(submission(), coursework()) is False
    assert "No lateness signal" in caplog.text


def test_no_due_date_and_no_signal_is_on_time():
    assert is_late(submission(), coursework(due=None)) is False


def test_submitted_at_without_a_due_date_cannot_be_judged():
    """A missing deadline is not evidence of lateness."""
    late = submission(submitted_at=DUE + timedelta(days=5))
    assert is_late(late, coursework(due=None)) is False


def test_work_without_the_coursework_object_is_safe():
    assert is_late(submission(late_state="LATE"), None) is True
    assert is_late(submission(), None) is False


def test_unknown_state_falls_through_instead_of_guessing():
    assert is_late(submission(late_state="SOMETHING_NEW"), coursework()) is False
    assert is_late(
        submission(late_state="SOMETHING_NEW", is_late=True), coursework()
    ) is True


# --------------------------------------------------------------------------- #
# 2. The penalty value
# --------------------------------------------------------------------------- #


def test_the_default_penalty_is_ten_points():
    assert DEFAULT_LATE_PENALTY_POINTS == 10


def test_penalty_is_only_charged_when_late():
    assert penalty_for(submission(late_state="LATE"), coursework()) == 10
    assert penalty_for(submission(late_state="ON_TIME"), coursework()) == 0


def test_penalty_is_configurable():
    late = submission(late_state="LATE")
    assert penalty_for(late, coursework(), penalty_points=25) == 25
    assert penalty_for(late, coursework(), penalty_points=0) == 0


def test_a_zero_penalty_switches_the_rule_off():
    assert penalty_for(submission(late_state="LATE"), coursework(), 0) == 0


def test_absurd_penalty_values_are_clamped():
    late = submission(late_state="LATE")
    assert penalty_for(late, coursework(), -5) == 0
    assert penalty_for(late, coursework(), 5000) == 100


# --------------------------------------------------------------------------- #
# 3. Applying the penalty
# --------------------------------------------------------------------------- #


def late_rows(graded: GradingResult) -> list[DeductionItem]:
    return [
        i for i in graded.deduction_breakdown if i.task_name == LATE_PENALTY_TASK_NAME
    ]


def test_the_score_is_reduced_by_the_penalty():
    assert apply_late_penalty(result(score=95), 10).score == 85


def test_the_penalty_is_visible_in_the_deduction_breakdown():
    rows = late_rows(apply_late_penalty(result(score=95), 10))
    assert len(rows) == 1
    assert rows[0].points_deducted == 10
    assert "deadline" in rows[0].reason


def test_the_breakdown_still_sums_to_the_remaining_score():
    """The teacher audits this: sum(points) must equal 100 - score."""
    graded = apply_late_penalty(result(score=95), 10)
    total = sum(i.points_deducted for i in graded.deduction_breakdown)
    assert total == 100 - graded.score


def test_the_feedback_names_the_late_penalty():
    graded = apply_late_penalty(result(score=95, feedback="מעולה!"), 10)
    assert late_penalty_sentence(10) in graded.feedback_hebrew
    assert "איחור" in graded.feedback_hebrew
    # The model's own feedback is preserved underneath.
    assert "מעולה!" in graded.feedback_hebrew
    assert graded.feedback_hebrew.startswith(late_penalty_sentence(10))


def test_a_zero_penalty_changes_nothing():
    graded = apply_late_penalty(result(score=95), 0)
    assert graded.score == 95
    assert graded.feedback_hebrew == "מעולה!"
    assert late_rows(graded) == []


def test_the_score_never_goes_below_zero():
    assert apply_late_penalty(result(score=5), 10).score == 0


def test_a_clamped_penalty_reports_the_points_actually_taken():
    """Only 5 points existed to take, so the breakdown must say 5, not 10."""
    graded = apply_late_penalty(result(score=5), 10)
    assert late_rows(graded)[0].points_deducted == 5
    assert sum(i.points_deducted for i in graded.deduction_breakdown) == 100


def test_a_zero_score_is_left_alone():
    graded = apply_late_penalty(result(score=0), 10)
    assert graded.score == 0
    assert late_rows(graded) == []


def test_applying_the_penalty_twice_does_not_double_charge():
    """A re-run must not stack two late rows or deduct 20 points."""
    once = apply_late_penalty(result(score=95), 10)
    twice = apply_late_penalty(once, 10)
    assert twice.score == 85
    assert len(late_rows(twice)) == 1
    assert twice.feedback_hebrew.count(late_penalty_sentence(10)) == 1
    assert sum(i.points_deducted for i in twice.deduction_breakdown) == 100 - twice.score


def test_feedback_with_no_text_still_gets_the_explanation():
    graded = apply_late_penalty(result(score=95, feedback="  "), 10)
    assert graded.feedback_hebrew == late_penalty_sentence(10)


# --------------------------------------------------------------------------- #
# 4. End-to-end through the grading entry point
# --------------------------------------------------------------------------- #


ASSIGNMENT = AssignmentConfig(
    assignment_id="lesson_1",
    title="Worksheet",
    tasks=[
        TaskDefinition(id=1, name="Task 1", description="print something"),
    ],
)


def _stub_completion(monkeypatch, score: int = 95) -> None:
    """Make ``evaluate_student_submission`` return a fixed grade, no network."""
    monkeypatch.setattr(
        evaluator, "_parse_result", lambda response: result(score=score)
    )
    monkeypatch.setattr(evaluator, "_get_client", lambda: object())
    monkeypatch.setattr(
        evaluator, "_call_completion", lambda client, system, prompt, fmt: object()
    )


def test_the_grading_entry_point_applies_the_penalty(monkeypatch):
    """The rule lives where every caller goes through, so it cannot be skipped."""
    _stub_completion(monkeypatch)
    graded = evaluator.evaluate_student_submission(
        "Noa", "print(1)", ASSIGNMENT, late_penalty_points=10
    )
    assert graded.score == 85
    assert late_penalty_sentence(10) in graded.feedback_hebrew
    assert len(late_rows(graded)) == 1


def test_the_default_penalty_is_zero_so_existing_callers_are_unchanged(monkeypatch):
    """A caller that passes nothing must not suddenly lose points."""
    _stub_completion(monkeypatch)
    graded = evaluator.evaluate_student_submission("Noa", "print(1)", ASSIGNMENT)
    assert graded.score == 95
    assert graded.feedback_hebrew == "מעולה!"
    assert late_rows(graded) == []


def test_penalty_for_feeds_the_entry_point_end_to_end():
    """The two halves the callers use compose into the documented behaviour."""
    late = submission(late_state="LATE")
    graded = apply_late_penalty(
        result(score=95), penalty_for(late, coursework(), 10)
    )
    assert graded.score == 85
    on_time = apply_late_penalty(
        result(score=95), penalty_for(submission(late_state="ON_TIME"), coursework(), 10)
    )
    assert on_time.score == 95


# --------------------------------------------------------------------------- #
# 6. The Hebrew the student actually reads
# --------------------------------------------------------------------------- #


def test_the_notice_reads_as_the_school_wants_it():
    """Pins the exact sentence. This text was wrong in production once.

    The shipped version misspelled the deadline ("מוען" for "מועד"), left the
    verb agreeing with a singular noun ("הופחה" against "נקודות"), and used
    the unclear "נקודת קריאה לאיחור". It reached a real student's feedback
    before it was caught, so the wording is asserted verbatim.
    """
    assert late_penalty_sentence(10) == (
        "העבודה הוגשה לאחר מועד ההגשה, ולכן הופחתו 10 נקודות בגין איחור."
    )


@pytest.mark.parametrize("points", [2, 5, 10, 15, 25])
def test_the_number_rendered_is_the_number_charged(points):
    assert f"הופחתו {points} נקודות" in late_penalty_sentence(points)


def test_a_single_point_uses_the_singular():
    """Hebrew takes the singular for one; "1 נקודות" is ungrammatical."""
    sentence = late_penalty_sentence(1)
    assert sentence == (
        "העבודה הוגשה לאחר מועד ההגשה, ולכן הופחה נקודה אחת בגין איחור."
    )
    assert "נקודות" not in sentence


def test_the_notice_carries_no_misspellings_or_stale_wording():
    sentence = late_penalty_sentence(10)
    for wrong in ("מוען", "הופחה נקודות", "נקודת קריאה", "קריאה"):
        assert wrong not in sentence, f"stale wording still present: {wrong}"


def test_a_clamped_penalty_reports_the_points_actually_taken():
    """Score 5 with a 10-point penalty charges 5 - and the text must say 5."""
    graded = apply_late_penalty(result(score=5), 10)
    assert "הופחתו 5 נקודות" in graded.feedback_hebrew
    assert "10" not in graded.feedback_hebrew


def test_rerendering_replaces_a_stale_notice_rather_than_stacking():
    """A notice left by an older run is swapped, not duplicated."""
    stale = (
        "העבודה הוגשה אחרי מוען ההגשה, ולכן הופחה נקודת קריאה לאיחור.\n\nמעולה!"
    )
    graded = apply_late_penalty(
        GradingResult(
            student_name="Noa",
            score=95,
            feedback_hebrew=stale,
            deduction_breakdown=[
                DeductionItem(task_name="Task 1", points_deducted=5, reason="gap")
            ],
            task_evaluations=result().task_evaluations,
        ),
        10,
    )
    assert graded.feedback_hebrew == f"{late_penalty_sentence(10)}\n\nמעולה!"
    assert "מוען" not in graded.feedback_hebrew


# --------------------------------------------------------------------------- #
# 7. A stale Streamlit server must fail with an actionable message
# --------------------------------------------------------------------------- #


def test_the_models_own_feedback_is_never_mistaken_for_a_notice():
    """A short feedback text that merely mentions lateness must survive."""
    feedback = "הגשת אחרי המועד.\n\nכל הכבוד, עבודה טובה מאוד!"
    graded = apply_late_penalty(
        GradingResult(
            student_name="Noa",
            score=95,
            feedback_hebrew=feedback,
            deduction_breakdown=[
                DeductionItem(task_name="Task 1", points_deducted=5, reason="gap")
            ],
            task_evaluations=result().task_evaluations,
        ),
        10,
    )
    assert "כל הכבוד, עבודה טובה מאוד!" in graded.feedback_hebrew
    assert "הגשת אחרי המועד." in graded.feedback_hebrew, (
        "the student's own sentence is not a notice and must not be dropped"
    )


# --------------------------------------------------------------------------- #
# 8. A stale Streamlit server must fail with an actionable message
# --------------------------------------------------------------------------- #


class _StaleSubmission:
    """A ``StudentSubmission`` built from the pre-policy schema.

    Stands in for what a long-running Streamlit server keeps in
    ``sys.modules`` after ``src.models`` is edited underneath it.
    """

    def __init__(self) -> None:
        self.student_id = "u1"
        self.student_name = "Noa"
        self.submission_id = "s1"
        self.state = "TURNED_IN"


def test_a_stale_submission_model_is_reported_clearly():
    """The real crash was a bare pydantic AttributeError with no explanation."""
    with pytest.raises(TypeError) as excinfo:
        is_late(_StaleSubmission(), coursework())

    message = str(excinfo.value)
    assert "late_state" in message
    assert "Restart the Streamlit server" in message, (
        "the error must say what to do, not just what broke"
    )


def test_the_stale_guard_does_not_fire_for_a_current_model():
    # A real StudentSubmission has every policy field, so grading proceeds.
    assert is_late(submission(late_state="ON_TIME"), coursework()) is False


def test_penalty_for_also_reports_a_stale_model():
    with pytest.raises(TypeError):
        penalty_for(_StaleSubmission(), coursework(), 10)


# --------------------------------------------------------------------------- #
# 9. The API layer actually captures the timing data
# --------------------------------------------------------------------------- #


def test_due_date_and_time_become_an_exact_utc_instant():
    raw = {
        "dueDate": {"year": 2026, "month": 3, "day": 1},
        "dueTime": {"hours": 14, "minutes": 0, "seconds": 0},
    }
    due_date, due_datetime = classroom_service._classroom_due_instant(raw)
    assert due_date == date(2026, 3, 1)
    assert due_datetime == DUE
    assert due_datetime.tzinfo is not None, "must compare with submission times"


def test_a_due_date_without_a_time_yields_no_instant():
    """A date alone cannot tell 23:50 from 00:10 the next day."""
    due_date, due_datetime = classroom_service._classroom_due_instant(
        {"dueDate": {"year": 2026, "month": 3, "day": 1}}
    )
    assert due_date == date(2026, 3, 1)
    assert due_datetime is None


def test_coursework_without_a_due_date_yields_nothing():
    assert classroom_service._classroom_due_instant({}) == (None, None)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-03-01T14:30:00Z", datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc)),
        (
            "2026-03-01T14:30:00.123456Z",
            datetime(2026, 3, 1, 14, 30, 0, 123456, tzinfo=timezone.utc),
        ),
        (
            "2026-03-01T16:30:00+02:00",
            datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc),
        ),
    ],
)
def test_rfc3339_timestamps_are_parsed_to_utc(raw, expected):
    assert classroom_service._parse_rfc3339(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-date", 12345, {}])
def test_unparseable_timestamps_degrade_to_none(raw):
    """A bad timestamp must not abort a grading run."""
    assert classroom_service._parse_rfc3339(raw) is None


def test_get_submissions_captures_the_lateness_signals():
    """The API layer must surface lateness, or the policy has nothing to read."""
    service = classroom_service.ClassroomService.__new__(
        classroom_service.ClassroomService
    )
    service._profile_cache = {"u1": "Noa"}

    def _fake_list(**kwargs):
        class _Req:
            def execute(self_inner):
                return {
                    "studentSubmissions": [
                        {
                            "id": "s1",
                            "userId": "u1",
                            "state": "TURNED_IN",
                            "late": True,
                            "updateTime": "2026-03-02T09:00:00Z",
                        }
                    ]
                }

        return _Req()

    from types import SimpleNamespace

    service.classroom = SimpleNamespace(
        courses=lambda: SimpleNamespace(
            courseWork=lambda: SimpleNamespace(
                studentSubmissions=lambda: SimpleNamespace(list=_fake_list)
            )
        )
    )

    submissions = classroom_service.ClassroomService.get_submissions(service, "c1", "cw1")
    assert len(submissions) == 1
    got = submissions[0]
    assert got.is_late is True
    assert got.submitted_at == datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    assert is_late(got, coursework()) is True


# --------------------------------------------------------------------------- #
# 10. The guarantee itself: a late submission's FINAL score is reduced
#
# The tests above either drive ``apply_late_penalty`` directly or stub out the
# response parser. Neither proves what the teacher actually gets, so these go
# through the whole path: a realistic Groq payload is parsed by the real
# ``_parse_result`` and only the network call is faked.
# --------------------------------------------------------------------------- #


# A perfect grade: the model returns 100 with an empty breakdown. A late
# submission graded like this must end up *below* 100, which is the whole
# point - if the deduction were ever computed and then dropped on the floor,
# 100 would sail through untouched.
MODEL_AWARDS_100 = {
    "student_name": "Noa",
    "score": 100,
    "feedback_hebrew": "עבודה מצוינת! כל הכבוד.",
    "deduction_breakdown": [],
    "task_evaluations": [
        {
            "task_name": "Task 1",
            "student_answer_found": "print(1)",
            "status": "CORRECT",
            "notes": "נכון",
        }
    ],
}


class _GroqResponse:
    """Minimal stand-in for an OpenAI-compatible chat completion."""

    def __init__(self, content: str) -> None:
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


def _stub_raw_completion(monkeypatch, payload: dict) -> None:
    """Fake only the HTTP call, so payload parsing stays under test."""
    monkeypatch.setattr(evaluator, "_get_client", lambda: object())
    monkeypatch.setattr(
        evaluator,
        "_call_completion",
        lambda client, system, prompt, fmt: _GroqResponse(json.dumps(payload)),
    )
    # The degrade-and-retry ladder memoises which stage last worked; reset it
    # so one test cannot make the next skip the strict stage.
    monkeypatch.setattr(evaluator, "_first_working_stage", 0)


def test_a_late_submission_loses_the_penalty_from_its_final_score(monkeypatch):
    """The headline guarantee: the notice renders *and* the points are gone.

    Asserting the feedback alone is not enough - a notice on a perfect grade
    reads as a bug to a teacher, and the Mashov CSV would still import 100. The
    number the student is given is the number asserted here.
    """
    _stub_raw_completion(monkeypatch, MODEL_AWARDS_100)
    late = submission(late_state="LATE")

    graded = evaluator.evaluate_student_submission(
        late.student_name,
        "print(1)",
        ASSIGNMENT,
        late_penalty_points=penalty_for(late, coursework(), 10),
    )

    assert graded.score == 90, "a 10-point penalty must leave 100 at 90"
    assert [i.points_deducted for i in late_rows(graded)] == [10]
    # The breakdown must still reconcile with the score the teacher imports.
    assert sum(i.points_deducted for i in graded.deduction_breakdown) == (
        100 - graded.score
    )
    assert late_penalty_sentence(10) in graded.feedback_hebrew


def test_an_on_time_submission_keeps_the_full_score(monkeypatch):
    """The mirror image: the penalty must not touch work handed in on time."""
    _stub_raw_completion(monkeypatch, MODEL_AWARDS_100)
    on_time = submission(late_state="ON_TIME")

    graded = evaluator.evaluate_student_submission(
        on_time.student_name,
        "print(1)",
        ASSIGNMENT,
        late_penalty_points=penalty_for(on_time, coursework(), 10),
    )

    assert graded.score == 100
    assert late_rows(graded) == []
    assert graded.feedback_hebrew == MODEL_AWARDS_100["feedback_hebrew"]


def test_a_naive_submission_timestamp_is_read_as_utc_instead_of_crashing():
    """An offset-less timestamp used to raise and abort the whole run.

    Classroom documents a bare timestamp as UTC, so it is stamped as such
    rather than compared raw - a TypeError here would take down every
    remaining student in the batch, not just this one.
    """
    late = submission(submitted_at=datetime(2026, 3, 1, 15, 0))
    on_time = submission(submitted_at=datetime(2026, 3, 1, 13, 0))

    assert is_late(late, coursework()) is True
    assert is_late(on_time, coursework()) is False


def test_a_timestamp_carrying_an_offset_is_compared_in_utc():
    """16:30+02:00 is 14:30 UTC - half an hour past the 14:00 deadline."""
    aware = datetime(2026, 3, 1, 16, 30, tzinfo=timezone(timedelta(hours=2)))
    assert is_late(submission(submitted_at=aware), coursework()) is True

    before = datetime(2026, 3, 1, 15, 0, tzinfo=timezone(timedelta(hours=2)))
    assert is_late(submission(submitted_at=before), coursework()) is False

