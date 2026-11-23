"""Bulk-import every Classroom course item into the teacher's classes.

Everything is created as a draft (``published=False``); the teacher publishes
per class when the material is needed. Items already present in a class are
skipped by the importer, and a per-item failure is recorded on the job
without aborting the run.

Typical use, from the project root::

    .venv\\Scripts\\python -m portal.scripts.bulk_import --preview-only
    .venv\\Scripts\\python -m portal.scripts.bulk_import --class-id 7
    .venv\\Scripts\\python -m portal.scripts.bulk_import

Point ``PORTAL_DATABASE_URL`` at a copy of the database for a dry run.
Nothing here prints tokens, Drive ids in bulk, or student data.
"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import event, select

from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.ingestion import importer
from portal.ingestion.classroom_source import ClassroomSource
from portal.ingestion.storage import ContentStore
from portal.models import ClassGroup, ClassroomLink, ROLE_TEACHER, User
from portal.paths import CONTENT_DIR


def _report(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--class-id", type=int, default=None)
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--teacher", default=None)
    args = parser.parse_args(argv)

    cfg = load_settings()
    _report({"database": cfg.database_url, "preview_only": args.preview_only})
    engine = build_engine(cfg.database_url)
    if cfg.database_url.startswith("sqlite"):
        # The live portal keeps writing while this script runs (autosave,
        # grading, the exam tick every 3 hours). The shared 5s busy timeout is tuned
        # for short web requests; a bulk import must wait out longer locks.
        @event.listens_for(engine, "connect")
        def _patient_timeout(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=120000")
            finally:
                cursor.close()

    init_db(engine)
    factory = build_session_factory(engine)
    source = ClassroomSource()
    store = ContentStore(CONTENT_DIR)
    try:
        with factory() as db:
            query = select(User).where(User.role == ROLE_TEACHER)
            if args.teacher:
                query = query.where(User.username == args.teacher.strip().lower())
            teacher = db.scalar(query.order_by(User.id))
            if teacher is None:
                raise SystemExit("error: no teacher account found.")
            groups = list(
                db.scalars(
                    select(ClassGroup)
                    .where(ClassGroup.teacher_id == teacher.id)
                    .order_by(ClassGroup.id)
                )
            )
            if args.class_id is not None:
                groups = [g for g in groups if g.id == args.class_id]
                if not groups:
                    raise SystemExit(
                        f"error: class {args.class_id} does not belong to "
                        f"{teacher.username}."
                    )
            for group in groups:
                link = db.scalar(
                    select(ClassroomLink).where(
                        ClassroomLink.class_id == group.id
                    )
                )
                if link is None:
                    _report(
                        {
                            "class": group.id,
                            "name": group.name,
                            "skipped": "no Classroom link",
                        }
                    )
                    continue
                job = importer.build_course_preview(
                    db,
                    source,
                    teacher_id=teacher.id,
                    group=group,
                    course_id=link.course_id,
                )
                if args.preview_only:
                    _report(
                        {
                            "class": group.id,
                            "name": group.name,
                            "job": job.id,
                            "state": job.state,
                            "counts": json.loads(job.counts_json or "{}"),
                        }
                    )
                    continue
                job.state = "queued"
                db.commit()
                counts = importer.apply_job(db, store, source, job)
                db.refresh(job)
                _report(
                    {
                        "class": group.id,
                        "name": group.name,
                        "job": job.id,
                        "state": job.state,
                        "counts": counts,
                        "error": job.error,
                    }
                )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())