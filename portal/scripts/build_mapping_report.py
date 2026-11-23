"""Build the data behind the alephon-to-Classroom name-mapping report.

Reads the school directories (אלפונים), the reviewed mapping file and the
portal database (read-only) and writes one JSON document that the Excel report
is rendered from. Nothing is written to the database.

Usage::

    python -m portal.scripts.build_mapping_report
    python -m portal.scripts.build_mapping_report --root "C:/.../אלפונים"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from portal.imports.matching import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    MappingRow,
    read_mapping,
)
from portal.imports.sources import class_directories, load_class_folder
from portal.sync.matching import class_key
from portal.timeutil import utcnow

DEFAULT_ROOT = Path(r"C:/Users/Idan Sarfati/Desktop/אלפונים")
DEFAULT_MAPPING = Path(__file__).resolve().parents[1] / "data" / "student_mapping.csv"
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "portal.db"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "student_mapping_report.json"

STATUS_IMPORTED = "מיובא"
STATUS_REVIEW = "לבדיקה"
STATUS_WEAK = "התאמה חלשה"
STATUS_UNCONFIRMED = "ודאות גבוהה — לא אושר"
STATUS_MISSING = "לא נמצא ב-Classroom"

STATUS_ORDER = (
    STATUS_MISSING,
    STATUS_REVIEW,
    STATUS_WEAK,
    STATUS_UNCONFIRMED,
    STATUS_IMPORTED,
)

CONFIDENCE_TEXT = {
    CONFIDENCE_HIGH: "ודאות גבוהה",
    CONFIDENCE_MEDIUM: "ודאות בינונית",
    CONFIDENCE_LOW: "ודאות נמוכה",
    CONFIDENCE_NONE: "ללא התאמה",
}


@dataclass(frozen=True)
class StudentRow:
    class_label: str
    national_id: str
    name: str
    status: str
    classroom_names: str
    confidence: str
    rows: int
    grades: int
    note: str


def _distinct(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = (value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _ordered_rows(rows: list[MappingRow]) -> list[MappingRow]:
    def rank(row: MappingRow) -> tuple[int, int]:
        if row.confirmed:
            return (0, row.csv_row)
        return (
            {
                CONFIDENCE_HIGH: 1,
                CONFIDENCE_MEDIUM: 2,
                CONFIDENCE_LOW: 3,
                CONFIDENCE_NONE: 4,
            }.get(row.confidence, 4),
            row.csv_row,
        )

    return sorted(rows, key=rank)


def _student_confidence(rows: list[MappingRow]) -> str:
    if any(row.confirmed for row in rows):
        return "ודאות גבוהה (מאושר)"
    for confidence in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW):
        if any(row.confidence == confidence for row in rows):
            return CONFIDENCE_TEXT[confidence]
    return CONFIDENCE_TEXT[CONFIDENCE_NONE]


def _grade_counts(db_path: Path) -> dict[str, int]:
    """Historical imported grades per national id, read-only."""
    if not db_path.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = connection.execute(
            "SELECT sr.national_id, COUNT(g.id) "
            "FROM portal_grades g "
            "JOIN portal_submissions s ON s.id = g.submission_id "
            "JOIN portal_student_records sr ON sr.student_id = s.student_id "
            "WHERE g.configured_model = 'classroom-import' "
            "AND sr.national_id IS NOT NULL "
            "GROUP BY sr.national_id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {str(national_id): int(count) for national_id, count in rows}


def build(root: Path, mapping_path: Path, db_path: Path) -> dict:
    directories = class_directories(root)
    mapping_rows = read_mapping(mapping_path)
    grades = _grade_counts(db_path)

    rows_by_student: dict[tuple[str, str], list[MappingRow]] = defaultdict(list)
    rows_by_class: dict[str, list[MappingRow]] = defaultdict(list)
    rows_by_id: dict[str, list[MappingRow]] = defaultdict(list)
    for row in mapping_rows:
        rows_by_class[class_key(row.class_label)].append(row)
        if row.student_id:
            rows_by_student[(class_key(row.class_label), row.student_id)].append(row)
            rows_by_id[row.student_id].append(row)

    classes: list[dict] = []
    students: list[StudentRow] = []
    matches: list[list[object]] = []
    issues: list[list[object]] = []

    total_grades = 0
    for class_label, folder in directories.items():
        alephon, sheets = load_class_folder(folder, class_label)
        class_lookup = class_key(class_label)
        class_rows = rows_by_class.get(class_lookup, [])
        display_label = class_rows[0].class_label if class_rows else class_label
        sheet_names = ", ".join(sheet.path.name for sheet in sheets)

        counts = {status: 0 for status in STATUS_ORDER}
        duplicate_rows = 0
        for student in alephon.students:
            matched = rows_by_student.get(
                (class_lookup, student.national_id), []
            )
            # The alephon may list a student in one class while he actually
            # learns in another (e.g. Kai Rapoport sits in the ז'3 Classroom
            # but is written on the ז'4 alephon). Fall back to the national id
            # so a confirmed row is never reported as "missing".
            cross_class_label = ""
            if not matched:
                elsewhere = rows_by_id.get(student.national_id, [])
                if elsewhere:
                    matched = elsewhere
                    cross_class_label = elsewhere[0].class_label
            confirmed = [row for row in matched if row.confirmed]
            if confirmed:
                status = STATUS_IMPORTED
            elif any(row.confidence == CONFIDENCE_MEDIUM for row in matched):
                status = STATUS_REVIEW
            elif any(row.confidence == CONFIDENCE_LOW for row in matched):
                status = STATUS_WEAK
            elif any(row.confidence == CONFIDENCE_HIGH for row in matched):
                status = STATUS_UNCONFIRMED
            else:
                status = STATUS_MISSING
            counts[status] += 1

            ordered = _ordered_rows(matched)
            # For an imported student show the name on the row that actually
            # supplied the grades; duplicate rows are listed on their own
            # sheets so they cannot be mistaken for a second identity.
            names = _distinct(
                [
                    row.classroom_name
                    for row in (confirmed if confirmed else ordered)
                ]
            )
            notes = _distinct([row.note for row in ordered if row.note])
            if len(matched) > 1:
                duplicate_rows += len(matched) - 1

            if status == STATUS_IMPORTED:
                note = "התאמה מאושרת; הציונים נכתבו לפורטל."
            elif status == STATUS_REVIEW:
                note = notes[0] if notes else "נדרשת בדיקה אנושית לפני ייבוא ציונים."
            elif status == STATUS_WEAK:
                note = notes[0] if notes else "הדמיון נמוך; לא יובאו ציונים."
            elif status == STATUS_UNCONFIRMED:
                note = "ההתאמה בעלת ודאות גבוהה אך אינה מאושרת בקובץ המיפוי."
            else:
                note = f"לא נמצאה שורת תלמיד תואמת ב-{sheet_names}."
            if cross_class_label:
                note += (
                    f" שורת Classroom נמצאת בכיתה {cross_class_label}; "
                    "התלמיד הועבר לכיתה זו גם בפורטל."
                )
            if len(matched) > 1:
                note += f" ({len(matched)} שורות Classroom)"

            class_grades = grades.get(student.national_id, 0)
            total_grades += class_grades
            students.append(
                StudentRow(
                    class_label=display_label,
                    national_id=student.national_id,
                    name=student.display_name,
                    status=status,
                    classroom_names=" / ".join(names),
                    confidence=_student_confidence(matched),
                    rows=len(matched),
                    grades=class_grades,
                    note=note,
                )
            )

        for row in class_rows:
            imported = row.usable
            if imported:
                imported_text = "כן"
            elif (
                row.confirmed is False
                and row.confidence == CONFIDENCE_HIGH
                and row.student_id
            ):
                imported_text = "לא — שורת כפילות"
            elif not row.student_id:
                imported_text = "לא — ללא ת.ז"
            else:
                imported_text = "לא"
            matches.append(
                [
                    row.class_label,
                    row.csv_file,
                    row.csv_row,
                    row.classroom_name,
                    row.classroom_email,
                    row.student_id,
                    row.student_name,
                    CONFIDENCE_TEXT.get(row.confidence, row.confidence),
                    "כן" if row.confirmed else "לא",
                    imported_text,
                    row.note,
                ]
            )

        classes.append(
            {
                "class_label": display_label,
                "alephon_students": len(alephon.students),
                "classroom_rows": len(class_rows),
                "classroom_students": len(
                    {row.student_id for row in class_rows if row.student_id}
                ),
                "imported": counts[STATUS_IMPORTED],
                "review": counts[STATUS_REVIEW],
                "weak": counts[STATUS_WEAK],
                "unconfirmed": counts[STATUS_UNCONFIRMED],
                "missing": counts[STATUS_MISSING],
                "duplicate_rows": len(class_rows)
                - len({row.student_id for row in class_rows if row.student_id}),
                "grades": sum(
                    grades.get(student.national_id, 0) for student in alephon.students
                ),
            }
        )

        # Issues: one line per student that is not fully imported.
        for student in students[-len(alephon.students) :]:
            if student.status == STATUS_IMPORTED:
                continue
            issues.append(
                [
                    student.status,
                    display_label,
                    student.name,
                    student.national_id,
                    student.classroom_names,
                    student.confidence,
                    student.note,
                ]
            )
        # Issues: duplicate Classroom rows (one line per student with >1 row).
        for (_, national_id), matched in rows_by_student.items():
            if len(matched) < 2:
                continue
            if class_key(matched[0].class_label) != class_lookup:
                continue
            names = " / ".join(_distinct([row.classroom_name for row in matched]))
            issues.append(
                [
                    "שורת כפילות",
                    display_label,
                    matched[0].student_name,
                    national_id,
                    names,
                    "—",
                    f"{len(matched)} שורות Classroom עבור אותו תלמיד.",
                ]
            )

    # Classroom rows that could not be attached to any alephon student.
    for row in mapping_rows:
        if row.student_id:
            continue
        issues.append(
            [
                "שורה ללא התאמה",
                row.class_label,
                row.classroom_name,
                "",
                row.classroom_name,
                CONFIDENCE_TEXT.get(row.confidence, row.confidence),
                row.note or "לא נמצא תלמיד תואם באלפון.",
            ]
        )
        matches.append(
            [
                row.class_label,
                row.csv_file,
                row.csv_row,
                row.classroom_name,
                row.classroom_email,
                "",
                "",
                CONFIDENCE_TEXT.get(row.confidence, row.confidence),
                "לא",
                "לא",
                row.note,
            ]
        )

    # Deduplicate rows just appended for unmatched classroom lines.
    seen_matches = set()
    unique_matches: list[list[object]] = []
    for row in matches:
        key = (row[1], row[2])
        if key in seen_matches:
            continue
        seen_matches.add(key)
        unique_matches.append(row)
    matches = unique_matches

    issues.sort(
        key=lambda row: (
            [item["class_label"] for item in classes].index(row[1]),
            STATUS_ORDER.index(row[0]) if row[0] in STATUS_ORDER else 9,
            str(row[2]),
        )
    )
    matches.sort(
        key=lambda row: (
            [item["class_label"] for item in classes].index(row[0]),
            str(row[1]),
            int(row[2]),
        )
    )
    students.sort(
        key=lambda student: (
            [item["class_label"] for item in classes].index(student.class_label),
            student.name,
        )
    )

    total_students = sum(item["alephon_students"] for item in classes)
    total_rows = sum(item["classroom_rows"] for item in classes)
    total_imported = sum(item["imported"] for item in classes)
    total_review = sum(item["review"] for item in classes)
    total_weak = sum(item["weak"] for item in classes)
    total_unconfirmed = sum(item["unconfirmed"] for item in classes)
    total_missing = sum(item["missing"] for item in classes)
    total_dup = sum(item["duplicate_rows"] for item in classes)
    total_classroom_students = sum(item["classroom_students"] for item in classes)
    total_grades = sum(item["grades"] for item in classes)
    rows_without_id = sum(1 for row in mapping_rows if not row.student_id)
    confirmed_rows = sum(1 for row in mapping_rows if row.confirmed)
    medium_rows = sum(
        1 for row in mapping_rows if row.confidence == CONFIDENCE_MEDIUM
    )
    low_rows = sum(1 for row in mapping_rows if row.confidence == CONFIDENCE_LOW)
    high_unconfirmed = sum(
        1
        for row in mapping_rows
        if row.confidence == CONFIDENCE_HIGH and not row.confirmed
    )

    totals = {
        "alephon_students": total_students,
        "classroom_rows": total_rows,
        "classroom_students": total_classroom_students,
        "imported": total_imported,
        "review": total_review,
        "weak": total_weak,
        "unconfirmed": total_unconfirmed,
        "missing": total_missing,
        "duplicate_rows": total_dup,
        "duplicate_students": sum(
            1
            for matched in rows_by_student.values()
            if len(matched) > 1
        ),
        "grades": total_grades,
        "coverage": (total_imported / total_students) if total_students else 0.0,
        "found_in_classroom": total_students - total_missing,
        "confirmed_rows": confirmed_rows,
        "high_unconfirmed_rows": high_unconfirmed,
        "medium_rows": medium_rows,
        "low_rows": low_rows,
        "rows_without_id": rows_without_id,
    }

    return {
        "generated_at": utcnow().strftime("%d/%m/%Y %H:%M"),
        "root": str(root),
        "mapping_file": str(mapping_path),
        "database": str(db_path),
        "totals": totals,
        "classes": classes,
        "students": [
            [
                student.class_label,
                student.national_id,
                student.name,
                student.status,
                student.classroom_names,
                student.confidence,
                student.rows,
                student.grades,
                student.note,
            ]
            for student in students
        ],
        "matches": matches,
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    try:
        report = build(args.root, args.mapping, args.db)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    totals = report["totals"]
    print(
        f"נכתב {args.out}\n"
        f"תלמידים: {totals['alephon_students']} · "
        f"מיובאים: {totals['imported']} · "
        f"לבדיקה: {totals['review']} · "
        f"חלשים: {totals['weak']} · "
        f"ללא התאמה: {totals['missing']} · "
        f"שורות: {totals['classroom_rows']} · "
        f"ציונים: {totals['grades']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
