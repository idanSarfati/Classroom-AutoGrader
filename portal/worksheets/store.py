"""Persistence for the structured widget values of a worksheet submission.

Only the *current* revision is stored. The composed Hebrew document remains
the authoritative answer the engine grades and the result page shows; this
payload is a UI convenience so a reload does not blank the student's table
cells and true/false choices.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from sqlalchemy.orm import Session

from portal.models import Submission, WorksheetResponse
from portal.timeutil import utcnow

logger = logging.getLogger(__name__)


def load_values(
    db: Session,
    submission: Submission | None,
    *,
    revision: int | None = None,
) -> dict[str, Any]:
    """The stored payload for a submission revision, or an empty map.

    Defaults to the submission's current revision. An explicit ``revision``
    lets the teacher's review read a preserved earlier hand-in - for example
    the accidental hand-in kept in history after an exam is reopened.
    """
    if submission is None:
        return {}
    wanted = submission.revision if revision is None else revision
    row = db.get(WorksheetResponse, (submission.id, wanted))
    if row is None or not row.payload:
        return {}
    try:
        data = json.loads(row.payload)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring unreadable worksheet payload for submission %s rev %s",
            submission.id,
            wanted,
        )
        return {}
    return data if isinstance(data, dict) else {}


def save_values(
    db: Session,
    submission: Submission,
    values: Mapping[str, Any],
) -> None:
    """Upsert the payload for the submission's current revision."""
    payload = json.dumps(values, ensure_ascii=False)
    row = db.get(WorksheetResponse, (submission.id, submission.revision))
    if row is None:
        row = WorksheetResponse(
            submission_id=submission.id,
            revision=submission.revision,
            payload=payload,
        )
        db.add(row)
    else:
        row.payload = payload
        row.updated_at = utcnow()
    db.flush()


__all__ = ["load_values", "save_values"]
