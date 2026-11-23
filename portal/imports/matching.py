"""Match alephon (Hebrew) names to the names Google Classroom exported.

The two sources rarely agree on spelling. The alephon is Hebrew and ordered
"family given"; the Classroom export is a mix of Hebrew, transliterated Latin,
Cyrillic and plain Google handles (``Mia``, ``angelspam``). Automatic matching
must therefore be **conservative**: only a match the teacher can trust is
pre-confirmed, everything else is written to a review file that the teacher
corrects before grades are imported.

The review file is plain UTF-8 CSV so it opens in Excel. Its header is stable
and the grade importer validates it; see :func:`write_mapping`.
"""
from __future__ import annotations

import csv
import difflib
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from portal.imports.sources import ClassroomSheet, ClassroomStudent, RosterStudent

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"

CONFIDENCE_LABELS = {
    CONFIDENCE_HIGH: "ודאות גבוהה",
    CONFIDENCE_MEDIUM: "לבדיקה",
    CONFIDENCE_LOW: "התאמה חלשה",
    CONFIDENCE_NONE: "לא נמצא",
}

MAPPING_HEADERS = (
    "class",
    "csv_file",
    "csv_row",
    "classroom_name",
    "classroom_email",
    "student_id",
    "student_name",
    "confidence",
    "confirmed",
    "note",
)

_APOSTROPHES = "'`׳’"
_QUOTES = '"״“”'
_HEBREW_RANGE = "\u0590-\u05ff"

# Letters that carry a sound; the transliteration is deliberately simple: it
# only has to be compared with another *transliteration*, never displayed.
_HEBREW_TO_LATIN = {
    "א": "a",
    "ב": "b",
    "ג": "g",
    "ד": "d",
    "ה": "h",
    "ו": "v",
    "ז": "z",
    "ח": "h",
    "ט": "t",
    "י": "y",
    "כ": "k",
    "ך": "k",
    "ל": "l",
    "מ": "m",
    "ם": "m",
    "נ": "n",
    "ן": "n",
    "ס": "s",
    "ע": "a",
    "פ": "p",
    "ף": "f",
    "צ": "ts",
    "ץ": "ts",
    "ק": "k",
    "ר": "r",
    "ש": "sh",
    "ת": "t",
}

_CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sh",
    "ъ": "",
    "ы": "i",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

_VOWELS = set("aeiou")
# Consonants that sound alike in Hebrew/English/Cyrillic transliteration.
_COARSE_CLASS = {
    "b": "b",
    "v": "b",
    "p": "b",
    "f": "b",
    "k": "k",
    "q": "k",
    "c": "k",
    "g": "k",
    "s": "s",
    "z": "s",
    "t": "t",
    "d": "t",
    "h": "h",
    "l": "l",
    "m": "m",
    "n": "n",
    "r": "r",
    "y": "y",
    "j": "g",
    "w": "b",
}
_COARSE_DIGRAPHS = (
    ("tch", "s"),
    ("shch", "s"),
    ("sh", "s"),
    ("ts", "s"),
    ("ch", "h"),
    ("kh", "h"),
    ("zh", "s"),
    ("ph", "b"),
    ("th", "t"),
)


@dataclass(frozen=True)
class MappingRow:
    """One Classroom roster line and the alephon student it belongs to.

    ``student_id`` is the national id (the portal's stable key). An empty
    value means "skip this Classroom line"; ``confirmed`` says whether the
    grade importer may use the row without asking again.
    """

    class_label: str
    csv_file: str
    csv_row: int
    classroom_name: str
    classroom_email: str
    student_id: str = ""
    student_name: str = ""
    confidence: str = CONFIDENCE_NONE
    confirmed: bool = False
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.student_id) and self.confirmed


@dataclass(frozen=True)
class SheetMatch:
    """All mapping rows produced for one Classroom sheet."""

    sheet: ClassroomSheet
    rows: tuple[MappingRow, ...]

    @property
    def matched(self) -> int:
        return sum(1 for row in self.rows if row.usable)

    @property
    def ambiguous(self) -> int:
        return sum(1 for row in self.rows if row.confidence != CONFIDENCE_NONE)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------


def _strip_marks(text: str) -> str:
    """Remove bidi controls, niqqud and cantillation marks."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    cleaned = "".join(
        char
        for char in decomposed
        if unicodedata.category(char) != "Cf"
        and not unicodedata.combining(char)
    )
    return unicodedata.normalize("NFC", cleaned)


def name_tokens(raw: str) -> list[str]:
    """Comparison tokens without apostrophes, punctuation or duplicated space."""
    text = _strip_marks(raw).casefold()
    for char in _APOSTROPHES + _QUOTES:
        text = text.replace(char, "")
    text = re.sub(r"[^\w\u0590-\u05ff]+", " ", text)
    return [token for token in text.split() if token]


def _latinize(token: str) -> str:
    """Transliterate Hebrew/Cyrillic tokens, leave Latin text untouched."""
    output: list[str] = []
    for char in token:
        if char in _HEBREW_TO_LATIN:
            output.append(_HEBREW_TO_LATIN[char])
        elif char in _CYRILLIC_TO_LATIN:
            output.append(_CYRILLIC_TO_LATIN[char])
        else:
            output.append(char)
    return "".join(output)


def _latin_key(token: str) -> str:
    text = _latinize(token).casefold()
    text = re.sub(r"[^a-z]+", "", text)
    if not text:
        return ""
    for old, new in (
        ("shch", "sh"),
        ("tch", "ts"),
        ("ph", "f"),
        ("ck", "k"),
        ("tz", "ts"),
        ("x", "ks"),
        ("q", "k"),
        ("w", "v"),
        ("j", "g"),
        ("y", "i"),
    ):
        text = text.replace(old, new)
    # ``c`` is /k/ before a/o/u and /s/ before e/i; a single class keeps the
    # comparison symmetric for "Cohen" and "Kohn".
    text = re.sub(r"c(?=[eiy])", "s", text)
    text = text.replace("c", "k")
    text = re.sub(r"(.)\1+", r"\1", text)
    return text


def _skeleton(token: str) -> str:
    return "".join(char for char in _latin_key(token) if char not in _VOWELS)


def _coarse(token: str) -> str:
    text = _latin_key(token)
    for old, new in _COARSE_DIGRAPHS:
        text = text.replace(old, new)
    text = re.sub(r"c(?=[eiy])", "s", text)
    text = text.replace("c", "k")
    text = re.sub(r"(.)\1+", r"\1", text)
    return "".join(
        _COARSE_CLASS.get(char, char)
        for char in text
        if char not in _VOWELS
    )


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    score = difflib.SequenceMatcher(None, left, right).ratio()
    short, long = sorted((left, right), key=len)
    if len(short) >= 3 and short in long:
        score = max(score, 0.9 - 0.04 * (len(long) - len(short)))
    return score


def token_similarity(left: str, right: str) -> float:
    """How alike two name tokens are, tolerant of vowels and sound-alikes."""
    if left == right:
        return 1.0
    full = _ratio(_latin_key(left), _latin_key(right))
    skeleton = _ratio(_skeleton(left), _skeleton(right)) * 0.93
    coarse = _ratio(_coarse(left), _coarse(right)) * 0.84
    return max(full, skeleton, coarse)


def name_similarity(left: str, right: str) -> tuple[float, bool]:
    """Return ``(score, complete)`` for two full names.

    ``complete`` is false when either side has a single token (a Classroom
    handle such as ``Mia``). Such rows can never be confirmed automatically:
    there is not enough information to tell two students apart.
    """
    a = name_tokens(left)
    b = name_tokens(right)
    if not a or not b:
        return 0.0, False
    # Two one-letter tokens ("R А") carry no identity information at all.
    complete = (
        len(a) >= 2
        and len(b) >= 2
        and all(len(token) >= 2 for token in a + b)
    )
    if complete and set(a) == set(b):
        return 1.0, True

    if len(a) == 1 or len(b) == 1:
        score = max(token_similarity(token, other) for token in a for other in b)
        return score, False

    # Small names: try both alignments; for longer names pair greedily.
    if len(a) <= 3 and len(b) <= 3:
        from itertools import permutations

        best = 0.0
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        for order in permutations(range(len(longer)), len(shorter)):
            total = sum(
                token_similarity(shorter[index], longer[position])
                for index, position in enumerate(order)
            )
            best = max(best, total / len(shorter))
        return best, complete

    used: set[int] = set()
    total = 0.0
    for token in a:
        choices = [
            (token_similarity(token, other), index)
            for index, other in enumerate(b)
            if index not in used
        ]
        best_score, best_index = max(choices, default=(0.0, -1))
        if best_index >= 0:
            used.add(best_index)
            total += best_score
    return total / len(a), complete


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _confidence(score: float, complete: bool, email_match: bool) -> str:
    """Only a complete-name match can be confirmed without a human.

    Shared family e-mails are common, and Google handles (``Olga Olga``,
    ``pearl tom``) prove nothing on their own; an e-mail match may raise a
    candidate but never auto-confirms one.
    """
    if complete and score >= 0.80:
        return CONFIDENCE_HIGH
    if complete and score >= 0.64:
        return CONFIDENCE_MEDIUM
    if email_match:
        return CONFIDENCE_MEDIUM
    if score >= 0.55:
        return CONFIDENCE_MEDIUM
    if score >= 0.45:
        return CONFIDENCE_LOW
    return CONFIDENCE_NONE


def _explain(score: float, complete: bool, email_match: bool) -> str:
    parts: list[str] = []
    if score >= 0.9 and complete:
        parts.append("שם מלא תואם")
    elif score >= 0.68:
        parts.append(f"דמיון שם {score:.0%}")
    elif score > 0:
        parts.append(f"דמיון חלש {score:.0%}")
    if email_match:
        parts.append("אימייל תואם")
    if not complete:
        parts.append("רק חלק מהשם הופיע ב-Classroom")
    return " · ".join(parts)


def _best_candidates(
    classroom: ClassroomStudent, roster: tuple[RosterStudent, ...]
) -> list[tuple[float, bool, bool, RosterStudent]]:
    email = classroom.email.casefold()
    scored: list[tuple[float, bool, bool, RosterStudent]] = []
    for student in roster:
        score, complete = name_similarity(classroom.display_name, student.display_name)
        email_match = bool(
            email and student.email and email == student.email.casefold()
        )
        if complete and email_match and score >= 0.78:
            score = max(score, 0.92)
        elif email_match:
            # A shared family inbox is a hint, not proof: keep it below the
            # automatic-confirmation threshold.
            score = max(score, 0.7)
        scored.append((score, complete, email_match, student))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def match_sheet(
    roster: tuple[RosterStudent, ...], sheet: ClassroomSheet
) -> SheetMatch:
    """Match every Classroom row to at most one alephon student.

    Rows for the same student are reduced to one: a second Classroom line with
    the same e-mail/name is a duplicate export row, and importing both would
    count the same grade twice. The winner is the row with the higher score;
    the others stay in the review file as ``confirmed=no``.
    """
    rows: list[MappingRow] = []
    for classroom in sheet.students:
        candidates = _best_candidates(classroom, roster)
        best = candidates[0] if candidates else None
        runner_up = candidates[1] if len(candidates) > 1 else None
        if best is None or best[0] < 0.45:
            rows.append(
                MappingRow(
                    class_label=sheet.class_label,
                    csv_file=sheet.path.name,
                    csv_row=classroom.row_number,
                    classroom_name=classroom.display_name,
                    classroom_email=classroom.email,
                    note="לא נמצא תלמיד מתאים באלפון",
                )
            )
            continue
        score, complete, email_match, student = best
        confidence = _confidence(score, complete, email_match)
        confirmed = confidence == CONFIDENCE_HIGH and bool(student.national_id)
        note = _explain(score, complete, email_match)
        if not student.national_id:
            note += " · חסרה ת.ז באלפון"
            confirmed = False
        if runner_up is not None and runner_up[0] >= 0.62:
            note += f" · גם {runner_up[3].display_name} דומה ({runner_up[0]:.0%})"
        rows.append(
            MappingRow(
                class_label=sheet.class_label,
                csv_file=sheet.path.name,
                csv_row=classroom.row_number,
                classroom_name=classroom.display_name,
                classroom_email=classroom.email,
                student_id=student.national_id,
                student_name=student.display_name,
                confidence=confidence,
                confirmed=confirmed,
                note=note,
            )
        )

    return SheetMatch(sheet=sheet, rows=tuple(_dedupe_rows(rows)))


def _dedupe_rows(rows: list[MappingRow]) -> list[MappingRow]:
    """Keep one confirmed row per student per sheet."""
    groups: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row.student_id:
            groups.setdefault(row.student_id, []).append(index)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        winner = max(
            indexes,
            key=lambda index: (rows[index].confirmed, _confidence_rank(rows[index])),
        )
        for index in indexes:
            if index == winner:
                continue
            rows[index] = replace(
                rows[index],
                confirmed=False,
                note=(rows[index].note + " · שורת כפילות").strip(" ·"),
            )
    return rows


def _confidence_rank(row: MappingRow) -> int:
    order = {
        CONFIDENCE_HIGH: 3,
        CONFIDENCE_MEDIUM: 2,
        CONFIDENCE_LOW: 1,
        CONFIDENCE_NONE: 0,
    }
    return order.get(row.confidence, 0)


def assign_class_label(class_label: str, rows: list[MappingRow]) -> list[MappingRow]:
    """Rewrite the class label on rows (the portal name may include an apostrophe)."""
    return [replace(row, class_label=class_label) for row in rows]


# ---------------------------------------------------------------------------
# Mapping file
# ---------------------------------------------------------------------------


def write_mapping(path: Path, rows: list[MappingRow]) -> int:
    """Write the teacher review file, replacing any previous version.

    The file is UTF-8 with a BOM so Excel opens Hebrew correctly. The teacher
    edits ``student_id`` / ``confirmed`` and saves; :func:`read_mapping`
    validates the result before any grade is written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(MAPPING_HEADERS)
        for row in rows:
            writer.writerow(
                [
                    row.class_label,
                    row.csv_file,
                    row.csv_row,
                    row.classroom_name,
                    row.classroom_email,
                    row.student_id,
                    row.student_name,
                    row.confidence,
                    "yes" if row.confirmed else "no",
                    row.note,
                ]
            )
    return len(rows)


_TRUE_VALUES = {"yes", "y", "true", "1", "כן", "מאושר"}
_FALSE_VALUES = {"", "no", "n", "false", "0", "לא"}


def read_mapping(path: Path) -> list[MappingRow]:
    """Read a reviewed mapping file.

    Raises :class:`ValueError` with a Hebrew message naming the first bad row,
    so the CLI can stop before touching the database.
    """
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ValueError(f"לא ניתן לקרוא את קובץ ההתאמה: {path}") from exc
    with handle:
        reader = csv.DictReader(handle)
        missing = [name for name in MAPPING_HEADERS if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                "קובץ ההתאמה חסר עמודות: " + ", ".join(missing)
            )
        rows: list[MappingRow] = []
        for line_number, raw in enumerate(reader, start=2):
            if not any((value or "").strip() for value in raw.values()):
                continue
            confirmed_raw = (raw.get("confirmed") or "").strip().casefold()
            if confirmed_raw in _TRUE_VALUES:
                confirmed = True
            elif confirmed_raw in _FALSE_VALUES:
                confirmed = False
            else:
                raise ValueError(
                    f"שורה {line_number}: ערך confirmed לא מוכר "
                    f"'{raw.get('confirmed')}' (השתמשו ב-yes או no)."
                )
            try:
                csv_row = int((raw.get("csv_row") or "0").strip() or 0)
            except ValueError as exc:
                raise ValueError(
                    f"שורה {line_number}: csv_row חייב להיות מספר."
                ) from exc
            student_id = re.sub(r"\D", "", (raw.get("student_id") or ""))
            rows.append(
                MappingRow(
                    class_label=(raw.get("class") or "").strip(),
                    csv_file=(raw.get("csv_file") or "").strip(),
                    csv_row=csv_row,
                    classroom_name=(raw.get("classroom_name") or "").strip(),
                    classroom_email=(raw.get("classroom_email") or "").strip().casefold(),
                    student_id=student_id,
                    student_name=(raw.get("student_name") or "").strip(),
                    confidence=(raw.get("confidence") or CONFIDENCE_NONE).strip(),
                    confirmed=confirmed,
                    note=(raw.get("note") or "").strip(),
                )
            )
    return rows


__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LABELS",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "CONFIDENCE_NONE",
    "MAPPING_HEADERS",
    "MappingRow",
    "SheetMatch",
    "assign_class_label",
    "match_sheet",
    "name_similarity",
    "name_tokens",
    "read_mapping",
    "token_similarity",
    "write_mapping",
]
