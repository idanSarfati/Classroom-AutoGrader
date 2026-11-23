"""Teacher preview of the student experience.

A teacher can open any class exactly as a student enrolled in it would see it:
the class feed lists only published lessons and assignments, the same redacted
rubric and the same read-only lesson viewer are used. A draft assignment is
also previewable by its owner - with a draft notice instead of the student
banner - so content can be checked before publishing. Nothing can be submitted
or edited here, and every route re-checks that the class, lesson or assignment
belongs to the signed-in teacher.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portal.grading import adapter, redaction
from portal.ingestion.storage import ContentStore, ContentStoreError
from portal.models import (
    Assignment,
    AssignmentAttachment,
    AssignmentContent,
    ClassGroup,
    Lesson,
    LessonSlide,
    SlideContent,
    User,
)
from portal.exams import config as exam_config
from portal.exams import service as exam_service
from portal.web import assignment_form, student_feed
from portal import worksheets
from portal.timeutil import utcnow
from portal.web.deps import get_db, render, require_teacher

router = APIRouter(prefix="/teacher/preview")


def _teacher_classes(db: Session, teacher: User) -> list[ClassGroup]:
    return list(
        db.scalars(
            select(ClassGroup)
            .where(ClassGroup.teacher_id == teacher.id)
            .order_by(ClassGroup.name)
        ).all()
    )


def _owned_class(db: Session, class_id: int, teacher: User) -> ClassGroup:
    group = db.get(ClassGroup, class_id)
    if group is None or group.teacher_id != teacher.id:
        raise HTTPException(status_code=404, detail="Class not found.")
    return group


def _preview_lesson(db: Session, lesson_id: int, teacher: User) -> Lesson:
    lesson = db.get(Lesson, lesson_id)
    if lesson is None or not lesson.published:
        raise HTTPException(status_code=404, detail="Lesson not found.")
    if lesson.class_id is None:
        if lesson.teacher_id != teacher.id:
            raise HTTPException(status_code=404, detail="Lesson not found.")
    else:
        _owned_class(db, lesson.class_id, teacher)
    return lesson


def _preview_assignment(db: Session, assignment_id: int, teacher: User) -> Assignment:
    # The owner may preview a draft too: that is how a teacher checks the
    # content before publishing it. The page shows a draft notice instead of
    # the "students see this" banner, and nothing can be submitted here.
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    _owned_class(db, assignment.class_id, teacher)
    return assignment


def _published_lessons(db: Session, class_id: int) -> list[Lesson]:
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


def _published_assignments(db: Session, class_id: int) -> list[Assignment]:
    return list(
        db.scalars(
            select(Assignment)
            .where(
                Assignment.class_id == class_id,
                Assignment.published.is_(True),
            )
            .order_by(Assignment.created_at.desc())
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


def _preview_context(db: Session, teacher: User, *, draft: bool = False) -> dict:
    return {
        "preview": True,
        "preview_base": "/teacher/preview",
        "preview_classes": _teacher_classes(db, teacher),
        "preview_draft": draft,
    }


@router.get("/classes/{class_id}")
def class_preview(
    class_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    lessons = _published_lessons(db, group.id)
    assignments = _published_assignments(db, group.id)
    slide_counts = _slide_counts(db, [lesson.id for lesson in lessons])
    topics = student_feed.build_class_feed(
        db,
        group,
        lessons,
        assignments,
        slide_counts,
        {},
        "/teacher/preview",
        catalog=student_feed.load_catalog(db, teacher.id),
    )
    return render(
        request,
        "student_class.html",
        user=teacher,
        group=group,
        assignments=assignments,
        lessons=lessons,
        slide_counts=slide_counts,
        submission_status={},
        topics=topics,
        preview_group_id=group.id,
        **_preview_context(db, teacher),
    )


@router.get("/lessons/{lesson_id}")
def lesson_preview(
    lesson_id: int,
    request: Request,
    slide: int = 0,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _preview_lesson(db, lesson_id, teacher)
    slides = _ordered_slides(db, lesson.id)
    index = max(0, min(slide, len(slides) - 1)) if slides else 0
    current = slides[index] if slides else None
    group = db.get(ClassGroup, lesson.class_id) if lesson.class_id else None
    content = db.get(SlideContent, current.id) if current is not None else None
    return render(
        request,
        "student_lesson.html",
        user=teacher,
        lesson=lesson,
        slides=slides,
        index=index,
        current=current,
        current_content=content,
        group=group,
        total=len(slides),
        preview_group_id=group.id if group else None,
        **_preview_context(db, teacher),
    )


@router.get("/assignments/{assignment_id}")
def assignment_preview(
    assignment_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    assignment = _preview_assignment(db, assignment_id, teacher)
    group = db.get(ClassGroup, assignment.class_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")

    if student_feed.is_grade_only(assignment):
        content = db.get(AssignmentContent, assignment.id)
        attachments = db.scalars(
            select(AssignmentAttachment)
            .where(AssignmentAttachment.assignment_id == assignment.id)
            .order_by(AssignmentAttachment.position, AssignmentAttachment.id)
        ).all()
        return render(
            request,
            "student_exam.html",
            user=teacher,
            group=group,
            assignment=assignment,
            content=content,
            attachments=attachments,
            submission=None,
            grade=None,
            result=None,
            status_labels={},
            status_badges={},
            task_status_labels={},
            preview_group_id=group.id,
            **_preview_context(db, teacher, draft=not assignment.published),
        )

    # An exam preview shows one version in its canonical order: the teacher
    # checks the paper itself, not any one student's shuffle.
    exam_name = str(assignment.exam_config or "").strip()
    if exam_name:
        config = exam_config.load_exam(exam_name)
        if config is None:
            raise HTTPException(
                status_code=503,
                detail="המבחן אינו זמין כרגע. בדקו את קובץ ההגדרה.",
            )
        paper = exam_service.canonical_paper(config, config.version_keys[0])
        content = db.get(AssignmentContent, assignment.id)
        attachments = db.scalars(
            select(AssignmentAttachment)
            .where(AssignmentAttachment.assignment_id == assignment.id)
            .order_by(AssignmentAttachment.position, AssignmentAttachment.id)
        ).all()
        return render(
            request,
            "student_assignment.html",
            user=teacher,
            group=group,
            assignment=assignment,
            rubric=None,
            content=content,
            attachments=attachments,
            submission=None,
            grade=None,
            grade_view=None,
            job=None,
            questions=None,
            answers={},
            worksheet=paper.display,
            worksheet_values={},
            answered_count=0,
            legacy_text="",
            fallback_needs_stdin=False,
            exam=config,
            exam_state="open",
            exam_locked=False,
            exam_version_label=paper.version.label,
            exam_total_points=paper.total_points,
            exam_opens_at=assignment.opens_at,
            exam_closes_at=assignment.closes_at,
            server_now=utcnow(),
            preview_group_id=group.id,
            **_preview_context(db, teacher, draft=not assignment.published),
        )

    try:
        binding = adapter.load_rubric(
            assignment.rubric_path,
            expected_sha256=assignment.rubric_sha256,
        )
        rubric = redaction.redact_rubric(binding.rubric)
    except adapter.RubricBindingError:
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
    return render(
        request,
        "student_assignment.html",
        user=teacher,
        group=group,
        assignment=assignment,
        rubric=rubric,
        content=content,
        attachments=attachments,
        submission=None,
        grade=None,
        grade_view=None,
        job=None,
        preview_group_id=group.id,
        worksheet=worksheets.for_rubric(assignment.rubric_path),
        worksheet_values={},
        questions=assignment_form.build_questions(rubric),
        answers={},
        answered_count=0,
        legacy_text="",
        fallback_needs_stdin=assignment_form.rubric_mentions_input(rubric),
        **_preview_context(db, teacher, draft=not assignment.published),
    )


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
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    assignment = _preview_assignment(db, assignment_id, teacher)
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
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _preview_lesson(db, lesson_id, teacher)
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