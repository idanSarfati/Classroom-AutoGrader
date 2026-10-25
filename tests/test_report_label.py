"""Tests for the Release-tab dropdown labels (no network, no Streamlit).

``src.report_label`` turns a saved ``EvaluationReport`` into one clean,
bidi-safe dropdown line like ``[Daily Check] [ז3] 1.2 - הגשת משימות |
07/10/2026 21:54`` instead of the raw ``exports/`` path whose Hebrew course
names carry invisible Unicode bidi controls that render garbled.

Run from the project root::

    python -m pytest tests/test_report_label.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.report_label import (  # noqa: E402
    clean_text,
    format_date,
    format_report_label,
    isolate,
    label_from_json,
    short_class_name,
    source_tag,
)

# A course name as Classroom returns it: RTL embedding (U+202B...U+202C)
# plus an LRM (U+200E) inside. Shape copied from a real exports/ path.
HEB_Z3 = chr(0x05d6) + chr(0x33)
CLEAN_COURSE = "פייתון א' - ז3"
RLM = chr(0x200f)
RLE = chr(0x202b)
LRM = chr(0x200e)
POP = chr(0x202c)
FSI = chr(0x2068)
PDI = chr(0x2069)
BIDI_COURSE = RLM + RLE + LRM + CLEAN_COURSE + POP
# Stray-control checks after a label: only OUR isolates may appear.
def _assert_no_stray_controls(label):
    for code in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                 0x200E, 0x200F, 0x061C, 0xFEFF):
        assert chr(code) not in label, hex(code)


def test_clean_text_strips_bidi_controls_but_keeps_hebrew():
    assert clean_text(BIDI_COURSE) == CLEAN_COURSE


def test_clean_text_collapses_whitespace_and_handles_none():
    assert clean_text("  1.2  -   הגשת  משימות ") == "1.2 - הגשת משימות"
    assert clean_text(None) == ""
    assert clean_text("") == ""


def test_short_class_name_keeps_only_the_class_token():
    assert short_class_name(CLEAN_COURSE) == "ז3"
    assert short_class_name(BIDI_COURSE) == "ז3"


def test_short_class_name_falls_back_without_separator():
    assert short_class_name("כיתה יחידה") == "כיתה יחידה"
    assert short_class_name("") == ""
    assert short_class_name(None) == ""


def test_source_tag_known_sources():
    assert source_tag("daily_check") == "Daily Check"
    assert source_tag("dashboard") == "Dry Run"


def test_source_tag_manual_and_unknown():
    assert source_tag(None) == "Manual"
def test_full_label_for_daily_check_report():
    label = format_report_label(
        course_name=BIDI_COURSE,
        course_work_title="1.2 - הגשת משימות",
        created_at="2026-10-07T21:54:49+03:00",
        source="daily_check",
    )
    # Structure: [tag] [class] assignment | date (day-first, Israeli order).
    assert label.startswith("[Daily Check] ")
    assert "| 07/10/2026 21:54" in label
    assert "הגשת משימות" in label
    assert "ז3" in label
    # The long subject name must never leak into the label...
    assert "פייתון" not in label
    _assert_no_stray_controls(label)


def test_each_hebrew_field_is_bidi_isolated():
    label = format_report_label(
        course_name=CLEAN_COURSE,
        course_work_title="1.2 - הגשת משימות",
        created_at="2026-10-07T21:54:49",
        source=None,
    )
    # FSI (U+2068) ... PDI (U+2069) around each RTL-capable field, so Hebrew
    # can never "grab" the brackets, the pipe, or the numeric date.
    assert label.count(FSI) >= 2
    assert label.count(PDI) >= 2


def test_label_without_source_defaults_to_manual():
    label = format_report_label(
        course_name="Class A",
        course_work_title="HW 2",
        created_at="2026-10-10T12:00:00",
    )
    assert label.startswith('[Manual] ')
    assert '[Manual] [' + FSI + 'Class A' + PDI + ']' in label
    assert FSI + 'HW 2' + PDI in label
    assert label.endswith('| 10/10/2026 12:00')


def test_title_pipe_is_neutralised():
    # A '|' inside the assignment title would read as the date separator.
    label = format_report_label("C", "2.1 | HW", "2026-10-10T12:00:00")
    assert label.endswith("| 10/10/2026 12:00")
    assert "2.1 - HW" in label


def test_isolate_wraps_and_cleans():
    assert isolate(HEB_Z3) == FSI + HEB_Z3 + PDI
    assert isolate("  ") == ""
    assert isolate(None) == ""  # type: ignore[arg-type]


def test_format_date_day_first_order():
    assert format_date("2026-10-07T21:54:49") == "07/10/2026 21:54"
    assert format_date("2026-10-07T21:54:49Z") == "07/10/2026 21:54"


def test_format_date_falls_back_to_cleaned_text():
    assert format_date("not a date") == "not a date"
    assert format_date(None) == ""


def _write_report_json(path: Path, **overrides) -> Path:
    data = {
        "created_at": "2026-10-07T21:54:49+03:00",
        "model": "qwen",
        "course_id": "c1",
        "course_name": BIDI_COURSE,
        "course_work_id": "w1",
        "course_work_title": "1.2 - הגשת משימות",
        "assignment_config_id": "lesson_1",
        "source": "daily_check",
        "entries": [],
    }
    data.update(overrides)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_label_from_json_reads_metadata(tmp_path):
    path = _write_report_json(tmp_path / "20261007-215449.json")
    label = label_from_json(path)
    assert label is not None
    assert label.startswith("[Daily Check] ")
    assert "07/10/2026 21:54" in label
    assert "הגשת משימות" in label


def test_label_from_json_returns_none_for_unreadable(tmp_path):
    assert label_from_json(tmp_path / "nope.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert label_from_json(broken) is None
    not_dict = tmp_path / "list.json"
    not_dict.write_text("[1, 2]", encoding="utf-8")
    assert label_from_json(not_dict) is None

    assert source_tag("") == "Manual"
    assert source_tag("evaluate_tab") == "Evaluate Tab"
