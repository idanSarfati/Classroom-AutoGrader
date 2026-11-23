"""Single background worker for import jobs.

There is exactly one worker on purpose:

* the existing ``ClassroomService`` serializes Google I/O with a process lock,
  so parallel imports would not be faster; and
* SQLite is happiest with one writer.

Jobs are resumable: applying commits per item, and rows already imported are
recognised on the next run. If the process dies while a job is ``running``,
starting the app again and pressing "resume" (or running the CLI) continues
where it stopped.
"""
from __future__ import annotations

import json
import logging
import queue
import threading

from portal.ingestion import catalog as catalog_store
from portal.ingestion import importer
from portal.ingestion.classroom_source import ClassroomSource
from portal.ingestion.storage import ContentStore
from portal.models import ImportJob
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)


class ImportWorker:
    def __init__(
        self,
        session_factory,
        content_dir,
        *,
        source_factory=None,
    ) -> None:
        self.session_factory = session_factory
        self.content_dir = content_dir
        self.source_factory = source_factory or ClassroomSource
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="portal-import-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def enqueue(self, job_id: int) -> None:
        self.start()
        self._queue.put(int(job_id))

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                self.process(job_id)
            except Exception:  # noqa: BLE001 - a worker must never die
                logger.exception("Import job %s crashed", job_id)

    def process(self, job_id: int) -> dict | None:
        """Run one job to completion in this thread."""
        with self.session_factory() as db:
            job = db.get(ImportJob, job_id)
            if job is None:
                logger.warning("Import job %s no longer exists", job_id)
                return None
            if job.state not in {"queued", "running", "preview"}:
                return None

            store = ContentStore(self.content_dir)
            try:
                source = self.source_factory()
            except Exception as exc:  # noqa: BLE001 - auth/configuration
                job.state = "failed"
                job.error = str(exc)
                job.finished_at = utcnow()
                db.commit()
                return None

            if job.kind == "catalog_sync":
                return self._catalog_sync(db, source, job)

            try:
                counts = importer.apply_job(db, store, source, job)
            except Exception as exc:  # noqa: BLE001 - report, never crash
                logger.exception("Import job %s failed", job_id)
                db.rollback()
                job = db.get(ImportJob, job_id)
                if job is not None:
                    job.state = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.finished_at = utcnow()
                    db.commit()
                return None
            return counts

    def _catalog_sync(self, db, source, job: ImportJob) -> dict | None:
        """Refresh the teacher's local catalog from Google once.

        The picker keeps serving the previous catalog while this runs and
        keeps it if the scan fails; only ``status``/``error`` change.
        """
        teacher_id = int(job.teacher_id)
        course_id = job.course_id or ""
        job.state = "running"
        job.started_at = utcnow()
        db.commit()
        try:
            counts = catalog_store.refresh(
                db, source, teacher_id, course_id=course_id
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash
            logger.exception("Catalog sync for teacher %s failed", teacher_id)
            db.rollback()
            job = db.get(ImportJob, job.id)
            if job is not None:
                job.state = "failed"
                job.error = str(exc)
                job.finished_at = utcnow()
                db.commit()
            catalog_store.record_failure(db, teacher_id, str(exc))
            return None
        job = db.get(ImportJob, job.id)
        if job is not None:
            job.state = "completed"
            job.counts_json = json.dumps(counts, ensure_ascii=False)
            job.error = ""
            job.finished_at = utcnow()
            db.commit()
        return counts
