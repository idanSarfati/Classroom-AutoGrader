"""Import historical Classroom grades after reviewing the name mapping.

Run ``import_students.py`` first: it creates the students, enrollments and the
review CSV. Then correct the review CSV and run::

    # Preview the grades that will be written (default; no DB writes).
    python -m portal.scripts.import_grades \\
        --source "C:\\Users\\...\\אלפונים"

    # Write them.
    python -m portal.scripts.import_grades \\
        --source "C:\\Users\\...\\אלפונים" --apply

Only rows with ``confirmed=yes`` and a national id are used. A column is
imported only when its title matches a portal assignment in the same class;
old quarters, exams and letter-grade columns are listed as skipped instead of
being guessed into the wrong assignment.

Rerunning is safe: historical grades are refreshed in place, while a grade
that already came from the portal grading engine is never overwritten.

``--exclude-title "0.3,0.4"`` leaves whole assignments out of the run — useful
for material that is still exported by Classroom but no longer taught.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.imports.grades import (
    ACTION_CONFLICT,
    ACTION_CREATE,
    ACTION_LABELS,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    GradeImportPlan,
    apply_grade_plan,
    build_grade_plan,
)
from portal.imports.matching import MappingRow, read_mapping
from portal.imports.sources import SourceError, class_directories, load_class_sheets
from portal.paths import DATA_DIR
from portal.sync.matching import class_key


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _print_plan(plan: GradeImportPlan, rows: list[MappingRow], *, verbose: bool) -> None:
    confirmed = sum(1 for row in rows if row.usable)
    print(
        f"שורות מאושרות בקובץ ההתאמה: {confirmed} מתוך {len(rows)}"
    )
    print(f"עמודות מטלה שתואמו למטלות בפורטל: {plan.matched_columns}")
    print(
        f"ציונים: {plan.created} חדשים, {plan.updated} עדכונים, "
        f"{plan.unchanged} ללא שינוי, {plan.conflicts} דילוגים."
    )
    drafts = sum(
        1
        for item in plan.grades
        if item.action in (ACTION_CREATE, ACTION_UPDATE)
        and not item.assignment_published
    )
    if drafts:
        published: dict[str, int] = {}
        draft_by_class: dict[str, int] = {}
        for item in plan.grades:
            if item.action not in (ACTION_CREATE, ACTION_UPDATE):
                continue
            if item.assignment_published:
                published[item.class_label] = published.get(item.class_label, 0) + 1
            else:
                draft_by_class[item.class_label] = (
                    draft_by_class.get(item.class_label, 0) + 1
                )
        print(
            f"  מתוכם {drafts} ציונים למטלות שטרם פורסמו; "
            "הם נשמרים אך יופיעו בספר הציונים ובפרופיל רק לאחר פרסום המטלה."
        )
        for name in sorted(draft_by_class):
            print(
                f"    {name}: {published.get(name, 0)} למטלות מפורסמות, "
                f"{draft_by_class[name]} לטיוטות."
            )

    if plan.skipped_columns:
        print()
        print("עמודות עם ציונים שלא יובאו (אין מטלה תואמת):")
        for item in plan.skipped_columns:
            print(
                f"  • {item.class_label} · {item.title} — "
                f"{item.numeric_cells} ציונים · {item.reason}"
            )

    if plan.issues:
        print()
        print("אזהרות:")
        for issue in dict.fromkeys(plan.issues):
            print(f"  - {issue}")

    if verbose:
        print()
        for item in plan.grades:
            action = ACTION_LABELS.get(item.action, item.action)
            note = f" · {item.note}" if item.note else ""
            published = "" if item.assignment_published else " · טיוטה"
            print(
                f"  • {item.class_label} · {item.student_name} · "
                f"{item.assignment_title} — {item.score} "
                f"({action}{published}){note}"
            )


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(
        description="Import historical Classroom grades from reviewed CSVs.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Folder that contains one sub-folder per class.",
    )
    parser.add_argument(
        "--mapping",
        default=str(DATA_DIR / "student_mapping.csv"),
        help="Reviewed mapping CSV produced by import_students.py.",
    )
    parser.add_argument(
        "--classes",
        default="",
        help="Comma-separated class filter (default: every folder found).",
    )
    parser.add_argument(
        "--exclude-title",
        action="append",
        default=[],
        metavar="PREFIX",
        help=(
            "Skip every assignment whose title starts with PREFIX. "
            "Repeatable; also accepts a comma-separated list."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the grades. Without it the command only previews.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="List every grade that will be written or skipped.",
    )
    args = parser.parse_args(argv)

    root = Path(args.source).expanduser()
    mapping_path = Path(args.mapping)
    if not root.is_dir():
        print(f"error: התיקייה לא נמצאה: {root}", file=sys.stderr)
        return 2
    if not mapping_path.is_file():
        print(
            f"error: קובץ ההתאמה לא נמצא: {mapping_path}\n"
            "הריצו קודם: python -m portal.scripts.import_students",
            file=sys.stderr,
        )
        return 2

    cfg = load_settings()
    engine = build_engine(cfg.database_url)
    init_db(engine)
    factory = build_session_factory(engine)
    try:
        with factory() as db:
            try:
                wanted = tuple(
                    value.strip()
                    for value in args.classes.split(",")
                    if value.strip()
                ) or None
                directories = class_directories(root, wanted)
                if not directories:
                    print(
                        "error: לא נמצאו תיקיות כיתה בתיקיית המקור.",
                        file=sys.stderr,
                    )
                    return 2
                allowed = {class_key(name) for name in directories}
                rows = [
                    row
                    for row in read_mapping(mapping_path)
                    if not allowed or class_key(row.class_label) in allowed
                ]
                sheets = []
                for key, folder in directories.items():
                    sheets.extend(load_class_sheets(folder, folder.name))

                exclude_titles = tuple(
                    value.strip()
                    for item in args.exclude_title
                    for value in item.split(",")
                    if value.strip()
                )
                plan = build_grade_plan(
                    db, sheets, rows, exclude_titles=exclude_titles
                )
                print(f"קובץ התאמה: {mapping_path}")
                print(
                    "כיתות: " + ", ".join(directories)
                    + f" ({len(directories)})"
                )
                print()
                _print_plan(plan, rows, verbose=args.verbose)

                if not args.apply:
                    print()
                    print(
                        "תצוגה מקדימה בלבד — לא נכתבו ציונים. "
                        "להרצה בפועל הוסיפו --apply."
                    )
                    return 0

                counts = apply_grade_plan(db, plan)
                print()
                print(
                    f"נשמרו: {counts['created']} ציונים חדשים, "
                    f"{counts['updated']} עדכונים. "
                    f"דילוגים: {counts['conflicts']}."
                )
                print("ייבוא הציונים הושלם.")
                return 0
            except (SourceError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
