"""Create the live exam assignment for the classes sitting an exam.

An exam assignment is an ordinary :class:`~portal.models.Assignment` with
``exam_config`` set: the portal then gives every student their own version and
order inside the window, and marks the hand-in with the answer key. Creating it
by hand would mean typing the window, the rubric path and the exam name - three
places to get wrong - so this script writes all of them together.

The script is **idempotent**. ``exam_config`` on a class is the identity, so
running it twice never duplicates a row; with ``--update-window`` it only
changes the opening/closing times of the exam assignments it already finds.

Dry-run by default, like the other operational scripts: it prints the plan and
writes nothing until ``--apply`` is given (with the server stopped, so the
running worker cannot race the insert).

Usage::

    python -m portal.scripts.create_exam_assignments --classes 3,4,7,8 \
        --opens 2026-11-11T00:00 --closes 2026-11-12T23:59
    python -m portal.scripts.create_exam_assignments --classes 3,4,7,8 \
        --opens 2026-11-11T00:00 --closes 2026-11-12T23:59 --apply

``--classes`` accepts database ids (``3``) and class names (``ח'3``), mixed.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from portal.config import load_settings
from portal.db import build_engine, build_session_factory
from portal.exams.config import load_exam
from portal.grading import adapter
from portal.models import Assignment, ClassGroup
from portal.timeutil import format_local, parse_local_datetime

DEFAULT_EXAM = "units_1_3"
DEFAULT_TITLE = "3.9 | מבחן מסכם - יחידות 1-3"


def _resolve_classes(db, raw: str) -> list[ClassGroup]:
    """Turn ``3,ח'3`` into class rows; an unknown token aborts the whole run."""
    prefixes = ("", "כיתה ", "כיתת ")
    groups: list[ClassGroup] = []
    seen: set[int] = set()
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        group = None
        if token.isdigit():
            group = db.get(ClassGroup, int(token))
        if group is None:
            for prefix in prefixes:
                name = prefix + token
                group = db.scalar(
                    select(ClassGroup).where(ClassGroup.name == name)
                )
                if group is not None:
                    break
        if group is None:
            raise SystemExit(f"class {token!r} was not found")
        if group.id not in seen:
            seen.add(group.id)
            groups.append(group)
    if not groups:
        raise SystemExit("no classes given")
    return groups


def _window(args, settings) -> tuple:
    opens = parse_local_datetime(args.opens, settings.timezone)
    closes = parse_local_datetime(args.closes, settings.timezone)
    if opens is None or closes is None:
        raise SystemExit("--opens and --closes are required (YYYY-MM-DDTHH:MM)")
    if closes <= opens:
        raise SystemExit("--closes must be later than --opens")
    return opens, closes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exam", default=DEFAULT_EXAM)
    parser.add_argument("--classes", required=True, help="ids and/or names, comma separated")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--opens", required=True, help="local wall clock, e.g. 2026-11-11T00:00")
    parser.add_argument("--closes", required=True, help="local wall clock, e.g. 2026-11-12T23:59")
    parser.add_argument(
        "--update-window",
        action="store_true",
        help="also rewrite opens_at/closes_at on an existing exam assignment",
    )
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    config = load_exam(args.exam)
    if config is None:
        print(f"exam {args.exam!r} is missing or invalid; nothing created.")
        return 2

    settings = load_settings()
    opens, closes = _window(args, settings)
    rubric = config.versions[0]
    rubric_path = rubric.rubric
    rubric_sha256 = adapter.hash_file(rubric.rubric_path)

    engine = build_engine(args.database_url or settings.database_url)
    factory = build_session_factory(engine)
    created = updated = unchanged = 0
    try:
        with factory() as db:
            groups = _resolve_classes(db, args.classes)
            print(
                f"exam: {config.name} ({len(config.versions)} versions, "
                f"{config.total_points} points)"
            )
            print(
                "window: "
                f"{format_local(opens, settings.timezone)} -> "
                f"{format_local(closes, settings.timezone)} ({settings.timezone})"
            )
            print(f"title: {args.title}")
            print(f"rubric: {rubric_path}")
            print()

            for group in groups:
                existing = db.scalar(
                    select(Assignment).where(
                        Assignment.class_id == group.id,
                        Assignment.exam_config == args.exam,
                    )
                )
                if existing is not None:
                    changed = args.update_window and (
                        existing.opens_at != opens or existing.closes_at != closes
                    )
                    if changed:
                        existing.opens_at = opens
                        existing.closes_at = closes
                        updated += 1
                        action = "update window"
                    else:
                        unchanged += 1
                        action = "unchanged"
                    print(
                        f"  {group.name:<8} assignment {existing.id}: {action}"
                        f" (published={existing.published})"
                    )
                    continue

                clash = db.scalar(
                    select(Assignment).where(
                        Assignment.class_id == group.id,
                        Assignment.title == args.title,
                        Assignment.exam_config == "",
                    )
                )
                if clash is not None:
                    print(
                        f"  {group.name:<8} REFUSED: assignment {clash.id} already "
                        f"uses this title without an exam config"
                    )
                    continue

                if args.apply:
                    assignment = Assignment(
                        class_id=group.id,
                        title=args.title,
                        instructions=config.instructions,
                        rubric_path=rubric_path,
                        rubric_sha256=rubric_sha256,
                        exam_config=config.name,
                        opens_at=opens,
                        closes_at=closes,
                        published=True,
                    )
                    db.add(assignment)
                    db.flush()
                    print(
                        f"  {group.name:<8} created assignment {assignment.id}"
                    )
                else:
                    print(f"  {group.name:<8} would create a published exam assignment")
                created += 1

            if args.apply:
                db.commit()
                print(f"\napplied: {created} created, {updated} updated.")
            else:
                print(f"\ndry-run: {created} to create, {updated} to update. Add --apply.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
