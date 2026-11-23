"""Single background worker for student grading.

There is exactly one worker on purpose:

* the engine's rate limiter is process-global and already paces *between*
  grades, so concurrent portal calls would fight it instead of helping;
* each grade may take minutes on a cold model; SQLite and the UI are both
  happier with one writer;
* a queue that can be inspected in the database is more useful to the teacher
  than in-memory futures.

The worker claims jobs from ``portal_grading_jobs`` (not from the in-memory
queue, which is only a wake signal). That means a restart, a crashed thread or
a job queued while the server was down all resume from the same source of
truth.

Failure policy:

* **Daily quota** (``DailyQuotaExhausted``): the job is *deferred*, not failed.
  The grade is still wanted; every Groq key is simply spent until the quota
  resets. The worker stops grading until ``next_attempt_at`` and then retries.
* **Per-minute 429** that survived the engine's own retries: defer for a
  shorter window instead of burning more requests.
* **Anything else**: the job fails with a readable Hebrew message. The
  submission stays visible, and the teacher can press "בדיקה מחדש".
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from portal.grading import adapter, service
from portal.models import (
    SUBMISSION_DEFERRED,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    SUBMISSION_GRADING,
    SUBMISSION_QUEUED,
    Assignment,
    Grade,
    GradingJob,
    Submission,
    User,
)
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_DEFER_SECONDS = 60 * 60
DEFAULT_RATE_LIMIT_DEFER_SECONDS = 15 * 60
GRADE_FAILED_MESSAGE = (
    "הבדיקה נכשלה. אפשר לנסות שוב, או לבקש מהמורה להריץ בדיקה מחדש."
)
QUOTA_DEFERRED_MESSAGE = (
    "הבדיקה נדחתה זמנית כי מכסת המודל היומית נוצלה. "
    "היא תרוץ אוטומטית כשהמכסה תתחדש - אין צורך להגיש שוב."
)
RATE_LIMIT_DEFERRED_MESSAGE = (
    "המודל עמוס כרגע. ההגשה נשמרה והבדיקה תרוץ אוטומטית בעוד כמה דקות."
)
# ``configured_model`` marker for a grade produced by the exam checker. It is
# not a model, but the column is where the grade records how it was produced.
EXAM_GRADE_SOURCE = "exam-checker"


def _one_line(exc: BaseException, limit: int = 400) -> str:
    text = " ".join(str(exc).split())
    return text[:limit]


def reset_stale(session_factory) -> int:
    """Requeue jobs interrupted by a restart.

    Unlike a catalog scan, a grading job is idempotent and the student's text
    is already saved, so resuming is safe. Called once at application startup
    before the worker thread starts.
    """
    with session_factory() as db:
        jobs = db.scalars(
            select(GradingJob).where(GradingJob.state == SUBMISSION_GRADING)
        ).all()
        for job in jobs:
            job.state = SUBMISSION_QUEUED
            job.started_at = None
            job.error = "הבדיקה נקטעה בהפעלה מחדש; היא תבוצע שוב."
        broken = db.scalars(
            select(Submission).where(Submission.status == SUBMISSION_GRADING)
        ).all()
        for submission in broken:
            submission.status = SUBMISSION_QUEUED
        if jobs or broken:
            db.commit()
        return len(jobs)


class GradingWorker:
    def __init__(
        self,
        session_factory,
        *,
        grade=None,
        pause=None,
        quota_defer_seconds: int = DEFAULT_QUOTA_DEFER_SECONDS,
        rate_limit_defer_seconds: int = DEFAULT_RATE_LIMIT_DEFER_SECONDS,
        poll_seconds: float = 1.0,
        clock=utcnow,
    ) -> None:
        self.session_factory = session_factory
        self._grade = grade or adapter.grade_submission
        self._pause = pause or adapter.between_grades_pause
        self.quota_defer_seconds = int(quota_defer_seconds)
        self.rate_limit_defer_seconds = int(rate_limit_defer_seconds)
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._clock = clock
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cooldown_until: datetime | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="portal-grading-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        self._queue.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def enqueue(self, _job_id: int | None = None) -> None:
        """Wake the worker; it claims the next queued job from the database."""
        self.start()
        self._wake.set()

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        while not self._stop.is_set():
            cooldown = self._cooldown_remaining()
            if cooldown > 0:
                # A wake signal must not bypass the quota window: the next
                # grade would fail exactly like the deferred one.
                self._wake.wait(min(cooldown, 60.0))
                self._wake.clear()
                continue
            try:
                worked = self.process_next()
            except Exception:  # noqa: BLE001 - a worker must never die
                logger.exception("Grading worker loop failed")
                worked = False
            if not worked:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    def process_next(self, *, pause: bool = True) -> bool:
        """Claim and grade at most one job. Returns False when the queue is idle."""
        if self._cooldown_remaining() > 0:
            return False
        job_id = self._claim_next_id()
        if job_id is None:
            return False
        record = self.process(job_id)
        # An exam is marked locally and instantly, so the model-pacing pause
        # would only slow a whole class down. It stays for LLM grades.
        if pause and self._has_pending() and not (
            isinstance(record, dict) and record.get("exam")
        ):
            try:
                self._pause()
            except Exception:  # noqa: BLE001 - pacing is best effort
                logger.debug("Pacing pause failed", exc_info=True)
        return True

    def _cooldown_remaining(self) -> float:
        with self._lock:
            until = self._cooldown_until
        if until is None:
            return 0.0
        remaining = (until - self._clock()).total_seconds()
        if remaining <= 0:
            with self._lock:
                self._cooldown_until = None
            return 0.0
        return remaining

    def _cool_down_for(self, seconds: int) -> None:
        until = self._clock() + timedelta(seconds=max(1, int(seconds)))
        with self._lock:
            if self._cooldown_until is None or until > self._cooldown_until:
                self._cooldown_until = until

    # --------------------------------------------------------------- claim
    def _claim_next_id(self) -> int | None:
        now = self._clock()
        with self.session_factory() as db:
            # A deferred job whose window elapsed is simply queued again.
            db.execute(
                update(GradingJob)
                .where(
                    GradingJob.state == SUBMISSION_DEFERRED,
                    GradingJob.next_attempt_at.is_not(None),
                    GradingJob.next_attempt_at <= now,
                )
                .values(state=SUBMISSION_QUEUED)
            )
            job = db.scalar(
                select(GradingJob)
                .where(GradingJob.state == SUBMISSION_QUEUED)
                .order_by(GradingJob.created_at, GradingJob.id)
                .limit(1)
            )
            job_id = int(job.id) if job is not None else None
            db.commit()
        return job_id

    def _has_pending(self) -> bool:
        now = self._clock()
        with self.session_factory() as db:
            queued = db.scalar(
                select(GradingJob.id)
                .where(GradingJob.state == SUBMISSION_QUEUED)
                .limit(1)
            )
            if queued is not None:
                return True
            return (
                db.scalar(
                    select(GradingJob.id)
                    .where(
                        GradingJob.state == SUBMISSION_DEFERRED,
                        GradingJob.next_attempt_at.is_not(None),
                        GradingJob.next_attempt_at <= now,
                    )
                    .limit(1)
                )
                is not None
            )

    def _claim(self, job_id: int) -> dict | None:
        """Mark one job running and return the immutable inputs to grade."""
        now = self._clock()
        with self.session_factory() as db:
            job = db.get(GradingJob, job_id)
            if job is None:
                return None
            if job.state == SUBMISSION_DEFERRED:
                if job.next_attempt_at is not None and job.next_attempt_at > now:
                    return None
            elif job.state != SUBMISSION_QUEUED:
                return None

            submission = db.get(Submission, job.submission_id)
            if submission is None:
                job.state = SUBMISSION_FAILED
                job.error = "ההגשה נמחקה לפני הבדיקה."
                job.finished_at = now
                db.commit()
                return None
            if submission.revision != job.revision:
                # The student handed in newer work while this job waited. The
                # newer revision has its own job; grading old text now would
                # waste a call and could confuse the review screen.
                job.state = "superseded"
                job.finished_at = now
                db.commit()
                return None

            assignment = db.get(Assignment, submission.assignment_id)
            student = db.get(User, submission.student_id)
            if assignment is None or student is None:
                job.state = SUBMISSION_FAILED
                job.error = "המטלה או התלמיד/ה אינם קיימים עוד."
                job.finished_at = now
                submission.status = SUBMISSION_FAILED
                db.commit()
                return None

            job.state = SUBMISSION_GRADING
            job.started_at = now
            job.attempts = int(job.attempts or 0) + 1
            job.error = ""
            job.next_attempt_at = None
            submission.status = SUBMISSION_GRADING
            db.commit()

            return {
                "job_id": int(job.id),
                "submission_id": int(submission.id),
                "revision": int(job.revision),
                "text": submission.text,
                "student_id": str(student.id),
                "student_name": student.display_name,
                "assignment_id": int(assignment.id),
                "rubric_path": assignment.rubric_path,
                "rubric_sha256": assignment.rubric_sha256,
                "exam_config": str(assignment.exam_config or ""),
                "due_at": assignment.due_at,
                "first_submitted_at": submission.first_submitted_at,
                "submitted_at": submission.submitted_at,
            }

    # ------------------------------------------------------------- grading
    def process(self, job_id: int) -> dict | None:
        """Run one job to completion in this thread."""
        snapshot = self._claim(job_id)
        if snapshot is None:
            return None
        if snapshot.get("exam_config"):
            return self._process_exam(snapshot)
        try:
            binding = adapter.load_rubric(
                snapshot["rubric_path"],
                expected_sha256=snapshot["rubric_sha256"] or None,
            )
            artifact = self._grade(
                student_id=snapshot["student_id"],
                student_name=snapshot["student_name"],
                submission_id=(
                    f"portal-{snapshot['submission_id']}"
                    f"-r{snapshot['revision']}"
                ),
                raw_doc_text=snapshot["text"],
                rubric=binding,
                due_at=snapshot["due_at"],
                first_submitted_at=snapshot["first_submitted_at"],
                submitted_at=snapshot["submitted_at"],
            )
        except adapter.DailyQuotaExhausted as exc:
            logger.warning("Daily quota exhausted for job %s: %s", job_id, exc)
            return self._defer(
                job_id,
                snapshot,
                QUOTA_DEFERRED_MESSAGE,
                self.quota_defer_seconds,
                detail=_one_line(exc),
            )
        except adapter.LLMAPIError as exc:
            if getattr(exc, "status_code", None) == 429:
                logger.warning("Rate limit persisted for job %s: %s", job_id, exc)
                return self._defer(
                    job_id,
                    snapshot,
                    RATE_LIMIT_DEFERRED_MESSAGE,
                    self.rate_limit_defer_seconds,
                    detail=_one_line(exc),
                )
            return self._fail(job_id, snapshot, exc)
        except adapter.LLMEvaluationError as exc:
            return self._fail(job_id, snapshot, exc)
        except adapter.RubricBindingError as exc:
            return self._fail(job_id, snapshot, exc)
        except Exception as exc:  # noqa: BLE001 - report, never crash
            logger.exception("Grading job %s crashed", job_id)
            return self._fail(job_id, snapshot, exc)

        record = artifact.to_record()
        self._persist_grade(snapshot, artifact, record)
        return record

    # ----------------------------------------------------------------- exam
    def _process_exam(self, snapshot: dict) -> dict | None:
        """Mark an exam hand-in with the deterministic checker, not the model.

        An exam has a written answer key, so the score must be reproducible,
        private and instant: no quota, no network, and the same answer always
        earns the same points.
        """
        from portal.exams import config as exam_config
        from portal.exams import service as exam_service

        try:
            with self.session_factory() as db:
                assignment = db.get(Assignment, snapshot["assignment_id"])
                submission = db.get(Submission, snapshot["submission_id"])
                if assignment is None or submission is None:
                    raise exam_service.ExamUnavailable(
                        "the assignment or the hand-in was deleted"
                    )
                config = exam_config.for_assignment(assignment)
                if config is None:
                    raise exam_service.ExamUnavailable(
                        f"exam {snapshot['exam_config']!r} is not available"
                    )
                report = exam_service.mark(
                    db,
                    assignment,
                    submission,
                    student_name=snapshot["student_name"],
                    config=config,
                )
        except Exception:  # noqa: BLE001 - report, never crash the worker
            logger.exception("Exam job %s failed", snapshot["job_id"])
            return self._fail(
                snapshot["job_id"], snapshot, RuntimeError("exam marking failed")
            )

        record = report.to_record(snapshot["student_name"])
        self._persist_exam_grade(snapshot, report, record)
        return record

    def _persist_exam_grade(self, snapshot: dict, report, record: dict) -> None:
        """Write the checker's report into the ordinary ``Grade`` row."""
        from portal.exams import manual as exam_manual

        audit = report.audit()
        now = utcnow()
        with self.session_factory() as db:
            grade = service.grade_for_revision(
                db, snapshot["submission_id"], snapshot["revision"]
            )
            if grade is not None and grade.result_json:
                # A re-check must not wipe the points the teacher set by hand.
                try:
                    old_record = json.loads(grade.result_json)
                except (TypeError, ValueError):
                    old_record = None
                if exam_manual.carry_over(old_record, record) or exam_manual.is_disqualified(record):
                    audit = record.get("audit") or audit
            if grade is None:
                grade = Grade(
                    submission_id=snapshot["submission_id"],
                    revision=snapshot["revision"],
                )
                db.add(grade)
            grade.job_id = snapshot["job_id"]
            grade.score = int(record.get("score") or report.score)
            grade.feedback_hebrew = str(record.get("feedback_hebrew") or "")
            grade.result_json = _json(record)
            grade.deduction_json = _json(record.get("deduction_breakdown") or [])
            grade.audit_json = _json(audit)
            grade.audit_ok = bool(audit.get("ok"))
            grade.audit_needs_review = bool(audit.get("needs_review"))
            grade.audit_summary = str(audit.get("summary") or "")[:500]
            grade.rubric_sha256 = snapshot.get("rubric_sha256") or ""
            grade.rubric_title = (
                f"{report.exam} · {report.version_label}"[:300]
            )
            grade.late_penalty_points = 0
            grade.configured_model = EXAM_GRADE_SOURCE
            grade.graded_at = now
            # The approval fields are deliberately untouched: a teacher sign-off
            # belongs to the hand-in and survives a same-revision re-mark. Only
            # a new student revision (a fresh Grade row) starts unapproved.

            job = db.get(GradingJob, snapshot["job_id"])
            if job is not None:
                job.state = SUBMISSION_GRADED
                job.error = ""
                job.finished_at = now
                job.next_attempt_at = None
            submission = db.get(Submission, snapshot["submission_id"])
            if submission is not None and submission.revision == snapshot["revision"]:
                submission.status = SUBMISSION_GRADED
            db.commit()
        logger.info(
            "Exam job %s marked submission %s rev %s: %s/%s",
            snapshot["job_id"],
            snapshot["submission_id"],
            snapshot["revision"],
            report.score,
            report.maximum,
        )

    def _persist_grade(self, snapshot: dict, artifact, record: dict) -> None:
        audit = record.get("audit") or None
        now = utcnow()
        with self.session_factory() as db:
            grade = service.grade_for_revision(
                db, snapshot["submission_id"], snapshot["revision"]
            )
            if grade is None:
                grade = Grade(
                    submission_id=snapshot["submission_id"],
                    revision=snapshot["revision"],
                )
                db.add(grade)
            grade.job_id = snapshot["job_id"]
            grade.score = int(record["score"])
            grade.feedback_hebrew = str(record.get("feedback_hebrew") or "")
            grade.result_json = artifact.result.model_dump_json()
            grade.deduction_json = _json(record.get("deduction_breakdown") or [])
            grade.audit_json = _json(audit) if audit is not None else ""
            grade.audit_ok = bool(audit.get("ok")) if audit else True
            grade.audit_needs_review = bool(
                audit.get("needs_review")
            ) if audit else False
            grade.audit_summary = str(
                (audit or {}).get("summary") or ""
            )[:500]
            grade.rubric_sha256 = record.get("rubric_sha256") or ""
            grade.rubric_title = record.get("rubric_title") or ""
            grade.late_penalty_points = int(
                record.get("late_penalty_points") or 0
            )
            grade.configured_model = record.get("configured_model") or ""
            grade.graded_at = now
            # The approval fields are deliberately untouched: a teacher sign-off
            # belongs to the hand-in and survives a same-revision regrade. A
            # resubmission opens a new revision with a fresh, unapproved Grade.

            job = db.get(GradingJob, snapshot["job_id"])
            if job is not None:
                job.state = SUBMISSION_GRADED
                job.error = ""
                job.finished_at = now
                job.next_attempt_at = None
            submission = db.get(Submission, snapshot["submission_id"])
            if submission is not None and submission.revision == snapshot["revision"]:
                submission.status = SUBMISSION_GRADED
            db.commit()
        logger.info(
            "Job %s graded submission %s revision %s: %s",
            snapshot["job_id"],
            snapshot["submission_id"],
            snapshot["revision"],
            record.get("score"),
        )

    # ------------------------------------------------------------- failures
    def _defer(
        self,
        job_id: int,
        snapshot: dict,
        message: str,
        seconds: int,
        *,
        detail: str = "",
    ) -> dict | None:
        now = self._clock()
        retry_at = now + timedelta(seconds=max(1, int(seconds)))
        with self.session_factory() as db:
            job = db.get(GradingJob, job_id)
            if job is None:
                return None
            job.state = SUBMISSION_DEFERRED
            job.next_attempt_at = retry_at
            job.finished_at = None
            job.error = message + (f" ({detail})" if detail else "")
            submission = db.get(Submission, snapshot["submission_id"])
            if submission is not None and submission.revision == snapshot["revision"]:
                submission.status = SUBMISSION_DEFERRED
            db.commit()
        self._cool_down_for(seconds)
        return None

    def _fail(self, job_id: int, snapshot: dict, exc: BaseException) -> dict | None:
        now = self._clock()
        detail = _one_line(exc)
        logger.warning("Grading job %s failed: %s", job_id, detail)
        with self.session_factory() as db:
            job = db.get(GradingJob, job_id)
            if job is None:
                return None
            job.state = SUBMISSION_FAILED
            job.error = f"{GRADE_FAILED_MESSAGE} ({type(exc).__name__}: {detail})"
            job.finished_at = now
            job.next_attempt_at = None
            submission = db.get(Submission, snapshot["submission_id"])
            if submission is not None and submission.revision == snapshot["revision"]:
                submission.status = SUBMISSION_FAILED
            db.commit()
        return None


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = ["GradingWorker", "reset_stale"]
