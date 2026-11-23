"""Move one student's enrollment to a different class.

Used for corrections that the alephon cannot express (a student listed in one
folder who actually learns in another class).  The student account, national
id, grades and submissions are untouched; only the class enrollment and the
identity card's class label move.

Usage::

    # preview
    python -m portal.scripts.move_student_class \\
        --national-id 223441650 --from "ז'4" --to "ז'3"

    # write
    python -m portal.scripts.move_student_class \\
        --national-id 223441650 --from "ז'4" --to "ז'3" --apply
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.models import ClassGroup, Enrollment, ROLE_STUDENT, StudentRecord, User
from portal.sync.matching import class_key, normalize_class_name

DEFAULT_NATIONAL_ID = "223441650"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--national-id", default=DEFAULT_NATIONAL_ID)
    parser.add_argument("--from", dest="from_class", default="ז'4")
    parser.add_argument("--to", dest="to_class", default="ז'3")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_settings()
    engine = build_engine(cfg.database_url)
    init_db(engine)
    factory = build_session_factory(engine)
    try:
        with factory() as db:
            record = db.scalar(
                select(StudentRecord).where(
                    StudentRecord.national_id == args.national_id
                )
            )
            if record is None:
                print(f"error: לא נמצא תלמיד עם ת.ז {args.national_id}", file=sys.stderr)
                return 2
            user = db.get(User, record.student_id)
            if user is None or user.role != ROLE_STUDENT:
                print("error: החשבון אינו חשבון תלמיד", file=sys.stderr)
                return 2

            groups = {
                class_key(normalize_class_name(group.name)): group
                for group in db.scalars(select(ClassGroup)).all()
            }
            target = groups.get(class_key(normalize_class_name(args.to_class)))
            source = groups.get(class_key(normalize_class_name(args.from_class)))
            if target is None:
                print(f"error: הכיתה '{args.to_class}' לא נמצאה", file=sys.stderr)
                return 2
            if source is None:
                print(f"error: הכיתה '{args.from_class}' לא נמצאה", file=sys.stderr)
                return 2

            enrollments = db.scalars(
                select(Enrollment).where(Enrollment.student_id == user.id)
            ).all()
            source_enrollment = next(
                (row for row in enrollments if row.class_id == source.id), None
            )
            target_enrollment = next(
                (row for row in enrollments if row.class_id == target.id), None
            )
            print(
                f"תלמיד: {user.display_name} ({record.national_id}) · "
                f"כיתה באלפון: {record.class_label}"
            )
            print(
                f"מעבר: {source.name} -> {target.name} · "
                f"רישום במקור: {'כן' if source_enrollment else 'לא'} · "
                f"רישום ביעד: {'כן' if target_enrollment else 'לא'}"
            )
            if not args.apply:
                print("תצוגה מקדימה בלבד — לא שונה דבר.")
                return 0

            if source_enrollment is not None:
                db.delete(source_enrollment)
            if target_enrollment is None:
                db.add(Enrollment(class_id=target.id, student_id=user.id))
            record.class_label = target.name
            db.commit()
            print("השינוי נשמר.")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
