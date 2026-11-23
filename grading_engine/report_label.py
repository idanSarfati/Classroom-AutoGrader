"""Bidi-safe, human labels for saved evaluation reports.

``exports/`` names are raw paths like
``‫פייתון א' - ז3/1.2 - הגשת משימות/20261007-215449.json`` - and the course
names carry invisible Unicode bidi controls (RTL embedding U+202B, pop U+202C,
LRM U+200E) that Google Classroom sprinkles in. Rendered as-is in a Streamlit
dropdown they interleave Hebrew and Latin unpredictably, so a teacher sees
garbled text instead of "which class, which assignment, when".

This module turns a report into one clean, structured line::

    [Daily Check] [ז3] 1.2 - הגשת משימות | 07/10/2026 21:54

The fix has two halves, both deliberate:

* **Strip** the stray explicit-direction controls from the source text, then
* **Isolate** each field that may contain Hebrew with Unicode bidi isolates
  (FSI/PDI, U+2068/U+2069) so a right-to-left run can never "grab" the
  surrounding brackets, pipes, or digits.

Streamlit renders in a real browser (which has a bidi engine), so we isolate
rather than reorder - unlike :func:`src.bidi_utils.bidi_print`, which reorders
for a console that has none. Never feed an isolate-wrapped string back into an
API payload, CSV, or log; build those from the original logical fields.

No Streamlit, no network - unit-tested in ``tests/test_report_label.py``.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# First-Strong Isolate / Pop Directional Isolate: wrap a possibly-RTL run so it
# renders as one coherent unit inside an otherwise left-to-right label.
_FSI = "\u2068"
_PDI = "\u2069"

# Bidi control characters Classroom bakes into course/assignment names. Stripped
# from display text (we add our own isolates); the originals are untouched on
# disk. Covers LRM/RLM, the embeddings/overrides, the isolates, and ALM.
_BIDI_CONTROLS = (
    "\u200e"  # LRM
    "\u200f"  # RLM
    "\u061c"  # Arabic letter mark
    "\u202a\u202b\u202c\u202d\u202e"  # LRE/RLE/POP/PDF/LRO
    "\u2066\u2067\u2068\u2069"  # isolates + PDI
    "\ufeff"  # BOM / zero-width no-break space
)

# Where a report came from -> the tag shown in the Release dropdown. Kept short
# and Latin so it never participates in the Hebrew bidi run.
SOURCE_TAGS = {
    "daily_check": "Daily Check",
    "dashboard": "Dry Run",
}


def clean_text(value: Any) -> str:
    """Strip bidi controls and collapse whitespace for display."""
    if value is None:
        return ""
    text = str(value)
    for control in _BIDI_CONTROLS:
        text = text.replace(control, "")
    return " ".join(text.split()).strip()


def isolate(text: str) -> str:
    """Wrap ``text`` in a first-strong isolate so it renders as one unit."""
    text = clean_text(text)
    if not text:
        return ""
    return f"{_FSI}{text}{_PDI}"


def short_class_name(course_name: Any) -> str:
    """Reduce ``‫פייתון א' - ז3`` to its class token ``ז3``.

    Classroom course names are ``<subject> - <class>``; the class is what a
    teacher scans for, so the label shows just that. A name with no `` - ``
    separator (or an empty one) falls back to the whole cleaned string.
    """
    cleaned = clean_text(course_name)
    if not cleaned:
        return ""
    if " - " in cleaned:
        return cleaned.rsplit(" - ", 1)[-1].strip() or cleaned
    return cleaned


def source_tag(source: Any) -> str:
    """Human tag for a report's ``source`` (``'Daily Check'``/``'Dry Run'``/...)."""
    if not source:
        return "Manual"
    key = str(source).strip()
    return SOURCE_TAGS.get(key, key.replace("_", " ").title())


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def format_date(value: Any) -> str:
    """``DD/MM/YYYY HH:MM`` (Israeli day-first order); '' when unparseable."""
    moment = _as_datetime(value)
    if moment is not None:
        return moment.strftime("%d/%m/%Y %H:%M")
    return clean_text(value)


def format_report_label(
    course_name: Any,
    course_work_title: Any,
    created_at: Any,
    source: Any = None,
) -> str:
    """Build the single dropdown line: ``[tag] [class] assignment | date``.

    Each RTL-capable field is isolated so Hebrew never bleeds into the Latin
    brackets, the ``|`` separator, or the numeric date.
    """
    parts = [f"[{source_tag(source)}]"]
    cls = short_class_name(course_name)
    if cls:
        parts.append(f"[{isolate(cls)}]")
    title = clean_text(course_work_title)
    if title:
        # A title's own '|' would read as the date separator, so show it as '-'.
        parts.append(isolate(title.replace(" | ", " - ")))
    when = format_date(created_at)
    if when:
        parts.append(f"| {when}")
    return " ".join(parts)


def label_from_report(report: Any) -> str:
    """Label for an :class:`~src.models.EvaluationReport` instance."""
    return format_report_label(
        course_name=getattr(report, "course_name", None),
        course_work_title=getattr(report, "course_work_title", None),
        created_at=getattr(report, "created_at", None),
        source=getattr(report, "source", None),
    )


def label_from_json(path: Path) -> Optional[str]:
    """Label built straight from a report file; ``None`` when it can't be read.

    Lets the Release tab label every candidate without a full Pydantic parse -
    a file that will later fail validation still gets a best-effort label, and a
    genuinely unreadable one returns ``None`` so the caller can skip or fall back.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return format_report_label(
        course_name=data.get("course_name"),
        course_work_title=data.get("course_work_title"),
        created_at=data.get("created_at"),
        source=data.get("source"),
    )

