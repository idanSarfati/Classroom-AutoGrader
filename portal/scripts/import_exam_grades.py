"""Import exam/quiz grades from the per-class Classroom grade workbooks.

Each class folder under the teacher's ``אלפונים`` directory can hold one
"הזנת ציונים לאירוע ציון" workbook. This command turns each of them into one
grade-only assignment per class and one imported grade per student::

    # Preview what will be written (default; no DB writes).
    python -m portal.scripts.import_exam_grades \\
        --source "C:\\Users\\...\\אלפונים"

    # Write the assignments and grades.
    python -m portal.scripts.import_exam_grades \\
        --source "C:\\Users\\...\\אלפונים" --apply

Matching is by national id plus class enrollment, never by name. Blank score
cells are skipped (an absent grade is not a zero) and rerunning is safe:
an unchanged score is left alone, a changed score is updated in place, and a
grade that came from the portal grading engine is never overwritten.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.imports.exams import (
    ACTION_CONFLICT,
    ACTION_CREATE,
    ACTION_LABELS,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    ExamImportPlan,
    apply_exam_plan,
    build_exam_plan,
    discover_exam_sheets,
)
from portal.imports.sources import SourceError
from portal.sync.matching import class_key, normalize_class_name


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _filter_by_class(sheets, wanted: tuple[str, ...]):
    if not wanted:
        return sheets
    keys = {class_key(normalize_class_name(name)) for name in wanted}
    return [
        sheet
        for sheet in sheets
        if class_key(normalize_class_name(sheet.class_label)) in keys
    ]


def _print_plan(plan: ExamImportPlan, *, verbose: bool) -> None:
    created_assignments = sum(
        1 for item in plan.assignments if item.action == ACTION_CREATE
    )
    print(
        f"אירועי ציון: {len(plan.assignments)} "
        f"({created_assignments} חדשים, "
        f"{len(plan.assignments) - created_assignments} קיימים)"
    )
    print(
        f"ציונים: {plan.created} חדשים, {plan.updated} עדכונים, "
        f"{plan.unchanged} ללא שינוי, {plan.conflicts} דילוגים."
    )
    if plan.blank_scores:
        print(
            f"תאים ללא ציון בקובץ: {plan.blank_scores} — לא ייובאו "
            "(היעדר ציון אינו אפס)."
        )

    per_event: dict[tuple[str, str], dict[str, int]] = {}
    for item in plan.grades:
        if item.action == ACTION_CONFLICT:
            continue
        bucket = per_event.setdefault(
            (item.class_label, item.exam_title), {"grades": 0}
        )
        bucket["grades"] += 1
    if per_event:
        print()
        print("לפי כיתה ואירוע:")
        for (class_label, title), counts in sorted(per_event.items()):
            print(f"  • {class_label} · {title} — {counts['grades']} ציונים")

    if plan.issues:
        print()
        print("אזהרות:")
        for issue in plan.issues:
            print(f"  - {issue}")

    if verbose:
        print()
        for item in plan.grades:
            action = ACTION_LABELS.get(item.action, item.action)
            note = f" · {item.note}" if item.note else ""
            print(
                f"  • {item.class_label} · {item.student_name} · "
                f"{item.exam_title} — {item.score} ({action}){note}"
            )


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(
        description="Import exam/quiz grades from Classroom grade workbooks.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Folder that contains one sub-folder per class.",
    )
    parser.add_argument(
        "--class",
        dest="classes",
        default="",
        help="Comma-separated class filter (default: every folder found).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the assignments and grades. Without it: preview only.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="List every grade that will be written or skipped.",
    )
    args = parser.parse_args(argv)

    root = Path(args.source).expanduser()
    if not root.is_dir():
        print(f"error: התיקייה לא נמצאה: {root}", file=sys.stderr)
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
                )
                sheets = _filter_by_class(discover_exam_sheets(root), wanted)
                if not sheets:
                    print(
                        "error: לא נמצאו קבצי ציוני מבחן/בוחן מתאימים.",
                        file=sys.stderr,
                    )
                    return 2

                plan = build_exam_plan(db, sheets)
                print("מקור: " + str(root))
                print(
                    "כיתות: " + ", ".join(sorted({sheet.class_label for sheet in sheets}))
                )
                print()
                _print_plan(plan, verbose=args.verbose)

                if not args.apply:
                    print()
                    print(
                        "תצוגה מקדימה בלבד — לא נכתבו ציונים. "
                        "להרצה בפועל הוסיפו --apply."
                    )
                    return 0

                counts = apply_exam_plan(db, plan)
                print()
                print(
                    f"נשמרו: {counts['assignments']} אירועי ציון חדשים, "
                    f"{counts['created']} ציונים חדשים, "
                    f"{counts['updated']} עדכונים. "
                    f"דילוגים: {counts['conflicts']}."
                )
                print("ייבוא ציוני המבחנים הושלם.")
                return 0
            except (SourceError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
