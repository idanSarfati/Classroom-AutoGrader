"""Command-line entry point for Classroom ingestion.

Typical use, from the project root::

    .venv\\Scripts\\python -m portal.scripts.classroom_import auth
    .venv\\Scripts\\python -m portal.scripts.classroom_import list-courses
    .venv\\Scripts\\python -m portal.scripts.classroom_import preview \\
        --course-id 123456 --class-id 2 --out plan.json
    .venv\\Scripts\\python -m portal.scripts.classroom_import apply --job-id 7

``preview`` only reads Google and writes portal rows; ``apply`` is explicit
and always creates drafts. Nothing here prints tokens, Drive ids in bulk, or
student data.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.ingestion import importer
from portal.ingestion.classroom_source import ClassroomSource, SourceError
from portal.ingestion.google_auth import IngestionAuthError, load_credentials
from portal.ingestion.storage import ContentStore
from portal.ingestion.types import ImportManifest
from portal.models import ClassGroup, ImportJob, ROLE_TEACHER, User
from portal.paths import CONTENT_DIR


def _open_db():
    cfg = load_settings()
    engine = build_engine(cfg.database_url)
    init_db(engine)
    return engine, build_session_factory(engine)


def _teacher(db, username: str | None) -> User:
    if username:
        teacher = db.scalar(
            select(User).where(
                User.username == username.strip().lower(),
                User.role == ROLE_TEACHER,
            )
        )
    else:
        teacher = db.scalar(
            select(User).where(User.role == ROLE_TEACHER).order_by(User.id)
        )
    if teacher is None:
        raise SystemExit(
            "error: no teacher account found. Create one with "
            "python -m portal.scripts.bootstrap_teacher."
        )
    return teacher


def _group(db, teacher: User, class_id: int) -> ClassGroup:
    group = db.get(ClassGroup, class_id)
    if group is None or group.teacher_id != teacher.id:
        raise SystemExit(f"error: class {class_id} does not belong to {teacher.username}.")
    return group


def _print_job(job: ImportJob) -> None:
    counts = json.loads(job.counts_json or "{}")
    print(f"job {job.id}: {job.state}  {counts}")
    if job.error:
        print(f"error: {job.error}")


def cmd_auth(_args) -> int:
    load_credentials(interactive=True)
    print("Google authorization is ready (portal/data/google_token.json).")
    return 0


def cmd_list_courses(_args) -> int:
    for course in ClassroomSource().list_courses():
        section = f" · {course.section}" if course.section else ""
        print(f"{course.id}\t{course.name}{section}")
    return 0


def cmd_preview(args) -> int:
    engine, factory = _open_db()
    try:
        with factory() as db:
            teacher = _teacher(db, args.teacher)
            group = _group(db, teacher, args.class_id)
            job = importer.build_course_preview(
                db,
                ClassroomSource(),
                teacher_id=teacher.id,
                group=group,
                course_id=args.course_id,
                include_drafts=args.include_drafts,
            )
            _print_job(job)
            if args.out:
                manifest = ImportManifest.from_json(
                    json.loads(job.manifest_json)
                )
                Path(args.out).write_text(
                    json.dumps(manifest.as_json(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"manifest written to {args.out}")
            for item in ImportManifest.from_json(
                json.loads(job.manifest_json)
            ).items:
                flag = "duplicate" if item.duplicate_of else item.suggested_action
                rubric = f" [{item.suggested_rubric}]" if item.suggested_rubric else ""
                print(f"  - {item.title} ({flag}){rubric}")
    finally:
        engine.dispose()
    return 0


def cmd_decks(args) -> int:
    engine, factory = _open_db()
    try:
        with factory() as db:
            teacher = _teacher(db, args.teacher)
            group = _group(db, teacher, args.class_id)
            source = ClassroomSource()
            if args.file_id:
                job = importer.build_deck_preview(
                    db,
                    source,
                    teacher_id=teacher.id,
                    group=group,
                    file_ids=args.file_id,
                    folder_id=args.folder_id or "",
                )
                _print_job(job)
                if args.apply:
                    counts = importer.apply_job(
                        db, ContentStore(CONTENT_DIR), source, job
                    )
                    print(f"applied: {counts}")
            else:
                decks = source.list_drive_decks(args.folder_id or "")
                for deck in decks:
                    print(f"{deck.file_id}\t{deck.title}")
                print(f"{len(decks)} deck(s).")
    finally:
        engine.dispose()
    return 0


def cmd_apply(args) -> int:
    engine, factory = _open_db()
    try:
        with factory() as db:
            job = db.get(ImportJob, args.job_id)
            if job is None:
                raise SystemExit(f"error: job {args.job_id} not found.")
            _teacher(db, args.teacher)
            job.state = "queued"
            db.commit()
            counts = importer.apply_job(
                db,
                ContentStore(CONTENT_DIR),
                ClassroomSource(),
                job,
                limit=args.limit,
            )
            _print_job(job)
            print(f"applied: {counts}")
    finally:
        engine.dispose()
    return 0


def cmd_apply_plan(args) -> int:
    engine, factory = _open_db()
    try:
        payload = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        manifest = ImportManifest.from_json(payload)
        if args.class_id:
            manifest.class_id = args.class_id
        with factory() as db:
            teacher = _teacher(db, args.teacher)
            group = _group(db, teacher, manifest.class_id)
            manifest.class_name = group.name
            job = ImportJob(
                teacher_id=teacher.id,
                class_id=group.id,
                course_id=manifest.course_id,
                kind=manifest.kind,
                state="queued",
                manifest_json=json.dumps(manifest.as_json(), ensure_ascii=False),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            counts = importer.apply_job(
                db,
                ContentStore(CONTENT_DIR),
                ClassroomSource(),
                job,
                limit=args.limit,
            )
            _print_job(job)
            print(f"applied: {counts}")
    finally:
        engine.dispose()
    return 0


def cmd_reuse(args) -> int:
    engine, factory = _open_db()
    try:
        with factory() as db:
            teacher = _teacher(db, args.teacher)
            group = _group(db, teacher, args.class_id)
            try:
                if args.lesson_id:
                    lesson = db.get(importer.Lesson, args.lesson_id)
                    if lesson is None:
                        raise SystemExit("error: lesson not found.")
                    clone = importer.reuse_lesson(db, lesson, group)
                    db.commit()
                    print(f"lesson {lesson.id} -> lesson {clone.id} in '{group.name}'.")
                else:
                    assignment = db.get(importer.Assignment, args.assignment_id)
                    if assignment is None:
                        raise SystemExit("error: assignment not found.")
                    clone = importer.reuse_assignment(db, assignment, group)
                    db.commit()
                    print(
                        f"assignment {assignment.id} -> assignment {clone.id} "
                        f"in '{group.name}'."
                    )
            except importer.ReuseError as exc:
                raise SystemExit(f"error: {exc}") from exc
    finally:
        engine.dispose()
    return 0


def cmd_status(args) -> int:
    engine, factory = _open_db()
    try:
        with factory() as db:
            job = db.get(ImportJob, args.job_id)
            if job is None:
                raise SystemExit(f"error: job {args.job_id} not found.")
            _print_job(job)
    finally:
        engine.dispose()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="Run the one-time Google consent flow.")
    auth.set_defaults(func=cmd_auth)

    courses = sub.add_parser("list-courses", help="List active Classroom courses.")
    courses.set_defaults(func=cmd_list_courses)

    preview = sub.add_parser("preview", help="Preview one course into a portal class.")
    preview.add_argument("--course-id", required=True)
    preview.add_argument("--class-id", type=int, required=True)
    preview.add_argument("--teacher", default=None)
    preview.add_argument("--include-drafts", action="store_true")
    preview.add_argument("--out", default="")
    preview.set_defaults(func=cmd_preview)

    decks = sub.add_parser("decks", help="List or import Drive presentations.")
    decks.add_argument("--class-id", type=int, required=True)
    decks.add_argument("--teacher", default=None)
    decks.add_argument("--folder-id", default="")
    decks.add_argument("--file-id", action="append", default=[])
    decks.add_argument("--apply", action="store_true")
    decks.set_defaults(func=cmd_decks)

    apply = sub.add_parser("apply", help="Apply a stored preview job.")
    apply.add_argument("--job-id", type=int, required=True)
    apply.add_argument("--teacher", default=None)
    apply.add_argument("--limit", type=int, default=None)
    apply.set_defaults(func=cmd_apply)

    plan = sub.add_parser("apply-plan", help="Apply a manifest JSON file.")
    plan.add_argument("--plan", required=True)
    plan.add_argument("--class-id", type=int, default=None)
    plan.add_argument("--teacher", default=None)
    plan.add_argument("--limit", type=int, default=None)
    plan.set_defaults(func=cmd_apply_plan)

    reuse = sub.add_parser("reuse", help="Copy a lesson/assignment to another class.")
    reuse.add_argument("--class-id", type=int, required=True)
    reuse.add_argument("--teacher", default=None)
    group = reuse.add_mutually_exclusive_group(required=True)
    group.add_argument("--lesson-id", type=int)
    group.add_argument("--assignment-id", type=int)
    reuse.set_defaults(func=cmd_reuse)

    status = sub.add_parser("status", help="Show one import job.")
    status.add_argument("--job-id", type=int, required=True)
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except IngestionAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
