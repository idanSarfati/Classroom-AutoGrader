"""Teacher corrections that calibrate the exam model, a few at a time.

When the teacher overrides the points the model gave a question - the ``ניקוד``
and ``הערה`` fields on the review paper - the correction is stored here: what the
student answered, what the automatic check awarded, what the teacher decided and
why. The next time the model marks the *same* question, a handful of these recent
corrections travel with the prompt as compact examples, so the model learns the
teacher's calibration instead of repeating the same mistake.

The whole point of this module is the **bound**. A feedback loop that pasted every
note into the system prompt would grow the request without limit and eventually
answer ``429`` from sheer token weight - the exact failure the selective re-check
was built to avoid. So the injected block is hard-capped:

* at most :data:`MAX_EXAMPLES` corrections per question (the most recent);
* each answer and note trimmed to :data:`MAX_ANSWER_CHARS` / :data:`MAX_NOTE_CHARS`;
* the assembled block never exceeds :data:`MAX_BLOCK_CHARS` characters - examples
  are added whole, one at a time, and the loop stops before the cap is crossed.

The cost is therefore fixed and small no matter how many corrections accumulate.
Scoped by exam definition and question key, not by class: one calibration helps
every class that sits the same question.

The DB helpers (:func:`record`, :func:`for_exam`) live here; the pure renderer
(:func:`render_block`) is what the DB-free checker ultimately receives, already
turned into a string by :mod:`portal.exams.service`.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.models import ExamLlmFeedback
from portal.timeutil import utcnow

# The hard ceiling on what one question's calibration may add to a prompt.
MAX_EXAMPLES = 3
MAX_ANSWER_CHARS = 120
MAX_NOTE_CHARS = 120
MAX_BLOCK_CHARS = 600

_WHITESPACE = re.compile(r"\s+")

_HEADER = (
    "הערות המורה מהגשות קודמות בשאלה זו (כיול ניקוד - למד מהן את שיקול הדעת של "
    "המורה, אך בדוק את התשובה הנוכחית לגופה ואל תעתיק ניקוד בלי לבדוק):"
)


def _trim(text: Any, limit: int) -> str:
    """Collapse whitespace and clip to ``limit`` characters, on a word boundary."""
    cleaned = _WHITESPACE.sub(" ", str(text or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    clipped = cleaned[:limit]
    # Prefer not to end mid-word when a space is near the cut.
    space = clipped.rfind(" ")
    if space > limit // 2:
        clipped = clipped[:space]
    return clipped.rstrip() + "…"


def record(
    db: Session,
    *,
    exam_config: str,
    item_key: str,
    answer: str,
    model_points: int,
    teacher_points: int,
    note: str = "",
    maximum: int = 0,
    submission_id: int | None = None,
    created_by: int | None = None,
) -> ExamLlmFeedback | None:
    """Store one teacher correction, or retract it when the teacher now agrees.

    A correction only carries signal when the teacher's number differs from the
    automatic one; an override that lands on the same score teaches nothing, so
    it is a no-op (and removes any earlier correction for this hand-in, in case
    the teacher is taking back a previous adjustment). One row per hand-in per
    question: re-adjusting replaces the stored correction rather than stacking
    duplicates that would crowd out other students' examples.

    Does not commit - the caller owns the transaction.
    """
    name = (exam_config or "").strip()
    key = (item_key or "").strip()
    if not name or not key:
        return None
    model_points = int(model_points or 0)
    teacher_points = int(teacher_points or 0)

    existing = None
    if submission_id is not None:
        existing = db.scalar(
            select(ExamLlmFeedback).where(
                ExamLlmFeedback.exam_config == name,
                ExamLlmFeedback.item_key == key,
                ExamLlmFeedback.submission_id == submission_id,
            )
        )

    if teacher_points == model_points:
        # The teacher agrees with the automatic mark: nothing to teach, and any
        # earlier correction for this hand-in is now retracted.
        if existing is not None:
            db.delete(existing)
        return None

    answer = _trim(answer, MAX_ANSWER_CHARS)
    note = _trim(note, MAX_NOTE_CHARS)
    if existing is not None:
        existing.answer = answer
        existing.model_points = model_points
        existing.teacher_points = teacher_points
        existing.maximum = int(maximum or 0)
        existing.note = note
        existing.created_by = created_by
        existing.created_at = utcnow()
        return existing

    row = ExamLlmFeedback(
        exam_config=name,
        item_key=key,
        answer=answer,
        model_points=model_points,
        teacher_points=teacher_points,
        maximum=int(maximum or 0),
        note=note,
        submission_id=submission_id,
        created_by=created_by,
    )
    db.add(row)
    # The session does not autoflush, so make the new row visible to a later
    # lookup in the same transaction (re-adjusting one hand-in must replace, not
    # stack). This is a flush, not a commit: the caller still owns the transaction.
    db.flush()
    return row


def retract(
    db: Session, *, exam_config: str, item_key: str, submission_id: int | None
) -> None:
    """Drop the stored correction for one hand-in's question (override undone).

    Does not commit - the caller owns the transaction.
    """
    name = (exam_config or "").strip()
    key = (item_key or "").strip()
    if not name or not key or submission_id is None:
        return
    existing = db.scalar(
        select(ExamLlmFeedback).where(
            ExamLlmFeedback.exam_config == name,
            ExamLlmFeedback.item_key == key,
            ExamLlmFeedback.submission_id == submission_id,
        )
    )
    if existing is not None:
        db.delete(existing)


def for_exam(db: Session, exam_config: str) -> dict[str, list[dict[str, Any]]]:
    """Recent corrections for one exam, keyed by question, newest first.

    Each question carries at most :data:`MAX_EXAMPLES` examples. The caller turns
    each list into a bounded prompt block with :func:`render_block`.
    """
    name = (exam_config or "").strip()
    if not name:
        return {}
    rows = db.scalars(
        select(ExamLlmFeedback)
        .where(ExamLlmFeedback.exam_config == name)
        .order_by(ExamLlmFeedback.created_at.desc(), ExamLlmFeedback.id.desc())
    ).all()
    by_item: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        bucket = by_item.setdefault(row.item_key, [])
        if len(bucket) >= MAX_EXAMPLES:
            continue
        bucket.append(
            {
                "answer": row.answer,
                "model_points": row.model_points,
                "teacher_points": row.teacher_points,
                "maximum": row.maximum,
                "note": row.note,
            }
        )
    return by_item


def render_block(examples: list[dict[str, Any]] | None) -> str:
    """One compact Hebrew calibration block, never longer than the hard cap.

    Examples are added whole while the running length stays within
    :data:`MAX_BLOCK_CHARS`; the first that would cross the cap ends the block.
    Returns ``""`` when there is nothing to show, so the prompt is unchanged for
    a question the teacher has never corrected.
    """
    if not examples:
        return ""
    lines = [_HEADER]
    total = len(_HEADER)
    for example in examples[:MAX_EXAMPLES]:
        if not isinstance(example, dict):
            continue
        answer = _trim(example.get("answer"), MAX_ANSWER_CHARS)
        note = _trim(example.get("note"), MAX_NOTE_CHARS)
        teacher = int(example.get("teacher_points") or 0)
        model = int(example.get("model_points") or 0)
        maximum = int(example.get("maximum") or 0)
        line = f'- תשובה: "{answer}" → המורה: {teacher}/{maximum} (אוטומטי: {model}).'
        if note:
            line += f" הערה: {note}"
        if total + len(line) + 1 > MAX_BLOCK_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


__all__ = [
    "MAX_ANSWER_CHARS",
    "MAX_BLOCK_CHARS",
    "MAX_EXAMPLES",
    "MAX_NOTE_CHARS",
    "for_exam",
    "record",
    "render_block",
    "retract",
]