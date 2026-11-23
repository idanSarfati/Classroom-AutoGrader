"""Teacher routes: classes, enrollments and assignment listings.

Every route here is teacher-only and every object access is checked against
the signed-in teacher's own classes. Rubric paths and hashes stay on this side
of the wire; students never see them.
"""
from __future__ import annotations

import json
import secrets
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from portal import chat, models, worksheets
from portal.auth import ratelimit
from portal.auth.security import hash_password
from portal.grading import adapter, service
from portal.ingestion import catalog, importer
from portal.ingestion.classroom_source import ClassroomSource, preferred_lesson_rubric
from portal.ingestion.images import (
    MAX_UPLOAD_BYTES,
    ImageUploadError,
    PreparedImage,
    prepare_slide_image,
)
from portal.ingestion.normalize import (
    DedupeInfo,
    clean_title,
    dedupe_coursework,
    dedupe_drive_items,
    html_to_text,
    is_python_b_material,
    leading_number,
    material_key,
    natural_key,
    title_key,
    topic_sort_key,
)
from portal.ingestion.storage import ContentStore, ContentStoreError
from portal.ingestion.types import ImportManifest
from portal.imports.students import generate_password
from portal.models import (
    GRADE_SOURCE_CLASSROOM,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    Assignment,
    AssignmentContent,
    ClassGroup,
    ClassroomLink,
    Enrollment,
    ExamAllocation,
    ExamRecheckPreview,
    ExamStudentWindow,
    Grade,
    GradingJob,
    ImportItem,
    ImportJob,
    Lesson,
    LessonSlide,
    LessonSource,
    SlideContent,
    Submission,
    User,
)
from portal.exams import auto_status as exam_auto_status
from portal.exams import calibration as exam_calibration
from portal.exams import config as exam_config
from portal.exams import feedback as exam_feedback
from portal.exams import llm_check as exam_llm_check
from portal.exams import llm_review as exam_llm_review
from portal.exams import manual as exam_manual
from portal.exams import service as exam_service
from portal.paths import PROJECT_ROOT, RUBRIC_ROOTS
from portal.timeutil import (
    format_local,
    parse_local_due_date,
    parse_local_datetime,
    utcnow,
)
from portal.validation import (
    assignment_title_error,
    class_name_error,
    clean_class_name,
    clean_display_name,
    display_name_error,
    lesson_title_error,
    normalize_username,
    password_error,
    slide_title_error,
    username_error,
)
from portal.web import student_feed
from portal.web.deps import (
    flash,
    get_db,
    render,
    require_teacher,
    verify_csrf,
)

router = APIRouter(prefix="/teacher")

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_MAX_INSTRUCTIONS = 20_000
_MAX_LESSON_SUMMARY = 20_000
_MAX_SLIDE_BODY = 20_000
_MAX_SLIDE_CODE = 20_000


def _new_class_code(db: Session) -> str:
    for _ in range(30):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        exists = db.scalar(select(ClassGroup.id).where(ClassGroup.code == code))
        if exists is None:
            return code
    raise RuntimeError("Could not allocate a unique class code.")


def _owned_class(db: Session, class_id: int, teacher: User) -> ClassGroup:
    group = db.get(ClassGroup, class_id)
    if group is None or group.teacher_id != teacher.id:
        # 404 rather than 403: a teacher should not learn that another
        # teacher's class id exists.
        raise HTTPException(status_code=404, detail="Class not found.")
    return group


def _owned_assignment(db: Session, assignment_id: int, teacher: User) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    _owned_class(db, assignment.class_id, teacher)
    return assignment


def _owned_student(db: Session, student_id: int, teacher: User) -> User:
    """A student account the teacher may act on: enrolled in one of their classes.

    404 rather than 403 so a teacher cannot probe whether an arbitrary account
    id exists. Teachers themselves are never a valid target.
    """
    student = db.get(User, student_id)
    if student is None or student.role != models.ROLE_STUDENT:
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


def _owned_lesson(db: Session, lesson_id: int, teacher: User) -> Lesson:
    lesson = db.get(Lesson, lesson_id)
    if lesson is None or lesson.teacher_id != teacher.id:
        raise HTTPException(status_code=404, detail="Lesson not found.")
    return lesson


def _owned_slide(db: Session, lesson: Lesson, slide_id: int) -> LessonSlide:
    slide = db.get(LessonSlide, slide_id)
    if slide is None or slide.lesson_id != lesson.id:
        raise HTTPException(status_code=404, detail="Slide not found.")
    return slide


def _ordered_slides(db: Session, lesson_id: int) -> list[LessonSlide]:
    return list(
        db.scalars(
            select(LessonSlide)
            .where(LessonSlide.lesson_id == lesson_id)
            .order_by(LessonSlide.position, LessonSlide.id)
        ).all()
    )


def _lesson_scope(
    db: Session, teacher: User, raw_class_id: str
) -> tuple[int | None, str | None]:
    """Resolve the optional class selector to an owned class id."""
    cleaned = (raw_class_id or "").strip()
    if not cleaned:
        return None, None
    try:
        class_id = int(cleaned)
    except ValueError:
        return None, "הכיתה שנבחרה אינה תקינה."
    group = db.get(ClassGroup, class_id)
    if group is None or group.teacher_id != teacher.id:
        return None, "הכיתה שנבחרה אינה שלכם."
    return class_id, None


def _checkbox_true(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"on", "true", "1", "yes"})


def _teacher_classes(db: Session, teacher: User) -> list[ClassGroup]:
    return list(
        db.scalars(
            select(ClassGroup)
            .where(ClassGroup.teacher_id == teacher.id)
            .order_by(ClassGroup.name)
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


@router.get("")
def dashboard(
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    classes = db.scalars(
        select(ClassGroup)
        .where(ClassGroup.teacher_id == teacher.id)
        .order_by(ClassGroup.created_at.desc())
    ).all()
    stats: dict[int, dict[str, int]] = {}
    for group in classes:
        students = db.scalar(
            select(func.count(Enrollment.id)).where(
                Enrollment.class_id == group.id
            )
        )
        assignments = db.scalar(
            select(func.count(Assignment.id)).where(
                Assignment.class_id == group.id
            )
        )
        published = db.scalar(
            select(func.count(Assignment.id)).where(
                Assignment.class_id == group.id,
                Assignment.published.is_(True),
            )
        )
        stats[group.id] = {
            "students": int(students or 0),
            "assignments": int(assignments or 0),
            "published": int(published or 0),
        }
    return render(
        request,
        "teacher_dashboard.html",
        user=teacher,
        classes=classes,
        stats=stats,
        auto_check_pending=exam_auto_status.pending_total(db, teacher.id),
        lesson_count=int(
            db.scalar(
                select(func.count(Lesson.id)).where(Lesson.teacher_id == teacher.id)
            )
            or 0
        ),
    )


@router.post(
    "/classes",
    dependencies=[Depends(verify_csrf)],
)
def create_class(
    request: Request,
    name: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    error = class_name_error(name)
    if error:
        flash(request, error, kind="error")
        return RedirectResponse("/teacher", status_code=303)
    group = ClassGroup(
        name=clean_class_name(name),
        code=_new_class_code(db),
        teacher_id=teacher.id,
    )
    db.add(group)
    db.commit()
    flash(request, f"הכיתה '{group.name}' נוצרה. קוד הכיתה: {group.code}", kind="success")
    return RedirectResponse(f"/teacher/classes/{group.id}", status_code=303)


def _exam_rubric_paths() -> set[str]:
    """Rubric files generated for an exam definition.

    They are engine-shaped descriptions of the paper, but the exam is marked by
    its answer key, not by this file. Offering one in the ordinary assignment
    form would invite a teacher to bind a paper-wide rubric to a single piece of
    homework, so the picker skips them.
    """
    generated: set[str] = set()
    for path in exam_config.EXAM_DIR.glob("*.json"):
        config = exam_config.load_exam(path.stem)
        if config is None:
            continue
        for version in config.versions:
            generated.add(str(version.rubric).replace("\\", "/"))
    return generated


def _rubric_choices() -> list[str]:
    """Project-relative rubric files offered in the assignment form."""
    choices: list[str] = []
    skip = _exam_rubric_paths()
    for root in RUBRIC_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            try:
                relative = path.relative_to(PROJECT_ROOT).as_posix()
            except ValueError:
                continue
            if relative in skip:
                continue
            choices.append(relative)
    return choices


def _exam_overviews(db: Session, assignments) -> dict[int, dict]:
    """What the class page needs to read an exam at a glance.

    An exam row does not behave like homework - the rubric hash is irrelevant
    and the deadline is a window - so the template renders it differently: the
    window, how many students got each version, and how many hand-ins were
    already checked by the key.
    """
    overviews: dict[int, dict] = {}
    for assignment in assignments:
        name = str(assignment.exam_config or "").strip()
        if not name:
            continue
        config = exam_config.load_exam(name)
        labels = (
            {version.key: version.label for version in config.versions}
            if config is not None
            else {}
        )
        counts = db.execute(
            select(ExamAllocation.version, func.count(ExamAllocation.id))
            .where(ExamAllocation.assignment_id == assignment.id)
            .group_by(ExamAllocation.version)
        ).all()
        # Personal make-up windows sit beside the class window: the teacher
        # grants one for a student who missed the sitting, and the class page
        # shows every grant so none is forgotten after the lesson.
        grants = db.execute(
            select(ExamStudentWindow, User)
            .join(User, User.id == ExamStudentWindow.student_id)
            .where(ExamStudentWindow.assignment_id == assignment.id)
            .order_by(User.display_name)
        ).all()
        overviews[assignment.id] = {
            "state": exam_service.window_state(assignment),
            "name": name,
            "release": exam_service.release_state(assignment),
            "release_label": exam_service.release_label(assignment),
            "versions": [
                {"label": labels.get(key, key), "count": int(total)}
                for key, total in sorted(counts)
            ],
            "opened": sum(int(total) for _key, total in counts),
            "overrides": [
                {
                    "student_id": student.id,
                    "name": student.display_name or student.username,
                    "opens_at": grant.opens_at,
                    "closes_at": grant.closes_at,
                }
                for grant, student in grants
            ],
        }
    return overviews


@router.get("/classes/{class_id}")
def class_page(
    class_id: int,
    request: Request,
    tab: str = "content",
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    # The class screen has two tabs: the merged content list and the roster.
    # An unknown value falls back to the content tab.
    class_tab = "students" if tab == "students" else "content"
    students = db.scalars(
        select(User)
        .join(Enrollment, Enrollment.student_id == User.id)
        .where(Enrollment.class_id == group.id)
        .order_by(User.display_name)
    ).all()
    assignments = db.scalars(
        select(Assignment)
        .where(Assignment.class_id == group.id)
        .order_by(Assignment.created_at.desc())
    ).all()
    lessons = db.scalars(
        select(Lesson)
        .where(Lesson.class_id == group.id)
        .order_by(Lesson.created_at.desc())
    ).all()

    # Cross-class reuse: everything this teacher already owns elsewhere.
    other_class_ids = select(ClassGroup.id).where(
        ClassGroup.teacher_id == teacher.id,
        ClassGroup.id != group.id,
    )
    reusable_lessons = db.scalars(
        select(Lesson)
        .where(
            Lesson.teacher_id == teacher.id,
            or_(
                Lesson.class_id.is_(None),
                Lesson.class_id.in_(other_class_ids),
            ),
        )
        .order_by(Lesson.created_at.desc())
        .limit(200)
    ).all()
    reusable_assignments = db.scalars(
        select(Assignment)
        .join(ClassGroup, ClassGroup.id == Assignment.class_id)
        .where(
            ClassGroup.teacher_id == teacher.id,
            Assignment.class_id != group.id,
        )
        .order_by(Assignment.created_at.desc())
        .limit(200)
    ).all()
    class_names = {
        item.id: item.name for item in _teacher_classes(db, teacher)
    }
    link = db.scalar(
        select(ClassroomLink).where(ClassroomLink.class_id == group.id)
    )
    grading_counts = service.class_grading_counts(db, group.id)
    slide_counts = _slide_counts(db, [lesson.id for lesson in lessons])
    # One merged, topic-grouped list for lessons and assignments: the teacher
    # sees the same order as students, with drafts and editing controls.
    topics = student_feed.build_class_feed(
        db,
        group,
        lessons,
        assignments,
        slide_counts,
        {},
        "/teacher",
        catalog=student_feed.load_catalog(db, teacher.id),
        show_status=False,
    )
    return render(
        request,
        "teacher_class.html",
        user=teacher,
        group=group,
        class_tab=class_tab,
        students=students,
        assignments=assignments,
        lessons=lessons,
        assignments_by_id={item.id: item for item in assignments},
        exam_overviews=_exam_overviews(db, assignments),
        topics=topics,
        slide_counts=slide_counts,
        rubric_choices=_rubric_choices(),
        timezone=request.app.state.portal_settings.timezone,
        reusable_lessons=reusable_lessons,
        reusable_assignments=reusable_assignments,
        class_names=class_names,
        classroom_link=link,
        grading_counts=grading_counts,
        waiting_total=sum(
            item["pending"] for item in grading_counts.values()
        ),
    )


@router.post("/classes/{class_id}/rename", dependencies=[Depends(verify_csrf)])
def rename_class(
    class_id: int,
    request: Request,
    name: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    error = class_name_error(name)
    if error:
        flash(request, error, kind="error")
    else:
        group.name = clean_class_name(name)
        db.commit()
        flash(request, "שם הכיתה עודכן.", kind="success")
    return RedirectResponse(f"/teacher/classes/{group.id}", status_code=303)


@router.post("/classes/{class_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_class(
    class_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    db.delete(group)
    db.commit()
    flash(request, f"הכיתה '{group.name}' נמחקה.", kind="success")
    return RedirectResponse("/teacher", status_code=303)


@router.post("/classes/{class_id}/students", dependencies=[Depends(verify_csrf)])
def add_student(
    class_id: int,
    request: Request,
    username: str = Form(default=""),
    display_name: str = Form(default=""),
    password: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    back = RedirectResponse(
        f"/teacher/classes/{group.id}?tab=students", status_code=303
    )
    for error in (
        username_error(username),
        display_name_error(display_name),
        password_error(password),
    ):
        if error:
            flash(request, error, kind="error")
            return back

    normalized = normalize_username(username)
    if db.scalar(select(User.id).where(User.username == normalized)) is not None:
        flash(request, "שם המשתמש הזה כבר קיים. בחרו שם אחר.", kind="error")
        return back

    student = User(
        username=normalized,
        display_name=clean_display_name(display_name),
        password_hash=hash_password(password),
        role=models.ROLE_STUDENT,
    )
    db.add(student)
    try:
        db.flush()
        db.add(Enrollment(class_id=group.id, student_id=student.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "לא ניתן היה ליצור את החשבון. נסו שוב.", kind="error")
        return back

    flash(
        request,
        f"החשבון של {student.display_name} נוצר ושויך לכיתה.",
        kind="success",
    )
    return back


@router.post(
    "/classes/{class_id}/students/{student_id}/remove",
    dependencies=[Depends(verify_csrf)],
)
def remove_student(
    class_id: int,
    student_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.class_id == group.id,
            Enrollment.student_id == student_id,
        )
    )
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found.")
    db.delete(enrollment)
    db.commit()
    flash(request, "התלמיד/ה הוסר/ה מהכיתה. החשבון נשמר.", kind="success")
    return RedirectResponse(
        f"/teacher/classes/{group.id}?tab=students", status_code=303
    )


@router.post(
    "/students/{student_id}/reset-password",
    dependencies=[Depends(verify_csrf)],
)
def reset_student_password(
    student_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Issue a new random password and show it to the teacher exactly once.

    The plaintext lives in this response body only: never in a flash (the
    session cookie is signed, not encrypted), never in a URL or redirect, and
    never in a log line. Only the Argon2id hash is stored.
    """
    student = _owned_student(db, student_id, teacher)
    password = generate_password()
    student.password_hash = hash_password(password)
    db.commit()
    # A student locked out by repeated wrong guesses must be able to use the
    # new password straight away, from any machine.
    ratelimit.clear_username(student.username)
    response = render(
        request,
        "teacher_password_reset.html",
        user=teacher,
        student=student,
        new_password=password,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/classes/{class_id}/assignments", dependencies=[Depends(verify_csrf)])
def create_assignment(
    class_id: int,
    request: Request,
    title: str = Form(default=""),
    instructions: str = Form(default=""),
    rubric_path: str = Form(default=""),
    due_at: str = Form(default=""),
    published: str | None = Form(default=None),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    back = RedirectResponse(f"/teacher/classes/{group.id}", status_code=303)

    title_problem = assignment_title_error(title)
    if title_problem:
        flash(request, title_problem, kind="error")
        return back
    if len(instructions or "") > _MAX_INSTRUCTIONS:
        flash(request, "הוראות המטלה ארוכות מדי.", kind="error")
        return back

    try:
        binding = adapter.load_rubric(rubric_path, allowed_roots=RUBRIC_ROOTS)
    except adapter.RubricBindingError as exc:
        flash(request, f"לא ניתן לחבר את קובץ המחוון: {exc}", kind="error")
        return back

    try:
        due = parse_local_due_date(
            due_at, request.app.state.portal_settings.timezone
        )
    except ValueError:
        flash(request, "תאריך היעד אינו תקין.", kind="error")
        return back

    assignment = Assignment(
        class_id=group.id,
        title=" ".join(title.split()),
        instructions=(instructions or "").strip(),
        rubric_path=str(binding.path),
        rubric_sha256=binding.sha256,
        due_at=due,
        published=bool(
            published and published.strip().lower() in {"on", "true", "1", "yes"}
        ),
    )
    db.add(assignment)
    db.commit()
    flash(
        request,
        f"המטלה '{assignment.title}' נוצרה "
        + ("ופורסמה." if assignment.published else "ונשמרה כטיוטה."),
        kind="success",
    )
    return back


@router.post(
    "/assignments/{assignment_id}/toggle",
    dependencies=[Depends(verify_csrf)],
)
def toggle_assignment(
    assignment_id: int,
    request: Request,
    due_at: str | None = Form(default=None),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    assignment = _owned_assignment(db, assignment_id, teacher)
    if not assignment.published:
        # Publishing must re-verify the bytes the teacher approved. A rubric
        # edited after binding would otherwise grade against different answers.
        try:
            adapter.load_rubric(
                assignment.rubric_path,
                expected_sha256=assignment.rubric_sha256,
            )
        except adapter.RubricBindingError as exc:
            flash(
                request,
                f"לא ניתן לפרסם - קובץ המחוון השתנה או חסר: {exc}",
                kind="error",
            )
            return RedirectResponse(
                f"/teacher/classes/{assignment.class_id}", status_code=303
            )
        # The publish panel lets the teacher set or change the deadline at the
        # moment the draft becomes visible. An absent field (older form or API
        # caller) keeps the date the draft was created with; an empty value
        # clears it. Submissions stay open after this instant - the grader
        # deducts the configured late penalty instead of refusing the hand-in.
        if due_at is not None:
            try:
                assignment.due_at = parse_local_due_date(
                    due_at, request.app.state.portal_settings.timezone
                )
            except ValueError:
                flash(request, "תאריך היעד אינו תקין.", kind="error")
                return RedirectResponse(
                    f"/teacher/classes/{assignment.class_id}", status_code=303
                )
        assignment.published = True
        message = f"המטלה '{assignment.title}' פורסמה."
    else:
        assignment.published = False
        message = f"המטלה '{assignment.title}' הוסרה מהפרסום."
    db.commit()
    flash(request, message, kind="success")
    return RedirectResponse(
        f"/teacher/classes/{assignment.class_id}", status_code=303
    )


@router.post(
    "/assignments/{assignment_id}/due",
    dependencies=[Depends(verify_csrf)],
)
def set_assignment_due(
    assignment_id: int,
    request: Request,
    due_at: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Change or clear the deadline of an assignment that is already live.

    Publishing asks for the date once, but afterwards the teacher still needs
    to extend or remove it without unpublishing - unpublishing would hide the
    task from students in the middle of a lesson. Submissions stay open after
    the deadline; the grader deducts the configured late penalty instead of
    refusing the hand-in. An empty value clears the date entirely.
    """
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/classes/{assignment.class_id}", status_code=303
    )
    try:
        due = parse_local_due_date(
            due_at, request.app.state.portal_settings.timezone
        )
    except ValueError:
        flash(request, "תאריך היעד אינו תקין.", kind="error")
        return back
    assignment.due_at = due
    db.commit()
    flash(
        request,
        f"תאריך היעד של '{assignment.title}' "
        + ("עודכן." if due is not None else "הוסר."),
        kind="success",
    )
    return back


@router.post(
    "/assignments/{assignment_id}/exam-window",
    dependencies=[Depends(verify_csrf)],
)
def set_exam_window(
    assignment_id: int,
    request: Request,
    opens_at: str = Form(default=""),
    closes_at: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Move the sitting window of an exam the class is already using.

    An exam is not homework: ``opens_at`` and ``closes_at`` are enforced by the
    engine - the paper is not served before the exam opens and nothing is
    accepted after it closes - so moving them must not require unpublishing
    (which would hide the exam in the middle of a lesson) or a shell script.
    Both values are wall-clock time in the school's zone, the same way the
    teacher reads them on the class page.

    An exam keeps both ends of its window: clearing them would silently remove
    the two guards, and a one-sided window is almost always a typo.
    """
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/classes/{assignment.class_id}", status_code=303
    )
    if not str(assignment.exam_config or "").strip():
        flash(
            request,
            "אפשר לקבוע חלון הגשה רק למטלה שהיא מבחן. "
            "למטלה רגילה יש תאריך יעד.",
            kind="error",
        )
        return back
    zone = request.app.state.portal_settings.timezone
    try:
        opens = parse_local_datetime(opens_at, zone)
        closes = parse_local_datetime(closes_at, zone)
    except ValueError:
        flash(request, "שעת הפתיחה או הסגירה אינה תקינה.", kind="error")
        return back
    if opens is None or closes is None:
        flash(
            request,
            "למבחן צריך גם מועד פתיחה וגם מועד סגירה. "
            "אם ביטלתם אחד מהם, החזירו אותו לפני השמירה.",
            kind="error",
        )
        return back
    if closes <= opens:
        flash(
            request,
            "מועד הסגירה חייב להיות אחרי מועד הפתיחה.",
            kind="error",
        )
        return back
    assignment.opens_at = opens
    assignment.closes_at = closes
    db.commit()
    flash(
        request,
        f"חלון ההגשה של '{assignment.title}' עודכן: "
        f"{format_local(opens, zone)} עד {format_local(closes, zone)}.",
        kind="success",
    )
    return back


@router.post(
    "/assignments/{assignment_id}/exam-window/student",
    dependencies=[Depends(verify_csrf)],
)
def set_student_exam_window(
    assignment_id: int,
    request: Request,
    student_id: str = Form(default=""),
    student_ids: list[str] = Form(default=[]),
    opens_at: str = Form(default=""),
    closes_at: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Give one or more students their own sitting window, as a make-up.

    The class window stays exactly as it is for everyone else. Each grant
    replaces both ends *for that student only*, so students who missed the
    lesson can sit later without reopening the paper for the whole class, and
    a student who sits early is not shown it to the rest. Several students
    can be ticked at once and share the same window in one save; the legacy
    single ``student_id`` field is still accepted.
    """
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/classes/{assignment.class_id}", status_code=303
    )
    if not str(assignment.exam_config or "").strip():
        flash(
            request,
            "אפשר לקבוע מועד אישי רק למטלה שהיא מבחן. "
            "למטלה רגילה יש תאריך יעד.",
            kind="error",
        )
        return back
    raw_ids = list(student_ids) + ([student_id] if student_id else [])
    chosen: list[int] = []
    for raw in raw_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value and value not in chosen:
            chosen.append(value)
    if not chosen:
        flash(
            request,
            "סמנו לפחות תלמיד/ה אחד/ת מהכיתה של המבחן הזה.",
            kind="error",
        )
        return back
    # Every ticked student must be a real enrollee; one bad id rejects the
    # whole save so a partial group never gets a window by accident.
    students: list[User] = []
    for chosen_id in chosen:
        student = db.get(User, chosen_id)
        enrolled = (
            db.scalar(
                select(Enrollment).where(
                    Enrollment.class_id == assignment.class_id,
                    Enrollment.student_id == chosen_id,
                )
            )
            if student is not None
            else None
        )
        if (
            student is None
            or student.role != models.ROLE_STUDENT
            or enrolled is None
        ):
            flash(
                request,
                "בחרו תלמיד/ה שרשומים בכיתה של המבחן הזה.",
                kind="error",
            )
            return back
        students.append(student)
    zone = request.app.state.portal_settings.timezone
    try:
        opens = parse_local_datetime(opens_at, zone)
        closes = parse_local_datetime(closes_at, zone)
    except ValueError:
        flash(request, "שעת הפתיחה או הסגירה אינה תקינה.", kind="error")
        return back
    if opens is None or closes is None:
        flash(
            request,
            "למועד אישי צריך גם שעת פתיחה וגם שעת סגירה.",
            kind="error",
        )
        return back
    if closes <= opens:
        flash(request, "מועד הסגירה חייב להיות אחרי מועד הפתיחה.", kind="error")
        return back

    for student in students:
        grant = db.scalar(
            select(ExamStudentWindow).where(
                ExamStudentWindow.assignment_id == assignment.id,
                ExamStudentWindow.student_id == student.id,
            )
        )
        if grant is None:
            db.add(
                ExamStudentWindow(
                    assignment_id=assignment.id,
                    student_id=student.id,
                    opens_at=opens,
                    closes_at=closes,
                )
            )
        else:
            grant.opens_at = opens
            grant.closes_at = closes
    db.commit()
    window_text = (
        f"{format_local(opens, zone)} עד {format_local(closes, zone)}"
    )
    if len(students) == 1:
        name = students[0].display_name or students[0].username
        message = (
            f"נקבע מועד אישי ל{name}: {window_text}. "
            "המועד הזה חל רק על התלמיד/ה הזו."
        )
    else:
        message = (
            f"נקבע מועד אישי ל־{len(students)} תלמידים: {window_text}. "
            "המועד הזה חל רק על התלמידים שסומנו."
        )
    flash(request, message, kind="success")
    return back


@router.post(
    "/assignments/{assignment_id}/exam-window/student/{student_id}/clear",
    dependencies=[Depends(verify_csrf)],
)
def clear_student_exam_window(
    assignment_id: int,
    student_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Remove a personal window so the class window applies again."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/classes/{assignment.class_id}", status_code=303
    )
    grant = db.scalar(
        select(ExamStudentWindow).where(
            ExamStudentWindow.assignment_id == assignment.id,
            ExamStudentWindow.student_id == student_id,
        )
    )
    if grant is None:
        flash(request, "לא נמצא מועד אישי לתלמיד/ה הזו.", kind="info")
        return back
    db.delete(grant)
    db.commit()
    flash(
        request,
        "המועד האישי הוסר. מעכשיו חל על התלמיד/ה חלון המבחן של הכיתה.",
        kind="success",
    )
    return back


@router.post(
    "/assignments/{assignment_id}/exam-release",
    dependencies=[Depends(verify_csrf)],
)
def set_exam_release(
    assignment_id: int,
    request: Request,
    release: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Decide how much of a graded exam the class may see.

    Marking happens at hand-in, so this is the one control that keeps a class
    sitting on Thursday from reading the paper a class sat on Wednesday. The
    three states are deliberate: ``held`` shows only the hand-in confirmation,
    ``scores`` shows the number without the paper or the explanations, and
    ``full`` shows the score, the per-question detail and the student's own
    answers. A teacher can move back to ``held`` at any time.

    This works on a draft exam too, and there it is the whole point: releasing
    the marks while the paper stays unpublished gives the score to the students
    who sat and shows the exam to nobody who did not. The gate itself is
    :func:`portal.exams.service.visible_to_student`, not this route.
    """
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/classes/{assignment.class_id}", status_code=303
    )
    if not str(assignment.exam_config or "").strip():
        flash(
            request,
            "אפשר לפרסם תוצאות רק למטלה שהיא מבחן. "
            "למטלה רגילה הציון מוצג מיד עם הבדיקה.",
            kind="error",
        )
        return back
    value = str(release or "").strip()
    if value not in (
        exam_service.RELEASE_HELD,
        exam_service.RELEASE_SCORES,
        exam_service.RELEASE_FULL,
    ):
        flash(request, "מצב הפרסום שנבחר אינו מוכר.", kind="error")
        return back
    assignment.exam_release = value
    db.commit()
    messages = {
        exam_service.RELEASE_HELD: (
            "התוצאות הוסתרו מחדש. התלמידים רואים רק את אישור ההגשה."
        ),
        exam_service.RELEASE_SCORES: (
            "הציון פורסם לכל הכיתה. הפירוט ודף המבחן נשארים מוסתרים."
        ),
        exam_service.RELEASE_FULL: (
            "התוצאות פורסמו במלואן: ציון, פירוט ותשובות התלמידים."
        ),
    }
    message = messages[value]
    if value != exam_service.RELEASE_HELD and not assignment.published:
        # The class-wide wording would be wrong here: with the exam hidden, the
        # release opens the marks only for the children who already sat.
        message = (
            "המבחן מוסתר מהתלמידים (טיוטה), ולכן הציון יופיע רק למי שכבר "
            "נבחן. תלמיד שטרם נבחן לא יראה כלום."
        )
    flash(
        request,
        f"{message} ({assignment.title})",
        kind="success",
    )
    return back


@router.post(
    "/assignments/{assignment_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def delete_assignment(
    assignment_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    assignment = _owned_assignment(db, assignment_id, teacher)
    class_id = assignment.class_id
    title = assignment.title
    db.delete(assignment)
    db.commit()
    flash(request, f"המטלה '{title}' נמחקה.", kind="success")
    return RedirectResponse(f"/teacher/classes/{class_id}", status_code=303)


# ------------------------------------------- add content from Google Classroom


def _coursework_rank(item) -> tuple[int, int, int, int]:
    """Published first, then coursework over Materials, then richest copy."""
    return (
        1 if (item.state or "").upper() == "PUBLISHED" else 0,
        0 if (item.work_type or "") == "COURSE_MATERIAL" else 1,
        len(item.description_html or ""),
        len(item.attachments),
    )


def _attachment_in_class(
    db: Session,
    teacher: User,
    group: ClassGroup,
    attachment,
    class_index: dict[str, tuple[str, int]],
    parent_existing,
) -> bool:
    """Whether one file attached to an assignment is already in class.

    A deck imported on its own has a lesson source; any other file is in class
    once its parent assignment was imported (the importer stored its bytes).
    """
    if parent_existing is not None:
        return True
    if attachment.file_id:
        existing, _ = importer.source_status(
            db, teacher.id, group.id, "drive_deck", attachment.file_id
        )
        if existing is not None:
            return True
    return class_index.get(title_key(attachment.title)) is not None


def _picker_context(
    request: Request,
    db: Session,
    teacher: User,
    group: ClassGroup,
    *,
    course_id: str = "",
    folder_id: str = "",
) -> dict:
    """Everything the one-screen picker needs.

    Coursework and Drive decks are listed together so the teacher can choose a
    specific assignment or teaching deck and say, per item, whether it becomes
    a lesson (material) or an assignment.
    """
    link = db.scalar(
        select(ClassroomLink).where(ClassroomLink.class_id == group.id)
    )
    # The picker reads only the local catalog. Google is contacted by the
    # explicit scan action, never by opening this page.
    cached = catalog.load(db, teacher.id)
    # The teacher does not use the פייתון ב' course at all; its decks are
    # already filtered out of the catalog, and the course itself is not
    # offered in the selector either.
    courses = [
        course
        for course in cached.courses
        if not is_python_b_material(course.name)
    ]
    # The catalog card renders its own error; keeping it there avoids a
    # duplicate flash at the top of the page.
    source_error = ""

    chosen = (course_id or "").strip()
    if not chosen and link is not None:
        chosen = link.course_id
    if not chosen and len(courses) == 1:
        chosen = courses[0].id

    snapshot = cached.snapshots.get(chosen) if chosen else None
    suggestions = cached.suggestions.get(chosen, {})

    # Courses keep copies of the same material under different Classroom ids;
    # remembering the titles already in class keeps the badge honest when the
    # teacher switches course copies.
    class_index = importer.class_title_index(db, teacher.id, group.id)

    coursework_rows: list[dict] = []
    if snapshot is not None:
        # Only the canonical copy of each duplicate group is listed. A copy the
        # teacher could not select anyway is noise, not information.
        kept, dedupe = dedupe_coursework(list(snapshot.coursework))
        # A Classroom draft that mirrors a published copy is the same
        # assignment to the teacher, not a second choice. Keep one row per
        # title, preferring the published copy.
        by_title: dict[str, object] = {}
        for item in kept:
            key = title_key(item.title) or f"__{item.id}"
            current = by_title.get(key)
            if current is None or _coursework_rank(item) > _coursework_rank(current):
                by_title[key] = item
        kept = [
            item
            for item in kept
            if by_title.get(title_key(item.title) or f"__{item.id}") is item
        ]
        coursework_groups_ids: dict[str, list[str]] = {}
        for item in snapshot.coursework:
            marker = dedupe.get(item.id)
            canonical_id = (
                marker.duplicate_of
                if marker is not None and marker.duplicate_of
                else item.id
            )
            coursework_groups_ids.setdefault(canonical_id, []).append(item.id)
        for coursework in kept:
            info = dedupe.get(coursework.id, DedupeInfo())
            is_draft = coursework.state == "DRAFT"
            title = clean_title(coursework.title)
            # A Classroom draft was never published to the class, so it is
            # not "already in class" even if a copy was imported earlier.
            if is_draft:
                existing, reusable = None, None
            else:
                existing, reusable = importer.source_status_group(
                    db,
                    teacher.id,
                    group.id,
                    "coursework",
                    coursework_groups_ids.get(coursework.id, [coursework.id]),
                )
                if existing is None:
                    matched = class_index.get(title_key(title))
                    if matched is not None:
                        existing = (matched[0], matched[1], "")
            rubric, score, reason = suggestions.get(
                coursework.id, ("", 0.0, "")
            )
            # The catalog suggestion is frozen at scan time. If the lesson
            # rubric did not exist yet, a fuzzy ".2" guess (lesson_2 for a 4.2
            # hand-in) is cached and pre-filled forever. Re-apply the hard
            # x.2 -> lesson_x rule live so the picker always offers the right
            # rubric without needing a fresh scan.
            preferred = preferred_lesson_rubric(coursework.title)
            if preferred:
                rubric, score, reason = preferred, 1.0, "lesson number match"
            default_action = (
                "assignment"
                if rubric and score >= importer.RUBRIC_SUGGEST_THRESHOLD
                else "lesson"
            )
            # A Materials-tab item is teaching material even when its title
            # happens to resemble an assignment title.
            if coursework.work_type == "COURSE_MATERIAL":
                default_action = "lesson"
            existing_type = ""
            if existing is not None:
                existing_type = existing[0]
                # Re-import should not ask the teacher to turn an existing
                # lesson into an assignment (the importer would refuse it).
                default_action = existing_type
                if existing_type == "assignment":
                    row_assignment = db.get(Assignment, existing[1])
                    if row_assignment is not None:
                        # The picker offers project-relative rubric names, so a
                        # stored absolute path would not match any option.
                        try:
                            rubric = (
                                Path(row_assignment.rubric_path)
                                .resolve()
                                .relative_to(PROJECT_ROOT)
                                .as_posix()
                            )
                        except (OSError, ValueError):
                            rubric = row_assignment.rubric_path
            coursework_rows.append(
                {
                    "id": coursework.id,
                    "kind": "coursework",
                    "pick_value": f"coursework:{coursework.id}",
                    "title": title,
                    "topic": coursework.topic_name or "",
                    "state": coursework.state,
                    "is_draft": coursework.state == "DRAFT",
                    "due": coursework.due_at,
                    "description": html_to_text(coursework.description_html)[:240],
                    "attachments": [
                        {
                            "title": item.title,
                            "mime_type": item.mime_type,
                            "kind": item.kind,
                            "in_class": (not is_draft)
                            and _attachment_in_class(
                                db, teacher, group, item, class_index, existing
                            ),
                        }
                        for item in coursework.attachments
                    ],
                    "presentations": len(coursework.presentations),
                    "documents": len(coursework.documents),
                    "duplicate_count": info.duplicate_count,
                    "already_imported": existing is not None,
                    "existing_type": existing_type,
                    "reusable_from": reusable[2] if reusable else "",
                    "rubric": rubric,
                    "rubric_score": score,
                    "rubric_reason": reason,
                    "default_action": default_action,
                    "is_material": coursework.work_type == "COURSE_MATERIAL",
                    "is_new": f"coursework:{coursework.id}" in cached.new_ids,
                }
            )

    coursework_groups: list[dict] = []
    group_index: dict[str, dict] = {}
    for row in coursework_rows:
        name = row["topic"] or "ללא נושא"
        if name not in group_index:
            group_index[name] = {
                "name": row["topic"],
                "kind": "coursework",
                "rows": [],
            }
            coursework_groups.append(group_index[name])
        group_index[name]["rows"].append(row)

    # Read like a textbook: topics in curriculum order, materials by their
    # lesson number (1.1, 1.2, 2.1 ... 2.10), not by Classroom's newest-first
    # order or a plain string sort.
    coursework_groups.sort(key=lambda section: topic_sort_key(section["name"]))

    deck_rows: list[dict] = []
    deck_error = ""
    if cached.has_data and cached.decks:
        kept, deck_info = dedupe_drive_items(list(cached.decks))
        deck_groups_ids: dict[str, list[str]] = {}
        for deck in cached.decks:
            marker = deck_info.get(deck.file_id)
            canonical_id = (
                marker.duplicate_of
                if marker is not None and marker.duplicate_of
                else deck.file_id
            )
            deck_groups_ids.setdefault(canonical_id, []).append(deck.file_id)
        for deck in kept:
            detail = deck_info.get(deck.file_id, DedupeInfo())
            existing, reusable = importer.source_status_group(
                db,
                teacher.id,
                group.id,
                "drive_deck",
                deck_groups_ids.get(deck.file_id, [deck.file_id]),
            )
            title = clean_title(deck.title)
            if existing is None:
                matched = class_index.get(title_key(title))
                if matched is not None:
                    existing = (matched[0], matched[1], "")
            deck_rows.append(
                {
                    "id": deck.file_id,
                    "kind": "deck",
                    "pick_value": f"deck:{deck.file_id}",
                    "title": title,
                    "copies": detail.duplicate_count + 1,
                    "already_imported": existing is not None,
                    "existing_type": existing[0] if existing is not None else "",
                    "reusable_from": reusable[2] if reusable else "",
                    "is_new": f"deck:{deck.file_id}" in cached.new_ids,
                }
            )
        deck_rows.sort(key=lambda row: natural_key(row["title"]))

    doc_rows: list[dict] = []
    if cached.has_data and cached.docs:
        kept_docs, doc_info = dedupe_drive_items(list(cached.docs))
        doc_groups_ids: dict[str, list[str]] = {}
        for doc in cached.docs:
            marker = doc_info.get(doc.file_id)
            canonical_id = (
                marker.duplicate_of
                if marker is not None and marker.duplicate_of
                else doc.file_id
            )
            doc_groups_ids.setdefault(canonical_id, []).append(doc.file_id)
        for doc in kept_docs:
            detail = doc_info.get(doc.file_id, DedupeInfo())
            existing, reusable = importer.source_status_group(
                db,
                teacher.id,
                group.id,
                "drive_doc",
                doc_groups_ids.get(doc.file_id, [doc.file_id]),
            )
            title = clean_title(doc.title)
            if existing is None:
                matched = class_index.get(title_key(title))
                if matched is not None:
                    existing = (matched[0], matched[1], "")
            doc_rows.append(
                {
                    "id": doc.file_id,
                    "kind": "doc",
                    "pick_value": f"doc:{doc.file_id}",
                    "title": title,
                    "copies": detail.duplicate_count + 1,
                    "already_imported": existing is not None,
                    "existing_type": existing[0] if existing is not None else "",
                    "reusable_from": reusable[2] if reusable else "",
                    "is_new": f"doc:{doc.file_id}" in cached.new_ids,
                }
            )
        doc_rows.sort(key=lambda row: natural_key(row["title"]))

    # The same lesson is often listed twice: once as Classroom coursework and
    # once as the Drive presentation/document it attaches. The coursework row
    # is the better choice (it carries the rubric, due date and type selector)
    # and its attachment holds the content, so hide the Drive copy instead of
    # offering the teacher two rows for one material.
    presentation_keys = set()
    document_keys = set()
    for row in coursework_rows:
        for attachment in row["attachments"]:
            mime_type = attachment.get("mime_type") or ""
            key = material_key(attachment.get("title") or "")
            if not key:
                continue
            if mime_type == "application/vnd.google-apps.presentation":
                presentation_keys.add(key)
            elif mime_type == "application/vnd.google-apps.document":
                document_keys.add(key)
    if presentation_keys:
        deck_rows = [
            row
            for row in deck_rows
            if material_key(row["title"]) not in presentation_keys
        ]
    if document_keys:
        doc_rows = [
            row
            for row in doc_rows
            if material_key(row["title"]) not in document_keys
        ]

    # A Drive deck named "2.1 | ..." belongs to topic 2. Topics without a
    # number in their name ("היכרות") are matched through the numbers of their
    # own rows (0.3, 0.4 ... -> 0), so the 0.x intro decks join them. A deck
    # whose number matches nothing goes to a final "no topic" group instead of
    # disappearing.
    numbered: dict[int, dict] = {}
    for section in coursework_groups:
        number = leading_number(section["name"])
        if number is not None:
            numbered.setdefault(number, section)

    for section in coursework_groups:
        if not section["name"] or leading_number(section["name"]) is not None:
            continue
        counts = Counter(
            number
            for number in (leading_number(row["title"]) for row in section["rows"])
            if number is not None
        )
        for number, count in counts.items():
            # Two rows agreeing on 0.x is a lesson family, not a stray "1".
            if count >= 2 and number not in numbered:
                numbered[number] = section
                break

    leftovers: list[dict] = []
    for row in deck_rows + doc_rows:
        section = numbered.get(leading_number(row["title"]))
        if section is None:
            leftovers.append(row)
        else:
            section["rows"].append(row)

    for section in coursework_groups:
        section["rows"].sort(
            key=lambda row: (
                natural_key(row["title"]),
                row["kind"] != "coursework",
                row.get("is_draft", False),
            )
        )

    picker_groups = list(coursework_groups)
    if leftovers:
        leftovers.sort(key=lambda row: natural_key(row["title"]))
        picker_groups.append(
            {
                "name": "חומרים ללא נושא",
                "kind": "decks",
                "rows": leftovers,
            }
        )

    return {
        "courses": courses,
        "chosen_course_id": chosen,
        "course_name": snapshot.course_name if snapshot is not None else "",
        "coursework_rows": coursework_rows,
        "coursework_groups": coursework_groups,
        "picker_groups": picker_groups,
        "picker_total": len(coursework_rows) + len(deck_rows) + len(doc_rows),
        "deck_rows": deck_rows,
        "doc_rows": doc_rows,
        "folder_id": folder_id,
        "source_error": source_error,
        "deck_error": deck_error,
        "classroom_link": link,
        "catalog_status": cached.status,
        "catalog_error": cached.error,
        "catalog_synced_at": cached.synced_at,
        "catalog_has_data": cached.has_data,
        "catalog_new_count": len(cached.new_ids),
    }


@router.get("/classes/{class_id}/new")
def new_class_content_page(
    class_id: int,
    request: Request,
    course_id: str = "",
    folder_id: str = "",
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    return render(
        request,
        "teacher_class_new.html",
        user=teacher,
        group=group,
        rubric_choices=_rubric_choices(),
        **_picker_context(
            request, db, teacher, group, course_id=course_id, folder_id=folder_id
        ),
    )


@router.post(
    "/classes/{class_id}/scan",
    dependencies=[Depends(verify_csrf)],
)
def scan_class_materials(
    class_id: int,
    request: Request,
    course_id: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Refresh the local Classroom catalog once, in the background.

    The picker keeps showing the previous catalog while this runs. The route
    returns immediately; ``teacher_class_new.html`` polls until the worker
    flips the catalog status back from ``refreshing``.
    """
    group = _owned_class(db, class_id, teacher)
    back = RedirectResponse(
        f"/teacher/classes/{group.id}/new", status_code=303
    )
    active = db.scalar(
        select(ImportJob).where(
            ImportJob.teacher_id == teacher.id,
            ImportJob.kind == "catalog_sync",
            ImportJob.state.in_(("queued", "running")),
        )
    )
    if active is not None:
        flash(request, "סריקה כבר רצה ברקע. העמוד יתעדכן כשתסתיים.", kind="info")
        return back
    worker = getattr(request.app.state, "import_worker", None)
    if worker is None:
        flash(
            request,
            "מעבד הסריקה אינו פעיל. הפעילו מחדש את השרת ונסו שוב.",
            kind="error",
        )
        return back

    link = db.scalar(
        select(ClassroomLink).where(ClassroomLink.class_id == group.id)
    )
    chosen_course = (course_id or "").strip()
    if not chosen_course and link is not None:
        chosen_course = link.course_id

    job = ImportJob(
        teacher_id=teacher.id,
        class_id=group.id,
        course_id=chosen_course,
        kind="catalog_sync",
        state="queued",
        manifest_json=json.dumps(
            {"teacher_id": teacher.id, "course_id": chosen_course},
            ensure_ascii=False,
        ),
        counts_json="",
        error="",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    catalog.mark_refreshing(db, teacher.id)
    worker.enqueue(job.id)
    flash(
        request,
        "סריקת החומרים רצה ברקע. אפשר להמשיך לעבוד; העמוד יתעדכן לבד.",
        kind="success",
    )
    return back


@router.post("/classes/{class_id}/new", dependencies=[Depends(verify_csrf)])
async def add_class_content(
    class_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    form = await request.form()
    course_id = str(form.get("course_id") or "").strip()
    folder_id = str(form.get("folder_id") or "").strip()
    back = RedirectResponse(
        f"/teacher/classes/{group.id}/new"
        f"?course_id={quote(course_id)}&folder_id={quote(folder_id)}",
        status_code=303,
    )

    coursework_ids: list[str] = []
    deck_ids: list[str] = []
    doc_ids: list[str] = []
    actions: dict[str, str] = {}
    rubrics: dict[str, str] = {}
    for raw in form.getlist("pick"):
        kind, _, source_id = str(raw).partition(":")
        source_id = source_id.strip()
        if not source_id:
            continue
        if kind == "coursework":
            target = str(
                form.get(f"target_coursework_{source_id}") or "lesson"
            ).strip()
            if target not in {"lesson", "assignment", "skip"}:
                target = "lesson"
            coursework_ids.append(source_id)
            actions[f"coursework:{source_id}"] = target
            rubrics[f"coursework:{source_id}"] = str(
                form.get(f"rubric_coursework_{source_id}") or ""
            ).strip()
        elif kind == "deck":
            deck_ids.append(source_id)
            actions[f"deck:{source_id}"] = "lesson"
        elif kind == "doc":
            doc_ids.append(source_id)
            actions[f"doc:{source_id}"] = "lesson"

    if not any(action != "skip" for action in actions.values()):
        flash(request, "בחרו לפחות פריט אחד והוסיפו אותו כשיעור או כמטלה.", kind="error")
        return back

    # Rubrics are verified before anything is queued, so an assignment can
    # never be created with a missing or unreadable rubric.
    problems: list[str] = []
    for key, action in actions.items():
        if action != "assignment":
            continue
        rubric = rubrics.get(key, "")
        if not rubric:
            problems.append("לאחת המטלות לא נבחר קובץ מחוון.")
            continue
        try:
            adapter.load_rubric(rubric, allowed_roots=RUBRIC_ROOTS)
        except adapter.RubricBindingError as exc:
            problems.append(f"המחוון '{rubric}' אינו תקין: {exc}")
    if problems:
        flash(request, " ".join(dict.fromkeys(problems)), kind="error")
        return back

    cached = catalog.load(db, teacher.id)
    snapshot = None
    if coursework_ids:
        if not course_id:
            flash(request, "לא נבחר קורס לקריאת הפריטים.", kind="error")
            return back
        snapshot = cached.snapshots.get(course_id)
        if snapshot is None:
            flash(
                request,
                "החומרים של הקורס עדיין לא נסרקו. לחצו על 'סריקת חומרים' ונסו שוב.",
                kind="error",
            )
            return back
        known = {item.id for item in snapshot.coursework}
        coursework_ids = [item for item in coursework_ids if item in known]
    known_decks = {deck.file_id: deck for deck in cached.decks}
    deck_ids = [item for item in deck_ids if item in known_decks]
    known_docs = {doc.file_id: doc for doc in cached.docs}
    doc_ids = [item for item in doc_ids if item in known_docs]
    if not coursework_ids and not deck_ids and not doc_ids:
        flash(request, "הפריטים שנבחרו כבר אינם זמינים בקורס.", kind="error")
        return back

    try:
        job = importer.build_selection_preview(
            db,
            _classroom_source(request),
            teacher_id=teacher.id,
            group=group,
            snapshot=snapshot,
            coursework_ids=coursework_ids,
            deck_ids=deck_ids,
            actions=actions,
            rubrics=rubrics,
            decks=known_decks,
            doc_ids=doc_ids,
            docs=known_docs,
        )
    except Exception as exc:  # noqa: BLE001 - auth/network/API message
        flash(request, f"לא ניתן להכין את הייבוא: {exc}", kind="error")
        return back

    usable = sum(
        1
        for item in ImportManifest.from_json(json.loads(job.manifest_json)).items
        if item.suggested_action != "skip"
    )
    worker = getattr(request.app.state, "import_worker", None)
    if worker is None:
        flash(
            request,
            "הבחירה נשמרה, אך מעבד הייבוא אינו פעיל. הפעילו מחדש את השרת.",
            kind="error",
        )
    else:
        worker.enqueue(job.id)
        flash(
            request,
            f"נוספו {usable} פריטים. הם נכנסו כטיוטות וההייבוא רץ ברקע.",
            kind="success",
        )
    return RedirectResponse(
        f"/teacher/import/jobs/{job.id}", status_code=303
    )


# --------------------------------------------------------------------- lessons


def _portal_source_index(db: Session, teacher: User) -> tuple[dict, dict, set]:
    """Where each Google source already lives anywhere in this account.

    Returns ``(by_source, by_title, imported_lesson_ids)``. Unlike the picker
    (which is per class), the course-content tree is account-wide: a material
    imported into any of the teacher's classes counts as "in the portal", and
    its published flag is what the tree shows. ``imported_lesson_ids`` lets the
    caller spot manual lessons that have no Google source at all.
    """
    class_ids = [group.id for group in _teacher_classes(db, teacher)]
    by_source: dict[tuple[str, str], dict] = {}
    by_title: dict[str, dict] = {}
    imported_lesson_ids: set[int] = set()

    def remember(entry: dict, titles: list[str]) -> None:
        for title in titles:
            key = title_key(title)
            if key:
                by_title.setdefault(key, entry)

    lesson_rows = db.execute(
        select(
            LessonSource.source_kind,
            LessonSource.source_id,
            LessonSource.original_title,
            Lesson.id,
            Lesson.title,
            Lesson.published,
        )
        .join(Lesson, Lesson.id == LessonSource.lesson_id)
        .where(Lesson.teacher_id == teacher.id)
        .order_by(Lesson.id)
    ).all()
    for row in lesson_rows:
        imported_lesson_ids.add(int(row.id))
        entry = {
            "type": "lesson",
            "id": int(row.id),
            "published": bool(row.published),
        }
        if row.source_kind and row.source_id:
            by_source.setdefault((row.source_kind, row.source_id), entry)
        remember(entry, [row.title, row.original_title])

    if class_ids:
        assignment_rows = db.execute(
            select(
                AssignmentContent.source_kind,
                AssignmentContent.source_id,
                AssignmentContent.original_title,
                Assignment.id,
                Assignment.title,
                Assignment.published,
            )
            .join(
                Assignment,
                Assignment.id == AssignmentContent.assignment_id,
            )
            .where(Assignment.class_id.in_(class_ids))
            .order_by(Assignment.id)
        ).all()
        for row in assignment_rows:
            entry = {
                "type": "assignment",
                "id": int(row.id),
                "published": bool(row.published),
            }
            if row.source_kind and row.source_id:
                by_source.setdefault((row.source_kind, row.source_id), entry)
            remember(entry, [row.title, row.original_title])

    return by_source, by_title, imported_lesson_ids


def _course_content_tree(db: Session, teacher: User) -> dict:
    """One account-wide, read-only tree of the course's teaching content.

    Every topic the course defines is listed - including topics whose items
    are still drafts or that hold nothing yet - and under each topic the
    lectures (Drive decks / documents) and the assignments / materials appear
    exactly once. Classroom keeps a copy of the same material per class; the
    dedupe here collapses those copies so the teacher reads one syllabus, not
    one per class. Each row also says whether it already lives in the portal
    and whether it is published to students.
    """
    cached = catalog.load(db, teacher.id)
    courses = [
        course
        for course in cached.courses
        if not is_python_b_material(course.name)
    ]
    groups = _teacher_classes(db, teacher)
    class_ids = [group.id for group in groups]
    chosen = ""
    if class_ids:
        for link in db.scalars(
            select(ClassroomLink).where(ClassroomLink.class_id.in_(class_ids))
        ).all():
            if link.course_id in cached.snapshots:
                chosen = link.course_id
                break
    if not chosen and len(courses) == 1:
        chosen = courses[0].id
    if not chosen:
        for course in courses:
            if course.id in cached.snapshots:
                chosen = course.id
                break
    snapshot = cached.snapshots.get(chosen) if chosen else None

    by_source, by_title, imported_lesson_ids = _portal_source_index(db, teacher)

    def portal_for(kind: str, ids: list[str], title: str) -> dict | None:
        for source_id in ids:
            entry = by_source.get((kind, source_id))
            if entry is not None:
                return entry
        return by_title.get(title_key(title))

    rows: list[dict] = []
    presentation_keys: set[str] = set()
    document_keys: set[str] = set()
    if snapshot is not None:
        kept, dedupe = dedupe_coursework(list(snapshot.coursework))
        by_title_cw: dict[str, object] = {}
        for item in kept:
            key = title_key(item.title) or f"__{item.id}"
            current = by_title_cw.get(key)
            if current is None or _coursework_rank(item) > _coursework_rank(current):
                by_title_cw[key] = item
        kept = [
            item
            for item in kept
            if by_title_cw.get(title_key(item.title) or f"__{item.id}") is item
        ]
        group_ids: dict[str, list[str]] = {}
        for item in snapshot.coursework:
            marker = dedupe.get(item.id)
            canonical = (
                marker.duplicate_of
                if marker is not None and marker.duplicate_of
                else item.id
            )
            group_ids.setdefault(canonical, []).append(item.id)
        for item in kept:
            title = clean_title(item.title)
            for attachment in item.attachments:
                key = material_key(attachment.title or "")
                if not key:
                    continue
                if attachment.mime_type == "application/vnd.google-apps.presentation":
                    presentation_keys.add(key)
                elif attachment.mime_type == "application/vnd.google-apps.document":
                    document_keys.add(key)
            rows.append(
                {
                    "kind": "coursework",
                    "kind_label": (
                        "חומר לימוד"
                        if item.work_type == "COURSE_MATERIAL"
                        else "מטלה"
                    ),
                    "title": title,
                    "topic": item.topic_name or "",
                    "is_draft": (item.state or "").upper() == "DRAFT",
                    "due": item.due_at,
                    "portal": portal_for(
                        "coursework", group_ids.get(item.id, [item.id]), title
                    ),
                }
            )

    drive_rows: list[dict] = []
    for kind, items in (("drive_deck", cached.decks), ("drive_doc", cached.docs)):
        kept, info = dedupe_drive_items(list(items))
        group_ids = {}
        for item in items:
            marker = info.get(item.file_id)
            canonical = (
                marker.duplicate_of
                if marker is not None and marker.duplicate_of
                else item.file_id
            )
            group_ids.setdefault(canonical, []).append(item.file_id)
        for item in kept:
            title = clean_title(item.title)
            key = material_key(title)
            if kind == "drive_deck" and key in presentation_keys:
                continue
            if kind == "drive_doc" and key in document_keys:
                continue
            drive_rows.append(
                {
                    "kind": "deck" if kind == "drive_deck" else "doc",
                    "kind_label": "הרצאה" if kind == "drive_deck" else "מסמך",
                    "title": title,
                    "topic": "",
                    "is_draft": False,
                    "due": None,
                    "portal": portal_for(
                        kind, group_ids.get(item.file_id, [item.file_id]), title
                    ),
                }
            )

    # Manual portal lessons have no Google source; they join the tree once,
    # under "no topic", so nothing the teacher built disappears from view.
    manual_lessons = db.scalars(
        select(Lesson)
        .where(Lesson.teacher_id == teacher.id)
        .order_by(Lesson.created_at.desc())
    ).all()
    for lesson in manual_lessons:
        if lesson.id in imported_lesson_ids:
            continue
        drive_rows.append(
            {
                "kind": "lesson",
                "kind_label": "שיעור",
                "title": lesson.title,
                "topic": "",
                "is_draft": False,
                "due": None,
                "portal": {
                    "type": "lesson",
                    "id": lesson.id,
                    "published": bool(lesson.published),
                },
            }
        )

    # Topics come from the course definition, so a topic with only drafts - or
    # with nothing imported yet - still shows up in the syllabus.
    topic_names: list[str] = []
    if snapshot is not None:
        topic_names = sorted(
            {name for name in snapshot.topics.values() if name},
            key=topic_sort_key,
        )
    sections: dict[str, dict] = {}
    for name in topic_names:
        sections[name] = {"name": name, "rows": []}
    numbered = {
        leading_number(name): name
        for name in topic_names
        if leading_number(name) is not None
    }
    leftovers: list[dict] = []
    for row in rows + drive_rows:
        name = row["topic"]
        if not name and row["kind"] in ("deck", "doc"):
            name = numbered.get(leading_number(row["title"]), "")
        if name:
            section = sections.get(name)
            if section is None:
                section = {"name": name, "rows": []}
                sections[name] = section
            section["rows"].append(row)
        else:
            leftovers.append(row)

    ordered = [sections[name] for name in sorted(sections, key=topic_sort_key)]
    for section in ordered:
        section["rows"].sort(
            key=lambda row: (
                natural_key(row["title"]),
                row["kind"] != "coursework",
                row["is_draft"],
            )
        )
    if leftovers:
        leftovers.sort(key=lambda row: natural_key(row["title"]))
        ordered.append({"name": "חומרים ללא נושא", "rows": leftovers})

    return {
        "course_name": snapshot.course_name if snapshot is not None else "",
        "groups": ordered,
        "total": sum(len(section["rows"]) for section in ordered),
        "catalog_has_data": cached.has_data,
        "catalog_synced_at": cached.synced_at,
        "catalog_status": cached.status,
        "catalog_error": cached.error,
    }


@router.get("/lessons")
def lessons_page(
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    return render(
        request,
        "teacher_lessons.html",
        user=teacher,
        tree=_course_content_tree(db, teacher),
        classes=_teacher_classes(db, teacher),
        selected_class=request.query_params.get("class_id", ""),
    )


@router.post("/lessons", dependencies=[Depends(verify_csrf)])
def create_lesson(
    request: Request,
    title: str = Form(default=""),
    summary: str = Form(default=""),
    class_id: str = Form(default=""),
    published: str | None = Form(default=None),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    back = RedirectResponse("/teacher/lessons", status_code=303)
    problem = lesson_title_error(title)
    if problem:
        flash(request, problem, kind="error")
        return back
    if len(summary or "") > _MAX_LESSON_SUMMARY:
        flash(request, "תקציר השיעור ארוך מדי.", kind="error")
        return back
    scope, scope_problem = _lesson_scope(db, teacher, class_id)
    if scope_problem:
        flash(request, scope_problem, kind="error")
        return back

    lesson = Lesson(
        teacher_id=teacher.id,
        class_id=scope,
        title=" ".join(title.split()),
        summary=(summary or "").strip(),
        published=_checkbox_true(published),
    )
    db.add(lesson)
    db.commit()
    flash(
        request,
        f"השיעור '{lesson.title}' נוצר. אפשר להוסיף שקופיות.",
        kind="success",
    )
    return RedirectResponse(f"/teacher/lessons/{lesson.id}", status_code=303)


@router.get("/lessons/{lesson_id}")
def lesson_page(
    lesson_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _owned_lesson(db, lesson_id, teacher)
    slides = _ordered_slides(db, lesson.id)
    contents = {
        slide.id: db.get(SlideContent, slide.id) for slide in slides
    }
    return render(
        request,
        "teacher_lesson.html",
        user=teacher,
        lesson=lesson,
        slides=slides,
        slide_contents=contents,
        lesson_source=db.get(LessonSource, lesson.id),
        classes=_teacher_classes(db, teacher),
    )


@router.post("/lessons/{lesson_id}/update", dependencies=[Depends(verify_csrf)])
def update_lesson(
    lesson_id: int,
    request: Request,
    title: str = Form(default=""),
    summary: str = Form(default=""),
    class_id: str = Form(default=""),
    published: str | None = Form(default=None),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _owned_lesson(db, lesson_id, teacher)
    back = RedirectResponse(f"/teacher/lessons/{lesson.id}", status_code=303)
    problem = lesson_title_error(title)
    if problem:
        flash(request, problem, kind="error")
        return back
    if len(summary or "") > _MAX_LESSON_SUMMARY:
        flash(request, "תקציר השיעור ארוך מדי.", kind="error")
        return back
    scope, scope_problem = _lesson_scope(db, teacher, class_id)
    if scope_problem:
        flash(request, scope_problem, kind="error")
        return back

    lesson.title = " ".join(title.split())
    lesson.summary = (summary or "").strip()
    lesson.class_id = scope
    lesson.published = _checkbox_true(published)
    db.commit()
    flash(request, "השיעור עודכן.", kind="success")
    return back


@router.post("/lessons/{lesson_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_lesson(
    lesson_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _owned_lesson(db, lesson_id, teacher)
    title = lesson.title
    pictures = list(
        db.scalars(
            select(SlideContent.image_blob_sha)
            .join(LessonSlide, LessonSlide.id == SlideContent.slide_id)
            .where(
                LessonSlide.lesson_id == lesson.id,
                SlideContent.image_blob_sha.is_not(None),
            )
        )
    )
    db.delete(lesson)
    db.commit()
    store = _content_store(request)
    for sha in pictures:
        store.delete_unreferenced(db, sha)
    db.commit()
    flash(request, f"השיעור '{title}' נמחק.", kind="success")
    return RedirectResponse("/teacher/lessons", status_code=303)


_MAX_ALT_TEXT = 300


def _clean_alt_text(raw: str, fallback: str) -> str:
    text = " ".join((raw or "").split())
    if not text:
        text = " ".join((fallback or "").split())
    return text[:_MAX_ALT_TEXT]


def _store_prepared_image(
    request: Request,
    db: Session,
    slide: LessonSlide,
    prepared: PreparedImage,
    alt_text: str,
) -> tuple[str | None, str | None]:
    """Persist a validated picture on an existing slide row.

    Returns ``(replaced_blob_sha, error_message)``. The caller commits first and
    only then drops the replaced blob, so a failed commit can never delete a
    file the database still points at.
    """
    store = _content_store(request)
    try:
        blob = store.save_and_register(db, prepared.webp, prepared.mime_type)
    except ContentStoreError:
        return None, "לא ניתן היה לשמור את התמונה. נסו שוב."

    content = db.get(SlideContent, slide.id)
    previous = content.image_blob_sha if content is not None else None
    if content is None:
        content = SlideContent(slide_id=slide.id)
        db.add(content)
    content.image_blob_sha = blob.sha256
    content.alt_text = _clean_alt_text(alt_text, slide.title)
    content.width = prepared.width
    content.height = prepared.height
    db.flush()
    replaced = previous if previous and previous != blob.sha256 else None
    return replaced, None


def _commit_and_prune(request: Request, db: Session, replaced: str | None) -> None:
    db.commit()
    if replaced:
        _content_store(request).delete_unreferenced(db, replaced)
        db.commit()


def _read_slide_image(image: UploadFile | None) -> tuple[PreparedImage | None, str | None]:
    """Decode an optional upload; return ``(prepared, error_message)``.

    Only the allowed size plus one byte is read, so an oversized upload cannot
    pull an unbounded file into memory before it is rejected.
    """
    if image is None or not (image.filename or "").strip():
        return None, None
    try:
        return prepare_slide_image(image.file.read(MAX_UPLOAD_BYTES + 1)), None
    except ImageUploadError as exc:
        return None, str(exc)


def _detach_slide_image(db: Session, slide: LessonSlide) -> str | None:
    """Unlink a slide's picture, keeping any imported text on the same row.

    Returns the blob that should be pruned once the change is committed.
    """
    content = db.get(SlideContent, slide.id)
    if content is None or not content.image_blob_sha:
        return None
    previous = content.image_blob_sha
    content.image_blob_sha = None
    content.alt_text = ""
    content.width = 0
    content.height = 0
    db.flush()
    return previous


@router.post("/lessons/{lesson_id}/slides", dependencies=[Depends(verify_csrf)])
def add_slide(
    lesson_id: int,
    request: Request,
    title: str = Form(default=""),
    body: str = Form(default=""),
    code: str = Form(default=""),
    alt_text: str = Form(default=""),
    image: UploadFile | None = File(default=None),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _owned_lesson(db, lesson_id, teacher)
    back = RedirectResponse(f"/teacher/lessons/{lesson.id}", status_code=303)

    prepared, image_problem = _read_slide_image(image)
    if image_problem:
        flash(request, image_problem, kind="error")
        return back

    problem = slide_title_error(title)
    if problem:
        flash(request, problem, kind="error")
        return back
    if len(body or "") > _MAX_SLIDE_BODY:
        flash(request, "תוכן השקופית ארוך מדי.", kind="error")
        return back
    if len(code or "") > _MAX_SLIDE_CODE:
        flash(request, "קטע הקוד ארוך מדי.", kind="error")
        return back
    if not (
        (title or "").strip()
        or (body or "").strip()
        or (code or "").strip()
        or prepared is not None
    ):
        flash(request, "יש להזין כותרת, תוכן, קוד או תמונה לשקופית.", kind="error")
        return back

    last_position = db.scalar(
        select(func.max(LessonSlide.position)).where(
            LessonSlide.lesson_id == lesson.id
        )
    )
    slide = LessonSlide(
        lesson_id=lesson.id,
        position=0 if last_position is None else int(last_position) + 1,
        title=" ".join((title or "").split()),
        body=(body or "").strip(),
        code=(code or "").strip(),
    )
    db.add(slide)
    replaced = None
    if prepared is not None:
        db.flush()  # the slide needs an id before its picture row can exist
        replaced, image_problem = _store_prepared_image(
            request, db, slide, prepared, alt_text
        )
        if image_problem:
            db.rollback()
            flash(request, image_problem, kind="error")
            return back
    _commit_and_prune(request, db, replaced)
    flash(request, "השקופית נוספה.", kind="success")
    return RedirectResponse(
        f"/teacher/lessons/{lesson.id}#slide-{slide.id}", status_code=303
    )


@router.post(
    "/lessons/{lesson_id}/slides/{slide_id}/update",
    dependencies=[Depends(verify_csrf)],
)
def update_slide(
    lesson_id: int,
    slide_id: int,
    request: Request,
    title: str = Form(default=""),
    body: str = Form(default=""),
    code: str = Form(default=""),
    alt_text: str = Form(default=""),
    image: UploadFile | None = File(default=None),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _owned_lesson(db, lesson_id, teacher)
    slide = _owned_slide(db, lesson, slide_id)
    back = RedirectResponse(f"/teacher/lessons/{lesson.id}#slide-{slide.id}", status_code=303)

    prepared, image_problem = _read_slide_image(image)
    if image_problem:
        flash(request, image_problem, kind="error")
        return back

    problem = slide_title_error(title)
    if problem:
        flash(request, problem, kind="error")
        return back
    if len(body or "") > _MAX_SLIDE_BODY:
        flash(request, "תוכן השקופית ארוך מדי.", kind="error")
        return back
    if len(code or "") > _MAX_SLIDE_CODE:
        flash(request, "קטע הקוד ארוך מדי.", kind="error")
        return back
    content = db.get(SlideContent, slide.id)
    has_image = content is not None and bool(content.image_blob_sha)
    if not (
        (title or "").strip()
        or (body or "").strip()
        or (code or "").strip()
        or prepared is not None
        or has_image
    ):
        flash(request, "יש להזין כותרת, תוכן, קוד או תמונה לשקופית.", kind="error")
        return back

    slide.title = " ".join((title or "").split())
    slide.body = (body or "").strip()
    slide.code = (code or "").strip()
    replaced = None
    if prepared is not None:
        replaced, image_problem = _store_prepared_image(
            request, db, slide, prepared, alt_text
        )
        if image_problem:
            db.rollback()
            flash(request, image_problem, kind="error")
            return back
    elif has_image and (alt_text or "").strip():
        # Editing the description of an existing picture must not force the
        # teacher to upload the same file again.
        content.alt_text = _clean_alt_text(alt_text, slide.title)
    _commit_and_prune(request, db, replaced)
    flash(request, "השקופית עודכנה.", kind="success")
    return back


@router.post(
    "/lessons/{lesson_id}/slides/{slide_id}/image/remove",
    dependencies=[Depends(verify_csrf)],
)
def remove_slide_image(
    lesson_id: int,
    slide_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Drop a slide's picture without deleting the slide or its text."""
    lesson = _owned_lesson(db, lesson_id, teacher)
    slide = _owned_slide(db, lesson, slide_id)
    replaced = _detach_slide_image(db, slide)
    _commit_and_prune(request, db, replaced)
    flash(request, "התמונה הוסרה מהשקופית.", kind="success")
    return RedirectResponse(
        f"/teacher/lessons/{lesson.id}#slide-{slide.id}", status_code=303
    )


@router.post(
    "/lessons/{lesson_id}/slides/{slide_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def delete_slide(
    lesson_id: int,
    slide_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _owned_lesson(db, lesson_id, teacher)
    slide = _owned_slide(db, lesson, slide_id)
    replaced = _detach_slide_image(db, slide)
    db.delete(slide)
    db.flush()
    for index, remaining in enumerate(_ordered_slides(db, lesson.id)):
        remaining.position = index
    _commit_and_prune(request, db, replaced)
    flash(request, "השקופית נמחקה.", kind="success")
    return RedirectResponse(f"/teacher/lessons/{lesson.id}", status_code=303)


@router.post(
    "/lessons/{lesson_id}/slides/{slide_id}/move",
    dependencies=[Depends(verify_csrf)],
)
def move_slide(
    lesson_id: int,
    slide_id: int,
    request: Request,
    direction: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _owned_lesson(db, lesson_id, teacher)
    slide = _owned_slide(db, lesson, slide_id)
    slides = _ordered_slides(db, lesson.id)
    index = next(
        (position for position, item in enumerate(slides) if item.id == slide.id),
        -1,
    )
    if index < 0:
        raise HTTPException(status_code=404, detail="Slide not found.")

    if direction == "up" and index > 0:
        slides[index - 1], slides[index] = slides[index], slides[index - 1]
    elif direction == "down" and index < len(slides) - 1:
        slides[index], slides[index + 1] = slides[index + 1], slides[index]
    else:
        flash(request, "השקופית כבר בקצה.", kind="info")
        return RedirectResponse(
            f"/teacher/lessons/{lesson.id}#slide-{slide.id}", status_code=303
        )

    for position, item in enumerate(slides):
        item.position = position
    db.commit()
    flash(request, "סדר השקופיות עודכן.", kind="success")
    return RedirectResponse(
        f"/teacher/lessons/{lesson.id}#slide-{slide.id}", status_code=303
    )


# ----------------------------------------------------------------- Classroom


def _content_store(request: Request) -> ContentStore:
    store = getattr(request.app.state, "content_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="אחסון התוכן אינו זמין.")
    return store


def _classroom_source(request: Request) -> ClassroomSource:
    """The Google boundary, injectable so tests never touch the network."""
    source = getattr(request.app.state, "classroom_source", None)
    return source if source is not None else ClassroomSource()


def _owned_job(db: Session, job_id: int, teacher: User) -> ImportJob:
    job = db.get(ImportJob, job_id)
    if job is None or job.teacher_id != teacher.id:
        raise HTTPException(status_code=404, detail="Import job not found.")
    return job


def _manifest_of(job: ImportJob) -> ImportManifest:
    try:
        return ImportManifest.from_json(json.loads(job.manifest_json or "{}"))
    except (TypeError, ValueError):
        return ImportManifest(
            kind="",
            course_id="",
            course_name="",
            class_id=job.class_id or 0,
            class_name="",
            items=[],
            generated_at="",
        )


def _job_counts(job: ImportJob) -> dict:
    try:
        value = json.loads(job.counts_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@router.get("/import")
def import_page(
    request: Request,
    class_id: int = 0,
    folder_id: str = "",
    show_decks: int = 0,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    classes = _teacher_classes(db, teacher)
    selected = None
    if class_id:
        selected = next((group for group in classes if group.id == class_id), None)
        if selected is None:
            flash(request, "הכיתה שנבחרה אינה שלכם.", kind="error")

    courses = []
    decks = []
    deck_groups: list[tuple[object, int]] = []
    source_error = ""
    source = _classroom_source(request)
    if selected is not None:
        try:
            courses = source.list_courses()
        except Exception as exc:  # noqa: BLE001 - shown as a teacher message
            source_error = str(exc)
        if (folder_id or show_decks) and not source_error:
            try:
                decks = source.list_drive_decks(folder_id.strip())
            except Exception as exc:  # noqa: BLE001
                source_error = str(exc)

    if decks:
        kept, dedupe_info = dedupe_drive_items(decks)
        deck_groups = []
        for deck in kept:
            info = dedupe_info.get(deck.file_id)
            deck_groups.append((deck, info.duplicate_count if info else 0))

    jobs = db.scalars(
        select(ImportJob)
        .where(
            ImportJob.teacher_id == teacher.id,
            ImportJob.kind != "catalog_sync",
        )
        .order_by(ImportJob.created_at.desc())
        .limit(10)
    ).all()
    links = {
        link.class_id: link
        for link in db.scalars(
            select(ClassroomLink).where(ClassroomLink.teacher_id == teacher.id)
        ).all()
    }
    return render(
        request,
        "teacher_import.html",
        user=teacher,
        classes=classes,
        selected=selected,
        courses=courses,
        decks=decks,
        deck_groups=deck_groups,
        folder_id=folder_id,
        source_error=source_error,
        jobs=jobs,
        job_counts={job.id: _job_counts(job) for job in jobs},
        links=links,
    )


@router.post("/import/preview", dependencies=[Depends(verify_csrf)])
def import_preview(
    request: Request,
    class_id: int = Form(...),
    course_id: str = Form(...),
    include_drafts: str | None = Form(default=None),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    try:
        job = importer.build_course_preview(
            db,
            _classroom_source(request),
            teacher_id=teacher.id,
            group=group,
            course_id=course_id.strip(),
            include_drafts=_checkbox_true(include_drafts),
        )
    except Exception as exc:  # noqa: BLE001 - auth/network/API message
        flash(request, f"לא ניתן לבנות תצוגה מקדימה: {exc}", kind="error")
        return RedirectResponse(
            f"/teacher/import?class_id={group.id}", status_code=303
        )
    flash(
        request,
        "התצוגה המקדימה מוכנה. בדקו כל פריט ובחרו מה לייבא.",
        kind="success",
    )
    return RedirectResponse(f"/teacher/import/jobs/{job.id}", status_code=303)


@router.post("/import/decks", dependencies=[Depends(verify_csrf)])
def import_decks(
    request: Request,
    class_id: int = Form(...),
    deck_ids: list[str] = Form(default=[]),
    deck_id: str = Form(default=""),
    folder_id: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    chosen = [value for value in deck_ids if value.strip()]
    if deck_id.strip():
        chosen.append(deck_id.strip())
    if not chosen:
        flash(request, "בחרו לפחות מצגת אחת לייבוא.", kind="error")
        return RedirectResponse(
            f"/teacher/import?class_id={group.id}&folder_id={quote(folder_id)}",
            status_code=303,
        )
    try:
        job = importer.build_deck_preview(
            db,
            _classroom_source(request),
            teacher_id=teacher.id,
            group=group,
            file_ids=chosen,
            folder_id=folder_id.strip(),
        )
    except Exception as exc:  # noqa: BLE001
        flash(request, f"לא ניתן לקרוא את המצגות: {exc}", kind="error")
        return RedirectResponse(
            f"/teacher/import?class_id={group.id}", status_code=303
        )
    return RedirectResponse(f"/teacher/import/jobs/{job.id}", status_code=303)


@router.get("/import/jobs/{job_id}")
def import_job_page(
    job_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    job = _owned_job(db, job_id, teacher)
    manifest = _manifest_of(job)
    records = db.scalars(
        select(ImportItem).where(ImportItem.job_id == job.id)
    ).all()
    return render(
        request,
        "teacher_import_job.html",
        user=teacher,
        job=job,
        manifest=manifest,
        counts=_job_counts(job),
        records={record.source_id: record for record in records},
        rubric_choices=_rubric_choices(),
        actions=importer.ACTIONS,
    )


@router.post(
    "/import/jobs/{job_id}/apply",
    dependencies=[Depends(verify_csrf)],
)
async def import_apply(
    job_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    job = _owned_job(db, job_id, teacher)
    manifest = _manifest_of(job)
    form = await request.form()

    for item in manifest.items:
        action = str(form.get(f"action_{item.source_id}") or "").strip()
        if action in importer.ACTIONS:
            item.suggested_action = action
        rubric = form.get(f"rubric_{item.source_id}")
        if rubric is not None:
            item.suggested_rubric = str(rubric).strip()

    job.manifest_json = json.dumps(manifest.as_json(), ensure_ascii=False)
    job.state = "queued"
    job.error = ""
    db.commit()

    worker = getattr(request.app.state, "import_worker", None)
    if worker is None:
        flash(
            request,
            "הייבוא נשמר אך אין מעבד פעיל. הריצו את הייבוא דרך שורת הפקודה.",
            kind="error",
        )
    else:
        worker.enqueue(job.id)
        flash(request, "הייבוא החל. אפשר לרענן את העמוד להתקדמות.", kind="success")
    return RedirectResponse(f"/teacher/import/jobs/{job.id}", status_code=303)


@router.post(
    "/import/jobs/{job_id}/resume",
    dependencies=[Depends(verify_csrf)],
)
def import_resume(
    job_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    job = _owned_job(db, job_id, teacher)
    job.state = "queued"
    job.error = ""
    db.commit()
    worker = getattr(request.app.state, "import_worker", None)
    if worker is not None:
        worker.enqueue(job.id)
        flash(request, "הייבוא ממשיך מהנקודה שבה נעצר.", kind="success")
    else:
        flash(request, "אין מעבד פעיל; הריצו resume דרך שורת הפקודה.", kind="error")
    return RedirectResponse(f"/teacher/import/jobs/{job.id}", status_code=303)


@router.get("/import/jobs/{job_id}/manifest")
def import_manifest_json(
    job_id: int,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    job = _owned_job(db, job_id, teacher)
    return JSONResponse(_manifest_of(job).as_json())


# -------------------------------------------------------------- cross-class reuse


@router.post(
    "/classes/{class_id}/reuse/lesson",
    dependencies=[Depends(verify_csrf)],
)
def reuse_lesson_in_class(
    class_id: int,
    request: Request,
    lesson_id: int = Form(...),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    lesson = _owned_lesson(db, lesson_id, teacher)
    try:
        clone = importer.reuse_lesson(db, lesson, group)
    except importer.ReuseError as exc:
        db.rollback()
        flash(request, str(exc), kind="error")
        return RedirectResponse(f"/teacher/classes/{group.id}", status_code=303)
    db.commit()
    flash(
        request,
        f"השיעור '{lesson.title}' הועתק לכיתה '{group.name}' כטיוטה.",
        kind="success",
    )
    return RedirectResponse(f"/teacher/lessons/{clone.id}", status_code=303)


@router.post(
    "/classes/{class_id}/reuse/assignment",
    dependencies=[Depends(verify_csrf)],
)
def reuse_assignment_in_class(
    class_id: int,
    request: Request,
    assignment_id: int = Form(...),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    assignment = _owned_assignment(db, assignment_id, teacher)
    try:
        importer.reuse_assignment(db, assignment, group)
    except importer.ReuseError as exc:
        db.rollback()
        flash(request, str(exc), kind="error")
        return RedirectResponse(f"/teacher/classes/{group.id}", status_code=303)
    db.commit()
    flash(
        request,
        f"המטלה '{assignment.title}' הועתקה לכיתה '{group.name}' כטיוטה.",
        kind="success",
    )
    return RedirectResponse(f"/teacher/classes/{group.id}", status_code=303)


# --------------------------------------------------------------------- chat
@router.get("/chat")
def chat_page(
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
    student: int | None = Query(default=None),
):
    """The teacher's chat tab: an inbox of students plus the open thread."""
    tz = request.app.state.portal_settings.timezone
    conversations = chat.conversations_for_teacher(db, teacher.id)
    active_id = None
    if student is not None and chat.can_message(db, student, teacher.id):
        active_id = student
    elif conversations:
        active_id = conversations[0]["student_id"]
    messages: list[dict] = []
    active_name = ""
    if active_id is not None:
        chat.mark_read(db, teacher.id, active_id, reader_role=models.ROLE_TEACHER)
        db.commit()
        messages = [
            chat.message_view(m, viewer_role=models.ROLE_TEACHER, timezone_name=tz)
            for m in chat.thread(db, teacher.id, active_id)
        ]
        student_row = db.get(User, active_id)
        active_name = (
            (student_row.display_name or student_row.username)
            if student_row
            else ""
        )
        for row in conversations:
            if row["student_id"] == active_id:
                row["unread"] = 0  # the open thread was just read
                break
    return render(
        request,
        "teacher_chat.html",
        user=teacher,
        conversations=conversations,
        active_student=active_id,
        active_name=active_name,
        messages=messages,
        viewer_role=models.ROLE_TEACHER,
        max_message_chars=chat.MAX_MESSAGE_CHARS,
    )


@router.get("/chat/thread")
def chat_thread(
    request: Request,
    student: int = Query(...),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """JSON: the open thread with one student, marked read for the teacher."""
    if not chat.can_message(db, student, teacher.id):
        raise HTTPException(status_code=403, detail="אין הרשאה לשיחה הזו.")
    chat.mark_read(db, teacher.id, student, reader_role=models.ROLE_TEACHER)
    db.commit()
    messages = [
        chat.message_view(
            m,
            viewer_role=models.ROLE_TEACHER,
            timezone_name=request.app.state.portal_settings.timezone,
        )
        for m in chat.thread(db, teacher.id, student)
    ]
    return JSONResponse(
        {
            "ok": True,
            "messages": messages,
            "unread": chat.total_unread(db, teacher.id, models.ROLE_TEACHER),
        }
    )


@router.post("/chat/send", dependencies=[Depends(verify_csrf)])
def chat_send(
    request: Request,
    student_id: int = Form(...),
    body: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Reply to a student. Teacher messages are general to the conversation."""
    if not chat.can_message(db, student_id, teacher.id):
        return JSONResponse(
            {"ok": False, "error": "אין הרשאה לשלוח הודעה לתלמיד הזה."},
            status_code=403,
        )
    try:
        message = chat.send(
            db,
            teacher_id=teacher.id,
            student_id=student_id,
            sender_role=models.ROLE_TEACHER,
            body=body,
        )
        db.commit()
    except chat.ChatError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "student_id": student_id,
            "message": chat.message_view(
                message,
                viewer_role=models.ROLE_TEACHER,
                timezone_name=request.app.state.portal_settings.timezone,
            ),
        }
    )


@router.get("/chat/unread")
def chat_unread(
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    return JSONResponse(
        {"unread": chat.total_unread(db, teacher.id, models.ROLE_TEACHER)}
    )


# ---------------------------------------------------------------- grading


def _grading_sections(db: Session, teacher: User, groups) -> list[dict]:
    """Assignments plus hand-in counts for one class or the whole account."""
    sections: list[dict] = []
    for group in groups:
        assignments = db.scalars(
            select(Assignment)
            .where(Assignment.class_id == group.id)
            .order_by(Assignment.created_at.desc())
        ).all()
        counts = service.class_grading_counts(db, group.id)
        enrolled = int(
            db.scalar(
                select(func.count(Enrollment.id)).where(
                    Enrollment.class_id == group.id
                )
            )
            or 0
        )
        sections.append(
            {
                "group": group,
                "rows": [
                    {
                        "assignment": assignment,
                        "counts": counts.get(assignment.id, {}),
                    }
                    for assignment in assignments
                ],
                "enrolled": enrolled,
            }
        )
    return sections


@router.get("/grading")
def grading_overview(
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    groups = _teacher_classes(db, teacher)
    return render(
        request,
        "teacher_grading.html",
        user=teacher,
        sections=_grading_sections(db, teacher, groups),
        group=None,
        waiting_total=service.waiting_count(db, teacher.id),
    )


@router.get("/classes/{class_id}/grading")
def class_grading_overview(
    class_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    group = _owned_class(db, class_id, teacher)
    return render(
        request,
        "teacher_grading.html",
        user=teacher,
        sections=_grading_sections(db, teacher, [group]),
        group=group,
        waiting_total=service.waiting_count(db, teacher.id),
    )


@router.get("/assignments/{assignment_id}/grading")
def assignment_grading_page(
    assignment_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    assignment = _owned_assignment(db, assignment_id, teacher)
    group = db.get(ClassGroup, assignment.class_id)
    rows = service.assignment_grading_rows(db, assignment)
    return render(
        request,
        "teacher_assignment_grading.html",
        user=teacher,
        group=group,
        assignment=assignment,
        rows=rows,
        status_labels=service.STATUS_LABELS,
        status_badges=service.STATUS_BADGES,
        not_submitted_label=service.NOT_SUBMITTED_LABEL,
    )


def _owned_submission(
    db: Session, submission_id: int, teacher: User
) -> tuple[Submission, Assignment]:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found.")
    assignment = _owned_assignment(db, submission.assignment_id, teacher)
    return submission, assignment


def _exam_review_scores(result: dict | None) -> dict[str, dict]:
    """Per-question marks for the review paper, keyed by item.

    Each question carries its parts' points and reasons, so the template can
    put the explanation under the answer it belongs to. Papers marked before
    per-part marks existed have no ``parts``; they keep the question's own
    note, which the template shows once under the question.
    """
    notes_by_key = {
        str(task.get("key")): str(task.get("notes") or "")
        for task in ((result or {}).get("tasks") or [])
        if isinstance(task, dict) and task.get("key")
    }
    scores: dict[str, dict] = {}
    for question in ((result or {}).get("exam_questions") or []):
        if not isinstance(question, dict) or not question.get("key"):
            continue
        key = str(question["key"])
        parts = {
            str(part.get("part")): part
            for part in (question.get("parts") or [])
            if isinstance(part, dict) and part.get("part")
        }
        scores[key] = {
            **question,
            "parts": parts,
            "notes": str(question.get("notes") or "") or notes_by_key.get(key, ""),
        }
    return scores


@router.get("/submissions/{submission_id}")
def submission_review_page(
    submission_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    submission, assignment = _owned_submission(db, submission_id, teacher)
    group = db.get(ClassGroup, assignment.class_id)
    student = db.get(User, submission.student_id)
    # A reopened exam sits on a fresh, empty continuation revision. Showing that
    # blank slot would look like the student's work vanished, so the review keeps
    # displaying the preserved previous hand-in (read-only) until they hand in
    # again. The accidental hand-in stays in history either way.
    reopened = submission.status == models.SUBMISSION_REOPENED
    if reopened:
        grade = service.grade_for_revision(
            db, submission.id, submission.revision - 1
        )
    else:
        grade = service.current_grade(db, submission)
    result = service.grade_view(grade)
    job = service.latest_job(db, submission)

    # While a model re-check is ready but not yet applied, the review must show
    # that latest verdict - the LLM's fresh score and per-part reasons - not the
    # older stored grade. The preview row already holds the full result record,
    # so it is projected into the same view shape; a banner tells the teacher it
    # is unapplied. A reopened exam keeps showing its preserved history instead.
    exam_preview = False
    exam_preview_stale = False
    if not reopened:
        preview = _recheck_preview(db, assignment)
        preview_record = exam_llm_check.preview_record(preview, submission.id)
        if preview_record is not None:
            if exam_llm_check.preview_is_stale(db, preview, submission.id):
                # The live grade was rewritten after this open preview was
                # computed (today that is almost always the automatic model
                # pass). The apply paths drop exactly such a row, so showing it
                # here would present an older, rules-only verdict as the model's
                # latest word - the teacher reads generic "לבדיקת המורה" comments
                # and concludes the model never ran. Keep the newer stored grade.
                exam_preview_stale = True
            else:
                exam_preview = True
                result = service.record_view(
                    preview_record,
                    model="exam-checker",
                    graded_at=preview.finished_at,
                    rubric_title=(result or {}).get("rubric_title", ""),
                    rubric_sha256=(result or {}).get("rubric_sha256", ""),
                    # The sign-off lives on the stored grade, not on the preview.
                    # Carry it into the projection so an approved paper keeps its
                    # "נסקר" badge and the teacher's note while the fresh verdict
                    # is on display - otherwise the approval looks lost.
                    reviewed_at=grade.reviewed_at if grade is not None else None,
                    review_note=(grade.review_note or "") if grade is not None else "",
                )

    # An exam is not one shared paper: this student sat a version in their own
    # order. The review has to show that exact paper and their stored answers,
    # otherwise the teacher is adjusting points against question titles with no
    # question in front of them.
    exam_paper = None
    exam_values: dict = {}
    exam_scores: dict = {}
    exam_version_label = ""
    exam_total_points = 0
    exam_name = str(assignment.exam_config or "").strip()
    if exam_name and student is not None:
        config = exam_config.load_exam(exam_name)
        if config is not None:
            paper = exam_service.review_paper(db, assignment, student, config)
            if paper is not None:
                exam_paper = paper.display
                exam_version_label = paper.version.label
                exam_total_points = paper.total_points
                exam_values = {
                    key: value
                    for key, value in worksheets.load_values(
                        db,
                        submission,
                        revision=(
                            submission.revision - 1 if reopened else None
                        ),
                    ).items()
                    if isinstance(value, dict)
                }
                exam_scores = _exam_review_scores(result)

    return render(
        request,
        "teacher_submission.html",
        user=teacher,
        group=group,
        assignment=assignment,
        student=student,
        submission=submission,
        grade=grade,
        result=result,
        job=job,
        reopened=reopened,
        exam_paper=exam_paper,
        exam_values=exam_values,
        exam_scores=exam_scores,
        exam_version_label=exam_version_label,
        exam_total_points=exam_total_points,
        exam_preview=exam_preview,
        exam_preview_stale=exam_preview_stale,
        status_labels=service.STATUS_LABELS,
        status_badges=service.STATUS_BADGES,
        task_status_labels=service.TASK_STATUS_LABELS,
    )


@router.get("/submissions/{submission_id}/grading-status")
def submission_grading_status(
    submission_id: int,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Status probe for the review page's pending marker.

    Same contract as the student probes: the page polls this instead of
    reloading itself blindly, so a teacher reading a hand-in is never jumped
    to the top of the page while the grade is still running.
    """
    submission, _assignment = _owned_submission(db, submission_id, teacher)
    return JSONResponse(
        {
            "status": submission.status,
            "pending": submission.status in ("queued", "grading"),
            "revision": submission.revision,
        }
    )


@router.post(
    "/submissions/{submission_id}/review",
    dependencies=[Depends(verify_csrf)],
)
def review_submission(
    submission_id: int,
    request: Request,
    note: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    submission, _assignment = _owned_submission(db, submission_id, teacher)
    grade = service.current_grade(db, submission)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}", status_code=303
    )
    if grade is None:
        flash(request, "אין עדיין ציון לסמן כנבדק.", kind="error")
        return back
    grade.reviewed_at = utcnow()
    grade.reviewed_by = teacher.id
    grade.review_note = (note or "").strip()[:5000]
    db.commit()
    flash(request, "ההגשה סומנה כנבדקה.", kind="success")
    return back


@router.post(
    "/submissions/{submission_id}/disqualify",
    dependencies=[Depends(verify_csrf)],
)
def disqualify_submission(
    submission_id: int,
    request: Request,
    score: str = Form(default="0"),
    reason: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Lock a hand-in to a fixed score - for example a copied exam the teacher
    disqualifies. The score is fixed: a later re-check or model pass cannot
    change it, and the paper is shielded from the selective model check. The
    reason is teacher-only; the student sees the disqualification and the score.
    """
    submission, assignment = _owned_submission(db, submission_id, teacher)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}", status_code=303
    )
    if not str(assignment.exam_config or "").strip():
        flash(request, "אפשר לפסול רק הגשה של מבחן עם מפתח תשובות.", kind="error")
        return back
    grade = service.current_grade(db, submission)
    if grade is None:
        flash(request, "אין עדיין ציון לפסול. המתינו לסיום הבדיקה.", kind="error")
        return back
    try:
        wanted = int((score or "").strip() or "0")
    except ValueError:
        flash(request, "הציון הקבוע חייב להיות מספר שלם.", kind="error")
        return back
    try:
        info = exam_manual.disqualify(
            grade,
            score=wanted,
            reason=reason,
            teacher_id=teacher.id,
            teacher_name=teacher.display_name,
        )
        db.commit()
    except exam_manual.ExamAdjustmentError as exc:
        flash(request, str(exc), kind="error")
        return back
    flash(
        request,
        f"ההגשה נפסלה ונקבע לה ציון קבוע {info['score']}. הציון נעול: בדיקה חוזרת או מודל לא ישנו אותו.",
        kind="success",
    )
    return back


@router.post(
    "/submissions/{submission_id}/disqualify/clear",
    dependencies=[Depends(verify_csrf)],
)
def clear_disqualification_submission(
    submission_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Undo a disqualification: restore the automatic score and re-check eligibility."""
    submission, _assignment = _owned_submission(db, submission_id, teacher)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}", status_code=303
    )
    grade = service.current_grade(db, submission)
    if grade is None:
        flash(request, "אין ציון לבטל את פסילתו.", kind="error")
        return back
    try:
        exam_manual.clear_disqualification(
            grade, teacher_id=teacher.id, teacher_name=teacher.display_name
        )
        db.commit()
    except exam_manual.ExamAdjustmentError as exc:
        flash(request, str(exc), kind="error")
        return back
    flash(
        request,
        "הפסילה בוטלה והציון האוטומטי הוחזר. אפשר לבדוק את ההגשה שוב.",
        kind="success",
    )
    return back


@router.post(
    "/submissions/{submission_id}/questions/{item_key}/adjust",
    dependencies=[Depends(verify_csrf)],
)
def adjust_exam_question(
    submission_id: int,
    item_key: str,
    request: Request,
    points: str = Form(default=""),
    note: str = Form(default=""),
    full: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Override one exam question's points; ``full=1`` gives full credit.

    When a model re-check is ready for this hand-in, the teacher is correcting the
    model's latest (still unapplied) verdict, so the override lands on the preview
    row - exactly what the review page shows and what a later apply will write.
    Either way the correction is stored as a bounded example that calibrates the
    model the next time it marks the same question.
    """
    submission, assignment = _owned_submission(db, submission_id, teacher)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}#question-{item_key}",
        status_code=303,
    )
    wanted = None
    if full != "1":
        text = (points or "").strip()
        if not text:
            flash(request, "יש להזין ניקוד מדויק לשאלה.", kind="error")
            return back
        try:
            wanted = int(text)
        except ValueError:
            flash(request, "הניקוד חייב להיות מספר שלם.", kind="error")
            return back

    preview, on_preview, preview_overtaken = _exam_edit_target(
        db, assignment, submission
    )
    info: dict | None = None
    try:
        if on_preview:
            # adjust_row persists the preview payload itself.
            info = exam_llm_check.adjust_row(
                db,
                preview,
                submission.id,
                item_key,
                points=wanted,
                note=note,
                teacher_id=teacher.id,
                teacher_name=teacher.display_name,
                full_credit=full == "1",
            )
        else:
            grade = service.current_grade(db, submission)
            if grade is None:
                flash(request, "אין עדיין ציון להתאים.", kind="error")
                return back
            info = exam_manual.adjust(
                grade,
                item_key,
                points=wanted,
                note=note,
                teacher_id=teacher.id,
                teacher_name=teacher.display_name,
                full_credit=full == "1",
            )
            db.commit()
    except (exam_manual.ExamAdjustmentError, exam_llm_check.LLMCheckError) as exc:
        flash(request, str(exc), kind="error")
        return back

    saved = None
    distilling = False
    if info is not None:
        saved = exam_feedback.record(
            db,
            exam_config=str(assignment.exam_config or ""),
            item_key=item_key,
            answer=info.get("answer", ""),
            model_points=info.get("auto_points", 0),
            teacher_points=info.get("points", 0),
            note=note,
            maximum=info.get("maximum", 0),
            submission_id=submission.id,
            created_by=teacher.id,
        )
        db.commit()
        # Once a question has gathered enough new corrections, distill them into
        # its grading-style profile in the background (one routed model call).
        # The save returns at once; a quota-exhausted distill keeps the previous
        # profile and a later save past the threshold retries it.
        exam_name = str(assignment.exam_config or "")
        if exam_calibration.needs_distill(db, exam_name, item_key):
            distilling = exam_calibration.distill_in_thread(
                request.app.state.session_factory,
                exam_name,
                item_key,
                title=info.get("label", ""),
            )
    if on_preview:
        message = "הניקוד עודכן בבדיקת המודל האחרונה (טרם הוחלה על הציון)."
    else:
        message = "הניקוד עודכן, וההתאמה נרשמה בהערת הסקירה."
        if preview_overtaken:
            message += " בדיקת המודל הישנה לא עודכנה: הציון החי נבדק מחדש אחריה."
    if saved is not None:
        message += " התיקון נשמר כדי לכוון את בדיקת המודל הבאה."
    if distilling:
        message += " סגנון הניקוד לשאלה זו מתעדכן ברקע."
    flash(request, message, kind="success")
    return back


@router.post(
    "/submissions/{submission_id}/questions/{item_key}/reset",
    dependencies=[Depends(verify_csrf)],
)
def reset_exam_question(
    submission_id: int,
    item_key: str,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Remove a manual override and restore the checker's points."""
    submission, assignment = _owned_submission(db, submission_id, teacher)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}#question-{item_key}",
        status_code=303,
    )
    preview, on_preview, _ = _exam_edit_target(db, assignment, submission)
    try:
        if on_preview:
            exam_llm_check.clear_row(
                db,
                preview,
                submission.id,
                item_key,
                teacher_id=teacher.id,
                teacher_name=teacher.display_name,
            )
        else:
            grade = service.current_grade(db, submission)
            if grade is None:
                flash(request, "אין עדיין ציון להתאים.", kind="error")
                return back
            exam_manual.clear(
                grade,
                item_key,
                teacher_id=teacher.id,
                teacher_name=teacher.display_name,
            )
            db.commit()
    except (exam_manual.ExamAdjustmentError, exam_llm_check.LLMCheckError) as exc:
        flash(request, str(exc), kind="error")
        return back
    # Taking back the override also takes back the correction it taught.
    exam_feedback.retract(
        db,
        exam_config=str(assignment.exam_config or ""),
        item_key=item_key,
        submission_id=submission.id,
    )
    db.commit()
    flash(request, "התאמת הניקוד בוטלה והניקוד האוטומטי הוחזר.", kind="success")
    return back


def _part_answer_text(
    db: Session, submission: Submission, item_key: str, part_key: str
) -> str:
    """The student's stored answer for one part, as plain text for feedback.

    A part correction teaches the model about *that part's* answer (the
    explanation, say, not the whole question), so the bounded example carries
    the part's own text rather than the combined question answer.
    """
    item_values = worksheets.load_values(db, submission).get(item_key)
    if not isinstance(item_values, dict):
        return ""
    value = item_values.get(part_key)
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


@router.post(
    "/submissions/{submission_id}/questions/{item_key}/parts/{part_key}/adjust",
    dependencies=[Depends(verify_csrf)],
)
def adjust_exam_question_part(
    submission_id: int,
    item_key: str,
    part_key: str,
    request: Request,
    points: str = Form(default=""),
    note: str = Form(default=""),
    full: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Override one *part's* points inside a multi-part exam question.

    The question total becomes the sum of its parts; each part is graded within
    its own rubric slice. Like the whole-question editor, when a model re-check
    is ready the override lands on the preview row, and the correction is stored
    as a bounded example that calibrates the model for this question (the note
    names the part so the model knows which answer was corrected).
    """
    submission, assignment = _owned_submission(db, submission_id, teacher)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}#part-{item_key}-{part_key}",
        status_code=303,
    )
    wanted = None
    if full != "1":
        text = (points or "").strip()
        if not text:
            flash(request, "יש להזין ניקוד מדויק לחלק.", kind="error")
            return back
        try:
            wanted = int(text)
        except ValueError:
            flash(request, "הניקוד חייב להיות מספר שלם.", kind="error")
            return back

    preview, on_preview, preview_overtaken = _exam_edit_target(
        db, assignment, submission
    )
    info: dict | None = None
    try:
        if on_preview:
            info = exam_llm_check.adjust_row(
                db,
                preview,
                submission.id,
                item_key,
                points=wanted,
                part_key=part_key,
                note=note,
                teacher_id=teacher.id,
                teacher_name=teacher.display_name,
                full_credit=full == "1",
            )
        else:
            grade = service.current_grade(db, submission)
            if grade is None:
                flash(request, "אין עדיין ציון להתאים.", kind="error")
                return back
            info = exam_manual.adjust_part(
                grade,
                item_key,
                part_key,
                points=wanted,
                note=note,
                teacher_id=teacher.id,
                teacher_name=teacher.display_name,
                full_credit=full == "1",
            )
            db.commit()
    except (exam_manual.ExamAdjustmentError, exam_llm_check.LLMCheckError) as exc:
        flash(request, str(exc), kind="error")
        return back

    saved = None
    distilling = False
    if info is not None:
        # Calibration stays per question: the part's correction feeds the
        # question's existing profile, and the note names the part.
        part_label = str(info.get("part_label") or "")
        feedback_note = f"{part_label}: {note}" if note else part_label
        saved = exam_feedback.record(
            db,
            exam_config=str(assignment.exam_config or ""),
            item_key=item_key,
            answer=_part_answer_text(db, submission, item_key, part_key),
            model_points=info.get("auto_points", 0),
            teacher_points=info.get("points", 0),
            note=feedback_note,
            maximum=info.get("maximum", 0),
            submission_id=submission.id,
            created_by=teacher.id,
        )
        db.commit()
        exam_name = str(assignment.exam_config or "")
        if exam_calibration.needs_distill(db, exam_name, item_key):
            distilling = exam_calibration.distill_in_thread(
                request.app.state.session_factory,
                exam_name,
                item_key,
                title=info.get("label", ""),
            )
    if on_preview:
        message = "הניקוד עודכן בבדיקת המודל האחרונה (טרם הוחלה על הציון)."
    else:
        message = "הניקוד עודכן, וההתאמה נרשמה בהערת הסקירה."
        if preview_overtaken:
            message += " בדיקת המודל הישנה לא עודכנה: הציון החי נבדק מחדש אחריה."
    if saved is not None:
        message += " התיקון נשמר כדי לכוון את בדיקת המודל הבאה."
    if distilling:
        message += " סגנון הניקוד לשאלה זו מתעדכן ברקע."
    flash(request, message, kind="success")
    return back


@router.post(
    "/submissions/{submission_id}/questions/{item_key}/parts/{part_key}/reset",
    dependencies=[Depends(verify_csrf)],
)
def reset_exam_question_part(
    submission_id: int,
    item_key: str,
    part_key: str,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Remove a part's manual override and restore the checker's points."""
    submission, assignment = _owned_submission(db, submission_id, teacher)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}#part-{item_key}-{part_key}",
        status_code=303,
    )
    preview, on_preview, _ = _exam_edit_target(db, assignment, submission)
    try:
        if on_preview:
            exam_llm_check.clear_row(
                db,
                preview,
                submission.id,
                item_key,
                part_key=part_key,
                teacher_id=teacher.id,
                teacher_name=teacher.display_name,
            )
        else:
            grade = service.current_grade(db, submission)
            if grade is None:
                flash(request, "אין עדיין ציון להתאים.", kind="error")
                return back
            exam_manual.clear_part(
                grade,
                item_key,
                part_key,
                teacher_id=teacher.id,
                teacher_name=teacher.display_name,
            )
            db.commit()
    except (exam_manual.ExamAdjustmentError, exam_llm_check.LLMCheckError) as exc:
        flash(request, str(exc), kind="error")
        return back
    exam_feedback.retract(
        db,
        exam_config=str(assignment.exam_config or ""),
        item_key=item_key,
        submission_id=submission.id,
    )
    db.commit()
    flash(request, "התאמת הניקוד בוטלה והניקוד האוטומטי הוחזר.", kind="success")
    return back


@router.post(
    "/submissions/{submission_id}/reopen",
    dependencies=[Depends(verify_csrf)],
)
def reopen_exam_submission(
    submission_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Let one student continue an exam they handed in by accident.

    Only an exam hand-in that still counts as an attempt can be reopened. The
    student's answers are kept as an editable draft and the single attempt is
    suspended until they hand in again, which locks it once more. The sitting
    window is unchanged: the student can continue only while it is open.
    """
    submission, assignment = _owned_submission(db, submission_id, teacher)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}", status_code=303
    )
    if not str(assignment.exam_config or "").strip():
        flash(request, "אפשר לפתוח מחדש רק הגשה של מבחן.", kind="error")
        return back
    if submission.status == models.SUBMISSION_REOPENED:
        flash(request, "המבחן כבר פתוח מחדש לתלמיד/ה הזו.", kind="info")
        return back
    if not exam_service.attempt_taken(submission):
        flash(
            request,
            "אין הגשה שנקלטה לפתוח מחדש - המבחן טרם הוגש או שהבדיקה נכשלה.",
            kind="error",
        )
        return back
    student = db.get(User, submission.student_id)
    exam_service.reopen(db, assignment, submission)
    db.commit()
    name = (student.display_name or student.username) if student else ""
    flash(
        request,
        f"המבחן נפתח מחדש עבור {name}. התשובות שנשמרו ישוחזרו אצל התלמיד/ה "
        "ואפשר להמשיך ולהגיש שוב, כל עוד חלון המבחן פתוח. ההגשה הקודמת נשמרת "
        "בהיסטוריה, והציון יוחלף רק אחרי הגשה חדשה.",
        kind="success",
    )
    return back


@router.post(
    "/submissions/{submission_id}/regrade",
    dependencies=[Depends(verify_csrf)],
)
def regrade_submission(
    submission_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    submission, _assignment = _owned_submission(db, submission_id, teacher)
    back = RedirectResponse(
        f"/teacher/submissions/{submission.id}", status_code=303
    )
    current = service.current_grade(db, submission)
    if current is not None and current.configured_model == GRADE_SOURCE_CLASSROOM:
        # The engine never produced this grade; regrading would create a second
        # revision with no submission text behind it and corrupt the history.
        flash(
            request,
            "זהו ציון היסטורי שיובא מ-Google Classroom ואין לו קוד לבדיקה מחדש.",
            kind="error",
        )
        return back
    job = service.queue_job(db, submission, force=True)
    db.commit()
    worker = getattr(request.app.state, "grading_worker", None)
    if worker is not None:
        worker.enqueue(job.id)
    flash(
        request,
        "הבדיקה הוכנסה לתור. הציון הקודם יוחלף כשהבדיקה תסתיים.",
        kind="success",
    )
    return RedirectResponse(
        f"/teacher/submissions/{submission.id}", status_code=303
    )


def _recheck_preview(db: Session, assignment: Assignment) -> ExamRecheckPreview | None:
    return db.scalar(
        select(ExamRecheckPreview).where(
            ExamRecheckPreview.assignment_id == assignment.id
        )
    )


def _exam_edit_target(
    db: Session, assignment: Assignment, submission: Submission
) -> tuple[ExamRecheckPreview | None, bool, bool]:
    """Where a manual point edit lands: the open preview row or the live grade.

    Returns ``(preview, edit_preview_row, preview_overtaken)``. A preview row
    only captures the edit while it is still the last word for this hand-in.
    Once the automatic pass has re-marked it, the review page shows the newer
    live grade (see :func:`portal.exams.llm_check.preview_is_stale`), so the
    correction has to go there too - otherwise the teacher would edit a row that
    nobody reads and that every apply path drops. ``preview_overtaken`` lets the
    success message say why the preview was left alone.
    """
    preview = _recheck_preview(db, assignment)
    on_preview = exam_llm_check.row_for(preview, submission.id) is not None
    overtaken = on_preview and exam_llm_check.preview_is_stale(
        db, preview, submission.id
    )
    return preview, on_preview and not overtaken, overtaken


# ------------------------------------------------- automatic check activity


@router.get("/exam-auto-check")
def exam_auto_check_page(
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Read-only activity screen for the automatic exam check pass.

    Shows what the automatic pass did (its run log), which papers still wait and
    why, and which assignments are paused by an open manual re-check preview.
    It never starts a run, applies a grade or calls the model - the pass keeps
    its own schedule and this page only reports on it.
    """
    status = exam_auto_status.build_status(
        db, teacher, request.app.state.portal_settings
    )
    return render(
        request,
        "teacher_auto_check.html",
        user=teacher,
        status=status,
        run_keep=exam_auto_status.RUN_KEEP,
    )


# ------------------------------------------------------- selective model check


def _stored_llm_state(grade: Grade | None) -> str:
    """The model state already recorded on a hand-in, for the selection list."""
    if grade is None:
        return ""
    try:
        result = json.loads(grade.result_json or "{}")
    except (TypeError, ValueError):
        return ""
    exam = result.get("exam") if isinstance(result, dict) else None
    return str(exam.get("llm_state") or "") if isinstance(exam, dict) else ""


def _llm_check_candidates(db: Session, assignment: Assignment) -> list[dict]:
    """Every checked hand-in of this exam, with its current score and state.

    The teacher picks from this list. The stored ``llm_state`` tells them which
    papers the model already judged and which still wait, so a retry can be
    aimed at the ones that need it. ``teacher_reviewed`` marks a hand-in the
    teacher already signed off ("סימון ההגשה כנבדקה"); those are shown as
    approved and kept out of the selectable pool so they are not re-checked.
    """
    submissions = db.scalars(
        select(Submission)
        .where(
            Submission.assignment_id == assignment.id,
            Submission.status.in_((SUBMISSION_GRADED, SUBMISSION_FAILED)),
        )
        .order_by(Submission.submitted_at.desc())
    ).all()
    candidates: list[dict] = []
    for submission in submissions:
        student = db.get(User, submission.student_id)
        name = (student.display_name if student else "") or (
            student.username if student else ""
        ) or f"#{submission.student_id}"
        grade = service.grade_for_revision(db, submission.id, submission.revision)
        state = _stored_llm_state(grade)
        candidates.append(
            {
                "submission_id": submission.id,
                "student_name": name,
                "score": grade.score if grade is not None else None,
                "revision": submission.revision,
                "llm_state": state,
                "state_label": exam_llm_check.STATE_LABELS.get(state, ""),
                "teacher_reviewed": grade is not None
                and grade.reviewed_at is not None,
            }
        )
    candidates.sort(key=lambda item: item["student_name"])
    return candidates


@router.get("/assignments/{assignment_id}/llm-check")
def llm_check_page(
    assignment_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """One screen: pick hand-ins, run the check, apply what passed, retry the rest."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    group = db.get(ClassGroup, assignment.class_id)
    preview = _recheck_preview(db, assignment)
    if not exam_llm_check.owns(preview):
        preview = None
    # A finished manual check whose every row was already applied, signed off by
    # the teacher, or overtaken by a newer live verdict holds nothing the apply
    # could still write. Retiring it returns the screen to the plain picker a
    # class without a manual check shows, instead of leaving a results card and a
    # per-student table that describe work that no longer exists. No grade moves.
    if preview is not None and exam_llm_check.retire_if_exhausted(db, preview):
        flash(
            request,
            "רשימת הבדיקה הישנה הוסתרה: בכל שורותיה כבר הוחל הציון, או ההגשה "
            "סומנה כנבדקה, או הציון החי נבדק מחדש אחריה - אין מה להחיל. "
            "שום ציון לא השתנה.",
            kind="info",
        )
        preview = None
    data = exam_llm_check.payload(preview) if preview is not None else None
    rows = data["rows"] if data else []
    stale_ids = exam_llm_check.stale_submission_ids(db, preview)
    all_candidates = _llm_check_candidates(db, assignment)
    selectable = [c for c in all_candidates if not c["teacher_reviewed"]]
    approved = [c for c in all_candidates if c["teacher_reviewed"]]
    # What this check can still do, by the same rules the apply path uses: rows
    # the teacher already signed off, a paper the automatic pass re-marked, or a
    # hand-in that moved on are not "results waiting to be applied", and a row
    # whose live grade already answers it is not "waiting for quota" either.
    # Counting from the workload keeps the apply button honest about what it
    # will write and keeps the retry list to the papers this screen can still do
    # something about.
    to_write, still_pending = exam_llm_check.preview_workload(db, preview)
    applicable = to_write
    # A deterministic-only result (model off, or no open item answered) is also
    # a finished, applicable mark - the unified screen applies it like reviewed.
    reviewed = [
        row
        for row in applicable
        if row.get("llm_state") == exam_llm_check.REVIEWED
    ]
    auto = [
        row for row in applicable if row.get("llm_state") == exam_llm_check.OFF
    ]
    waiting = still_pending
    changed = [
        row
        for row in applicable
        if row.get("old_score") is not None
        and row.get("new_score") is not None
        and row["old_score"] != row["new_score"]
    ]
    enabled = exam_llm_review.enabled()
    # A hand-in the teacher already signed off ("סימון ההגשה כנבדקה") is shown
    # as approved, not as something to pick again: the model must not overwrite
    # a paper a human already looked at. The sign-off survives every automatic
    # write on the same revision; only a new student revision (a fresh Grade
    # row) returns the hand-in to the pool unapproved.
    return render(
        request,
        "teacher_llm_check.html",
        user=teacher,
        group=group,
        assignment=assignment,
        preview=preview,
        interrupted=exam_llm_check.interrupted(preview),
        rows=rows,
        reviewed=reviewed,
        auto=auto,
        applicable=applicable,
        waiting=waiting,
        changed=changed,
        stale_ids=stale_ids,
        candidates=selectable,
        approved=approved,
        llm_enabled=enabled,
        routing=exam_llm_review.routing_status() if enabled else "",
        state_labels=exam_llm_check.STATE_LABELS,
        changed_questions=sum(int(row.get("changed") or 0) for row in applicable),
        manual_kept=sum(int(row.get("manual_kept") or 0) for row in rows),
    )


@router.get("/assignments/{assignment_id}/llm-check/status")
def llm_check_status(
    assignment_id: int,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Lightweight JSON the running screen polls instead of reloading the page.

    Returns the preview's coarse state plus the live per-question progress of a
    running model check, so the teacher sees movement during the minute or two a
    paper takes. Read-only: it never starts, applies or changes a grade, and it
    is scoped to the teacher's own assignment exactly like the page itself.
    """
    assignment = _owned_assignment(db, assignment_id, teacher)
    preview = _recheck_preview(db, assignment)
    if not exam_llm_check.owns(preview):
        return JSONResponse(
            {"state": "idle", "done": 0, "total": 0, "error": "", "progress": None}
        )
    if exam_llm_check.interrupted(preview):
        # The background thread died (server restart / crash). Report a terminal
        # state so the polling screen reloads into the recovery card instead of
        # spinning on a frozen count forever.
        return JSONResponse(
            {
                "state": "interrupted",
                "done": preview.done,
                "total": preview.total,
                "error": "",
                "progress": None,
            }
        )
    progress = (
        exam_llm_check.progress_for(preview.id)
        if preview.state == exam_llm_check.RUNNING
        else None
    )
    return JSONResponse(
        {
            "state": preview.state,
            "done": preview.done,
            "total": preview.total,
            "error": preview.error or "",
            "progress": progress,
        }
    )


@router.post(
    "/assignments/{assignment_id}/llm-check",
    dependencies=[Depends(verify_csrf)],
)
def start_llm_check(
    assignment_id: int,
    request: Request,
    submission_ids: list[int] = Form(default=[]),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Check the chosen hand-ins (deterministic + model), touching no score."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/assignments/{assignment.id}/llm-check", status_code=303
    )
    try:
        preview = exam_llm_check.start(
            db, assignment, submission_ids, teacher_id=teacher.id
        )
    except exam_llm_check.LLMCheckError as exc:
        flash(request, str(exc), kind="error")
        return back
    exam_llm_check.run_in_thread(request.app.state.session_factory, preview.id)
    if exam_llm_review.enabled():
        message = (
            "הבדיקה רצה ברקע: חלק א' אוטומטי, והחלקים הפתוחים עם המודל ככל "
            "שהמכסה מספיקה. התוצאות יוצגו כאן להחלה — שום ציון לא משתנה עד שתחליטו."
        )
    else:
        message = (
            "הבדיקה האוטומטית רצה ברקע. התוצאות יוצגו כאן להחלה — "
            "שום ציון לא משתנה עד שתחליטו."
        )
    flash(request, message, kind="success")
    return back


@router.post(
    "/assignments/{assignment_id}/llm-check/extend",
    dependencies=[Depends(verify_csrf)],
)
def extend_llm_check(
    assignment_id: int,
    request: Request,
    submission_ids: list[int] = Form(default=[]),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Check more hand-ins after a run, keeping the papers already checked."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/assignments/{assignment.id}/llm-check", status_code=303
    )
    preview = _recheck_preview(db, assignment)
    if not exam_llm_check.owns(preview):
        flash(request, "אין בדיקת מודל פעילה.", kind="error")
        return back
    try:
        preview = exam_llm_check.extend(
            db, assignment, preview, submission_ids, teacher_id=teacher.id
        )
    except exam_llm_check.LLMCheckError as exc:
        flash(request, str(exc), kind="error")
        return back
    exam_llm_check.run_in_thread(request.app.state.session_factory, preview.id)
    flash(
        request,
        "בודק את ההגשות הנוספות. ההגשות שכבר נבדקו נשמרות כאן.",
        kind="success",
    )
    return back


@router.post(
    "/assignments/{assignment_id}/llm-check/retry",
    dependencies=[Depends(verify_csrf)],
)
def retry_llm_check(
    assignment_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Re-run only the papers still waiting or failed; keep the reviewed ones."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/assignments/{assignment.id}/llm-check", status_code=303
    )
    preview = _recheck_preview(db, assignment)
    if not exam_llm_check.owns(preview):
        flash(request, "אין בדיקת מודל פעילה.", kind="error")
        return back
    try:
        preview = exam_llm_check.retry(db, assignment, preview, teacher_id=teacher.id)
    except exam_llm_check.LLMCheckError as exc:
        flash(request, str(exc), kind="error")
        return back
    exam_llm_check.run_in_thread(request.app.state.session_factory, preview.id)
    flash(request, "בודק שוב את ההגשות שחיכו למכסה.", kind="success")
    return back


@router.post(
    "/assignments/{assignment_id}/llm-check/apply",
    dependencies=[Depends(verify_csrf)],
)
def apply_llm_check(
    assignment_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Write the finished papers; the waiting ones stay for another run."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/assignments/{assignment.id}/llm-check", status_code=303
    )
    preview = _recheck_preview(db, assignment)
    if not exam_llm_check.owns(preview):
        flash(request, "אין בדיקת מודל מוכנה להחלה.", kind="error")
        return back
    try:
        applied = exam_llm_check.apply(db, preview)
    except exam_llm_check.LLMCheckError as exc:
        flash(request, str(exc), kind="error")
        return back
    if applied:
        flash(
            request,
            f"הציונים החדשים נשמרו ל־{applied} הגשות. מה שעדיין ממתין למכסה נשאר לבדיקה חוזרת.",
            kind="success",
        )
    else:
        flash(request, "אין ציונים חדשים להחלה כרגע.", kind="info")
    return back


@router.post(
    "/assignments/{assignment_id}/llm-check/cancel",
    dependencies=[Depends(verify_csrf)],
)
def cancel_llm_check(
    assignment_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    assignment = _owned_assignment(db, assignment_id, teacher)
    preview = _recheck_preview(db, assignment)
    if exam_llm_check.owns(preview):
        exam_llm_check.discard(db, preview)
    flash(request, "הבדיקה בוטלה. הציונים לא השתנו.", kind="info")
    return RedirectResponse(
        f"/teacher/classes/{assignment.class_id}", status_code=303
    )


# ------------------------------------------------- grading-style calibration


def _calibration_order(config: exam_config.ExamConfig) -> list[tuple[str, str]]:
    """Question order and titles for the ledger, from the canonical paper.

    Every version shares the same item keys, so one version's canonical order
    covers the whole exam. A missing paper must not break the ledger - the
    caller falls back to the order implied by the corrections themselves.
    """
    try:
        paper = exam_service.canonical_paper(config, config.version_keys[0])
    except Exception:  # noqa: BLE001 - the ledger still works without a paper
        return []
    return [
        (item.key, f"{item.label} · {item.title}".strip(" ·"))
        for item in paper.canonical.items
    ]


@router.get("/assignments/{assignment_id}/calibration")
def calibration_page(
    assignment_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """The teacher's correction ledger by question, with each distilled profile."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    group = db.get(ClassGroup, assignment.class_id)
    config = exam_config.for_assignment(assignment)
    if config is None:
        flash(request, "מטלה זו אינה מבחן פעיל.", kind="error")
        return RedirectResponse(
            f"/teacher/classes/{assignment.class_id}", status_code=303
        )
    order = _calibration_order(config)
    entries = exam_calibration.ledger(db, config.name, order=order or None)
    enabled = exam_llm_review.enabled()
    return render(
        request,
        "teacher_calibration.html",
        user=teacher,
        group=group,
        assignment=assignment,
        config=config,
        ledger=entries,
        llm_enabled=enabled,
        routing=exam_llm_review.routing_status() if enabled else "",
        threshold=exam_calibration.DISTILL_THRESHOLD,
        max_profile_chars=exam_calibration.MAX_PROFILE_CHARS,
        total_corrections=sum(entry["count"] for entry in entries),
    )


@router.post(
    "/assignments/{assignment_id}/calibration/distill",
    dependencies=[Depends(verify_csrf)],
)
def distill_calibration(
    assignment_id: int,
    request: Request,
    item_key: str = Form(...),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Distill one question's corrections into its profile now, in the background."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/assignments/{assignment.id}/calibration", status_code=303
    )
    config = exam_config.for_assignment(assignment)
    if config is None:
        flash(request, "מטלה זו אינה מבחן פעיל.", kind="error")
        return back
    if not exam_llm_review.enabled():
        flash(
            request,
            "בדיקת המודל כבויה, אי אפשר לזקק פרופיל. הפעילו EXAM_LLM_REVIEW בשרת.",
            kind="error",
        )
        return back
    key = (item_key or "").strip()
    if not exam_calibration.all_for_item(db, config.name, key):
        flash(request, "אין עדיין תיקונים לשאלה זו.", kind="error")
        return back
    title = key
    for candidate_key, candidate_title in _calibration_order(config):
        if candidate_key == key:
            title = candidate_title
            break
    started = exam_calibration.distill_in_thread(
        request.app.state.session_factory,
        config.name,
        key,
        title=title,
        force=True,
    )
    if started:
        flash(
            request,
            "מזקק את סגנון הניקוד לשאלה זו ברקע. רעננו בעוד רגע כדי לראות את הפרופיל.",
            kind="success",
        )
    else:
        flash(request, "הזיקוק לשאלה זו כבר רץ ברקע.", kind="info")
    return back


@router.post(
    "/assignments/{assignment_id}/calibration/profile",
    dependencies=[Depends(verify_csrf)],
)
def save_calibration_profile(
    assignment_id: int,
    request: Request,
    item_key: str = Form(...),
    profile: str = Form(default=""),
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    """Save the teacher's own wording for a question's profile and lock it."""
    assignment = _owned_assignment(db, assignment_id, teacher)
    back = RedirectResponse(
        f"/teacher/assignments/{assignment.id}/calibration", status_code=303
    )
    config = exam_config.for_assignment(assignment)
    if config is None:
        flash(request, "מטלה זו אינה מבחן פעיל.", kind="error")
        return back
    key = (item_key or "").strip()
    text = (profile or "").strip()
    if not text:
        flash(request, "יש לכתוב את כללי הניקוד לפני השמירה.", kind="error")
        return back
    exam_calibration.set_profile(db, config.name, key, text, teacher_id=teacher.id)
    db.commit()
    flash(
        request,
        "סגנון הניקוד נשמר וננעל. הזיקוק האוטומטי לא ידרוס את הנוסח שלכם.",
        kind="success",
    )
    return back


# ---------------------------------------------------------------- teacher media


def _media_headers(sha256: str) -> dict[str, str]:
    return {
        "ETag": f'"{sha256}"',
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }


@router.get("/lessons/{lesson_id}/slides/{slide_id}/image")
def teacher_slide_image(
    lesson_id: int,
    slide_id: int,
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    lesson = _owned_lesson(db, lesson_id, teacher)
    slide = _owned_slide(db, lesson, slide_id)
    content = db.get(SlideContent, slide.id)
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
