"""Import Classroom grade-event workbooks (בוחן / מבחן) into the portal.

Every class folder under the teacher's ``אלפונים`` directory may hold one
Classroom "הזנת ציונים" workbook: a header line naming the event and its date,
then one row per student with a national id and a numeric grade. This module
turns those sheets into the portal's existing submission/grade records so an
exam is visible in the gradebook, the student class feed and the student's
history without inventing a second grading pipeline.

Each event becomes one *grade-only* assignment in its class: it has no rubric,
students cannot submit to it, and its grades are marked as imported. That is
deliberate - an exam score is a result the teacher entered, not an engine
output, so it must never be confused with a graded hand-in.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.grading.service import grade_for_revision
from portal.imports.sources import (
    SourceError,
    parse_grade,
    parse_hebrew_date,
    read_xlsx_rows,
)
from portal.ingestion.normalize import title_key
from portal.models import (
    GRADE_SOURCE_CLASSROOM,
    ROLE_STUDENT,
    SUBMISSION_GRADED,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    StudentRecord,
    Submission,
    User,
)
from portal.sync.matching import class_key, normalize_class_name
from portal.timeutil import utcnow

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_UNCHANGED = "unchanged"
ACTION_CONFLICT = "conflict"

ACTION_LABELS = {
    ACTION_CREATE: "ייווצר",
    ACTION_UPDATE: "יתעדכן",
    ACTION_UNCHANGED: "ללא שינוי",
    ACTION_CONFLICT: "דילוג",
}

EXAM_INSTRUCTIONS = (
    "אירוע ציון: {event}. הציון הוזן על ידי המורה ואין צורך להגיש "
    "קוד או קובץ במטלה זו."
)
EXAM_FEEDBACK = "ציון {event} שהוזן על ידי המורה."

_SUMMARY_NAMES = {"ממוצע", 'סה"כ', "סהכ", "סך הכל", "סיכום"}
_EVENT_RE = re.compile(
    r"אירוע\s+ציון\s*[:：]\s*(?P<name>.+?)\s*[-–—]\s*"
    r"(?P<date>\d{1,2}[./]\d{1,2}[./]\d{2,4})\s*$"
)


@dataclass(frozen=True)
class ExamStudentRow:
    row_number: int
    national_id: str
    student_name: str
    score: float | None
    note: str = ""


@dataclass(frozen=True)
class ExamSheet:
    path: Path
    class_label: str
    event_name: str
    exam_date: date | None
    rows: tuple[ExamStudentRow, ...]
    warnings: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        if self.exam_date is None:
            return self.event_name
        return f"{self.event_name} - {self.exam_date.strftime('%d/%m/%Y')}"


@dataclass(frozen=True)
class ExamAssignmentPlan:
    class_id: int
    class_label: str
    title: str
    event_name: str
    exam_date: date | None
    action: str = ACTION_CREATE
    assignment_id: int = field(default=0, repr=False)
    note: str = ""


@dataclass(frozen=True)
class PlannedExamGrade:
    class_id: int
    class_label: str
    student_name: str
    username: str
    exam_title: str
    event_name: str
    score: int
    raw_score: float
    source_file: str
    row_number: int
    action: str = ACTION_CREATE
    note: str = ""
    student_db_id: int = field(default=0, repr=False)
    graded_at: datetime = field(default_factory=utcnow, repr=False)


@dataclass(frozen=True)
class ExamImportPlan:
    assignments: tuple[ExamAssignmentPlan, ...]
    grades: tuple[PlannedExamGrade, ...]
    issues: tuple[str, ...] = ()
    blank_scores: int = 0

    @property
    def created(self) -> int:
        return sum(1 for item in self.grades if item.action == ACTION_CREATE)

    @property
    def updated(self) -> int:
        return sum(1 for item in self.grades if item.action == ACTION_UPDATE)

    @property
    def unchanged(self) -> int:
        return sum(1 for item in self.grades if item.action == ACTION_UNCHANGED)

    @property
    def conflicts(self) -> int:
        return sum(1 for item in self.grades if item.action == ACTION_CONFLICT)

    @property
    def actionable(self) -> int:
        return self.created + self.updated


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def _find_header(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows[:15]):
        joined = " ".join(row)
        if "ציון" in joined and ("ת.ז" in joined or "תעודת זהות" in joined):
            return index
    raise SourceError("לא נמצאה שורת כותרת עם ת.ז וציון בקובץ המבחן.")


def _column(header: list[str], *needles: str) -> int:
    for index, cell in enumerate(header):
        text = _clean(cell)
        if any(needle in text for needle in needles):
            return index
    return -1


def parse_exam_workbook(
    path: Path, class_label: str | None = None
) -> ExamSheet:
    """Read one Classroom grade-event workbook.

    The folder name is the class label (the workbook's own ``כיתה`` column is
    only a number), and the first line carries the event name and date.
    Summary rows such as ``ממוצע`` are ignored, never imported as a student.
    """
    rows = read_xlsx_rows(path)
    if not rows:
        raise SourceError(f"קובץ המבחן ריק: {path.name}")

    header_at = _find_header(rows)
    header = rows[header_at]
    id_col = _column(header, "ת.ז", "תעודת זהות")
    name_col = _column(header, "שם התלמיד", "שם תלמיד", "שם")
    score_col = _column(header, "ציון מספרי")
    if score_col < 0:
        score_col = _column(header, "ציון")
    note_col = _column(header, "הערה")

    title_line = ""
    for row in rows[:header_at]:
        joined = _clean(" ".join(row))
        if "אירוע ציון" in joined:
            title_line = joined
            break
    match = _EVENT_RE.search(title_line)
    if match:
        event_name = _clean(match.group("name"))
        exam_date = parse_hebrew_date(match.group("date"))
    else:
        event_name = _clean(title_line.split(":", 1)[-1]) or path.stem
        exam_date = parse_hebrew_date(event_name)
    if not event_name:
        raise SourceError(f"לא נמצא שם אירוע ציון בקובץ {path.name}.")

    label = _clean(class_label) if class_label else path.parent.name
    students: list[ExamStudentRow] = []
    warnings: list[str] = []
    for offset, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        def get(column: int) -> str:
            return _clean(row[column]) if 0 <= column < len(row) else ""

        name = get(name_col)
        national_id = "".join(ch for ch in get(id_col) if ch.isdigit())
        if not name and not national_id:
            continue
        if name in _SUMMARY_NAMES:
            continue
        if not national_id:
            warnings.append(f"שורה {offset}: לתלמיד/ה {name} אין ת.ז; השורה דולגה.")
            continue
        score = parse_grade(get(score_col))
        if score is None:
            # An empty cell is an absent grade, not a zero.
            students.append(
                ExamStudentRow(
                    row_number=offset,
                    national_id=national_id,
                    student_name=name,
                    score=None,
                    note=get(note_col),
                )
            )
            continue
        if score < 0 or score > 100:
            warnings.append(
                f"שורה {offset}: הציון {score:g} של {name} מחוץ לטווח 0-100; "
                "השורה דולגה."
            )
            continue
        students.append(
            ExamStudentRow(
                row_number=offset,
                national_id=national_id,
                student_name=name,
                score=score,
                note=get(note_col),
            )
        )
    if not students:
        raise SourceError(f"קובץ המבחן {path.name} לא הכיל שורות תלמידים.")
    return ExamSheet(
        path=path,
        class_label=label,
        event_name=event_name,
        exam_date=exam_date,
        rows=tuple(students),
        warnings=tuple(warnings),
    )


def discover_exam_sheets(root: Path) -> list[ExamSheet]:
    """Read every grade-event workbook under ``root/<class>/*.xlsx``."""
    if not root.is_dir():
        raise SourceError(f"התיקייה לא נמצאה: {root}")
    sheets: list[ExamSheet] = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir()):
        for path in sorted(folder.glob("*.xlsx")):
            if "אלפון" in path.name:
                continue
            sheets.append(parse_exam_workbook(path, folder.name))
    return sheets


def _graded_at(exam_date: date | None) -> datetime:
    if exam_date is None:
        return utcnow()
    return datetime.combine(exam_date, time(12, 0))


def build_exam_plan(db: Session, sheets: list[ExamSheet]) -> ExamImportPlan:
    groups = {
        class_key(normalize_class_name(group.name)): group
        for group in db.scalars(select(ClassGroup)).all()
    }
    records = {
        record.national_id: record
        for record in db.scalars(select(StudentRecord)).all()
        if record.national_id
    }
    users = {user.id: user for user in db.scalars(select(User)).all()}
    enrollments = {
        (row.class_id, row.student_id)
        for row in db.scalars(select(Enrollment)).all()
    }
    assignments_by_class: dict[int, list[Assignment]] = defaultdict(list)
    for assignment in db.scalars(select(Assignment)).all():
        assignments_by_class[assignment.class_id].append(assignment)

    issues: list[str] = []
    assignment_plans: list[ExamAssignmentPlan] = []
    grade_plans: list[PlannedExamGrade] = []
    blank_scores = 0
    seen_events: set[tuple[int, str]] = set()

    for sheet in sheets:
        issues.extend(f"{sheet.class_label}: {warning}" for warning in sheet.warnings)
        key = class_key(normalize_class_name(sheet.class_label))
        group = groups.get(key)
        if group is None:
            issues.append(
                f"כיתה '{sheet.class_label}' בקובץ המבחן לא נמצאה בפורטל; "
                "הקובץ דולג."
            )
            continue

        event_key = title_key(sheet.title)
        if (group.id, event_key) in seen_events:
            issues.append(
                f"{group.name}: אירוע '{sheet.title}' הופיע ביותר מקובץ אחד; "
                "הקובץ הנוסף דולג."
            )
            continue
        seen_events.add((group.id, event_key))

        existing = next(
            (
                item
                for item in assignments_by_class.get(group.id, [])
                if title_key(item.title) == event_key
            ),
            None,
        )
        if existing is not None and existing.rubric_path:
            issues.append(
                f"{group.name}: קיימת מטלה עם מחוון בשם '{sheet.title}'; "
                "ציוני המבחן לא ייובאו אליה."
            )
            continue
        assignment_plans.append(
            ExamAssignmentPlan(
                class_id=group.id,
                class_label=group.name,
                title=sheet.title,
                event_name=sheet.event_name,
                exam_date=sheet.exam_date,
                action=ACTION_UNCHANGED if existing is not None else ACTION_CREATE,
                assignment_id=existing.id if existing is not None else 0,
            )
        )

        rows_by_student: dict[int, list[ExamStudentRow]] = defaultdict(list)
        for row in sheet.rows:
            if row.score is None:
                blank_scores += 1
                continue
            record = records.get(row.national_id)
            user = users.get(record.student_id) if record is not None else None
            if user is None or user.role != ROLE_STUDENT:
                issues.append(
                    f"{group.name}: {row.student_name} — הת.ז לא נמצאה "
                    "כחשבון תלמיד בפורטל; השורה דולגה."
                )
                continue
            if (group.id, user.id) not in enrollments:
                issues.append(
                    f"{group.name}: {row.student_name} אינו משויך לכיתה; "
                    "השורה דולגה."
                )
                continue
            rows_by_student[user.id].append(row)

        for student_id, student_rows in rows_by_student.items():
            scores = {row.score for row in student_rows}
            if len(scores) > 1:
                issues.append(
                    f"{group.name}: נמצאו ציונים שונים לאותו תלמיד בקובץ "
                    f"'{sheet.path.name}'; השורה דולגה."
                )
                continue
            row = student_rows[0]
            user = users[student_id]
            planned = PlannedExamGrade(
                class_id=group.id,
                class_label=group.name,
                student_name=user.display_name,
                username=user.username,
                exam_title=sheet.title,
                event_name=sheet.event_name,
                score=max(0, min(100, round(row.score or 0))),
                raw_score=float(row.score or 0),
                source_file=sheet.path.name,
                row_number=row.row_number,
                student_db_id=student_id,
                graded_at=_graded_at(sheet.exam_date),
            )
            if existing is None:
                grade_plans.append(planned)
                continue
            submission = db.scalar(
                select(Submission).where(
                    Submission.assignment_id == existing.id,
                    Submission.student_id == student_id,
                )
            )
            if submission is None:
                grade_plans.append(planned)
                continue
            if (submission.text or "").strip():
                grade_plans.append(
                    submitted_conflict(planned, "קיימת הגשה בפורטל; לא נדרסה.")
                )
                continue
            grade = grade_for_revision(db, submission.id, submission.revision)
            if grade is None:
                grade_plans.append(planned)
                continue
            if grade.configured_model != GRADE_SOURCE_CLASSROOM:
                grade_plans.append(
                    submitted_conflict(planned, "קיים ציון מהמנוע; לא נדרס.")
                )
                continue
            grade_plans.append(
                PlannedExamGrade(
                    **{
                        **_as_dict(planned),
                        "action": (
                            ACTION_UNCHANGED
                            if grade.score == planned.score
                            else ACTION_UPDATE
                        ),
                    }
                )
            )

    grade_plans.sort(
        key=lambda item: (item.class_label, item.student_name, item.exam_title)
    )
    return ExamImportPlan(
        assignments=tuple(assignment_plans),
        grades=tuple(grade_plans),
        issues=tuple(dict.fromkeys(issues)),
        blank_scores=blank_scores,
    )


def _as_dict(item: PlannedExamGrade) -> dict:
    return {
        "class_id": item.class_id,
        "class_label": item.class_label,
        "student_name": item.student_name,
        "username": item.username,
        "exam_title": item.exam_title,
        "event_name": item.event_name,
        "score": item.score,
        "raw_score": item.raw_score,
        "source_file": item.source_file,
        "row_number": item.row_number,
        "student_db_id": item.student_db_id,
        "graded_at": item.graded_at,
        "note": item.note,
    }


def submitted_conflict(item: PlannedExamGrade, note: str) -> PlannedExamGrade:
    return PlannedExamGrade(
        **{**_as_dict(item), "action": ACTION_CONFLICT, "note": note}
    )


def apply_exam_plan(db: Session, plan: ExamImportPlan) -> dict[str, int]:
    """Create the grade-only assignments, submissions and grades in one run."""
    db.commit()  # close the planning read transaction
    counts = {"assignments": 0, "created": 0, "updated": 0, "unchanged": 0, "conflicts": 0}
    assignment_ids: dict[tuple[int, str], int] = {}

    for item in plan.assignments:
        if item.action == ACTION_CREATE:
            assignment = Assignment(
                class_id=item.class_id,
                title=item.title,
                instructions=EXAM_INSTRUCTIONS.format(event=item.event_name),
                rubric_path="",
                rubric_sha256="",
                due_at=_graded_at(item.exam_date),
                published=True,
            )
            db.add(assignment)
            db.flush()
            counts["assignments"] += 1
        else:
            assignment = db.get(Assignment, item.assignment_id)
            if assignment is None:  # pragma: no cover - plan/session mismatch
                continue
        assignment_ids[(item.class_id, title_key(item.title))] = assignment.id

    for item in plan.grades:
        if item.action in (ACTION_UNCHANGED, ACTION_CONFLICT):
            counts[
                "unchanged" if item.action == ACTION_UNCHANGED else "conflicts"
            ] += 1
            continue
        assignment_id = assignment_ids.get((item.class_id, title_key(item.exam_title)))
        if assignment_id is None:  # pragma: no cover - plan/session mismatch
            continue

        submission = db.scalar(
            select(Submission).where(
                Submission.assignment_id == assignment_id,
                Submission.student_id == item.student_db_id,
            )
        )
        if submission is None:
            submission = Submission(
                assignment_id=assignment_id,
                student_id=item.student_db_id,
                text="",
                revision=1,
                status=SUBMISSION_GRADED,
                first_submitted_at=item.graded_at,
                submitted_at=item.graded_at,
            )
            db.add(submission)
            db.flush()
        else:
            submission.status = SUBMISSION_GRADED

        grade = grade_for_revision(db, submission.id, submission.revision)
        if grade is None:
            grade = Grade(
                submission_id=submission.id,
                revision=submission.revision,
            )
            db.add(grade)
            counts["created"] += 1
        else:
            counts["updated"] += 1

        grade.score = item.score
        grade.feedback_hebrew = EXAM_FEEDBACK.format(event=item.event_name)
        grade.result_json = json.dumps(
            {
                "task_evaluations": [],
                "exam_import": {
                    "source": "classroom-grade-event",
                    "event": item.event_name,
                    "file": item.source_file,
                    "row": item.row_number,
                    "raw_score": item.raw_score,
                },
            },
            ensure_ascii=False,
        )
        grade.deduction_json = "[]"
        grade.audit_json = ""
        grade.audit_ok = True
        grade.audit_needs_review = False
        grade.audit_summary = "ייבוא ציון מבחן/בוחן"
        grade.late_penalty_points = 0
        grade.configured_model = GRADE_SOURCE_CLASSROOM
        grade.graded_at = item.graded_at
        grade.rubric_title = ""
        grade.rubric_sha256 = ""

    db.commit()
    return counts


__all__ = [
    "ACTION_CONFLICT",
    "ACTION_CREATE",
    "ACTION_LABELS",
    "ACTION_UNCHANGED",
    "ACTION_UPDATE",
    "EXAM_FEEDBACK",
    "EXAM_INSTRUCTIONS",
    "ExamAssignmentPlan",
    "ExamImportPlan",
    "ExamSheet",
    "ExamStudentRow",
    "PlannedExamGrade",
    "apply_exam_plan",
    "build_exam_plan",
    "discover_exam_sheets",
    "parse_exam_workbook",
]
