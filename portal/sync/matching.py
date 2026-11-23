"""Normalize Hebrew class names and match them to Classroom courses.

Google stores the course name with invisible bidi controls and a geresh
(``"‫פייתון א' - ז3‬"``) while the portal class is written ``"ז'3"``. Comparing
the raw strings never works. A *class key* removes bidi controls, apostrophes,
spaces and punctuation, keeping only Hebrew letters and digits:

    "ז'3"                  -> "ז3"
    "פייתון א' - ז3"       -> "פייתוןאז3"
    "פייתון א' - ז3" token -> "ז3"   (matched on the course's own tokens)

The target list is fixed on purpose. ח8 is a course the teacher does not use
in the portal and must never be created by an expansion run.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# The classes this feature initializes. ז3 is the reference/source class; the
# rest receive its content. ח8 is intentionally absent (excluded).
TARGET_CLASSES: tuple[str, ...] = (
    "ז3",
    "ז4",
    "ח3",
    "ח4",
    "ח6",
    "ט2",
    "ט3",
    "ט4",
)
EXCLUDED_CLASSES: tuple[str, ...] = ("ח8",)

# How a newly created portal class is displayed. Existing classes keep their
# current name; only new rows use this map.
TARGET_CLASS_DISPLAY: dict[str, str] = {
    "ז3": "ז'3",
    "ז4": "ז'4",
    "ח3": "ח'3",
    "ח4": "ח'4",
    "ח6": "ח'6",
    "ט2": "ט'2",
    "ט3": "ט'3",
    "ט4": "ט'4",
}

_APOSTROPHES = "'’`׳"
_QUOTES = '"״'
# Course names split on a dash, colon, pipe or slash: "פייתון א' - ז3".
_SEGMENT_SPLIT_RE = re.compile(r"[\-–—:|/\\•·]+")
# A run of Hebrew letters, Latin letters or digits. Used for whitespace names
# such as "פייתון א ז3" where there is no separator.
_TOKEN_RE = re.compile(r"[0-9A-Za-z\u0590-\u05FF]+")


def _strip_controls(value: str) -> str:
    return "".join(
        ch for ch in str(value or "") if unicodedata.category(ch) != "Cf"
    )


def _fold(value: str) -> str:
    text = _strip_controls(value).casefold()
    for char in _APOSTROPHES + _QUOTES:
        text = text.replace(char, "")
    return text


def class_key(value: str) -> str:
    """A comparison key that makes ``"ז'3"`` and ``"ז3"`` identical."""
    return re.sub(r"[^\w\u0590-\u05FF]+", "", _fold(value))


def normalize_class_name(value: str) -> str:
    """Readable name with bidi controls removed and whitespace collapsed."""
    text = _strip_controls(value)
    return re.sub(r"\s+", " ", text).strip()


def _course_keys(course_name: str) -> set[str]:
    keys: set[str] = set()
    whole = class_key(course_name)
    if whole:
        keys.add(whole)
    for segment in _SEGMENT_SPLIT_RE.split(_strip_controls(course_name)):
        key = class_key(segment)
        if key:
            keys.add(key)
    for token in _TOKEN_RE.findall(_strip_controls(course_name)):
        key = class_key(token)
        if key:
            keys.add(key)
    return keys


def find_course_matches(
    courses: Iterable[object], class_name: str
) -> list[object]:
    """Every course whose name carries ``class_name`` as a whole token.

    ``courses`` are objects with a ``name`` attribute (``CourseInfo`` in
    production, simple fakes in tests). Returning a list lets the caller report
    an ambiguous account instead of silently linking the wrong course.
    """
    wanted = class_key(class_name)
    if not wanted:
        return []
    return [
        course
        for course in courses
        if wanted in _course_keys(str(getattr(course, "name", "") or ""))
    ]
