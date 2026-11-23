"""Import the school's student directories (אלפון) into the portal.

Typical use::

    # 1. Preview: reads the files, writes only the mapping review CSV.
    python -m portal.scripts.import_students \\
        --source "C:\\Users\\...\\אלפונים"

    # 2. Create the accounts, enrollments and the review CSV.
    python -m portal.scripts.import_students \\
        --source "C:\\Users\\...\\אלפונים" --apply

    # 3. Correct the review CSV in Excel, then run import_grades.py.

Expected folder layout (one folder per class)::

    אלפונים/
      ז3/  <alephon>.xlsx   <classroom export>.csv
      ז4/  ...
      ...

The alephon supplies authoritative names and national ids; the Classroom CSV
is only read to build the name-matching review file. Grades are imported
separately by :mod:`portal.scripts.import_grades`.

The command never prints a password. New accounts receive random passwords
written to ``portal/data/student_credentials.csv`` (git-ignored), one row per
account, so the teacher can hand out slips.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.imports.matching import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    MappingRow,
    assign_class_label,
    match_sheet,
    write_mapping,
)
from portal.imports.sources import (
    SourceError,
    class_directories,
    load_class_folder,
)
from portal.imports.students import (
    StudentPlan,
    apply_student_plan,
    build_student_plan,
    write_credentials,
)
from portal.models import ClassGroup
from portal.paths import DATA_DIR
from portal.sync.matching import class_key, normalize_class_name


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _parse_wanted(raw: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    return values


def _print_plan(plan: StudentPlan, *, verbose: bool) -> None:
    per_class: dict[str, list] = {}
    for entry in plan.entries:
        per_class.setdefault(entry.group.name, []).append(entry)

    for name, entries in per_class.items():
        new = sum(1 for entry in entries if entry.creates_account)
        updated = sum(
            1
            for entry in entries
            if entry.changes and not entry.creates_account
        )
        enrollments = sum(1 for entry in entries if entry.enrolls)
        warnings = [entry for entry in entries if entry.warning]
        print(
            f"• {name} — {len(entries)} תלמידים: "
            f"{new} חשבונות חדשים, {updated} עדכונים, "
            f"{enrollments} שיוכים לכיתה"
        )
        for entry in warnings:
            print(f"  אזהרה: {entry.display_name} — {entry.warning}")
        if verbose:
            for entry in entries:
                if not entry.actionable:
                    continue
                actions = ", ".join(entry.changes) or "שיוך לכיתה"
                print(
                    f"    - {entry.display_name} "
                    f"({entry.national_id or 'ללא ת.ז'}) — {actions}"
                )
    for warning in plan.warnings:
        print(f"אזהרה: {warning}")
    print()
    print(
        f'סה"כ: {plan.new_accounts} חשבונות חדשים, '
        f"{plan.updated} עדכונים, {plan.new_enrollments} שיוכים לכיתה, "
        f"{plan.unchanged} ללא שינוי"
        + (f", {plan.skipped} דילוגים" if plan.skipped else "")
        + "."
    )


def _print_matching(rows: list[MappingRow]) -> None:
    counts = {
        level: sum(1 for row in rows if row.confidence == level)
        for level in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_NONE)
    }
    duplicates = sum(1 for row in rows if "שורת כפילות" in row.note)
    print()
    print(
        "התאמת שמות Classroom: "
        f"{counts[CONFIDENCE_HIGH]} אוטומטיות, "
        f"{counts[CONFIDENCE_MEDIUM]} לבדיקה, "
        f"{counts[CONFIDENCE_LOW]} חלשות, "
        f"{counts[CONFIDENCE_NONE]} ללא התאמה"
        + (f", {duplicates} כפילויות" if duplicates else "")
        + "."
    )


def _mapping_rows(
    db, directories: dict[str, Path]
) -> tuple[list, list[MappingRow]]:
    from portal.imports.sources import Alephon

    groups = {
        class_key(normalize_class_name(group.name)): group
        for group in db.scalars(select(ClassGroup)).all()
    }
    alephons: list[Alephon] = []
    rows: list[MappingRow] = []
    source_warnings: list[str] = []
    for key, folder in directories.items():
        alephon, sheets = load_class_folder(folder, folder.name)
        alephons.append(alephon)
        source_warnings.extend(alephon.warnings)
        group = groups.get(key)
        label = group.name if group is not None else normalize_class_name(alephon.class_label)
        for sheet in sheets:
            source_warnings.extend(sheet.warnings)
            match = match_sheet(alephon.students, sheet)
            rows.extend(assign_class_label(label, list(match.rows)))
    for warning in source_warnings:
        print(f"אזהרת מקור: {warning}")
    return alephons, rows


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(
        description="Import school student directories (אלפון).",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Folder that contains one sub-folder per class (ז3, ז4, ...).",
    )
    parser.add_argument(
        "--classes",
        default="",
        help="Comma-separated class filter (default: every folder found).",
    )
    parser.add_argument(
        "--mapping-out",
        default=str(DATA_DIR / "student_mapping.csv"),
        help="Where to write the name-matching review CSV.",
    )
    parser.add_argument(
        "--credentials-out",
        default=str(DATA_DIR / "student_credentials.csv"),
        help="Where to append credentials for accounts created by --apply.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the plan. Without it nothing in the database changes.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="List every student and the fields that will change.",
    )
    parser.add_argument(
        "--overwrite-mapping",
        action="store_true",
        help="Replace an existing review CSV (it may already be edited).",
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
                wanted = _parse_wanted(args.classes) or None
                directories = class_directories(root, wanted)
                if not directories:
                    print(
                        "error: לא נמצאו תיקיות כיתה בתיקיית המקור.",
                        file=sys.stderr,
                    )
                    return 2
                print(
                    "כיתות שנמצאו: " + ", ".join(directories)
                    + f" ({len(directories)})"
                )

                alephons, mapping_rows = _mapping_rows(db, directories)
                plan = build_student_plan(db, alephons)
                print()
                _print_plan(plan, verbose=args.verbose)
                _print_matching(mapping_rows)

                mapping_path = Path(args.mapping_out)
                if mapping_path.is_file() and mapping_path.stat().st_size > 0:
                    if args.overwrite_mapping:
                        write_mapping(mapping_path, mapping_rows)
                        print(f"קובץ התאמה נכתב מחדש: {mapping_path}")
                    else:
                        print(
                            f"קובץ התאמה קיים ולא נדרס: {mapping_path} "
                            "(הוסיפו --overwrite-mapping כדי ליצור מחדש)."
                        )
                else:
                    write_mapping(mapping_path, mapping_rows)
                    print(f"קובץ התאמה לבדיקה: {mapping_path}")
                print(
                    "  אשרו רק שורות שמתאימות; שורה ללא ת.ז לא תיובא. "
                    "אפשר לתקן student_id או confirmed (yes/no)."
                )

                if not args.apply:
                    print()
                    print(
                        "תצוגה מקדימה בלבד — לא נכתבו חשבונות. "
                        "להרצה בפועל הוסיפו --apply."
                    )
                    return 0

                print(
                    "  כותב לבסיס הנתונים: "
                    f"{plan.new_accounts} חשבונות חדשים, "
                    f"{plan.updated} עדכונים, "
                    f"{plan.new_enrollments} שיוכים..."
                )
                credentials = apply_student_plan(db, plan)
                if credentials:
                    credentials_path = Path(args.credentials_out)
                    write_credentials(credentials_path, credentials)
                    print()
                    print(
                        f"נוצרו {len(credentials)} חשבונות. "
                        f"סיסמאות נשמרו בקובץ: {credentials_path}"
                    )
                    print(
                        "  הקובץ מכיל סיסמאות בטקסט גלוי ולכן אינו נכנס ל-git. "
                        "העבירו את הפרטים לתלמידים ומחקו אותו."
                    )
                else:
                    print()
                    print("לא נוצרו חשבונות חדשים; לא נכתב קובץ סיסמאות.")
                print("הייבוא הושלם.")
                return 0
            except (SourceError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
