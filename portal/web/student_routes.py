"""Student routes: my classes, lessons and published assignments.

Student-facing routes never load a rubric file except through the adapter, and
they only ever render the allowlisted projection from
:mod:`portal.grading.redaction`. Rubric paths, hashes and answer fields stay
server-side.
"""
from __future__ import annotations

import logging
import math
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from portal.grading import adapter, redaction, service
from portal.ingestion.storage import ContentStore, ContentStoreError
from portal.models import (
    ROLE_STUDENT,
    Assignment,
    AssignmentAttachment,
    AssignmentContent,
    ClassGroup,
    Enrollment,
    Lesson,
    LessonSlide,
    SlideContent,
    Submission,
    User,
)
from portal.sandbox.executor import (
    MAX_CODE_CHARS,
    MAX_STDIN_CHARS,
    SandboxBusy,
    SandboxError,
    SandboxExecutor,
)
from portal.timeutil import utcnow
from portal.web import assignment_form, student_feed
from portal import chat, drafts, worksheets
from portal.exams import config as exam_config
from portal.exams import service as exam_service
from portal.web.deps import (
    flash,
    get_db,
    render,
    require_user,
    verify_csrf,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/student")


def _enrollment(db: Session, class_id: int, student_id: int) -> Enrollment | None:
    return db.scalar(
        select(Enrollment).where(
            Enrollment.class_id == class_id,
            Enrollment.student_id == student_id,
        )
    )


def _assignments_for_student(
    db: Session, class_id: int, student: User
) -> list[Assignment]:
    """What one student's class list shows: everything published, plus more.

    The extra case is an exam kept as a draft so a later class cannot sit it,
    whose marks the teacher nevertheless released. Those results belong only to
    the students who already handed in, so a draft exam joins the list when this
    student has a hand-in on it - and for a student who has not sat it the list
    is exactly as empty as before. One rule, read from
    :func:`portal.exams.service.visible_to_student`, so the list and the page
    can never disagree about who may in.
    """
    rows = list(
        db.scalars(
            select(Assignment)
            .where(Assignment.class_id == class_id)
            .order_by(Assignment.created_at.desc())
        ).all()
    )
    if not rows:
        return []
    handed_in = {
        submission.assignment_id: submission
        for submission in db.scalars(
            select(Submission).where(
                Submission.student_id == student.id,
                Submission.assignment_id.in_([item.id for item in rows]),
            )
        ).all()
    }
    return [
        item
        for item in rows
        if exam_service.visible_to_student(item, handed_in.get(item.id))
    ]


def _published_lessons_for_class(db: Session, class_id: int) -> list[Lesson]:
    return list(
        db.scalars(
            select(Lesson)
            .where(
                Lesson.class_id == class_id,
                Lesson.published.is_(True),
            )
            .order_by(Lesson.created_at.desc(), Lesson.id.desc())
        ).all()
    )


def _visible_lessons(db: Session, student: User) -> list[Lesson]:
    """Published global lessons plus lessons of classes the student joined."""
    class_ids = select(Enrollment.class_id).where(
        Enrollment.student_id == student.id
    )
    return list(
        db.scalars(
            select(Lesson)
            .where(
                Lesson.published.is_(True),
                or_(
                    Lesson.class_id.is_(None),
                    Lesson.class_id.in_(class_ids),
                ),
            )
            .order_by(Lesson.created_at.desc(), Lesson.id.desc())
        ).all()
    )


def _slide_counts(db: Session, lesson_ids: list[int]) -> dict[int, int]:
    if not lesson_ids:
        return {}
    rows = db.execute(
        select(LessonSlide.lesson_id, func.count(LessonSlide.id))
        .where(LessonSlide.lesson_id.in_(lesson_ids))
        .group_by(LessonSlide.lesson_id)
    ).all()
    return {int(lesson_id): int(count) for lesson_id, count in rows}


def _ordered_slides(db: Session, lesson_id: int) -> list[LessonSlide]:
    return list(
        db.scalars(
            select(LessonSlide)
            .where(LessonSlide.lesson_id == lesson_id)
            .order_by(LessonSlide.position, LessonSlide.id)
        ).all()
    )


def _visible_lesson(
    db: Session, lesson_id: int, student: User
) -> Lesson:
    lesson = db.get(Lesson, lesson_id)
    if lesson is None or not lesson.published:
        raise HTTPException(status_code=404, detail="Lesson not found.")
    if lesson.class_id is not None and _enrollment(
        db, lesson.class_id, student.id
    ) is None:
        raise HTTPException(status_code=404, detail="Lesson not found.")
    return lesson


@router.get("")
def dashboard(
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if student.role != ROLE_STUDENT:
        return RedirectResponse("/teacher", status_code=303)
    classes = db.scalars(
        select(ClassGroup)
        .join(Enrollment, Enrollment.class_id == ClassGroup.id)
        .where(Enrollment.student_id == student.id)
        .order_by(ClassGroup.name)
    ).all()
    catalogs: dict[int, object] = {}
    feeds: dict[int, list] = {}
    for group in classes:
        if group.teacher_id not in catalogs:
            catalogs[group.teacher_id] = student_feed.load_catalog(
                db, group.teacher_id
            )
        group_lessons = _published_lessons_for_class(db, group.id)
        group_assignments = _assignments_for_student(db, group.id, student)
        feeds[group.id] = student_feed.build_class_feed(
            db,
            group,
            group_lessons,
            group_assignments,
            _slide_counts(db, [lesson.id for lesson in group_lessons]),
            service.student_status_map(
                db, student.id, [item.id for item in group_assignments]
            ),
            "/student",
            catalog=catalogs[group.teacher_id],
        )
    global_lessons = list(
        db.scalars(
            select(Lesson)
            .where(Lesson.published.is_(True), Lesson.class_id.is_(None))
            .order_by(Lesson.created_at.desc(), Lesson.id.desc())
        ).all()
    )
    return render(
        request,
        "student_dashboard.html",
        user=student,
        classes=classes,
        feeds=feeds,
        global_lessons=global_lessons,
        global_slide_counts=_slide_counts(
            db, [lesson.id for lesson in global_lessons]
        ),
    )


@router.post("/join", dependencies=[Depends(verify_csrf)])
def join_class(
    request: Request,
    code: str = Form(default=""),
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if student.role != ROLE_STUDENT:
        return RedirectResponse("/teacher", status_code=303)
    normalized = "".join((code or "").split()).upper()
    group = db.scalar(select(ClassGroup).where(ClassGroup.code == normalized))
    if group is None:
        flash(request, "קוד הכיתה לא נמצא. בדקו אותו עם המורה.", kind="error")
        return RedirectResponse("/student", status_code=303)

    if _enrollment(db, group.id, student.id) is not None:
        flash(request, f"כבר רשומים לכיתה '{group.name}'.", kind="info")
        return RedirectResponse(f"/student/classes/{group.id}", status_code=303)

    db.add(Enrollment(class_id=group.id, student_id=student.id))
    try:
        db.commit()
    except IntegrityError:
        # A double-submit (or two tabs) raced the uniqueness constraint; the
        # student is enrolled either way, so this is not an error for them.
        db.rollback()
        flash(request, f"כבר רשומים לכיתה '{group.name}'.", kind="info")
        return RedirectResponse(f"/student/classes/{group.id}", status_code=303)
    flash(request, f"נרשמתם לכיתה '{group.name}'.", kind="success")
    return RedirectResponse(f"/student/classes/{group.id}", status_code=303)


@router.get("/classes/{class_id}")
def class_page(
    class_id: int,
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if student.role != ROLE_STUDENT:
        return RedirectResponse("/teacher", status_code=303)
    if _enrollment(db, class_id, student.id) is None:
        raise HTTPException(status_code=404, detail="Class not found.")
    group = db.get(ClassGroup, class_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Class not found.")
    lessons = _published_lessons_for_class(db, group.id)
    assignments = _assignments_for_student(db, group.id, student)
    slide_counts = _slide_counts(db, [lesson.id for lesson in lessons])
    statuses = service.student_status_map(
        db, student.id, [item.id for item in assignments]
    )
    topics = student_feed.build_class_feed(
        db,
        group,
        lessons,
        assignments,
        slide_counts,
        statuses,
        "/student",
        catalog=student_feed.load_catalog(db, group.teacher_id),
    )
    return render(
        request,
        "student_class.html",
        user=student,
        group=group,
        assignments=assignments,
        lessons=lessons,
        slide_counts=slide_counts,
        submission_status=statuses,
        topics=topics,
    )


def _exam_attachments(db: Session, assignment: Assignment) -> list:
    return list(
        db.scalars(
            select(AssignmentAttachment)
            .where(AssignmentAttachment.assignment_id == assignment.id)
            .order_by(AssignmentAttachment.position, AssignmentAttachment.id)
        ).all()
    )


def _exam_page(
    request: Request,
    db: Session,
    student: User,
    assignment: Assignment,
    group: ClassGroup | None,
    config,
):
    """Render one exam: the gate, the open paper, or the hand-in state.

    The paper is allocated on the first open inside the window and never
    re-drawn. After the single hand-in the teacher's release decides what is
    shown: only a confirmation ("held"), the score by itself ("scores"), or the
    score and the paper exactly as it was sat ("full"). Marking happens at
    hand-in, so hiding the result is what keeps a class sitting later from
    seeing a paper that was already graded.
    """
    window = exam_service.student_window(db, assignment, student)
    state = exam_service.window_state(assignment, window=window)
    submission = service.submission_for(db, assignment.id, student.id)
    release = exam_service.release_state(assignment)
    # A draft exam is never a sitting: the only student who can reach it already
    # handed in, and came for the mark the teacher released. Reading that as
    # "taken" keeps the gate, the editable paper and the hand-in button off the
    # page, so the hidden paper stays hidden even for the child who sat it.
    taken = exam_service.attempt_taken(submission) or not assignment.published
    content = db.get(AssignmentContent, assignment.id)
    attachments = _exam_attachments(db, assignment)

    if not taken and state != exam_service.OPEN:
        return render(
            request,
            "student_exam_gate.html",
            user=student,
            group=group,
            assignment=assignment,
            content=content,
            attachments=attachments,
            exam=config,
            exam_state=state,
            exam_personal_window=window.personal,
            exam_opens_at=window.opens_at,
            exam_closes_at=window.closes_at,
        )

    if taken:
        paper = exam_service.review_paper(db, assignment, student, config)
    else:
        paper = exam_service.student_paper(db, assignment, student, config)
    if paper is None:
        logger.error(
            "Cannot build the paper for assignment %s student %s",
            assignment.id,
            student.id,
        )
        raise HTTPException(
            status_code=503,
            detail="דף המבחן אינו זמין כרגע. פנו למורה.",
        )

    if taken and release != exam_service.RELEASE_FULL:
        # The student gets a confirmation, and at most the number. The paper,
        # the explanations and the submitted answers stay on the server until
        # the teacher publishes them.
        grade = service.current_grade(db, submission) if submission else None
        return render(
            request,
            "student_exam_submitted.html",
            user=student,
            group=group,
            assignment=assignment,
            content=content,
            attachments=attachments,
            exam=config,
            exam_release=release,
            exam_release_label=exam_service.release_label(assignment),
            exam_version_label=paper.version.label,
            exam_total_points=paper.total_points,
            submission=submission,
            grade_view=(
                service.student_grade_view(grade)
                if release == exam_service.RELEASE_SCORES
                else None
            ),
            status_labels=service.STATUS_LABELS,
            status_badges=service.STATUS_BADGES,
        )

    # The display paper is what the student sees: their version, their order.
    # Values are keyed by item key, so the arrangement never moves an answer.
    worksheet = paper.display
    worksheet_values: dict = {}
    if taken:
        worksheet_values = {
            key: value
            for key, value in worksheets.load_values(db, submission).items()
            if isinstance(value, dict)
        }
    else:
        draft = drafts.load(db, assignment.id, student.id)
        if draft is not None and draft.kind == "worksheet":
            worksheet_values = {
                key: value
                for key, value in draft.values.items()
                if isinstance(value, dict)
            }

    grade = service.current_grade(db, submission) if submission else None
    job = service.latest_job(db, submission) if submission else None
    return render(
        request,
        "student_assignment.html",
        user=student,
        group=group,
        assignment=assignment,
        rubric=None,
        content=content,
        attachments=attachments,
        submission=submission,
        grade=grade,
        grade_view=service.student_grade_view(grade),
        job=job,
        status_labels=service.STATUS_LABELS,
        status_badges=service.STATUS_BADGES,
        task_status_labels=service.TASK_STATUS_LABELS,
        max_submission_chars=service.MAX_SUBMISSION_CHARS,
        questions=None,
        answers={},
        worksheet=worksheet,
        worksheet_values=worksheet_values,
        answered_count=worksheet.count_answered(worksheet_values),
        legacy_text="",
        draft_restored=bool(worksheet_values) and not taken,
        code_value="",
        fallback_needs_stdin=False,
        exam=config,
        exam_state=state,
        exam_locked=taken,
        exam_release=release,
        exam_version_label=paper.version.label,
        exam_total_points=paper.total_points,
        exam_opens_at=window.opens_at,
        exam_closes_at=window.closes_at,
        server_now=utcnow(),
    )


@router.get("/assignments/{assignment_id}")
def assignment_page(
    assignment_id: int,
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if student.role != ROLE_STUDENT:
        return RedirectResponse("/teacher", status_code=303)
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or _enrollment(db, assignment.class_id, student.id) is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    submission = service.submission_for(db, assignment.id, student.id)
    if not exam_service.visible_to_student(assignment, submission):
        # Not a page this student has - which for a hidden exam means the one
        # child who never sat it, and for any draft means everyone.
        raise HTTPException(status_code=404, detail="Assignment not found.")
    group = db.get(ClassGroup, assignment.class_id)

    # An exam/quiz event carries a teacher-entered score and no rubric. There
    # is nothing to hand in, so it gets a read-only score page instead of the
    # assignment form (and never a rubric-binding error).
    if student_feed.is_grade_only(assignment):
        grade = service.current_grade(db, submission) if submission else None
        content = db.get(AssignmentContent, assignment.id)
        attachments = db.scalars(
            select(AssignmentAttachment)
            .where(AssignmentAttachment.assignment_id == assignment.id)
            .order_by(AssignmentAttachment.position, AssignmentAttachment.id)
        ).all()
        return render(
            request,
            "student_exam.html",
            user=student,
            group=group,
            assignment=assignment,
            content=content,
            attachments=attachments,
            submission=submission,
            grade=grade,
            result=service.student_grade_view(grade),
            status_labels=service.STATUS_LABELS,
            status_badges=service.STATUS_BADGES,
            task_status_labels=service.TASK_STATUS_LABELS,
        )

    # An exam is not one fixed paper: each student sits a version, in an order,
    # drawn once for them. That replaces the single rubric below entirely, so
    # the exam branch must come before any rubric binding.
    exam_name = str(assignment.exam_config or "").strip()
    if exam_name:
        config = exam_config.load_exam(exam_name)
        if config is None:
            logger.error(
                "Cannot serve exam %r for assignment %s: definition is invalid",
                exam_name,
                assignment.id,
            )
            raise HTTPException(
                status_code=503,
                detail="המבחן אינו זמין כרגע. פנו למורה ונסו שוב מאוחר יותר.",
            )
        return _exam_page(request, db, student, assignment, group, config)

    # The file must still match the bytes the teacher published. A changed or
    # missing rubric is a teacher-side problem, so the student gets a neutral
    # unavailable message and the details stay in the server log.
    try:
        binding = adapter.load_rubric(
            assignment.rubric_path,
            expected_sha256=assignment.rubric_sha256,
        )
        rubric = redaction.redact_rubric(binding.rubric)
    except adapter.RubricBindingError:
        logger.exception(
            "Cannot serve assignment %s: rubric binding failed",
            assignment.id,
        )
        raise HTTPException(
            status_code=503,
            detail="המטלה אינה זמינה כרגע. פנו למורה ונסו שוב מאוחר יותר.",
        )

    content = db.get(AssignmentContent, assignment.id)
    attachments = db.scalars(
        select(AssignmentAttachment)
        .where(AssignmentAttachment.assignment_id == assignment.id)
        .order_by(AssignmentAttachment.position, AssignmentAttachment.id)
    ).all()
    submission = service.submission_for(db, assignment.id, student.id)
    grade = service.current_grade(db, submission) if submission else None
    job = service.latest_job(db, submission) if submission else None

    # A draft is work the student has typed but not submitted. It wins only
    # when it is newer than the current submission, so an old draft can never
    # replace graded work.
    draft = drafts.load(db, assignment.id, student.id)
    draft_fresh = draft is not None and (
        submission is None or draft.updated_at > submission.updated_at
    )
    draft_restored = False
    code_value = ""

    # A curated x.3 sheet keeps its own item shapes (table, choices, code).
    # Everything else keeps the per-rubric-item frames from Phase 3.
    worksheet = worksheets.for_rubric(assignment.rubric_path)
    worksheet_values: dict = {}
    if worksheet is not None:
        questions = worksheet.questions()
        if draft_fresh and draft.kind == "worksheet":
            worksheet_values = {
                key: value
                for key, value in draft.values.items()
                if isinstance(value, dict)
            }
            draft_restored = True
        else:
            worksheet_values = worksheets.load_values(db, submission)
        answers = {}
        legacy_text = ""
        if submission and not worksheet_values and not draft_restored:
            # A hand-in from before the sheet was curated. It still grades,
            # but its widgets cannot be reconstructed, so it stays visible as
            # one text block instead of being guessed into the wrong cells.
            legacy_text = (submission.text or "").strip()
        answered_count = worksheet.count_answered(worksheet_values)
    else:
        # One answer frame per rubric item, in the same order the teacher sees.
        # A submission written before this layout has no per-question headings;
        # it is shown as the previous single text instead of being guessed into
        # the wrong frames.
        questions = assignment_form.build_questions(rubric)
        if draft_fresh and draft.kind == "answers":
            answers = {
                str(key): str(value)
                for key, value in draft.values.items()
                if isinstance(value, str)
            }
            draft_restored = True
        elif submission and questions:
            answers = assignment_form.split_answers(
                questions, submission.text or ""
            )
        else:
            answers = {}
        legacy_text = ""
        if submission and questions and not answers and not draft_restored:
            legacy_text = (submission.text or "").strip()
        answered_count = sum(
            1
            for question in questions
            if (answers.get(question.key) or "").strip()
        )
        if not questions:
            # A rubric with no recognizable items keeps the old single box.
            if draft_fresh and draft.kind == "code":
                code_value = str(draft.values.get("code") or "")
                draft_restored = bool(code_value.strip())
            elif submission is not None:
                code_value = submission.text or ""

    return render(
        request,
        "student_assignment.html",
        user=student,
        group=group,
        assignment=assignment,
        rubric=rubric,
        content=content,
        attachments=attachments,
        submission=submission,
        grade=grade,
        grade_view=service.student_grade_view(grade),
        job=job,
        status_labels=service.STATUS_LABELS,
        status_badges=service.STATUS_BADGES,
        task_status_labels=service.TASK_STATUS_LABELS,
        max_submission_chars=service.MAX_SUBMISSION_CHARS,
        questions=questions,
        answers=answers,
        worksheet=worksheet,
        worksheet_values=worksheet_values,
        answered_count=answered_count,
        legacy_text=legacy_text,
        draft_restored=draft_restored,
        code_value=code_value,
        fallback_needs_stdin=assignment_form.rubric_mentions_input(rubric),
    )


@router.get("/assignments/{assignment_id}/grading-status")
def assignment_grading_status(
    assignment_id: int,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Tiny JSON probe for the pending-grade poller in app.js.

    The page must not reload itself blindly while a grade is pending: that
    jumps to the top and wipes code the student is typing. The poller asks
    this endpoint instead and reloads only when the status actually leaves
    queued/grading. Same visibility rules as the assignment page itself.
    """
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or _enrollment(db, assignment.class_id, student.id) is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    submission = service.submission_for(db, assignment.id, student.id)
    if not exam_service.visible_to_student(assignment, submission):
        raise HTTPException(status_code=404, detail="Assignment not found.")
    status = submission.status if submission is not None else "none"
    return JSONResponse(
        {
            "status": status,
            "pending": status in ("queued", "grading"),
            "revision": submission.revision if submission is not None else 0,
        }
    )


@router.post(
    "/assignments/{assignment_id}/submit",
    dependencies=[Depends(verify_csrf)],
)
async def submit_assignment(
    assignment_id: int,
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Queue the hand-in and answer at once.

    The page sends one ``answer_<n>`` field per rubric item; they are composed
    into the single labelled document the engine grades. A legacy ``code``
    field is still accepted for old clients and tests.

    A browser form gets a 303 back to the assignment page. ``fetch`` callers
    send ``Accept: application/json`` and get the literal **202 Accepted**
    with the job id, which is what the editor uses so the UI never waits for
    the model.
    """
    wants_json = "application/json" in request.headers.get("accept", "").lower()
    # ``verify_csrf`` already read and cached the body, so this does not block
    # again. Declaring a fixed set of ``answer_*`` parameters would cap the
    # rubric at that number of questions.
    form = await request.form()

    def failure(message: str, status: int):
        if wants_json:
            return JSONResponse({"error": message}, status_code=status)
        flash(request, message, kind="error")
        return RedirectResponse(
            f"/student/assignments/{assignment_id}#submission", status_code=303
        )

    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or not assignment.published:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if _enrollment(db, assignment.class_id, student.id) is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if student_feed.is_grade_only(assignment):
        return failure(
            "זו אירוע ציון - אין מטלה להגשה. הציון מוזן על ידי המורה.",
            400,
        )

    # An exam is a closed, one-shot paper: the window and the single attempt
    # are checked before anything is stored, and the student's own version is
    # used so the composed document matches the numbering they saw.
    exam_name = str(assignment.exam_config or "").strip()
    single_attempt = False
    if exam_name:
        config = exam_config.load_exam(exam_name)
        if config is None:
            logger.error(
                "Refusing submission on assignment %s: exam %r is invalid",
                assignment.id,
                exam_name,
            )
            return failure(
                "המבחן אינו זמין כרגע. פנו למורה ונסו שוב מאוחר יותר.", 503
            )
        state = exam_service.student_window_state(db, assignment, student)
        if state == exam_service.BEFORE:
            return failure(
                "המבחן טרם נפתח. אפשר להגיש רק אחרי מועד הפתיחה.", 403
            )
        if state == exam_service.CLOSED:
            return failure(
                "המבחן נסגר. לא ניתן להגיש אחרי מועד הסגירה. פנו למורה.", 403
            )
        if exam_service.attempt_taken(
            service.submission_for(db, assignment.id, student.id)
        ):
            return failure(
                "המבחן מוגש פעם אחת בלבד - ההגשה שלכם כבר נקלטה.", 409
            )
        paper = exam_service.student_paper(db, assignment, student, config)
        worksheet = paper.display
        single_attempt = True
    else:
        # A curated x.3 sheet parses its own field names (tables, radios, code)
        # and composes the same labelled document the engine has always
        # received.
        worksheet = worksheets.for_rubric(assignment.rubric_path)

    if worksheet is not None:
        worksheet_values = worksheet.collect(form)
        if not worksheet.has_any(worksheet_values):
            return failure(
                "לא הוגשה תשובה. ענו לפחות על שאלה אחת לפני ההגשה."
                if single_attempt
                else "לא הוגשה תשובה. ענו לפחות על תרגיל אחד לפני ההגשה.",
                400,
            )
        code = worksheet.compose(worksheet_values)
        answer_values = None
    else:
        worksheet_values = None
        answer_values = {
            key: value
            for key, value in form.items()
            if key.startswith(assignment_form.ANSWER_FIELD_PREFIX)
            and isinstance(value, str)
        }

    if worksheet is None and answer_values:
        # The rubric is needed to know the question order and headings. It is
        # the same file the page rendered, and the same failure mode: a teacher
        # side problem, never a reason to store a partial document.
        try:
            binding = adapter.load_rubric(
                assignment.rubric_path,
                expected_sha256=assignment.rubric_sha256,
            )
            questions = assignment_form.build_questions(
                redaction.redact_rubric(binding.rubric)
            )
        except adapter.RubricBindingError:
            logger.exception(
                "Cannot accept submission for assignment %s: rubric binding failed",
                assignment.id,
            )
            return failure(
                "המטלה אינה זמינה כרגע. פנו למורה ונסו שוב מאוחר יותר.", 503
            )
        answers = {
            question.key: answer_values.get(question.field_name, "")
            for question in questions
        }
        if not assignment_form.has_any_answer(questions, answers):
            return failure(
                "לא הוגש קוד. כתבו או הדביקו את הפתרון לפני ההגשה.", 400
            )
        code = assignment_form.compose_answers(questions, answers)
    elif worksheet is None:
        code = str(form.get("code") or "")

    if len(code) > service.MAX_SUBMISSION_CHARS:
        return failure(
            "הקוד ארוך מדי להגשה. קצרו אותו או העלו רק את החלק הרלוונטי.",
            413,
        )
    if not code.strip():
        return failure("לא הוגש קוד. כתבו או הדביקו את הפתרון לפני ההגשה.", 400)

    try:
        outcome = service.submit_work(
            db,
            assignment,
            student,
            code,
            single_attempt=single_attempt,
        )
    except service.AttemptLimitReached:
        # A second tab or a late double-click lost the race inside the same
        # call that writes the row, so the first attempt still stands.
        db.rollback()
        return failure(
            "המבחן מוגש פעם אחת בלבד - ההגשה שלכם כבר נקלטה.", 409
        )
    if worksheet is not None and worksheet_values is not None:
        # The payload is bound to the revision submit_work just produced, so
        # it always matches the graded text and never an older attempt.
        worksheets.save_values(db, outcome.submission, worksheet_values)
    # The hand-in is now the source of truth; a draft left behind would be
    # restored over it on the next visit.
    drafts.clear(db, assignment.id, student.id)
    db.commit()
    if outcome.job is not None:
        worker = getattr(request.app.state, "grading_worker", None)
        if worker is not None:
            worker.enqueue(outcome.job.id)

    if wants_json:
        return JSONResponse(
            {
                "accepted": True,
                "queued": outcome.job is not None,
                "submission_id": outcome.submission.id,
                "revision": outcome.submission.revision,
                "status": outcome.submission.status,
                "job_id": outcome.job.id if outcome.job is not None else None,
            },
            status_code=202,
        )

    if outcome.job is not None:
        flash(
            request,
            (
                "המבחן הוגש. הציון יפורסם כאן אחרי שהמורה תפרסם אותו."
                if single_attempt
                else "ההגשה נקלטה ותיבדק ברקע. אפשר להמשיך ללמוד - הציון יופיע כאן."
            ),
            kind="success",
        )
    else:
        flash(request, "ההגשה כבר נקלטה ובדיקה שלה קיימת.", kind="info")
    if single_attempt:
        # The popup that confirms the hand-in is driven by this flag, both in
        # the fetch path and after a plain form post.
        return RedirectResponse(
            f"/student/assignments/{assignment.id}?submitted=1#submitted",
            status_code=303,
        )
    return RedirectResponse(
        f"/student/assignments/{assignment.id}#submission", status_code=303
    )


@router.post(
    "/assignments/{assignment_id}/draft",
    dependencies=[Depends(verify_csrf)],
)
async def save_assignment_draft(
    assignment_id: int,
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the answers the student has typed, without submitting them.

    The page calls this a moment after typing stops and again when the tab is
    hidden. It accepts the same fields as the submit route but never creates a
    submission, job or grade. An empty form clears the stored draft.
    """
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or not assignment.published:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if _enrollment(db, assignment.class_id, student.id) is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if student_feed.is_grade_only(assignment):
        raise HTTPException(status_code=404, detail="Assignment not found.")

    form = await request.form()
    exam_name = str(assignment.exam_config or "").strip()
    if exam_name:
        # An exam autosaves only while it is open and unanswered. Outside the
        # window the page is a gate, so a late autosave has nothing to save.
        config = exam_config.load_exam(exam_name)
        if config is None:
            raise HTTPException(status_code=503, detail="Exam unavailable.")
        if exam_service.attempt_taken(
            service.submission_for(db, assignment.id, student.id)
        ):
            return JSONResponse({"saved": False, "locked": True})
        if (
            exam_service.student_window_state(db, assignment, student)
            != exam_service.OPEN
        ):
            return JSONResponse({"saved": False, "closed": True})
        paper = exam_service.student_paper(db, assignment, student, config)
        worksheet = paper.display
    else:
        worksheet = worksheets.for_rubric(assignment.rubric_path)
    if worksheet is not None:
        values = worksheet.collect(form)
        if not worksheet.has_any(values):
            drafts.clear(db, assignment.id, student.id)
            db.commit()
            return JSONResponse({"saved": False, "cleared": True})
        kind = "worksheet"
    else:
        prefix = assignment_form.ANSWER_FIELD_PREFIX
        answers = {
            key[len(prefix) :]: value
            for key, value in form.items()
            if key.startswith(prefix) and isinstance(value, str)
        }
        answers = {
            key: value[: service.MAX_SUBMISSION_CHARS]
            for key, value in answers.items()
        }
        if any(value.strip() for value in answers.values()):
            kind, values = "answers", answers
        else:
            code = str(form.get("code") or "")
            if not code.strip():
                drafts.clear(db, assignment.id, student.id)
                db.commit()
                return JSONResponse({"saved": False, "cleared": True})
            kind = "code"
            values = {"code": code[: service.MAX_SUBMISSION_CHARS]}

    if drafts.payload_size(kind, values) > drafts.MAX_DRAFT_BYTES:
        return JSONResponse(
            {"error": "הטיוטה גדולה מדי לשמירה. קצרו את התשובות."},
            status_code=413,
        )
    saved = drafts.save(
        db,
        assignment.id,
        student.id,
        kind=kind,
        values=values,
    )
    db.commit()
    return JSONResponse(
        {"saved": True, "updated_at": saved.updated_at.isoformat()}
    )


@router.get("/submissions/{submission_id}")
def submission_result(
    submission_id: int,
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The student's own result page: grade, feedback and task verdicts."""
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=404, detail="Submission not found.")
    submission = db.get(Submission, submission_id)
    if submission is None or submission.student_id != student.id:
        raise HTTPException(status_code=404, detail="Submission not found.")
    assignment = db.get(Assignment, submission.assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Submission not found.")
    if not exam_service.visible_to_student(assignment, submission):
        # A hidden exam whose marks were not released is not a page this
        # student has - even though the hand-in exists. Without this gate a
        # direct /student/submissions/{id} link would leak the title, the
        # submitted text and (once marked) the feedback for an assignment the
        # student cannot open. ``submission`` here is that student's own
        # hand-in, so a released hidden exam still opens for exactly the
        # children who sat it.
        raise HTTPException(status_code=404, detail="Submission not found.")
    group = db.get(ClassGroup, assignment.class_id)
    # An exam hand-in is marked instantly, but the teacher decides when the
    # result becomes visible. The same three states as the exam page apply here:
    # held hides everything, scores shows the number alone, full shows the
    # per-task detail and the submitted text.
    exam_name = str(assignment.exam_config or "").strip()
    exam_release = (
        exam_service.release_state(assignment) if exam_name else None
    )
    grade = service.current_grade(db, submission)
    if exam_release == exam_service.RELEASE_HELD:
        grade = None
    job = service.latest_job(db, submission)
    return render(
        request,
        "student_result.html",
        user=student,
        group=group,
        assignment=assignment,
        submission=submission,
        grade=grade,
        result=service.student_grade_view(grade),
        job=job,
        exam_release=exam_release,
        exam_release_label=(
            exam_service.release_label(assignment) if exam_name else ""
        ),
        status_labels=service.STATUS_LABELS,
        status_badges=service.STATUS_BADGES,
        task_status_labels=service.TASK_STATUS_LABELS,
    )


@router.get("/submissions/{submission_id}/grading-status")
def submission_grading_status(
    submission_id: int,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Status probe for the result page's pending card (see assignment probe)."""
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=404, detail="Submission not found.")
    submission = db.get(Submission, submission_id)
    if submission is None or submission.student_id != student.id:
        raise HTTPException(status_code=404, detail="Submission not found.")
    assignment = db.get(Assignment, submission.assignment_id)
    if assignment is None or not exam_service.visible_to_student(
        assignment, submission
    ):
        # Same door as the result page above: a hidden exam whose marks were
        # not released has no status probe for this student either.
        raise HTTPException(status_code=404, detail="Submission not found.")
    return JSONResponse(
        {
            "status": submission.status,
            "pending": submission.status in ("queued", "grading"),
            "revision": submission.revision,
        }
    )


# --------------------------------------------------------------------- chat
@router.get("/chat")
def chat_page(
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
    teacher: int | None = Query(default=None),
):
    """The student's chat tab: a thread with each teacher of their classes."""
    if student.role != ROLE_STUDENT:
        return RedirectResponse("/teacher/chat", status_code=303)
    tz = request.app.state.portal_settings.timezone
    conversations = chat.conversations_for_student(db, student.id)
    active_id = None
    if teacher is not None and any(
        c["teacher_id"] == teacher for c in conversations
    ):
        active_id = teacher
    elif conversations:
        active_id = conversations[0]["teacher_id"]
    messages: list[dict] = []
    active_name = ""
    if active_id is not None:
        chat.mark_read(db, active_id, student.id, reader_role=ROLE_STUDENT)
        db.commit()
        messages = [
            chat.message_view(m, viewer_role=ROLE_STUDENT, timezone_name=tz)
            for m in chat.thread(db, active_id, student.id)
        ]
        active = next(c for c in conversations if c["teacher_id"] == active_id)
        active_name = active["name"]
        active["unread"] = 0  # the open thread was just read
    return render(
        request,
        "student_chat.html",
        user=student,
        conversations=conversations,
        active_teacher=active_id,
        active_name=active_name,
        messages=messages,
        viewer_role=ROLE_STUDENT,
        max_message_chars=chat.MAX_MESSAGE_CHARS,
    )


@router.get("/chat/thread")
def chat_thread(
    request: Request,
    teacher: int = Query(...),
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """JSON: the open thread with one teacher, marked read for the student."""
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=403, detail="Student access required.")
    if not chat.can_message(db, student.id, teacher):
        raise HTTPException(status_code=403, detail="אין הרשאה לשיחה הזו.")
    chat.mark_read(db, teacher, student.id, reader_role=ROLE_STUDENT)
    db.commit()
    tz = request.app.state.portal_settings.timezone
    messages = [
        chat.message_view(m, viewer_role=ROLE_STUDENT, timezone_name=tz)
        for m in chat.thread(db, teacher, student.id)
    ]
    return JSONResponse(
        {
            "ok": True,
            "messages": messages,
            "unread": chat.total_unread(db, student.id, ROLE_STUDENT),
        }
    )


@router.post("/chat/send", dependencies=[Depends(verify_csrf)])
def chat_send(
    request: Request,
    teacher_id: int = Form(...),
    body: str = Form(default=""),
    assignment_id: str = Form(default=""),
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a message. From a task bubble the assignment sets teacher + context."""
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=403, detail="Student access required.")
    context = None
    if assignment_id.strip():
        try:
            aid = int(assignment_id.strip())
        except ValueError:
            aid = None
        if aid is not None:
            context = chat.assignment_context(db, aid, student.id)
            if context is None:
                return JSONResponse(
                    {"ok": False, "error": "המשימה לא נמצאה או שאינה שלך."},
                    status_code=400,
                )
    target = context["teacher_id"] if context else teacher_id
    if not chat.can_message(db, student.id, target):
        return JSONResponse(
            {"ok": False, "error": "אין הרשאה לשלוח הודעה למורה הזה."},
            status_code=403,
        )
    try:
        message = chat.send(
            db,
            teacher_id=target,
            student_id=student.id,
            sender_role=ROLE_STUDENT,
            body=body,
            assignment_id=context["assignment_id"] if context else None,
            context_label=context["label"] if context else "",
        )
        db.commit()
    except chat.ChatError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "teacher_id": target,
            "message": chat.message_view(
                message,
                viewer_role=ROLE_STUDENT,
                timezone_name=request.app.state.portal_settings.timezone,
            ),
        }
    )


@router.get("/chat/unread")
def chat_unread(
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if student.role != ROLE_STUDENT:
        return JSONResponse({"unread": 0})
    return JSONResponse(
        {"unread": chat.total_unread(db, student.id, ROLE_STUDENT)}
    )


# --------------------------------------------------------------------- lessons


@router.get("/lessons")
def lessons_index(
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if student.role != ROLE_STUDENT:
        return RedirectResponse("/teacher", status_code=303)
    lessons = _visible_lessons(db, student)
    class_names = {
        group.id: group.name
        for group in db.scalars(
            select(ClassGroup)
            .join(Enrollment, Enrollment.class_id == ClassGroup.id)
            .where(Enrollment.student_id == student.id)
        ).all()
    }
    return render(
        request,
        "student_lessons.html",
        user=student,
        lessons=lessons,
        class_names=class_names,
        slide_counts=_slide_counts(db, [lesson.id for lesson in lessons]),
    )


@router.get("/lessons/{lesson_id}")
def lesson_view(
    lesson_id: int,
    request: Request,
    slide: int = 0,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if student.role != ROLE_STUDENT:
        return RedirectResponse("/teacher", status_code=303)
    lesson = _visible_lesson(db, lesson_id, student)
    slides = _ordered_slides(db, lesson.id)
    index = max(0, min(slide, len(slides) - 1)) if slides else 0
    current = slides[index] if slides else None
    group = db.get(ClassGroup, lesson.class_id) if lesson.class_id else None
    content = db.get(SlideContent, current.id) if current is not None else None
    return render(
        request,
        "student_lesson.html",
        user=student,
        lesson=lesson,
        slides=slides,
        index=index,
        current=current,
        current_content=content,
        group=group,
        total=len(slides),
    )


# --------------------------------------------------------------------- media


def _content_store(request: Request) -> ContentStore:
    store = getattr(request.app.state, "content_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Content storage unavailable.")
    return store


def _media_headers(sha256: str) -> dict[str, str]:
    return {
        "ETag": f'"{sha256}"',
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }


_INLINE_MIME = {"image/webp", "image/png", "image/jpeg", "application/pdf"}


@router.get("/assignments/{assignment_id}/media/{attachment_id}")
def assignment_media(
    assignment_id: int,
    attachment_id: int,
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Serve one locally imported attachment after visibility checks.

    Every request re-checks who may see this assignment, class membership and
    that the row actually belongs to this assignment - the same door as the
    assignment page, so the pictures on a released result load for exactly the
    students the result itself loads for. A blob SHA is never part of the URL.
    """
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or _enrollment(db, assignment.class_id, student.id) is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    submission = service.submission_for(db, assignment.id, student.id)
    if not exam_service.visible_to_student(assignment, submission):
        raise HTTPException(status_code=404, detail="Assignment not found.")

    attachment = db.get(AssignmentAttachment, attachment_id)
    if (
        attachment is None
        or attachment.assignment_id != assignment.id
        or not attachment.blob_sha
    ):
        raise HTTPException(status_code=404, detail="Attachment not found.")
    try:
        path, blob = _content_store(request).path_for_sha(db, attachment.blob_sha)
    except ContentStoreError:
        raise HTTPException(status_code=404, detail="Attachment not found.")

    mime = (attachment.mime_type or blob.mime_type or "").strip().lower()
    if mime not in _INLINE_MIME:
        mime = "application/octet-stream"
    disposition = "inline" if mime in _INLINE_MIME else "attachment"
    filename = quote(
        (attachment.title or "attachment").replace('"', "").replace("\n", " ")
    )
    headers = {
        **_media_headers(blob.sha256),
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
    }
    return FileResponse(path, media_type=mime, headers=headers)


@router.get("/lessons/{lesson_id}/slides/{position}/image")
def slide_image(
    lesson_id: int,
    position: int,
    request: Request,
    student: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=404, detail="Lesson not found.")
    lesson = _visible_lesson(db, lesson_id, student)
    slides = _ordered_slides(db, lesson.id)
    if position < 0 or position >= len(slides):
        raise HTTPException(status_code=404, detail="Slide not found.")
    content = db.get(SlideContent, slides[position].id)
    if content is None or not content.image_blob_sha:
        raise HTTPException(status_code=404, detail="Slide image not found.")
    try:
        path, blob = _content_store(request).path_for_sha(
            db, content.image_blob_sha
        )
    except ContentStoreError:
        raise HTTPException(status_code=404, detail="Slide image not found.")
    return FileResponse(
        path,
        media_type=blob.mime_type or "image/webp",
        headers=_media_headers(blob.sha256),
    )


# ------------------------------------------------------------- code playground


@router.post("/sandbox/run")
async def run_sandbox(
    request: Request,
    code: str = Form(default=""),
    stdin: str = Form(default=""),
    student: User = Depends(require_user),
):
    """Run playground code for this student and return its output as JSON.

    The endpoint is deliberately conservative: CSRF checked by hand (so a
    failure is JSON, not an HTML error page), a per-student sliding-window
    rate limit, hard size caps and a bounded subprocess. Code is never saved
    and never leaves the temporary working directory.
    """
    try:
        await verify_csrf(request)
    except HTTPException as exc:
        # The IDE console is English, unlike the rest of the Hebrew portal.
        return JSONResponse(
            {
                "error": (
                    "Security token missing or invalid. "
                    "Refresh the page and try again."
                )
            },
            status_code=exc.status_code,
        )

    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=403, detail="Student access required.")
    if len(code) > MAX_CODE_CHARS:
        return JSONResponse(
            {
                "error": (
                    "Code is too long to run. Shorten it or run only the "
                    "relevant part."
                )
            },
            status_code=413,
        )
    if not code.strip():
        return JSONResponse(
            {"error": "No code to run."}, status_code=400
        )
    if len(stdin) > MAX_STDIN_CHARS:
        return JSONResponse(
            {
                "error": (
                    "Input (stdin) is too long. Shorten it and try again."
                )
            },
            status_code=413,
        )

    limiter = getattr(request.app.state, "sandbox_limiter", None)
    if limiter is not None:
        allowed, retry_after = limiter.allow(f"student:{student.id}")
        if not allowed:
            wait = max(1, math.ceil(retry_after))
            message = (
                "Too many runs in a short time. "
                f"Try again in {wait} seconds."
            )
            return JSONResponse(
                {"error": message, "retry_after": wait},
                status_code=429,
                headers={"Retry-After": str(wait)},
            )

    executor = getattr(request.app.state, "sandbox_executor", None)
    if executor is None:
        logger.error("Sandbox executor is not configured")
        return JSONResponse(
            {"error": "The run environment is unavailable right now."},
            status_code=503,
        )

    try:
        result = await run_in_threadpool(executor.run, code, stdin)
    except SandboxBusy:
        return JSONResponse(
            {
                "error": (
                    "The run environment is busy. Try again in a moment."
                )
            },
            status_code=429,
            headers={"Retry-After": "2"},
        )
    except SandboxError:
        logger.exception("Sandbox failed to start")
        return JSONResponse(
            {"error": "The run environment is unavailable right now."},
            status_code=500,
        )
    except Exception:
        logger.exception("Unexpected sandbox failure")
        return JSONResponse(
            {"error": "The run failed due to an internal error."},
            status_code=500,
        )

    payload = result.to_dict()
    payload["ok"] = result.exit_code == 0 and not result.timed_out
    payload["timeout_seconds"] = getattr(
        executor, "timeout_seconds", None
    )
    return JSONResponse(payload)


# ---------------------------------------------------- interactive IDE console
#
# The one-shot /sandbox/run endpoint above is still used for a full run with
# supplied stdin. The IDE console instead opens a session: stdout/stderr are
# polled incrementally and each line the student types is forwarded to the
# child, so ``input()`` prompts can be answered while the program is running.


def _sandbox_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _sandbox_csrf_failure(request: Request) -> JSONResponse | None:
    try:
        await verify_csrf(request)
    except HTTPException as exc:
        return _sandbox_error(
            "Security token missing or invalid. Refresh the page and try again.",
            exc.status_code,
        )
    return None


def _interactive_session(request: Request, session_id: str, student: User):
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=403, detail="Student access required.")
    store = getattr(request.app.state, "sandbox_sessions", None)
    if store is None:
        return None
    return store.get(session_id, student.id)


@router.post("/sandbox/sessions")
async def start_sandbox_session(
    request: Request,
    code: str = Form(default=""),
    student: User = Depends(require_user),
):
    """Open a live sandbox run whose stdin stays open for the IDE console."""
    failure = await _sandbox_csrf_failure(request)
    if failure is not None:
        return failure
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=403, detail="Student access required.")
    if len(code) > MAX_CODE_CHARS:
        return _sandbox_error(
            "Code is too long to run. Shorten it or run only the relevant part.",
            413,
        )
    if not code.strip():
        return _sandbox_error("No code to run.", 400)

    limiter = getattr(request.app.state, "sandbox_limiter", None)
    if limiter is not None:
        allowed, retry_after = limiter.allow(f"student:{student.id}")
        if not allowed:
            wait = max(1, math.ceil(retry_after))
            return JSONResponse(
                {
                    "error": (
                        "Too many runs in a short time. "
                        f"Try again in {wait} seconds."
                    ),
                    "retry_after": wait,
                },
                status_code=429,
                headers={"Retry-After": str(wait)},
            )

    executor = getattr(request.app.state, "sandbox_executor", None)
    store = getattr(request.app.state, "sandbox_sessions", None)
    if not isinstance(executor, SandboxExecutor) or store is None:
        return _sandbox_error(
            "The run environment is unavailable right now.", 503
        )
    try:
        session = store.start(executor, code, owner_id=student.id)
    except SandboxBusy:
        return JSONResponse(
            {"error": "The run environment is busy. Try again in a moment."},
            status_code=429,
            headers={"Retry-After": "2"},
        )
    except SandboxError:
        logger.exception("Could not start a sandbox session")
        return _sandbox_error(
            "The run environment is unavailable right now.", 500
        )
    return JSONResponse({"session_id": session.id})


@router.get("/sandbox/sessions/{session_id}")
async def poll_sandbox_session(
    session_id: str,
    request: Request,
    stdout_since: int = Query(default=0, ge=0),
    stderr_since: int = Query(default=0, ge=0),
    student: User = Depends(require_user),
):
    session = _interactive_session(request, session_id, student)
    if session is None:
        return _sandbox_error("Session not found.", 404)
    return JSONResponse(session.poll(stdout_since, stderr_since))


@router.post("/sandbox/sessions/{session_id}/input")
async def input_sandbox_session(
    session_id: str,
    request: Request,
    line: str = Form(default=""),
    student: User = Depends(require_user),
):
    failure = await _sandbox_csrf_failure(request)
    if failure is not None:
        return failure
    session = _interactive_session(request, session_id, student)
    if session is None:
        return _sandbox_error("Session not found.", 404)
    try:
        session.send_input(line)
    except ValueError:
        return _sandbox_error(
            "Input is too long. Shorten it and try again.", 413
        )
    except SandboxError:
        return _sandbox_error(
            "The program is no longer waiting for input.", 409
        )
    return JSONResponse({"ok": True})


@router.post("/sandbox/sessions/{session_id}/stop")
async def stop_sandbox_session(
    session_id: str,
    request: Request,
    student: User = Depends(require_user),
):
    failure = await _sandbox_csrf_failure(request)
    if failure is not None:
        return failure
    if student.role != ROLE_STUDENT:
        raise HTTPException(status_code=403, detail="Student access required.")
    store = getattr(request.app.state, "sandbox_sessions", None)
    if store is None or not store.stop(session_id, student.id):
        return _sandbox_error("Session not found.", 404)
    return JSONResponse({"ok": True})
