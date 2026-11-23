"""Portal database tables (Phase 1 + Phase 2 + Phase 2.5 + Phase 3).

All table names carry a ``portal_`` prefix so the portal database can never be
confused with, or joined to, any table the existing system may add later.
Phase 1 added users, classes, enrollments and assignments; Phase 2 adds
lessons (global or class-linked) and their ordered slides; Phase 2.5 adds
Classroom import jobs, content-addressed media metadata and rich lesson /
assignment content. Phase 3 adds student submissions, the grading job queue
and the persisted grading result (score, feedback, per-task verdicts, audit).

New tables are picked up by ``create_all`` on the next start. A change to an
*existing* table is different: it needs an ordered migration in
:mod:`portal.migrations`, which ``init_db`` runs right after ``create_all``.
Migration 0001 adds the exam window columns to ``portal_assignments``;
migration 0002 adds the exam result release state.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from portal.db import Base
from portal.timeutil import utcnow

ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"
ROLES = (ROLE_TEACHER, ROLE_STUDENT)

# Exam results are marked the moment a paper is handed in, but the student does
# not see them until the teacher says so. "held" is the default, so a class that
# sits early can never leak the paper to a class that sits later: "scores"
# releases only the number, "full" releases the score, the explanations and the
# student's own paper.
EXAM_RELEASE_HELD = "held"
EXAM_RELEASE_SCORES = "scores"
EXAM_RELEASE_FULL = "full"
EXAM_RELEASE_STATES = (
    EXAM_RELEASE_HELD,
    EXAM_RELEASE_SCORES,
    EXAM_RELEASE_FULL,
)
EXAM_RELEASE_LABELS = {
    EXAM_RELEASE_HELD: "התוצאות מוסתרות",
    EXAM_RELEASE_SCORES: "ציונים בלבד",
    EXAM_RELEASE_FULL: "פרסום מלא",
}


class User(Base):
    __tablename__ = "portal_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Login names are stored lowercase; the UI normalises input the same way.
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), index=True, default=ROLE_STUDENT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username!r} role={self.role}>"


class StudentRecord(Base):
    """School-directory (אלפון) data attached to a portal student.

    The portal's ``User`` row is deliberately small (login name, display name,
    hash). The national id, contact details and parent phones live here so an
    alephon import can update them without touching login data, and so the
    national id can be the stable idempotency key even if a display name is
    corrected. ``national_id`` is nullable: a row without one still imports,
    but only name-based matching can find it on a rerun.
    """

    __tablename__ = "portal_student_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    national_id: Mapped[str | None] = mapped_column(
        String(20), unique=True, nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    parent_name: Mapped[str] = mapped_column(String(160), default="")
    parent_phone: Mapped[str] = mapped_column(String(40), default="")
    parent_email: Mapped[str] = mapped_column(String(200), default="")
    class_label: Mapped[str] = mapped_column(String(40), default="")
    source_file: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<StudentRecord student={self.student_id} "
            f"id={self.national_id!r}>"
        )


class ClassGroup(Base):
    """A teacher's class. ``Class`` is a Python keyword-ish name, hence the suffix."""

    __tablename__ = "portal_classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ClassGroup {self.name!r} code={self.code!r}>"


class Enrollment(Base):
    __tablename__ = "portal_enrollments"
    __table_args__ = (
        UniqueConstraint(
            "class_id",
            "student_id",
            name="uq_portal_enrollments_class_student",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int] = mapped_column(
        ForeignKey("portal_classes.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Assignment(Base):
    """A portal assignment bound to an existing rubric file.

    ``rubric_path`` + ``rubric_sha256`` are teacher-only data: they pin the
    exact file approved at publish time. They are never sent to a student,
    because rubric files contain the answers.
    """

    __tablename__ = "portal_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int] = mapped_column(
        ForeignKey("portal_classes.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(160))
    instructions: Mapped[str] = mapped_column(Text, default="")
    rubric_path: Mapped[str] = mapped_column(String(1024))
    rubric_sha256: Mapped[str] = mapped_column(String(64))
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ``due_at`` only feeds the late penalty: homework may still be handed in
    # afterwards. An exam is different. ``exam_config`` names a definition in
    # portal/exams, and the window is enforced - before ``opens_at`` the paper
    # is not served at all, after ``closes_at`` nothing is accepted.
    exam_config: Mapped[str] = mapped_column(String(64), default="")
    opens_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # How much of an exam hand-in the student may see. A stale or unknown value
    # is treated as "held": the safe side is always the hidden one.
    exam_release: Mapped[str] = mapped_column(
        String(16), default=EXAM_RELEASE_HELD
    )
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def exam_release_state(self) -> str:
        return (
            self.exam_release
            if self.exam_release in EXAM_RELEASE_STATES
            else EXAM_RELEASE_HELD
        )

    @property
    def exam_score_visible(self) -> bool:
        """True once the teacher released at least the numeric score."""
        return self.exam_release_state in (EXAM_RELEASE_SCORES, EXAM_RELEASE_FULL)

    @property
    def exam_review_visible(self) -> bool:
        """True once the teacher released the explanations and the paper too."""
        return self.exam_release_state == EXAM_RELEASE_FULL

    @property
    def exam_is_engine(self) -> bool:
        """True when this assignment sits a paper from ``portal/exams``."""
        return bool((self.exam_config or "").strip())

    @property
    def exam_results_released(self) -> bool:
        """The teacher published this exam's marks.

        This is deliberately *not* the same as ``published``. Releasing the
        marks is the decision that makes a result course data; hiding the paper
        is the decision that keeps a class which has not sat yet from seeing
        it. A teacher who wants exactly that combination - marks out, exam
        invisible - sets the release and unpublishes the assignment, so every
        gate that asks "is this a grade the class is allowed to know?" reads
        this property instead of ``published``.
        """
        return self.exam_is_engine and self.exam_score_visible

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Assignment {self.title!r} published={self.published}>"


class Lesson(Base):
    """A lesson/presentation owned by a teacher.

    ``class_id`` is ``None`` for a global lesson visible to every enrolled
    student; otherwise the lesson is visible only to that class. Lessons are
    kept in the portal database rather than linked from Google Drive, so
    students can study from a phone without an external account.
    """

    __tablename__ = "portal_lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    # NULL = global lesson. Deleting a class deletes its lessons; the teacher
    # account (and global lessons) survive.
    class_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_classes.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text, default="")
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    @property
    def is_global(self) -> bool:
        return self.class_id is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Lesson {self.title!r} published={self.published}>"


class LessonSlide(Base):
    """One screen of a lesson, in display order within its lesson."""

    __tablename__ = "portal_lesson_slides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("portal_lessons.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(160), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    code: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LessonSlide lesson={self.lesson_id} position={self.position}>"


# ---------------------------------------------------------------------------
# Phase 2.5: Classroom ingestion
# ---------------------------------------------------------------------------


class ClassroomLink(Base):
    """A portal class linked to one Google Classroom course.

    Several portal classes may link to the same course (a teacher may teach
    the same syllabus to two groups), so the uniqueness is on ``class_id``,
    not on the course.
    """

    __tablename__ = "portal_classroom_links"
    __table_args__ = (
        UniqueConstraint("class_id", name="uq_portal_classroom_links_class"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    class_id: Mapped[int] = mapped_column(
        ForeignKey("portal_classes.id", ondelete="CASCADE"), index=True
    )
    course_id: Mapped[str] = mapped_column(String(128), index=True)
    course_name: Mapped[str] = mapped_column(String(200), default="")
    section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )


class ImportJob(Base):
    """One preview or import run, owned by a teacher.

    ``manifest_json`` holds the preview exactly as the teacher saw it, so
    applying never re-queries Google and never changes under the teacher's
    feet. ``state`` moves through preview -> queued -> running -> completed
    (or partial / failed / cancelled).
    """

    __tablename__ = "portal_import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    class_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_classes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    course_id: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(20), default="course")
    state: Mapped[str] = mapped_column(
        String(20), default="preview", index=True
    )
    manifest_json: Mapped[str] = mapped_column(Text, default="")
    counts_json: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ImportItem(Base):
    """Per-source state, so a re-import never duplicates content.

    The unique key spans the target class: the same Classroom item may be
    imported into two portal classes, but only once into each. ``payload_json``
    stores the teacher's decision (action, rubric, title) for the worker.
    """

    __tablename__ = "portal_import_items"
    __table_args__ = (
        UniqueConstraint(
            "teacher_id",
            "class_id",
            "source_kind",
            "source_id",
            name="uq_portal_import_items_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("portal_import_jobs.id", ondelete="CASCADE"), index=True
    )
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    class_id: Mapped[int] = mapped_column(
        ForeignKey("portal_classes.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    parent_id: Mapped[str] = mapped_column(String(128), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    decision: Mapped[str] = mapped_column(String(20), default="skip")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    message: Mapped[str] = mapped_column(Text, default="")
    portal_type: Mapped[str] = mapped_column(String(20), default="")
    portal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_version: Mapped[str] = mapped_column(String(64), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    duplicate_of: Mapped[str] = mapped_column(String(128), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class ContentBlob(Base):
    """Metadata for one immutable, content-addressed file under content/."""

    __tablename__ = "portal_content_blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    rel_path: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(100))
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LessonSource(Base):
    """Provenance for an imported lesson (1:1 with ``portal_lessons``)."""

    __tablename__ = "portal_lesson_sources"

    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("portal_lessons.id", ondelete="CASCADE"), primary_key=True
    )
    source_kind: Mapped[str] = mapped_column(
        String(30), default="manual", index=True
    )
    source_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    source_version: Mapped[str] = mapped_column(String(64), default="")
    topic_name: Mapped[str] = mapped_column(String(200), default="")
    render_mode: Mapped[str] = mapped_column(String(20), default="text")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    original_title: Mapped[str] = mapped_column(String(300), default="")
    source_blob_sha: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reused_from_lesson_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )


class SlideContent(Base):
    """Rich content for one lesson slide (1:1 with ``portal_lesson_slides``).

    Imported text lessons use ``body_html``; imported decks use
    ``image_blob_sha``. The plain ``LessonSlide.body``/``code`` fields remain
    for manually authored slides.
    """

    __tablename__ = "portal_slide_content"

    slide_id: Mapped[int] = mapped_column(
        ForeignKey("portal_lesson_slides.id", ondelete="CASCADE"),
        primary_key=True,
    )
    image_blob_sha: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    alt_text: Mapped[str] = mapped_column(String(300), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    text_snapshot: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")


class AssignmentContent(Base):
    """Rich instructions and provenance for an imported assignment."""

    __tablename__ = "portal_assignment_content"

    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("portal_assignments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    topic_name: Mapped[str] = mapped_column(String(200), default="")
    source_kind: Mapped[str] = mapped_column(String(30), default="")
    source_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    source_version: Mapped[str] = mapped_column(String(64), default="")
    original_title: Mapped[str] = mapped_column(String(300), default="")
    attachments_json: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reused_from_assignment_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )


class AssignmentAttachment(Base):
    """A locally stored attachment shown on the student assignment page.

    Only rows with a ``blob_sha`` are servable. Drive ids and source links are
    never stored here; the importer keeps those in teacher-only import state.
    """

    __tablename__ = "portal_assignment_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("portal_assignments.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), default="")
    mime_type: Mapped[str] = mapped_column(String(100), default="")
    blob_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ClassroomCatalog(Base):
    """The teacher's last scan of Classroom courses and Drive decks.

    The picker must open instantly and must not hit Google on every view, so
    one explicit scan stores the normalized list here. ``catalog_json`` holds
    only teacher-facing metadata (titles, topics, attachment names, rubric
    suggestions); media is still downloaded at import time.

    ``status`` is ``empty`` before the first scan, ``refreshing`` while the
    background worker runs, ``ready`` afterwards, and ``failed`` when a scan
    could not reach Google (the previous catalog is kept).
    """

    __tablename__ = "portal_classroom_catalog"
    __table_args__ = (
        UniqueConstraint("teacher_id", name="uq_portal_classroom_catalog_teacher"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="empty")
    error: Mapped[str] = mapped_column(Text, default="")
    catalog_json: Mapped[str] = mapped_column(Text, default="")
    new_ids_json: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# Phase 3: submissions and grading
# ---------------------------------------------------------------------------

# Submission and job states. ``deferred`` is deliberately distinct from
# ``failed``: the work is valid, but every Groq key is out of daily quota and
# the job will be retried without the student doing anything.
SUBMISSION_QUEUED = "queued"
SUBMISSION_GRADING = "grading"
SUBMISSION_GRADED = "graded"
SUBMISSION_FAILED = "failed"
SUBMISSION_DEFERRED = "deferred"
# The teacher let one student continue an exam they handed in by accident. It
# is deliberately outside the "attempt taken" states: the single attempt is
# suspended until the student hands in again, which moves the row back to
# ``queued`` and locks it once more. Only an exam ever enters this state.
SUBMISSION_REOPENED = "reopened"

JOB_STATES = (
    SUBMISSION_QUEUED,
    SUBMISSION_GRADING,
    SUBMISSION_GRADED,
    SUBMISSION_FAILED,
    SUBMISSION_DEFERRED,
    "superseded",
)

# A grade whose ``configured_model`` carries this marker was imported from a
# Google Classroom export. It is not engine output: there is no submission
# text, no rubric and no audit behind it, and the UI must not offer a regrade.
GRADE_SOURCE_CLASSROOM = "classroom-import"
ACTIVE_JOB_STATES = (
    SUBMISSION_QUEUED,
    SUBMISSION_GRADING,
    SUBMISSION_DEFERRED,
)


class Submission(Base):
    """One student's current hand-in for one assignment.

    There is at most one row per (assignment, student); a resubmission updates
    the text and bumps ``revision``. ``first_submitted_at`` is written once and
    **never** touched again, because the engine's late policy resolves lateness
    from the original hand-in time - a late resubmission of on-time work must
    not be penalised.
    """

    __tablename__ = "portal_submissions"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "student_id",
            name="uq_portal_submissions_assignment_student",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("portal_assignments.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    # The student's text exactly as typed; the adapter passes it verbatim.
    text: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(
        String(20), default=SUBMISSION_QUEUED, index=True
    )
    first_submitted_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Submission assignment={self.assignment_id} "
            f"student={self.student_id} rev={self.revision} {self.status}>"
        )


class WorksheetResponse(Base):
    """Structured widget state for one revision of a mixed worksheet.

    The graded text stays one readable labelled document; this row only exists
    so a reloaded page can restore table cells and radio choices exactly. It is
    keyed by revision, so an older payload can never leak into newer work, and
    it is deleted with its submission through the foreign key.
    """

    __tablename__ = "portal_worksheet_responses"

    submission_id: Mapped[int] = mapped_column(
        ForeignKey("portal_submissions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<WorksheetResponse submission={self.submission_id} "
            f"rev={self.revision}>"
        )


class AnswerDraft(Base):
    """One student's in-progress work, saved before they press submit.

    A phone can refresh, a tab can close and a shared computer can lose the
    page; a draft keeps the typed answers on the server so the student can
    continue from any device. A draft never creates a submission, job or
    grade. Submitting deletes it, and a draft older than the latest submission
    is ignored so it can never overwrite newer graded work.
    """

    __tablename__ = "portal_answer_drafts"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "student_id",
            name="uq_portal_answer_drafts_assignment_student",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("portal_assignments.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<AnswerDraft assignment={self.assignment_id} "
            f"student={self.student_id}>"
        )


class ExamAllocation(Base):
    """Which paper one student sits, and the seed that shuffles it.

    The row is written once, the first time the student opens the exam after it
    starts, and never changed. That is what makes a refresh, a second device or
    a dropped connection safe: the version and the question order come from
    stored values, not from a fresh random draw, so answers already typed can
    never land under a different question.

    ``seed`` drives every shuffle (question order inside each part, and the
    option order inside a multiple-choice question). Field names are derived
    from item/part keys, which do not move, so the composed document and the
    answer key stay aligned no matter what order the student saw.
    """

    __tablename__ = "portal_exam_allocations"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "student_id",
            name="uq_portal_exam_allocations_assignment_student",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("portal_assignments.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(24))
    seed: Mapped[int] = mapped_column(BigInteger)
    allocated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExamAllocation assignment={self.assignment_id} "
            f"student={self.student_id} version={self.version}>"
        )


class ExamStudentWindow(Base):
    """A personal sitting window for one student on one exam.

    The assignment window is the class-wide rule; this row is the teacher's
    explicit exception for a make-up sitting. It replaces *both* ends for that
    student only - a student finishing an exam they missed may sit after the
    class window closed, and a student moved earlier may sit before it opened.
    Keeping it in its own table means the grant can exist before the paper is
    ever opened, and deleting it restores the class-wide window untouched.
    """

    __tablename__ = "portal_exam_windows"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "student_id",
            name="uq_portal_exam_windows_assignment_student",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("portal_assignments.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    opens_at: Mapped[datetime] = mapped_column(DateTime)
    closes_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExamStudentWindow assignment={self.assignment_id} "
            f"student={self.student_id}>"
        )


class GradingJob(Base):
    """One queued grading attempt, processed by the single grading worker.

    A resubmission creates a job for the new ``revision``. A regrade requested
    by the teacher creates another job for the same revision; only one active
    job per revision is allowed, so a double click cannot buy two model calls.

    ``next_attempt_at`` is set when the job is deferred because every provider
    hit its *daily* quota. ``attempts`` counts claim cycles, including quota
    deferrals (each deferral already proves the quota is still spent).
    """

    __tablename__ = "portal_grading_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("portal_submissions.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(
        String(20), default=SUBMISSION_QUEUED, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<GradingJob submission={self.submission_id} "
            f"rev={self.revision} {self.state}>"
        )


class Grade(Base):
    """The persisted engine output for one submission revision.

    ``result_json`` is the full ``GradingResult`` model dump, so the original
    artifact can be re-read without the engine. ``score``, ``feedback_hebrew``,
    ``deduction_json`` and ``audit_json`` are denormalised copies: lists and
    teacher filtering must not parse a large blob per row.
    """

    __tablename__ = "portal_grades"
    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            "revision",
            name="uq_portal_grades_submission_revision",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("portal_submissions.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_grading_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    score: Mapped[int] = mapped_column(Integer, default=0)
    feedback_hebrew: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="")
    deduction_json: Mapped[str] = mapped_column(Text, default="")
    audit_json: Mapped[str] = mapped_column(Text, default="")
    audit_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    audit_needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True
    )
    audit_summary: Mapped[str] = mapped_column(String(500), default="")
    rubric_sha256: Mapped[str] = mapped_column(String(64), default="")
    rubric_title: Mapped[str] = mapped_column(String(300), default="")
    late_penalty_points: Mapped[int] = mapped_column(Integer, default=0)
    configured_model: Mapped[str] = mapped_column(String(120), default="")
    graded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), nullable=True
    )
    review_note: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Grade submission={self.submission_id} "
            f"rev={self.revision} score={self.score}>"
        )


class ExamRecheckPreview(Base):
    """A dry run of the exam checker, shown to the teacher before it is applied.

    Re-marking a class after an answer-key fix can move many scores. The
    preview stores what *would* happen - one row per hand-in with the old and
    new points of every question - so the teacher can compare and press apply.
    Nothing here touches ``portal_grades``; the payload is the only copy.

    One row per assignment: starting a new preview replaces the old one, and a
    payload is only trusted while ``state`` is ``ready``.
    """

    __tablename__ = "portal_exam_recheck_previews"
    __table_args__ = (
        UniqueConstraint("assignment_id", name="uq_exam_recheck_previews_assignment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("portal_assignments.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16), default="running")
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="")
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExamRecheckPreview assignment={self.assignment_id} "
            f"state={self.state} {self.done}/{self.total}>"
        )


class ExamLlmFeedback(Base):
    """One teacher correction of a model-scored exam question.

    When the teacher overrides the points the model gave a question (the
    ``ניקוד`` + ``הערה`` fields on the review paper), the correction is stored
    here: what the student answered, what the model awarded, what the teacher
    decided, and why. At grading time a few recent corrections for the same
    question travel with the prompt as compact examples, so the model learns
    the teacher's calibration. The block is hard-capped (see
    :mod:`portal.exams.feedback`) so it can never grow a request into a 429.

    Scoped by exam definition and question key, not by class: the same question
    recalibrates every class that sits it. ``answer`` and ``note`` are trimmed
    snapshots, never the live rubric.
    """

    __tablename__ = "portal_exam_llm_feedback"
    __table_args__ = (
        Index("ix_exam_llm_feedback_exam_item", "exam_config", "item_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_config: Mapped[str] = mapped_column(String(64), default="")
    item_key: Mapped[str] = mapped_column(String(64), default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    model_points: Mapped[int] = mapped_column(Integer, default=0)
    teacher_points: Mapped[int] = mapped_column(Integer, default=0)
    maximum: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    submission_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_submissions.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExamLlmFeedback {self.exam_config}/{self.item_key} "
            f"{self.model_points}->{self.teacher_points}/{self.maximum}>"
        )


class ExamGradingProfile(Base):
    """A distilled grading-style profile for one exam question.

    The correction log (:class:`ExamLlmFeedback`) grows without limit, but the
    model cannot read all of it on every paper without swelling the request into
    a 429. So once a question gathers enough corrections, the portal spends one
    background model call to *distill* them into this short, bounded profile -
    the teacher's general grading style for that question, not specific students.
    Grading injects only this small profile, so the per-paper prompt stays tiny
    no matter how many corrections accumulate.

    ``source_fingerprint`` records which corrections the profile reflects, so the
    distiller knows when new ones make it stale. ``locked`` is set when the teacher
    edits the profile by hand: automatic distillation then leaves it alone until
    the teacher asks for a refresh. Scoped by exam + question, like the log.
    """

    __tablename__ = "portal_exam_grading_profile"
    __table_args__ = (
        UniqueConstraint("exam_config", "item_key", name="uq_exam_profile_exam_item"),
        Index("ix_exam_grading_profile_exam", "exam_config"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_config: Mapped[str] = mapped_column(String(64), default="")
    item_key: Mapped[str] = mapped_column(String(64), default="")
    profile: Mapped[str] = mapped_column(Text, default="")
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    source_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    distilled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExamGradingProfile {self.exam_config}/{self.item_key} "
            f"chars={len(self.profile)} sources={self.source_count} "
            f"locked={self.locked}>"
        )


class Message(Base):
    """One chat message between a student and their teacher.

    A conversation is the ``(teacher_id, student_id)`` pair. Most messages are
    general (``assignment_id`` is NULL); a message sent from the bubble inside a
    task page carries that assignment and a denormalised ``context_label`` so the
    teacher sees which task the student is asking about even if the assignment is
    later renamed or removed. ``sender_role`` is ``student`` or ``teacher``;
    ``read_at`` is set when the *recipient* opens the thread, which drives the
    unread badges. A new table: picked up by ``create_all``, no migration needed.
    """

    __tablename__ = "portal_messages"
    __table_args__ = (
        Index("ix_portal_messages_teacher_student", "teacher_id", "student_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_assignments.id", ondelete="SET NULL"), nullable=True
    )
    context_label: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    sender_role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Message {self.id} {self.sender_role} "
            f"t{self.teacher_id}/s{self.student_id}>"
        )


class ExamAutoCheckRun(Base):
    """One tick of the automatic exam model pass, for the teacher's activity log.

    The scheduler (:mod:`portal.exams.auto_check`) re-marks a few pending exam
    hand-ins every 3 hours. Until now the only trace was a journal line the teacher
    never sees. Each tick now appends one row here: when it ran, how many papers
    it selected, and how many it finished / deferred / failed, plus a compact
    per-paper detail (``detail_json``) so the screen can say *which* hand-in
    failed and *why* (quota, provider error) without re-reading every grade.

    This is an append-only activity log, not a source of truth for scores: the
    authoritative mark still lives on the ``Grade`` row. Rows are pruned to the
    most recent ``AUTO_CHECK_RUN_KEEP`` so the table cannot grow without bound.
    A new table: picked up by ``create_all``, no migration needed.
    """

    __tablename__ = "portal_exam_auto_check_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # "scheduler" for the 3-hour daemon, "cli" for a manual one-shot run.
    source: Mapped[str] = mapped_column(String(16), default="scheduler")
    selected: Mapped[int] = mapped_column(Integer, default=0)
    written: Mapped[int] = mapped_column(Integer, default=0)
    deferred: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    # One entry per selected paper: submission_id, assignment_id, student name,
    # outcome (written/deferred/failed), the resulting llm_state and a short
    # human-readable reason. Bounded by the batch size, so never large.
    detail_json: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExamAutoCheckRun {self.id} {self.source} "
            f"sel={self.selected} wrote={self.written} "
            f"defer={self.deferred} fail={self.failed}>"
        )
