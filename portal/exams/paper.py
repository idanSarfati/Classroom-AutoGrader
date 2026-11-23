"""Which paper a student sits, and in what order they see it.

Two independent things are randomised per student:

* the **version** (א', ב' or ג') - drawn once, when the exam is first opened,
  and stored in ``portal_exam_allocations``;
* the **order** - the questions inside each scored part, and the options inside
  each multiple-choice question - derived from a stored seed.

Both are stored, never re-drawn. A student who refreshes, reconnects or opens
the exam on another device sees exactly the same paper. Anything else would be
dangerous: a new shuffle would move a typed answer under a different question.

The answerable fields are named after item and part *keys*, which never move.
The shuffled paper is only for display; the document sent to the checker is
always composed from the canonical paper, so the answer key cannot drift.
"""
from __future__ import annotations

import random
import secrets
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.exams.config import ExamConfig, ExamVersion
from portal.models import ExamAllocation
from portal.worksheets.schema import Item, Part, Worksheet, load_by_slug

# A seed is kept inside the signed 64-bit range so it round-trips through
# SQLite and JSON without becoming a float.
_MAX_SEED = (1 << 63) - 1


class ExamPaperError(RuntimeError):
    """The paper for this version is missing or invalid."""


@dataclass(frozen=True)
class Paper:
    """One student's exam: the canonical paper and the shuffled display copy."""

    version: ExamVersion
    canonical: Worksheet
    display: Worksheet
    seed: int
    shuffled: bool

    @property
    def total_points(self) -> int:
        return self.canonical.total_points


# ------------------------------------------------------------------ allocation
def allocation_for(
    db: Session, assignment_id: int, student_id: int
) -> ExamAllocation | None:
    return db.scalar(
        select(ExamAllocation).where(
            ExamAllocation.assignment_id == assignment_id,
            ExamAllocation.student_id == student_id,
        )
    )


def allocate(
    db: Session,
    config: ExamConfig,
    assignment_id: int,
    student_id: int,
    *,
    rng: random.Random | None = None,
) -> ExamAllocation:
    """Return this student's allocation, drawing one only the first time.

    The caller commits. Two requests racing on the first open can both draw;
    the unique constraint decides, and :func:`ensure_allocation` re-reads the
    winner, so the student still ends up with one stable paper.
    """
    existing = allocation_for(db, assignment_id, student_id)
    if existing is not None:
        return existing
    chooser = rng or random.SystemRandom()
    version = chooser.choice(config.versions)
    seed = chooser.randrange(0, _MAX_SEED)
    row = ExamAllocation(
        assignment_id=assignment_id,
        student_id=student_id,
        version=version.key,
        seed=seed,
    )
    db.add(row)
    db.flush()
    return row


def ensure_allocation(
    db: Session, config: ExamConfig, assignment_id: int, student_id: int
) -> ExamAllocation:
    """Allocate, tolerating a concurrent first open by the same student."""
    try:
        return allocate(db, config, assignment_id, student_id)
    except Exception:  # noqa: BLE001 - only a lost unique race is expected
        db.rollback()
        existing = allocation_for(db, assignment_id, student_id)
        if existing is None:
            raise
        return existing


# ----------------------------------------------------------------------- paper
def load_paper(version: ExamVersion) -> Worksheet:
    worksheet = load_by_slug(version.worksheet)
    if worksheet is None:
        raise ExamPaperError(
            f"worksheet {version.worksheet!r} for version {version.key} "
            "is missing or invalid"
        )
    if not version.label:
        raise ExamPaperError(f"version {version.key} has no label")
    return worksheet


def paper_for(
    config: ExamConfig,
    version: ExamVersion,
    seed: int | None = None,
) -> Paper:
    """Build the display paper. ``seed=None`` keeps the canonical order."""
    canonical = load_paper(version)
    if seed is None:
        display = _renumber(canonical, canonical.items)
        return Paper(
            version=version,
            canonical=canonical,
            display=display,
            seed=0,
            shuffled=False,
        )
    ordered = _shuffle_items(config, canonical, seed)
    display = _renumber(canonical, ordered)
    if config.shuffle_options:
        display = _shuffle_options(config, display, seed)
    return Paper(
        version=version,
        canonical=canonical,
        display=display,
        seed=seed,
        shuffled=ordered != canonical.items,
    )


def paper_for_allocation(
    config: ExamConfig, allocation: ExamAllocation
) -> Paper:
    return paper_for(config, config.version(allocation.version), allocation.seed)


# -------------------------------------------------------------------- private
def _rng(seed: int, scope: str) -> random.Random:
    # A named stream per scope: adding a shuffle later cannot reorder an
    # existing stream, so a stored seed keeps giving the same paper for the
    # lifetime of the exam.
    return random.Random(f"{seed}:{scope}")


def _shuffle_items(
    config: ExamConfig, canonical: Worksheet, seed: int
) -> tuple[Item, ...]:
    ordered: list[Item] = []
    for index, (section, items) in enumerate(canonical.sections()):
        group = list(items)
        if config.shuffle_questions:
            _rng(seed, f"items:{section or index}").shuffle(group)
        ordered.extend(group)
    return tuple(ordered)


def _shuffle_options(
    config: ExamConfig, worksheet: Worksheet, seed: int
) -> Worksheet:
    if not config.shuffle_options:
        return worksheet
    items: list[Item] = []
    for item in worksheet.items:
        parts: list[Part] = []
        for part in item.parts:
            if part.kind != "choice":
                parts.append(part)
                continue
            statement_rng = _rng(seed, f"options:{item.key}:{part.key}")
            statements = []
            for statement in part.statements:
                options = list(part.options_for(statement))
                statement_rng.shuffle(options)
                statements.append(replace(statement, options=tuple(options)))
            parts.append(replace(part, statements=tuple(statements)))
        items.append(replace(item, parts=tuple(parts)))
    return replace(worksheet, items=tuple(items))


def _renumber(worksheet: Worksheet, items: tuple[Item, ...]) -> Worksheet:
    """Number the display copy 1..N and label it "שאלה", not "תרגיל"."""
    renumbered = tuple(
        replace(item, number=position, label_override=f"שאלה {position}")
        for position, item in enumerate(items, start=1)
    )
    return replace(worksheet, items=renumbered)


__all__ = [
    "ExamPaperError",
    "Paper",
    "allocate",
    "allocation_for",
    "ensure_allocation",
    "load_paper",
    "paper_for",
    "paper_for_allocation",
]