"""Turn reviewed Classroom CSV rows into portal submissions and grades.

The importer is deliberately narrow:

* only rows the teacher confirmed in the mapping file are used;
* a column is imported only when its title matches a portal assignment of the
  same class (the same ``title_key`` rule the content importer uses);
* a student must already exist and be enrolled (``import_students.py`` does
  that);
* an existing *portal* submission or grade is never overwritten. Historical
  rows are marked with ``configured_model = "classroom-import"`` and can be
  refreshed by rerunning the import.

Duplicate Classroom exports contain the same assignment twice (``X`` and
``X [Auto]``) and sometimes the same student twice. Columns with the same
title are merged; two different scores for one student/assignment become a
conflict instead of a guess.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.grading.service import grade_for_revision
from portal.imports.matching import MappingRow
from portal.imports.sources import ClassroomColumn, ClassroomSheet, parse_grade
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

HISTORICAL_FEEDBACK = "ציון היסטורי שהועבר מ-Google Classroom."


@dataclass(frozen=True)
class PlannedGrade:
    """One historical score for one student and one portal assignment."""

    class_label: str
    student_name: str
    username: str
    assignment_title: str
    assignment_published: bool
    score: int
    raw_score: float
    max_points: float
    column_title: str
    csv_file: str
    csv_row: int
    action: str = ACTION_CREATE
    note: str = ""
    student_db_id: int = field(default=0, repr=False)
    assignment_id: int = field(default=0, repr=False)
    graded_at: datetime = field(default_factory=utcnow, repr=False)


@dataclass(frozen=True)
class SkippedColumn:
    class_label: str
    title: str
    numeric_cells: int
    reason: str


@dataclass(frozen=True)
class GradeImportPlan:
    grades: tuple[PlannedGrade, ...]
    skipped_columns: tuple[SkippedColumn, ...] = ()
    issues: tuple[str, ...] = ()
    matched_columns: int = 0

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


def _norm_points(value: float | None) -> float:
    return value if value and value > 0 else 100.0


def _scaled_score(raw: float, max_points: float | None) -> int:
    maximum = _norm_points(max_points)
    score = round(raw / maximum * 100)
    return max(0, min(100, score))


def _graded_at(column: ClassroomColumn, assignment: Assignment) -> datetime:
    """Historical timestamp: column due date, then the portal deadline, then now."""
    due = column.due_at or (
        assignment.due_at.date() if assignment.due_at else None
    )
    if isinstance(due, date):
        return datetime.combine(due, time(12, 0))
    return utcnow()


def _find_classroom_student(sheet: ClassroomSheet, row_number: int):
    for student in sheet.students:
        if student.row_number == row_number:
            return student
    return None


def _anchor_student(sheet: ClassroomSheet, row: MappingRow) -> tuple[object, str]:
    """Return the Classroom student a mapping row means, plus a problem note.

    ``csv_row`` is only a hint. **Google re-sorts the gradebook on every
    export**, so a row number captured from an older file can point at a
    different child in a newer one — and the importer would silently grade the
    wrong student. The e-mail recorded in the mapping is stable, so it decides
    whenever it resolves to exactly one row. When it cannot be resolved the
    importer refuses the row instead of guessing from a stale position.
    """
    email = (row.classroom_email or "").strip().lower()
    if not email:
        student = _find_classroom_student(sheet, row.csv_row)
        if student is None:
            return None, f"שורה {row.csv_row} לא נמצאה בקובץ המקור."
        return student, ""
    matches = [
        student
        for student in sheet.students
        if student.email.strip().lower() == email
    ]
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return None, f"האימייל {email} לא נמצא בקובץ המקור."
    return None, f"האימייל {email} מופיע {len(matches)} פעמים בקובץ המקור."


def _is_excluded(title: str, prefixes: tuple[str, ...]) -> bool:
    """True when an assignment title starts with one of the excluded keys."""
    if not prefixes:
        return False
    key = title_key(title)
    return any(key.startswith(prefix) for prefix in prefixes)


def _column_groups(sheet: ClassroomSheet) -> dict[str, list[ClassroomColumn]]:
    groups: dict[str, list[ClassroomColumn]] = defaultdict(list)
    for column in sheet.columns:
        groups[title_key(column.title)].append(column)
    return groups


def _merged_value(
    row, columns: list[ClassroomColumn]
) -> tuple[float | None, ClassroomColumn | None, tuple[float, ...]]:
    """Return one value for duplicate columns of the same title.

    The third element lists every *different* numeric value found. More than
    one means the duplicate export columns disagree, and the importer reports a
    conflict instead of silently trusting one of them.
    """
    ordered = sorted(
        columns,
        key=lambda column: (
            column.title.casefold().endswith("[auto]"),
            column.max_points is None,
            column.index,
        ),
    )
    readings = [
        (parse_grade(row.scores.get(column.index, "")), column)
        for column in ordered
    ]
    values = [
        (value, column) for value, column in readings if value is not None
    ]
    if not values:
        return None, ordered[0] if ordered else None, ()
    first_value, first_column = values[0]
    distinct = tuple(sorted({value for value, _column in values}))
    return first_value, first_column, distinct


def build_grade_plan(
    db: Session,
    sheets: list[ClassroomSheet],
    mapping: list[MappingRow],
    *,
    exclude_titles: tuple[str, ...] = (),
) -> GradeImportPlan:
    """Plan the historical grades for every confirmed mapping row.

    ``exclude_titles`` leaves whole assignments out of the run: a column whose
    portal title starts with one of these keys is reported as excluded instead
    of written, which is how a one-off import skips material the teacher has
    already dropped from the curriculum.
    """
    excluded = tuple(
        dict.fromkeys(title_key(item) for item in exclude_titles if title_key(item))
    )
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
    sheets_by_key = {
        (class_key(normalize_class_name(sheet.class_label)), sheet.path.name): sheet
        for sheet in sheets
    }

    assignments_by_class: dict[int, list[Assignment]] = defaultdict(list)
    for assignment in db.scalars(select(Assignment)).all():
        assignments_by_class[assignment.class_id].append(assignment)

    issues: list[str] = []
    by_class: dict[str, list[MappingRow]] = defaultdict(list)
    for row in mapping:
        by_class[class_key(row.class_label)].append(row)

    # (group, student_id, assignment_id, column_title) -> list of readings
    readings: dict[tuple[int, int, int, str], list[PlannedGrade]] = {}
    skipped: dict[tuple[int, str], SkippedColumn] = {}
    matched_columns = 0
    # Class -> rows whose CSV position moved between exports and had to be
    # re-anchored by e-mail. Reported once per class, never per row.
    reanchored: dict[str, int] = defaultdict(int)

    for key, rows in by_class.items():
        group = groups.get(key)
        if group is None:
            issues.append(
                f"כיתה '{rows[0].class_label}' בקובץ ההתאמה לא נמצאה בפורטל; "
                "השורות שלה דולגו."
            )
            continue
        assignments = assignments_by_class.get(group.id, [])
        by_title: dict[str, Assignment] = {}
        excluded_keys: set[str] = set()
        for assignment in assignments:
            column_key = title_key(assignment.title)
            if _is_excluded(assignment.title, excluded):
                excluded_keys.add(column_key)
                continue
            by_title.setdefault(column_key, assignment)

        # One report per class and per CSV column, regardless of the mapping.
        seen_sheets: set[str] = set()
        for row in rows:
            sheet_key = (key, row.csv_file)
            sheet = sheets_by_key.get(sheet_key)
            if sheet is None:
                if row.csv_file not in seen_sheets:
                    issues.append(
                        f"{group.name}: הקובץ '{row.csv_file}' לא נמצא במקור."
                    )
                    seen_sheets.add(row.csv_file)
                continue
            if row.csv_file in seen_sheets:
                continue
            seen_sheets.add(row.csv_file)
            groups_for_sheet = _column_groups(sheet)
            for column_key, columns in groups_for_sheet.items():
                assignment = by_title.get(column_key)
                numeric = sum(
                    1
                    for student in sheet.students
                    if _merged_value(student, columns)[0] is not None
                )
                if assignment is None:
                    if numeric:
                        skipped[(group.id, column_key)] = SkippedColumn(
                            class_label=group.name,
                            title=columns[0].title,
                            numeric_cells=numeric,
                            reason=(
                                "הוחרג מהריצה (--exclude-title)"
                                if column_key in excluded_keys
                                else "אין מטלה תואמת בפורטל"
                            ),
                        )
                    continue
                matched_columns += 1

        for row in rows:
            if not row.usable:
                continue
            sheet = sheets_by_key.get((key, row.csv_file))
            if sheet is None:
                continue
            classroom, problem = _anchor_student(sheet, row)
            if classroom is None:
                issues.append(
                    f"{group.name}: {row.classroom_name} — {problem} "
                    "הציונים של שורה זו לא יובאו."
                )
                continue
            if classroom.row_number != row.csv_row:
                reanchored[group.name] += 1
            record = records.get(row.student_id)
            user = users.get(record.student_id) if record is not None else None
            if user is None or user.role != ROLE_STUDENT:
                issues.append(
                    f"{group.name}: ת.ז {row.student_id} "
                    f"({row.classroom_name}) לא נמצאה כחשבון תלמיד; "
                    "הריצו קודם import_students.py."
                )
                continue
            if (group.id, user.id) not in enrollments:
                issues.append(
                    f"{group.name}: {user.display_name} אינו משויך לכיתה; "
                    "הריצו קודם import_students.py."
                )
                continue
            for column_key, columns in _column_groups(sheet).items():
                assignment = by_title.get(column_key)
                if assignment is None:
                    continue
                raw, column, conflicting = _merged_value(classroom, columns)
                if raw is None or column is None:
                    continue
                maximum = _norm_points(column.max_points)
                planned = PlannedGrade(
                    class_label=group.name,
                    student_name=user.display_name,
                    username=user.username,
                    assignment_title=assignment.title,
                    assignment_published=bool(assignment.published),
                    score=_scaled_score(raw, maximum),
                    raw_score=raw,
                    max_points=maximum,
                    column_title=column.title,
                    csv_file=sheet.path.name,
                    csv_row=row.csv_row,
                    student_db_id=user.id,
                    assignment_id=assignment.id,
                    graded_at=_graded_at(column, assignment),
                )
                if len(conflicting) > 1:
                    planned = replace(
                        planned,
                        action=ACTION_CONFLICT,
                        note=(
                            "עמודות כפולות בקובץ אינן מסכימות: "
                            + ", ".join(f"{value:g}" for value in conflicting)
                        ),
                    )
                readings.setdefault(
                    (group.id, user.id, assignment.id, column_key), []
                ).append(planned)

    planned_grades: list[PlannedGrade] = []
    for (class_id, student_id, assignment_id, _column_key), items in readings.items():
        first = items[0]
        values = {item.raw_score for item in items}
        if first.action == ACTION_CONFLICT:
            planned_grades.append(first)
            continue
        if len(values) > 1:
            planned_grades.append(
                replace(
                    first,
                    action=ACTION_CONFLICT,
                    note=(
                        "נמצאו ציונים שונים לאותה מטלה: "
                        + ", ".join(f"{value:g}" for value in sorted(values))
                    ),
                )
            )
            continue

        submission = db.scalar(
            select(Submission).where(
                Submission.assignment_id == assignment_id,
                Submission.student_id == student_id,
            )
        )
        grade = (
            grade_for_revision(db, submission.id, submission.revision)
            if submission is not None
            else None
        )
        if submission is None:
            planned_grades.append(first)
            continue
        if grade is None:
            if (submission.text or "").strip():
                planned_grades.append(
                    replace(
                        first,
                        action=ACTION_CONFLICT,
                        note="קיימת הגשה בפורטל ללא ציון; לא נדרסה.",
                    )
                )
            else:
                planned_grades.append(replace(first, action=ACTION_CREATE))
            continue
        if grade.configured_model != GRADE_SOURCE_CLASSROOM:
            planned_grades.append(
                replace(
                    first,
                    action=ACTION_CONFLICT,
                    note="קיים ציון מהמנוע; לא נדרס.",
                )
            )
            continue
        action = (
            ACTION_UNCHANGED if grade.score == first.score else ACTION_UPDATE
        )
        planned_grades.append(replace(first, action=action))

    planned_grades.sort(
        key=lambda item: (item.class_label, item.student_name, item.assignment_title)
    )
    for name in sorted(reanchored):
        issues.append(
            f"{name}: {reanchored[name]} שובצו מחדש לפי אימייל "
            "(ייצוא Classroom חדש ממיין את רשימת התלמידים מחדש)."
        )
    return GradeImportPlan(
        grades=tuple(planned_grades),
        skipped_columns=tuple(
            sorted(skipped.values(), key=lambda item: (item.class_label, item.title))
        ),
        issues=tuple(issues),
        matched_columns=matched_columns,
    )


def apply_grade_plan(db: Session, plan: GradeImportPlan) -> dict[str, int]:
    """Write creates/updates in one transaction; return per-action counts."""
    # Close the planning read transaction (see students.apply_student_plan).
    db.commit()
    counts = {"created": 0, "updated": 0, "unchanged": 0, "conflicts": 0}
    for item in plan.grades:
        if item.action in (ACTION_UNCHANGED, ACTION_CONFLICT):
            counts[
                "unchanged" if item.action == ACTION_UNCHANGED else "conflicts"
            ] += 1
            continue

        submission = db.scalar(
            select(Submission).where(
                Submission.assignment_id == item.assignment_id,
                Submission.student_id == item.student_db_id,
            )
        )
        if submission is None:
            submission = Submission(
                assignment_id=item.assignment_id,
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

        original = (
            f"ניקוד מקורי: {item.raw_score:g} מתוך {item.max_points:g}."
            if item.max_points != 100
            else ""
        )
        grade.score = item.score
        grade.feedback_hebrew = " ".join(
            part for part in (HISTORICAL_FEEDBACK, original) if part
        )
        grade.result_json = json.dumps(
            {
                "task_evaluations": [],
                "historical_import": {
                    "source": "classroom-csv",
                    "column": item.column_title,
                    "raw_score": item.raw_score,
                    "max_points": item.max_points,
                    "csv_file": item.csv_file,
                    "csv_row": item.csv_row,
                },
            },
            ensure_ascii=False,
        )
        grade.deduction_json = "[]"
        grade.audit_json = ""
        grade.audit_ok = True
        grade.audit_needs_review = False
        grade.audit_summary = "ייבוא היסטורי מ-Google Classroom"
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
    "HISTORICAL_FEEDBACK",
    "GradeImportPlan",
    "PlannedGrade",
    "SkippedColumn",
    "apply_grade_plan",
    "build_grade_plan",
]
