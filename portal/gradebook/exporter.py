"""CSV export for the class gradebook.

Excel on Windows is the target (Israeli teachers open these files in Excel),
so the bytes are encoded as **UTF-8 with a BOM** and rows use CRLF. Without
the BOM, Excel guesses the legacy Hebrew codepage and renders every name as
mojibake.

The report is deliberately one row per student and one column per published
assignment: teachers sort, filter and total it in Excel, so a pre-pivoted
"long" format would be less useful. Numeric scores are written as numbers;
ungraded cells carry a Hebrew status instead, which keeps averages honest
(Excel ignores text in ``AVERAGE``).

Spreadsheet formula injection is neutralised: any text cell that starts with
``=``, ``+``, ``-``, ``@``, tab or CR is prefixed with an apostrophe, so a
student calling themselves ``=HYPERLINK(...)`` cannot execute in the
teacher's spreadsheet.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from portal.gradebook.matrix import (
    CELL_FAILED,
    CELL_GRADED,
    CELL_LABELS,
    CELL_MISSING,
    CELL_QUEUED,
    Gradebook,
    StudentRow,
)
from portal.models import ClassGroup
from portal.timeutil import utcnow

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

BASE_HEADERS = ("שם תלמיד", "שם משתמש")
TAIL_HEADERS = (
    "ממוצע",
    "הוגשו",
    "חסרים",
    "ממתינים לבדיקה",
    "נכשלו",
    "באיחור",
    "לסקירה",
    "הערות בדיקה",
)


def _safe_text(value: object) -> str:
    """Return text that Excel will never interpret as a formula."""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _one_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _unique_titles(book: Gradebook) -> list[str]:
    """Assignment columns must stay distinguishable when titles repeat."""
    counts: dict[str, int] = {}
    titles: list[str] = []
    for assignment in book.assignments:
        base = _one_line(assignment.title) or f"מטלה {assignment.id}"
        counts[base] = counts.get(base, 0) + 1
        suffix = "" if counts[base] == 1 else f" ({counts[base]})"
        titles.append(base + suffix)
    return titles


def _cell_value(cell) -> str | int:
    if cell.status == CELL_GRADED and cell.score is not None:
        return int(cell.score)
    if cell.status == CELL_FAILED:
        return CELL_LABELS[CELL_FAILED]
    if cell.status == CELL_QUEUED:
        return CELL_LABELS[CELL_QUEUED]
    return CELL_LABELS[CELL_MISSING]


def _review_notes(book: Gradebook, row: StudentRow) -> str:
    notes = [
        f"{assignment.title}: {_one_line(cell.review_note)}"
        for assignment, cell in zip(book.assignments, row.cells)
        if _one_line(cell.review_note)
    ]
    return " | ".join(notes)


def gradebook_to_csv(book: Gradebook) -> bytes:
    """Render one gradebook snapshot as UTF-8-with-BOM CSV bytes."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    assignment_titles = _unique_titles(book)
    headers = [*BASE_HEADERS, *assignment_titles, *TAIL_HEADERS]
    writer.writerow([_safe_text(item) for item in headers])

    for row in book.rows:
        values: list[str | int] = [
            _safe_text(row.student.display_name),
            _safe_text(row.student.username),
        ]
        values.extend(_cell_value(cell) for cell in row.cells)
        values.extend(
            (
                "" if row.average is None else row.average,
                row.submitted_count,
                row.missing_count,
                row.queued_count,
                row.failed_count,
                row.late_count,
                row.needs_review_count,
                _safe_text(_review_notes(book, row)),
            )
        )
        writer.writerow(values)

    # ``utf-8-sig`` is exactly UTF-8 plus the BOM Excel needs.
    return buffer.getvalue().encode("utf-8-sig")


def export_filename(group: ClassGroup, now: datetime | None = None) -> str:
    """ASCII-only download name (HTTP headers cannot carry Hebrew here)."""
    stamp = (now or utcnow()).strftime("%Y%m%d")
    code = "".join(ch for ch in group.code if ch.isalnum()) or str(group.id)
    return f"gradebook-{code}-{stamp}.csv"


__all__ = [
    "BASE_HEADERS",
    "TAIL_HEADERS",
    "export_filename",
    "gradebook_to_csv",
]
