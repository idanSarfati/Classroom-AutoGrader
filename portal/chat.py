"""Student↔teacher chat: conversations, threads and unread counts.

A conversation is the ``(teacher_id, student_id)`` pair. A student reaches their
teacher(s) through the classes they are enrolled in; a teacher reaches the
students of their own classes. Most messages are general; a message sent from the
bubble inside a task page also carries the assignment and a denormalised context
label, so the teacher sees which task the question is about.

These are DB-aware helpers with no FastAPI dependency, so the routes stay thin and
the logic is testable on its own. ``read_at`` is set when the *recipient* opens a
thread; everything unread is a message from the other side that the viewer has
not opened yet.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
    Message,
    Submission,
    User,
)
from portal.timeutil import format_local, utcnow

MAX_MESSAGE_CHARS = 4000
THREAD_LIMIT = 200
_EPOCH = datetime(1970, 1, 1)


class ChatError(ValueError):
    """A message cannot be sent: empty, too long, or not allowed."""


# ------------------------------------------------------------------ who can talk
def student_teachers(db: Session, student_id: int) -> list[User]:
    """The distinct teachers of the classes this student is enrolled in."""
    return list(
        db.scalars(
            select(User)
            .join(ClassGroup, ClassGroup.teacher_id == User.id)
            .join(Enrollment, Enrollment.class_id == ClassGroup.id)
            .where(Enrollment.student_id == student_id, User.role == ROLE_TEACHER)
            .distinct()
            .order_by(User.display_name)
        ).all()
    )


def teacher_students(db: Session, teacher_id: int) -> list[User]:
    """The distinct students enrolled in any of this teacher's classes."""
    return list(
        db.scalars(
            select(User)
            .join(Enrollment, Enrollment.student_id == User.id)
            .join(ClassGroup, ClassGroup.id == Enrollment.class_id)
            .where(ClassGroup.teacher_id == teacher_id, User.role == ROLE_STUDENT)
            .distinct()
            .order_by(User.display_name)
        ).all()
    )


def can_message(db: Session, student_id: int, teacher_id: int) -> bool:
    """A student may message only a teacher of a class they are enrolled in."""
    count = db.scalar(
        select(func.count())
        .select_from(Enrollment)
        .join(ClassGroup, ClassGroup.id == Enrollment.class_id)
        .where(
            Enrollment.student_id == student_id,
            ClassGroup.teacher_id == teacher_id,
        )
    )
    return bool(count)


def assignment_context(
    db: Session, assignment_id: int, student_id: int
) -> dict[str, Any] | None:
    """The teacher + label for a task bubble, if the student owns that task.

    Returns ``None`` when the assignment does not exist, the student is not
    enrolled in its class, or the student may not see the assignment at all -
    so a forged assignment id can never redirect a message to another teacher
    or leak another class's task title.

    Visibility is the same door as the assignment page
    (:func:`portal.exams.service.visible_to_student`, inlined here to keep this
    module free of exam imports): a published assignment is visible to the
    whole class, while a draft exam with released marks is visible only to a
    student who handed it in. An enrolled student who never sat a hidden exam
    therefore gets no context for it - no title leak, no redirect - while the
    student who did sit it can still ask the teacher about their result.
    """
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        return None
    enrolled = db.scalar(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.class_id == assignment.class_id,
            Enrollment.student_id == student_id,
        )
    )
    if not enrolled:
        return None
    if not assignment.published:
        submission = db.scalar(
            select(Submission).where(
                Submission.assignment_id == assignment.id,
                Submission.student_id == student_id,
            )
        )
        if submission is None or not assignment.exam_results_released:
            return None
    group = db.get(ClassGroup, assignment.class_id)
    if group is None:
        return None
    return {
        "teacher_id": group.teacher_id,
        "assignment_id": assignment.id,
        "label": assignment.title,
    }


# -------------------------------------------------------------------- the thread
def thread(
    db: Session, teacher_id: int, student_id: int, *, limit: int = THREAD_LIMIT
) -> list[Message]:
    """The latest ``limit`` messages of one conversation, oldest first."""
    rows = db.scalars(
        select(Message)
        .where(
            Message.teacher_id == teacher_id,
            Message.student_id == student_id,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def send(
    db: Session,
    *,
    teacher_id: int,
    student_id: int,
    sender_role: str,
    body: str,
    assignment_id: int | None = None,
    context_label: str = "",
) -> Message:
    """Append one message to a conversation. The caller commits."""
    text = (body or "").strip()
    if not text:
        raise ChatError("אי אפשר לשלוח הודעה ריקה.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ChatError(f"ההודעה ארוכה מדי (עד {MAX_MESSAGE_CHARS} תווים).")
    if sender_role not in (ROLE_STUDENT, ROLE_TEACHER):
        raise ChatError("שולח לא חוקי.")
    message = Message(
        teacher_id=teacher_id,
        student_id=student_id,
        assignment_id=assignment_id,
        context_label=(context_label or "").strip()[:200],
        body=text,
        sender_role=sender_role,
    )
    db.add(message)
    db.flush()
    return message


def _other_role(reader_role: str) -> str:
    return ROLE_TEACHER if reader_role == ROLE_STUDENT else ROLE_STUDENT


def mark_read(
    db: Session, teacher_id: int, student_id: int, *, reader_role: str
) -> int:
    """Mark the other side's unread messages as read; return how many."""
    messages = db.scalars(
        select(Message).where(
            Message.teacher_id == teacher_id,
            Message.student_id == student_id,
            Message.sender_role == _other_role(reader_role),
            Message.read_at.is_(None),
        )
    ).all()
    now = utcnow()
    for message in messages:
        message.read_at = now
    return len(messages)


def unread_count(
    db: Session, teacher_id: int, student_id: int, *, reader_role: str
) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.teacher_id == teacher_id,
                Message.student_id == student_id,
                Message.sender_role == _other_role(reader_role),
                Message.read_at.is_(None),
            )
        )
        or 0
    )


def total_unread(db: Session, user_id: int, role: str) -> int:
    """Every unread message addressed to this user, for the navigation badge."""
    column = Message.student_id if role == ROLE_STUDENT else Message.teacher_id
    return int(
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                column == user_id,
                Message.sender_role == _other_role(role),
                Message.read_at.is_(None),
            )
        )
        or 0
    )


# --------------------------------------------------------------- conversation lists
def _last_message(db: Session, teacher_id: int, student_id: int) -> Message | None:
    return db.scalar(
        select(Message)
        .where(
            Message.teacher_id == teacher_id,
            Message.student_id == student_id,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )


def _summary(
    db: Session, teacher_id: int, student_id: int, name: str, reader_role: str
) -> dict[str, Any]:
    last = _last_message(db, teacher_id, student_id)
    return {
        "teacher_id": teacher_id,
        "student_id": student_id,
        "name": name,
        "unread": unread_count(
            db, teacher_id, student_id, reader_role=reader_role
        ),
        "last_body": (last.body if last else "")[:160],
        "last_at": last.created_at if last else None,
        "last_sender": last.sender_role if last else "",
        "has_messages": last is not None,
    }


def _sort_key(summary: dict[str, Any]) -> tuple:
    stamp = summary["last_at"] or _EPOCH
    # Seconds since the epoch via a timedelta: datetime.timestamp() can raise
    # OSError on Windows for instants at/before the local-time epoch.
    seconds = (stamp - _EPOCH).total_seconds()
    return (0 if summary["has_messages"] else 1, -seconds, summary["name"])


def conversations_for_student(db: Session, student_id: int) -> list[dict[str, Any]]:
    """One row per teacher the student can talk to, most recent activity first."""
    rows = [
        _summary(
            db,
            teacher.id,
            student_id,
            teacher.display_name or teacher.username,
            ROLE_STUDENT,
        )
        for teacher in student_teachers(db, student_id)
    ]
    rows.sort(key=_sort_key)
    return rows


def conversations_for_teacher(db: Session, teacher_id: int) -> list[dict[str, Any]]:
    """The teacher's inbox: students who have an active conversation."""
    student_ids = db.scalars(
        select(Message.student_id)
        .where(Message.teacher_id == teacher_id)
        .distinct()
    ).all()
    rows = []
    for student_id in student_ids:
        student = db.get(User, student_id)
        if student is None:
            continue
        rows.append(
            _summary(
                db,
                teacher_id,
                student_id,
                student.display_name or student.username,
                ROLE_TEACHER,
            )
        )
    rows.sort(key=_sort_key)
    return rows


# -------------------------------------------------------------------- projection
def message_view(
    message: Message, *, viewer_role: str, timezone_name: str = "Asia/Jerusalem"
) -> dict[str, Any]:
    """A template/JSON-safe view of one message (no internal ids beyond the row).

    ``created_at`` is the naive-UTC ISO instant the browser turns into local
    time for live updates; ``created_local`` is the same instant already
    formatted in the school's zone for the first server render.
    """
    return {
        "id": message.id,
        "body": message.body,
        "sender_role": message.sender_role,
        "mine": message.sender_role == viewer_role,
        "context_label": message.context_label,
        "assignment_id": message.assignment_id,
        "created_at": (
            message.created_at.isoformat(timespec="seconds")
            if message.created_at
            else ""
        ),
        "created_local": format_local(message.created_at, timezone_name),
    }


__all__ = [
    "MAX_MESSAGE_CHARS",
    "THREAD_LIMIT",
    "ChatError",
    "assignment_context",
    "can_message",
    "conversations_for_student",
    "conversations_for_teacher",
    "mark_read",
    "message_view",
    "send",
    "student_teachers",
    "teacher_students",
    "thread",
    "total_unread",
    "unread_count",
]