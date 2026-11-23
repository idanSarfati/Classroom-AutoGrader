"""Initialize the school classes and replicate the reference class content.

Examples::

    # Preview only (default). Nothing is written.
    python -m portal.scripts.sync_classes

    # Create the missing classes, link each to its Classroom course, copy ז'3
    # and publish only what each target course already has published.
    python -m portal.scripts.sync_classes --apply

    # Keep everything as a draft in two classes, publish everything in another.
    python -m portal.scripts.sync_classes --apply \\
        --class-mode "ח3=draft" --class-mode "ט2=published"

    # Force one lesson published and another kept as a draft in every class.
    python -m portal.scripts.sync_classes --apply \\
        --publish "3.2" --draft "תרגול יחידות 1-2"

Publication modes: ``classroom`` (default), ``draft``, ``published``,
``source``. The run is idempotent: re-running it creates nothing new and only
updates publication flags, so it is safe to use as a weekly progress sync.

The command uses the teacher's last Classroom catalog scan. Pass ``--refresh``
to scan Google first (slow, needs Google credentials).
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.ingestion import catalog
from portal.models import ROLE_TEACHER, User
from portal.sync import (
    MODES,
    TARGET_CLASSES,
    SyncError,
    apply_plan,
    build_plan,
)
from portal.validation import normalize_username


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _teacher(db, username: str | None) -> User:
    if username:
        teacher = db.scalar(
            select(User).where(
                User.username == normalize_username(username),
                User.role == ROLE_TEACHER,
            )
        )
        if teacher is None:
            raise SyncError(f"המורה '{username}' לא נמצא.")
        return teacher
    teachers = list(
        db.scalars(select(User).where(User.role == ROLE_TEACHER)).all()
    )
    if not teachers:
        raise SyncError(
            "אין חשבון מורה בפורטל. הריצו קודם "
            "python -m portal.scripts.bootstrap_teacher."
        )
    if len(teachers) > 1:
        names = ", ".join(teacher.username for teacher in teachers)
        raise SyncError(
            "יש כמה חשבונות מורה; בחרו אחד עם --teacher. "
            f"אפשרויות: {names}."
        )
    return teachers[0]


def _parse_targets(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return TARGET_CLASSES
    values = tuple(
        value.strip() for value in raw.split(",") if value.strip()
    )
    return values or TARGET_CLASSES


def _parse_class_modes(values: list[str]) -> dict[str, str]:
    modes: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SyncError(
                f"פורמט שגוי ל---class-mode: '{value}'. השתמשו בכיתה=מצב."
            )
        name, mode = (part.strip() for part in value.split("=", 1))
        if not name:
            raise SyncError("חסר שם כיתה ב---class-mode.")
        if mode not in MODES:
            raise SyncError(
                f"מצב פרסום לא מוכר '{mode}'. האפשרויות: {', '.join(MODES)}."
            )
        modes[name] = mode
    return modes


def _print_plan(plan, *, verbose: bool) -> None:
    print(f"תוכן להעתקה: {plan.source_lessons} שיעורים, "
          f"{plan.source_assignments} מטלות")
    synced = (
        plan.catalog_synced_at.strftime("%d/%m/%Y %H:%M")
        if plan.catalog_synced_at
        else "לא ידוע"
    )
    print(f"קטלוג Classroom: {plan.catalog_status or 'ריק'} (סריקה: {synced})")
    if plan.catalog_error:
        print(f"אזהרת קטלוג: {plan.catalog_error}")
    print()

    for item in plan.classes:
        if item.is_source:
            continue
        if item.error:
            print(f"• {item.display_name} — שגיאה: {item.error}")
            continue
        changes = []
        if not item.class_exists:
            changes.append("כיתה תיווצר")
        if item.link_will_create:
            changes.append("קישור Classroom ייווצר")
        if item.link_will_update:
            changes.append("קישור Classroom יתעדכן")
        state = ", ".join(changes) if changes else "מוכן"
        print(f"• {item.display_name} — {state}")
        print(f"  קורס: {item.course_name or '—'} ({item.course_id})")
        print(
            f"  ייוצרו: {item.new_lessons} שיעורים, "
            f"{item.new_assignments} מטלות | "
            f"יפורסמו: {item.publish_count} | טיוטות: {item.draft_count}"
        )
        for warning in item.warnings:
            print(f"  אזהרה: {warning}")
        if verbose:
            for planned in item.items:
                classroom = (
                    f", Classroom: {planned.classroom_state}"
                    if planned.classroom_state
                    else ""
                )
                print(
                    f"    - [{planned.kind}] {planned.title} — "
                    f"{planned.action_label}, {planned.state_label}"
                    f"{classroom}"
                )
        print()

    print(
        f"סה\"כ: {plan.classes_to_create} כיתות חדשות, "
        f"{plan.items_to_create} פריטים חדשים, "
        f"{plan.items_to_publish} פריטים שיפורסמו, "
        f"{plan.items_to_keep_draft} פריטים כטיוטה."
    )
    if plan.errors:
        print("שגיאות:")
        for error in plan.errors:
            print(f"  - {error}")


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(
        description="Initialize classes and replicate the reference class."
    )
    parser.add_argument("--teacher", help="Username of the teacher account.")
    parser.add_argument("--source", default="ז3", help="Reference class (default: ז3).")
    parser.add_argument(
        "--classes",
        default="",
        help="Comma-separated target classes (default: all supported).",
    )
    parser.add_argument(
        "--mode",
        default="classroom",
        choices=MODES,
        help="Default publication mode (default: classroom).",
    )
    parser.add_argument(
        "--class-mode",
        action="append",
        default=[],
        metavar="CLASS=MODE",
        help="Per-class publication mode; repeatable.",
    )
    parser.add_argument(
        "--publish",
        action="append",
        default=[],
        metavar="TEXT",
        help="Force items whose title contains TEXT to be published.",
    )
    parser.add_argument(
        "--draft",
        action="append",
        default=[],
        metavar="TEXT",
        help="Force items whose title contains TEXT to stay a draft.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Scan Google Classroom first (slow, needs Google credentials).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the plan. Without it the command only previews.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="List every lesson and assignment in the preview.",
    )
    args = parser.parse_args(argv)

    cfg = load_settings()
    engine = build_engine(cfg.database_url)
    init_db(engine)
    factory = build_session_factory(engine)
    try:
        with factory() as db:
            try:
                teacher = _teacher(db, args.teacher)
                targets = _parse_targets(args.classes)
                class_modes = _parse_class_modes(args.class_mode)
                if args.refresh:
                    print("סורק את Google Classroom… (עשוי לקחת מספר דקות)")
                    from portal.ingestion.classroom_source import ClassroomSource

                    try:
                        counts = catalog.refresh(db, ClassroomSource(), teacher.id)
                        print(
                            "הסריקה הושלמה: "
                            f"{counts['coursework']} פריטים בקורסים שנסרקו."
                        )
                    except Exception as exc:  # noqa: BLE001 - keep cached data
                        print(f"אזהרה: הסריקה נכשלה ({exc}). "
                              "משתמשים בקטלוג הקודם.")

                plan = build_plan(
                    db,
                    teacher.id,
                    source_name=args.source,
                    targets=targets,
                    mode=args.mode,
                    class_modes=class_modes,
                    force_publish=args.publish,
                    force_draft=args.draft,
                )
            except SyncError as exc:
                print(f"שגיאה: {exc}", file=sys.stderr)
                return 2

            _print_plan(plan, verbose=args.verbose)
            if not args.apply:
                print()
                print("תצוגה בלבד — לא נכתב דבר. "
                      "הוסיפו --apply כדי לבצע.")
                return 0 if not plan.errors else 1

            outcome = apply_plan(db, plan)
            db.commit()
            print()
            print("בוצע.")
            print(outcome.summary_he())
            return 0 if not outcome.errors else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
