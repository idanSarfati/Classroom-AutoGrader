"""Repoint existing exam assignments at a different exam definition, in place.

Used when the paper a class was given turned out to be the wrong level and the
teacher replaces it before anyone sat it. The assignment row is the identity:
the class, the time window, the release state and the published flag stay
untouched. Only the exam name, the inherited instructions and the pinned rubric
are rewritten - the three fields a student's paper is built from.

Safety rules, checked before anything is written:

* the assignment must exist and must already be an exam (``exam_config`` set);
* it must have **no submissions** and **no drawn exam versions**
  (``portal_exam_allocations``) and **no personal windows** - otherwise a
  student has already opened the old paper, and changing it would move their
  answers under different questions.

Dry-run by default; ``--apply`` writes. With the server stopped, like the other
operational scripts, so no worker races the update.

Usage::

    python -m portal.scripts.replace_assignment_exam --exam unit3_input_easy \\
        --assignments 69,70
    python -m portal.scripts.replace_assignment_exam --exam unit3_input_easy \\
        --assignments 69,70 --apply
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, select

from portal.config import load_settings
from portal.db import build_engine, build_session_factory
from portal.exams.config import load_exam
from portal.grading import adapter
from portal.models import (
    Assignment,
    ExamAllocation,
    ExamStudentWindow,
    Submission,
)


def _ids(raw: str) -> list[int]:
    try:
        values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError:
        raise SystemExit("--assignments takes database ids, e.g. 69,70")
    if not values:
        raise SystemExit("no assignments given")
    return values


def _count(db, model, assignment_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(model)
            .where(model.assignment_id == assignment_id)
        )
        or 0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exam", required=True)
    parser.add_argument("--assignments", required=True, help="database ids, comma separated")
    parser.add_argument(
        "--title",
        default=None,
        help="optional new title; the current title is kept when omitted",
    )
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    config = load_exam(args.exam)
    if config is None:
        print(f"exam {args.exam!r} is missing or invalid; nothing changed.")
        return 2
    rubric = config.versions[0]
    rubric_path = rubric.rubric
    rubric_sha256 = adapter.hash_file(rubric.rubric_path)

    settings = load_settings()
    engine = build_engine(args.database_url or settings.database_url)
    factory = build_session_factory(engine)
    try:
        with factory() as db:
            assignments = []
            problems: list[str] = []
            allowed_models = (
                ("הגשות", Submission),
                ("גרסאות מבחן שנמשכו", ExamAllocation),
                ("חלונות אישיים", ExamStudentWindow),
            )
            for assignment_id in _ids(args.assignments):
                assignment = db.get(Assignment, assignment_id)
                if assignment is None:
                    problems.append(f"מטלה {assignment_id}: לא נמצאה")
                    continue
                if not str(assignment.exam_config or "").strip():
                    problems.append(
                        f"מטלה {assignment_id}: אינה מטלת מבחן (אין exam_config)"
                    )
                    continue
                blocked = [
                    f"{label}: {count}"
                    for label, model in allowed_models
                    if (count := _count(db, model, assignment_id))
                ]
                if blocked:
                    problems.append(
                        f"מטלה {assignment_id}: כבר יש פעילות ({', '.join(blocked)}) "
                        "- לא מחליפים בוחן מתחת לתלמיד/ה שפתח/ה אותו"
                    )
                    continue
                assignments.append(assignment)

            print(f"exam: {config.name} ({len(config.versions)} versions, "
                  f"{config.total_points} points)")
            print(f"rubric: {rubric_path}")
            if args.title:
                print(f"title:  {args.title}")
            print()

            for assignment in assignments:
                print(
                    f"  assignment {assignment.id}: {assignment.exam_config} -> "
                    f"{config.name} (published={assignment.published})"
                )

            if problems:
                print()
                for problem in problems:
                    print(f"  REFUSED: {problem}")
                print("\nnothing changed.")
                return 1

            if not args.apply:
                print(f"\ndry-run: {len(assignments)} to update. Add --apply.")
                return 0

            for assignment in assignments:
                assignment.exam_config = config.name
                assignment.instructions = config.instructions
                assignment.rubric_path = rubric_path
                assignment.rubric_sha256 = rubric_sha256
                if args.title:
                    assignment.title = args.title
            db.commit()
            print(f"\napplied: {len(assignments)} updated.")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
