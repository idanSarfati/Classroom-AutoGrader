"""Phase 5 teacher routes: gradebook, CSV export and student profiles.

Every route is teacher-only and every id is checked against the signed-in
teacher's own classes. A foreign class or student yields **404**, not 403, so
one teacher cannot probe another teacher's roster by id.

The gradebook is read-only: no route here grades, mutates a submission or
touches the engine. Export and screen are built from one
:func:`~portal.gradebook.matrix.build_gradebook` snapshot, so the CSV always
matches what the teacher just saw.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.gradebook import (
    CELL_BADGES,
    CELL_LABELS,
    export_filename,
    gradebook_to_csv,
    build_gradebook,
    build_student_profile,
)
from portal.models import (
    ROLE_STUDENT,
    ClassGroup,
    Enrollment,
    StudentRecord,
    User,
)
from portal.web.deps import get_db, render, require_teacher

router = APIRouter(prefix="/teacher")


def _owned_class(db: Session, class_id: int, teacher: User) -> ClassGroup:
    group = db.get(ClassGroup, class_id)
    if group is None or group.teacher_id != teacher.id:
        # 404 rather than 403: a teacher should not learn that another
        # teacher's class id exists.
        raise HTTPException(status_code=404, detail="Class not found.")
    return group


def _owned_student(db: Session, student_id: int, teacher: User) -> User:
    student = db.get(User, student_id)
    if student is None or student.role != ROLE_STUDENT:
        raise HTTPException(status_code=404, detail="Student not found.")
    enrolled = db.scalar(
        select(Enrollment.id)
        .join(ClassGroup, ClassGroup.id == Enrollment.class_id)
        .where(
            Enrollment.student_id == student.id,
            ClassGroup.teacher_id == teacher.id,
        )
        .limit(1)
    )
    if enrolled is None:
        raise HTTPException(status_code=404, detail="Student not found.")
    return student


@router.get("/classes/{class_id}/gradebook")
def class_gradebook(
    class_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    book = build_gradebook(db, group)
    response = render(
        request,
        "teacher_gradebook.html",
        user=teacher,
        group=group,
        book=book,
        cell_labels=CELL_LABELS,
        cell_badges=CELL_BADGES,
    )
    # Grades and names must not sit in a shared browser's disk cache.
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/classes/{class_id}/export")
def export_gradebook(
    class_id: int,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    payload = gradebook_to_csv(build_gradebook(db, group))
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{export_filename(group)}"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/students/{student_id}/profile")
def student_profile(
    student_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    student = _owned_student(db, student_id, teacher)
    profile = build_student_profile(db, teacher, student)
    if profile is None:  # pragma: no cover - guard for a concurrent removal
        raise HTTPException(status_code=404, detail="Student not found.")
    record = db.scalar(
        select(StudentRecord).where(StudentRecord.student_id == student.id)
    )
    response = render(
        request,
        "teacher_student_profile.html",
        user=teacher,
        student=student,
        profile=profile,
        record=record,
        cell_labels=CELL_LABELS,
        cell_badges=CELL_BADGES,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


__all__ = ["router"]
