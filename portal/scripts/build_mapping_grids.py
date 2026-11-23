"""Turn the mapping report JSON into the grids the workbook writer renders.

The report script (``build_mapping_report``) produces the facts; this script
lays them out as the four sheets of the Excel workbook. Keeping the layout
here means the workbook can be rebuilt from the JSON alone, without touching
the database except for one read-only count of draft grades.

Usage::

    python -m portal.scripts.build_mapping_grids
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_REPORT = DATA_DIR / "student_mapping_report.json"
DEFAULT_OUT = DATA_DIR / "student_mapping_report_grids.json"

STUDENTS_HEADER = [
    "כיתה",
    "ת.ז",
    "שם באלפון",
    "סטטוס",
    "שם ב-Classroom",
    "רמת ודאות",
    "שורות Classroom",
    "ציונים שיובאו",
]

MATCHES_HEADER = [
    "כיתה",
    "קובץ CSV",
    "שורה",
    "שם ב-Classroom",
    "ת.ז מותאמת",
    "שם באלפון",
    "רמת ודאות",
    "יובא",
]

ISSUES_HEADER = [
    "סוג",
    "כיתה",
    "שם",
    "ת.ז",
    "שם ב-Classroom",
    "רמת ודאות",
    "הערה",
]

CLASS_HEADER = [
    "כיתה",
    "תלמידי אלפון",
    "שורות Classroom",
    "תלמידים ב-Classroom",
    "יובאו",
    "לבדיקה",
    "התאמה חלשה",
    "לא נמצאו",
    "שורות כפולות",
    "ציונים שיובאו",
    "כיסוי",
]

LEGEND = [
    ("מיובא", "התאמה שאושרה; הציונים נכתבו לפרופיל התלמיד בפורטל."),
    (
        "ודאות גבוהה (מאושר)",
        "ההתאמה אושרה ידנית על ידי המורה לאחר בדיקת השמות.",
    ),
    (
        "שורת כפילות",
        "אותו תלמיד מופיע ביותר משורה אחת; הציונים מכל השורות מוזגו לפרופיל אחד.",
    ),
    (
        "לא נמצא ב-Classroom",
        "לא קיימת שורת תלמיד תואמת בקובץ הציונים של הכיתה.",
    ),
    (
        "שורה ללא התאמה",
        "שורת Classroom שלא שויכה לאף תלמיד באלפון; לא יובאה.",
    ),
]


def _percent(part: int, whole: int) -> str:
    if not whole:
        return "0.0%"
    return f"{part / whole * 100:.1f}%"


def _draft_counts(db_path: str, total: int) -> tuple[int, int]:
    """Return ``(published, draft)`` historical grades, read-only."""
    if not db_path or not Path(db_path).is_file():
        return total, 0
    try:
        connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return total, 0
    try:
        rows = connection.execute(
            "SELECT a.published, COUNT(*) "
            "FROM portal_grades g "
            "JOIN portal_submissions s ON s.id = g.submission_id "
            "JOIN portal_assignments a ON a.id = s.assignment_id "
            "WHERE g.configured_model = 'classroom-import' "
            "GROUP BY a.published"
        ).fetchall()
    except sqlite3.Error:
        return total, 0
    finally:
        connection.close()
    counts = {bool(published): int(count) for published, count in rows}
    published = counts.get(True, 0)
    draft = counts.get(False, 0)
    if published + draft != total:
        return total, 0
    return published, draft


def _cross_class_note(students: list[list[object]]) -> str:
    moved: list[str] = []
    for student in students:
        class_label, name, note = student[0], student[2], student[8]
        if "שורת Classroom נמצאת בכיתה" in str(note):
            moved.append(f"{name} ({class_label}) — {note}")
    if not moved:
        return "כל תלמידי האלפון שויכו לכיתה אחת; לא בוצעו מעברי כיתה."
    return "מעברי כיתה: " + " · ".join(moved)


def build(report: dict) -> dict:
    totals = report["totals"]
    classes = report["classes"]
    student_rows = report["students"]
    total_students = int(totals["alephon_students"])

    summary: list[list[object]] = []
    blank = [None] * 11

    class_names = ", ".join(item["class_label"] for item in classes)
    summary.append(
        [
            "דוח התאמת שמות — אלפון מול קובצי הציונים של Google Classroom",
            *blank[1:],
        ]
    )
    summary.append(
        [
            f"הופק ב-{report['generated_at']} · {len(classes)} כיתות · "
            "מקור: תיקיית אלפונים, student_mapping.csv ובסיס הנתונים של הפורטל",
            *blank[1:],
        ]
    )
    summary.append(list(blank))
    summary.append(["סיכום כללי", *blank[1:]])
    summary.append(["מדד", "ערך", "פירוש", *blank[3:]])

    duplicate_unconfirmed = (
        int(totals["high_unconfirmed_rows"])
        + int(totals["medium_rows"])
        + int(totals["low_rows"])
    )
    rows_without_id = int(totals["rows_without_id"])
    published, draft = _draft_counts(report.get("database", ""), int(totals["grades"]))

    summary.append(
        [
            "תלמידים באלפון",
            total_students,
            f"{len(classes)} כיתות: {class_names}",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "שורות תלמיד בקובצי הציונים",
            int(totals["classroom_rows"]),
            f"כולל {int(totals['duplicate_rows'])} שורות כפולות",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "תלמידים שהופיעו לפחות פעם אחת",
            int(totals["found_in_classroom"]),
            f"{_percent(int(totals['found_in_classroom']), total_students)} מתלמידי האלפון",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "יובאו בהצלחה (התאמה מאושרת)",
            int(totals["imported"]),
            f"{_percent(int(totals['imported']), total_students)} מתלמידי האלפון · "
            f"{int(totals['grades'])} ציונים נכתבו",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "ממתינים לבדיקה (ודאות בינונית)",
            int(totals["review"]),
            "לא יובאו ציונים עד לאישור ידני",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "התאמה חלשה (ודאות נמוכה)",
            int(totals["weak"]),
            "לא יובאו ציונים",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "לא נמצאו בקובץ הציונים",
            int(totals["missing"]),
            f"{_percent(int(totals['missing']), total_students)} מתלמידי האלפון",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "שורות כפילות",
            int(totals["duplicate_rows"]),
            f"{int(totals['duplicate_students'])} תלמידים עם יותר משורה אחת",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "ציונים היסטוריים שיובאו",
            int(totals["grades"]),
            "כל הציונים מקורם ב-Google Classroom",
            *blank[3:],
        ]
    )
    summary.append(
        [
            "שורות Classroom ללא ת.ז",
            rows_without_id,
            (
                "כל השורות הותאמו או סומנו"
                if rows_without_id == 0
                else f"{rows_without_id} שורות לא משויכות לתלמיד ולא יובאו"
            ),
            *blank[3:],
        ]
    )

    summary.append(list(blank))
    summary.append(["פירוט לפי כיתה", *blank[1:]])
    summary.append([*CLASS_HEADER])

    for item in classes:
        students_count = int(item["alephon_students"])
        summary.append(
            [
                item["class_label"],
                students_count,
                int(item["classroom_rows"]),
                int(item["classroom_students"]),
                int(item["imported"]),
                int(item["review"]),
                int(item["weak"]),
                int(item["missing"]),
                int(item["duplicate_rows"]),
                int(item["grades"]),
                (int(item["imported"]) / students_count) if students_count else 0.0,
            ]
        )
    summary.append(
        [
            'סה"כ',
            total_students,
            int(totals["classroom_rows"]),
            int(totals["classroom_students"]),
            int(totals["imported"]),
            int(totals["review"]),
            int(totals["weak"]),
            int(totals["missing"]),
            int(totals["duplicate_rows"]),
            int(totals["grades"]),
            (int(totals["imported"]) / total_students) if total_students else 0.0,
        ]
    )

    summary.append(list(blank))
    summary.append(["מקרא", *blank[1:]])
    for label, description in LEGEND:
        summary.append([label, description, *blank[2:]])

    summary.append(list(blank))
    summary.append(["הערות", *blank[1:]])
    summary.append(
        [
            "1",
            "הדוח מבוסס על קובץ המיפוי המאושר portal/data/student_mapping.csv, "
            "על קובצי האלפון המקוריים ועל בסיס הנתונים של הפורטל.",
            *blank[2:],
        ]
    )
    summary.append(
        [
            "2",
            f"{duplicate_unconfirmed} שורות בעלות ודאות גבוהה או בינונית לא אושרו שוב "
            f"משום שהן כפילות של תלמיד שכבר אושר; {rows_without_id} שורות ללא ת.ז "
            "סומנו כשקיפות ולא יובאו.",
            *blank[2:],
        ]
    )
    summary.append(
        [
            "3",
            f"{published} מתוך {int(totals['grades'])} הציונים מוצגים במטריצת הציונים; "
            f"{draft} ציוני ז'3 שייכים למטלות במצב טיוטה ויוצגו לאחר פרסומן.",
            *blank[2:],
        ]
    )
    summary.append(
        [
            "4",
            "ציונים היסטוריים יובאו ללא טקסט הגשה, ולכן אינם ניתנים לבדיקה מחדש "
            "במנוע הבדיקה.",
            *blank[2:],
        ]
    )
    summary.append(["5", _cross_class_note(student_rows), *blank[2:]])

    students = [STUDENTS_HEADER] + [
        [
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            int(row[6]),
            int(row[7]),
        ]
        for row in student_rows
    ]
    matches = [MATCHES_HEADER] + [
        [row[0], row[1], int(row[2]), row[3], row[5], row[6], row[7], row[9]]
        for row in report["matches"]
    ]
    issues = [ISSUES_HEADER] + [list(row) for row in report["issues"]]

    return {
        "summary": summary,
        "students": students,
        "matches": matches,
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        grids = build(report)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(grids, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"נכתב {args.out}\n"
        f"סיכום: {len(grids['summary'])} שורות · "
        f"תלמידים: {len(grids['students']) - 1} · "
        f"התאמות: {len(grids['matches']) - 1} · "
        f"בעיות: {len(grids['issues']) - 1}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
