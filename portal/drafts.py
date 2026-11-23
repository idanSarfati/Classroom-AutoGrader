"""In-progress answers, saved before the student presses submit.

A draft is deliberately separate from a submission: it creates no grading job,
no score and no revision. It only holds the fields the student has typed so a
refresh, a closed tab, a lost connection or a different computer does not throw
away a long worksheet.

The payload is a small JSON document::

    {"kind": "worksheet", "values": {...}}   # curated x.3 sheet
    {"kind": "answers",   "values": {...}}   # one entry per rubric question
    {"kind": "code",      "values": {"code": "..."}}

A draft newer than the latest submission wins on the assignment page; an older
one is ignored, and a successful submission deletes it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.models import AnswerDraft
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)

DRAFT_KINDS = frozenset({"worksheet", "answers", "code"})

# The composed submission is capped by the engine service. A draft stores the
# raw fields, so give it a little more room for JSON and headings, but never
# let an accidental paste fill the database.
MAX_DRAFT_BYTES = 300_000


@dataclass(frozen=True)
class Draft:
    kind: str
    values: dict[str, Any]
    updated_at: datetime


def load(db: Session, assignment_id: int, student_id: int) -> Draft | None:
    """The student's saved draft for this assignment, or ``None``.

    An unreadable or unknown payload is treated as no draft instead of
    breaking the assignment page.
    """
    row = db.scalar(
        select(AnswerDraft).where(
            AnswerDraft.assignment_id == assignment_id,
            AnswerDraft.student_id == student_id,
        )
    )
    if row is None or not row.payload:
        return None
    try:
        data = json.loads(row.payload)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring unreadable draft for assignment %s student %s",
            assignment_id,
            student_id,
        )
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "")
    values = data.get("values")
    if kind not in DRAFT_KINDS or not isinstance(values, dict):
        return None
    return Draft(kind=kind, values=values, updated_at=row.updated_at)


def save(
    db: Session,
    assignment_id: int,
    student_id: int,
    *,
    kind: str,
    values: Mapping[str, Any],
) -> Draft:
    """Create or replace the draft; the caller owns the transaction."""
    if kind not in DRAFT_KINDS:
        raise ValueError(f"unknown draft kind {kind!r}")
    payload = serialize(kind, values)
    row = db.scalar(
        select(AnswerDraft).where(
            AnswerDraft.assignment_id == assignment_id,
            AnswerDraft.student_id == student_id,
        )
    )
    if row is None:
        row = AnswerDraft(
            assignment_id=assignment_id,
            student_id=student_id,
            payload=payload,
        )
        db.add(row)
    else:
        row.payload = payload
        row.updated_at = utcnow()
    db.flush()
    return Draft(kind=kind, values=dict(values), updated_at=row.updated_at)


def clear(db: Session, assignment_id: int, student_id: int) -> bool:
    """Delete the draft if it exists; the caller owns the transaction."""
    row = db.scalar(
        select(AnswerDraft).where(
            AnswerDraft.assignment_id == assignment_id,
            AnswerDraft.student_id == student_id,
        )
    )
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def serialize(kind: str, values: Mapping[str, Any]) -> str:
    return json.dumps(
        {"kind": kind, "values": dict(values)}, ensure_ascii=False
    )


def payload_size(kind: str, values: Mapping[str, Any]) -> int:
    return len(serialize(kind, values).encode("utf-8"))


__all__ = [
    "DRAFT_KINDS",
    "Draft",
    "MAX_DRAFT_BYTES",
    "clear",
    "load",
    "payload_size",
    "save",
    "serialize",
]
