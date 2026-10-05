"""Pydantic models for structured Classroom AutoGrader data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Course(BaseModel):
    """An active Google Classroom course."""

    id: str = Field(description="Classroom-assigned course identifier.")
    name: str = Field(description="Human-readable course name.")
    section: Optional[str] = Field(
        default=None,
        description="Course section label, if the teacher set one.",
    )


class CourseWork(BaseModel):
    """A published assignment (coursework) item."""

    id: str = Field(description="Classroom-assigned coursework identifier.")
    title: str = Field(description="Assignment title.")
    due_date: Optional[date] = Field(
        default=None,
        description="Due date in UTC, if the teacher set one.",
    )
    due_datetime: Optional[datetime] = Field(
        default=None,
        description=(
            "Exact due instant in UTC (``dueDate`` + ``dueTime``), if set. "
            "Needed for the late-submission check: a date alone cannot decide "
            "whether work handed in at 23:50 on the due date was late."
        ),
    )
    state: Optional[str] = Field(
        default=None,
        description=(
            "Coursework state reported by the API: ``PUBLISHED``, ``DRAFT`` "
            "or ``DELETED``. ``None`` when a caller did not select the field, "
            "which is treated as published rather than skipped - dropping a "
            "real assignment on a guess would hide work that is waiting."
        ),
    )
    work_type: Optional[str] = Field(
        default=None,
        description=(
            "``ASSIGNMENT`` for real assignments; ``SHORT_ANSWER_QUESTION`` "
            "and ``MULTIPLE_CHOICE_QUESTION`` for question items, which are "
            "answered inline in Classroom and cannot carry a Google Doc."
        ),
    )


class StudentSubmission(BaseModel):
    """Structured view of one student's submission for one assignment."""

    student_id: str = Field(description="User id of the submitting student.")
    student_name: str = Field(description="Display name resolved from the user profile.")
    submission_id: str = Field(description="Classroom-assigned submission id.")
    state: str = Field(description="Submission state, e.g. TURNED_IN or NEW.")
    doc_id: Optional[str] = Field(
        default=None,
        description="Google Docs file id of the attachment, when extractable.",
    )
    raw_code: str = Field(
        default="",
        description="Text extracted from the attached Google Doc.",
    )
    late_state: Optional[str] = Field(
        default=None,
        description=(
            "Classroom's own lateness verdict (``LATE`` / ``ON_TIME`` / "
            "``NEEDS_GRADING``) when the API reports one."
        ),
    )
    is_late: Optional[bool] = Field(
        default=None,
        description=(
            "Classroom's boolean ``late`` flag. ``None`` when the API did not "
            "report it, which must not be read as 'on time'."
        ),
    )
    submitted_at: Optional[datetime] = Field(
        default=None,
        description=(
            "When the submission was last updated by the student (UTC), used "
            "as a fallback when Classroom reports no lateness verdict."
        ),
    )


class TaskDefinition(BaseModel):
    """One gradable task inside an assignment's rubric."""

    id: int = Field(description="Ordinal task number within the assignment.")
    name: str = Field(description="Short human-readable task name.")
    description: str = Field(
        description="What the student must do to complete the task.",
    )
    exempt: bool = Field(
        default=False,
        description=(
            "Marks a known-broken task (malformed definition, impossible "
            "requirement, obsolete exercise). Exempt tasks are skipped "
            "entirely while grading: no status, no points, no feedback."
        ),
    )


class AssignmentConfig(BaseModel):
    """Lightweight teacher-provided definition of an assignment."""

    assignment_id: str = Field(description="Stable identifier, e.g. 'lesson_1'.")
    title: str = Field(description="Assignment title as shown to students.")
    tasks: list[TaskDefinition] = Field(
        description="Tasks the submission is graded against.",
    )


# The four allowed per-task verdicts (spec: CORRECT / PARTIALLY_CORRECT /
# INCORRECT / MISSING), modeled as a Literal so the JSON schema constrains
# the LLM to exactly these values.
TaskStatus = Literal["CORRECT", "PARTIALLY_CORRECT", "INCORRECT", "MISSING"]


class TaskEvaluation(BaseModel):
    """Per-task verdict produced by the LLM evaluator."""

    task_name: str = Field(description="Name of the evaluated task.")
    student_answer_found: str = Field(
        description=(
            "Short quote or description of the student's relevant text; "
            "empty string when status is MISSING."
        ),
    )
    status: TaskStatus = Field(
        description="CORRECT, PARTIALLY_CORRECT, INCORRECT, or MISSING.",
    )
    notes: str = Field(description="Brief technical observation, in English.")


class DeductionItem(BaseModel):
    """One itemised point deduction, for the teacher's report.

    Every field is required (no optionals) so the model stays inside Groq's
    strict JSON-Schema subset, and ``points_deducted`` is a plain int so the
    breakdown can be summed and audited.
    """

    task_name: str = Field(
        description=(
            "Name of the task that lost points (copied from the Tasks JSON), "
            "or 'General' for a whole-submission deduction."
        ),
    )
    points_deducted: int = Field(
        ge=0,
        le=100,
        description=(
            "Points taken off the overall score because of this task "
            "(2-5 for a minor gap, more only for a substantial one)."
        ),
    )
    reason: str = Field(
        description=(
            "One short sentence in English: what is missing and why it "
            "matters pedagogically."
        ),
    )


class GradingResult(BaseModel):
    """Structured outcome of a single-pass LLM evaluation."""

    student_name: str = Field(description="Name of the graded student.")
    score: int = Field(
        ge=0,
        le=100,
        description="Overall assignment score from 0 to 100.",
    )
    feedback_hebrew: str = Field(
        description=(
            "Warm, pedagogical, constructive feedback in Hebrew for a "
            "middle-school student."
        ),
    )
    deduction_breakdown: list[DeductionItem] = Field(
        default_factory=list,
        description=(
            "Itemised list of every deduction taken off the overall score: "
            "one entry per task that lost points, empty when nothing was "
            "deducted. The points_deducted values should sum to 100 - score."
        ),
    )
    task_evaluations: list[TaskEvaluation] = Field(
        description="One evaluation per assignment task.",
    )


class ReportEntry(BaseModel):
    """One graded student inside a saved evaluation report."""

    student_id: str = Field(description="User id of the graded student.")
    student_name: str = Field(description="Display name of the student.")
    submission_id: str = Field(description="Classroom submission to release.")
    doc_id: Optional[str] = Field(
        default=None,
        description="Google Doc that receives the feedback comment.",
    )
    result: GradingResult = Field(description="Full evaluation outcome.")


class EvaluationReport(BaseModel):
    """Saved evaluation run consumed by the Review & Release workflow."""

    version: int = Field(default=1, description="Report schema version.")
    created_at: datetime = Field(description="When the evaluation ran.")
    model: str = Field(description="LLM model that produced the grades.")
    course_id: str = Field(description="Course the report belongs to.")
    course_name: str = Field(description="Human-readable course name.")
    course_work_id: str = Field(description="Assignment the report belongs to.")
    course_work_title: str = Field(description="Human-readable assignment title.")
    assignment_config_id: str = Field(
        description="Rubric config id, e.g. 'lesson_1'.",
    )
    csv_file: Optional[str] = Field(
        default=None,
        description=(
            "File name of the Mashov CSV exported next to this report, so "
            "the release flow can clean both files up together."
        ),
    )
    entries: list[ReportEntry] = Field(
        description="Graded students, in report order.",
    )
