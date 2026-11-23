"""Portal-owned mixed worksheets (the x.3 "תרגול לפיצוח" sheets).

The Classroom Doc is not parsed at runtime. Each sheet is described once in a
small, validated JSON file next to this package, named after the grading rubric
(``lesson_2_worksheet.json``). The page, the stored answer payload and the text
handed to the grading engine are all derived from that file, so a future sheet
is a content task, not a code change.

Anything unusable (missing file, invalid schema) returns ``None`` and the
assignment page falls back to the previous rubric-summary form. The student
never sees a broken page because of a typo in a worksheet file.
"""
from __future__ import annotations

from portal.worksheets.schema import (
    Item,
    Part,
    TableColumn,
    TableRow,
    Worksheet,
    WorksheetSpecError,
    for_rubric,
)
from portal.worksheets.store import load_values, save_values

__all__ = [
    "Item",
    "Part",
    "TableColumn",
    "TableRow",
    "Worksheet",
    "WorksheetSpecError",
    "for_rubric",
    "load_values",
    "save_values",
]
