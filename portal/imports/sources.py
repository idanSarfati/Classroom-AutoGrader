"""Readers for the two import sources: the school alephon and Classroom CSVs.

Phase 7 deliberately uses the standard library only. The alephon arrives as an
``.xlsx`` file with a handful of shared strings and one worksheet, and the
school network has no package index; a small OOXML reader is safer than adding
``openpyxl`` to the deployment.

The Classroom export is the CSV Google produces from the *Students* tab: three
preamble rows (names, dates, max points) followed by one row per student. Some
columns are duplicates (``X`` and ``X [Auto]``); both are kept, keyed by their
column index, and title matching decides which one a portal assignment uses.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_HEBREW_MONTHS = {
    "ינו": 1,
    "פבר": 2,
    "מרץ": 3,
    "מרס": 3,
    "אפר": 4,
    "מאי": 5,
    "יוני": 6,
    "יולי": 7,
    "אוג": 8,
    "ספט": 9,
    "אוק": 10,
    "נוב": 11,
    "דצמ": 12,
}

_NON_GRADE_VALUES = {
    "",
    "-",
    "—",
    "לא הוגש",
    "לא הוגשה",
    "חסר",
    "n/a",
    "na",
    "none",
    "missing",
    "unturned in",
    "turned in",
    "assigned",
    "returned",
    "reserved",
}

# Google Classroom exports a gradebook in whichever language the school's
# workspace uses. These are the English equivalents of the Hebrew preamble
# markers the readers look for, so a ``Surname/First Name/Email`` sheet parses
# exactly like a ``שם משפחה/שם פרטי`` one.
_ENGLISH_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_DATE_ROW_LABELS = {"תאריך", "date"}
_POINTS_ROW_LABELS = {"נקודות", "points"}


class SourceError(ValueError):
    """A source file cannot be read or does not have the expected shape."""


@dataclass(frozen=True)
class RosterStudent:
    """One row of an alephon (school directory) file."""

    row_number: int
    national_id: str
    display_name: str
    class_label: str
    email: str = ""
    phone: str = ""
    parent_name: str = ""
    parent_phone: str = ""
    parent_email: str = ""
    source_file: str = ""

    @property
    def family_name(self) -> str:
        parts = self.display_name.split()
        return parts[0] if parts else ""

    @property
    def given_name(self) -> str:
        parts = self.display_name.split()
        return parts[1] if len(parts) > 1 else ""


@dataclass(frozen=True)
class Alephon:
    path: Path
    class_label: str
    students: tuple[RosterStudent, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClassroomColumn:
    index: int
    title: str
    due_at: date | None = None
    max_points: float | None = None


@dataclass(frozen=True)
class ClassroomStudent:
    row_number: int
    last_name: str
    first_name: str
    email: str
    scores: dict[int, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return " ".join(
            part for part in (self.last_name.strip(), self.first_name.strip()) if part
        )


@dataclass(frozen=True)
class ClassroomSheet:
    path: Path
    class_label: str
    columns: tuple[ClassroomColumn, ...]
    students: tuple[ClassroomStudent, ...]
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _clean(value: object) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.replace("\u00a0", " ").split()).strip()


def _cell_text(value: object) -> str:
    """Normalise a spreadsheet/CSV value, turning 222270951.0 into a string."""
    text = _clean(value)
    if re.fullmatch(r"-?\d+\.0", text):
        return text[:-2]
    return text


def _maybe_email(value: str) -> tuple[str, str]:
    """Return ``(email, phone)`` for a cell that may hold either."""
    text = _clean(value)
    if "@" in text:
        return text.lower(), ""
    return "", text


def parse_hebrew_date(raw: str) -> date | None:
    """Parse ``14 בספט׳ 2026`` or ``14/09/2026``; unknown values yield None."""
    text = _clean(raw)
    if not text:
        return None
    numeric = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", text)
    if numeric:
        day, month, year = (int(part) for part in numeric.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    month = re.fullmatch(
        r"(\d{1,2})\s+ב?([\u05d0-\u05ea\"'׳״]+)\.?\s+(\d{4})", text
    )
    if month:
        day = int(month.group(1))
        name = re.sub(r"[\"'׳״.]", "", month.group(2))
        number = _HEBREW_MONTHS.get(name[:4]) or _HEBREW_MONTHS.get(name[:3])
        if number:
            try:
                return date(int(month.group(3)), number, day)
            except ValueError:
                return None
    latin = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", text)
    if latin:
        number = _ENGLISH_MONTHS.get(latin.group(2).lower())
        if number:
            try:
                return date(int(latin.group(3)), number, int(latin.group(1)))
            except ValueError:
                return None
    return None


def parse_grade(raw: str) -> float | None:
    text = _clean(raw).replace(",", ".")
    if text.lower() in _NON_GRADE_VALUES:
        return None
    match = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


# ---------------------------------------------------------------------------
# XLSX (alephon)
# ---------------------------------------------------------------------------


def read_xlsx_rows(path: Path) -> list[list[str]]:
    """Read the first worksheet of a simple ``.xlsx`` file as text rows."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SourceError(f"לא ניתן לפתוח את קובץ האקסל: {path.name}") from exc
    with archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(_NS + "si"):
                shared.append(
                    "".join(node.text or "" for node in item.iter(_NS + "t"))
                )
        sheets = sorted(
            name
            for name in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        if not sheets:
            raise SourceError(f"לא נמצא גיליון בקובץ האקסל: {path.name}")
        sheet = ET.fromstring(archive.read(sheets[0]))

    rows: list[list[str]] = []
    for row in sheet.iter(_NS + "row"):
        values: dict[int, str] = {}
        for cell in row.findall(_NS + "c"):
            reference = cell.get("r") or ""
            letters = "".join(ch for ch in reference if ch.isalpha())
            index = _column_index(letters)
            node = cell.find(_NS + "v")
            kind = cell.get("t")
            if kind == "inlineStr":
                inline = cell.find(_NS + "is")
                value = (
                    "".join(
                        item.text or "" for item in inline.iter(_NS + "t")
                    )
                    if inline is not None
                    else ""
                )
            elif node is None:
                value = ""
            elif kind == "s":
                try:
                    value = shared[int(node.text or "0")]
                except (ValueError, IndexError):
                    value = ""
            else:
                value = node.text or ""
            values[index] = _cell_text(value)
        width = max(values) + 1 if values else 0
        rows.append([values.get(i, "") for i in range(width)])
    return rows


def _column_index(letters: str) -> int:
    index = 0
    for letter in letters.upper():
        index = index * 26 + (ord(letter) - 64)
    return index - 1


def _header_index(rows: list[list[str]], *needles: str) -> int | None:
    for index, row in enumerate(rows[:15]):
        joined = " ".join(row)
        if all(needle in joined for needle in needles):
            return index
    return None


def _find_column(header: list[str], *needles: str, exact: bool = False) -> int | None:
    for index, cell in enumerate(header):
        text = _clean(cell)
        if exact and not any(text == needle for needle in needles):
            continue
        if not exact and not any(needle in text for needle in needles):
            continue
        return index
    return None


def parse_alephon(path: Path, class_label: str | None = None) -> Alephon:
    """Parse one school-directory workbook.

    The header row is found by content (``ת.ז`` + ``שם התלמיד``), so exports
    with extra title rows keep working. Rows without a student name are
    ignored; a missing national id is kept and reported, not invented.
    """
    rows = read_xlsx_rows(path)
    header_at = _header_index(rows, "ת.ז", "שם")
    if header_at is None:
        raise SourceError(
            f"לא נמצאה שורת כותרת עם 'ת.ז' ו'שם התלמיד' בקובץ {path.name}"
        )
    header = rows[header_at]
    id_col = _find_column(header, "ת.ז", "תעודת זהות")
    name_col = _find_column(header, "שם התלמיד", "שם תלמיד")
    if name_col is None:
        name_col = _find_column(header, "שם")
    email_col = _find_column(header, "דואר אלקטרוני של התלמיד")
    phone_col = _find_column(header, "סלולרי של התלמיד")
    parent1_name_col = _find_column(header, "שם הורה 1")
    parent1_phone_col = _find_column(header, "סלולארי הורה 1")
    parent1_email_col = _find_column(header, 'דוא"ל הורה 1', 'דוא"ל הורה')
    if parent1_email_col is None:
        parent1_email_col = _find_column(header, "הורה 1")
    parent2_name_col = _find_column(header, "שם הורה 2")
    parent2_phone_col = _find_column(header, "סלולארי הורה 2")
    parent2_email_col = _find_column(header, 'דוא"ל הורה 2')
    class_col = _find_column(header, "שכבה")

    label = _clean(class_label) if class_label else path.parent.name
    students: list[RosterStudent] = []
    warnings: list[str] = []
    for offset, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        def get(column: int | None) -> str:
            return _cell_text(row[column]) if column is not None and column < len(row) else ""

        name = _clean(get(name_col))
        if not name:
            continue
        # The folder/class label (e.g. "ז3") identifies the group; the
        # workbook's ``שכבה`` column only says "ז" and would collapse classes.
        student_class = label or _clean(get(class_col))
        email, phone = _maybe_email(get(email_col))
        if not phone:
            phone = _clean(get(phone_col))
        parent_name = _clean(get(parent1_name_col))
        parent_phone = _clean(get(parent1_phone_col))
        parent_email, parent_email_as_phone = _maybe_email(get(parent1_email_col))
        if parent2_name_col is not None:
            second_name = _clean(get(parent2_name_col))
            second_phone = _clean(get(parent2_phone_col))
            if second_name or second_phone:
                parent_name = " / ".join(
                    part for part in (parent_name, second_name) if part
                )
                parent_phone = " / ".join(
                    part for part in (parent_phone, second_phone) if part
                )
            second_email, _ = _maybe_email(get(parent2_email_col))
            if second_email:
                parent_email = " / ".join(
                    part for part in (parent_email, second_email) if part
                )
        national_id = re.sub(r"\D", "", get(id_col))
        if not national_id:
            warnings.append(f"שורה {offset}: לתלמיד/ה {name} אין ת.ז.")
        students.append(
            RosterStudent(
                row_number=offset,
                national_id=national_id,
                display_name=name,
                class_label=student_class,
                email=email or parent_email_as_phone,
                phone=phone,
                parent_name=parent_name,
                parent_phone=parent_phone,
                parent_email=parent_email,
                source_file=str(path),
            )
        )
    if not students:
        raise SourceError(f"קובץ האלפון {path.name} לא הכיל תלמידים.")
    return Alephon(
        path=path,
        class_label=label,
        students=tuple(students),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Classroom CSV
# ---------------------------------------------------------------------------


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1255", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_csv_rows(path: Path) -> list[list[str]]:
    try:
        text = _decode(path.read_bytes())
    except OSError as exc:
        raise SourceError(f"לא ניתן לקרוא את קובץ הציונים: {path.name}") from exc
    sample = text[:4096]
    delimiter = ","
    if sample.count(";") > sample.count(",") and sample.count(";") > 0:
        delimiter = ";"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [[_clean(cell) for cell in row] for row in reader]


def parse_classroom_csv(path: Path, class_label: str | None = None) -> ClassroomSheet:
    """Parse a Google Classroom *Students* export.

    Supports the usual three-row preamble (names, dates, max points). Files
    without it are treated as a plain roster with 100-point columns.
    """
    rows = _read_csv_rows(path)
    header_at = None
    for needles in (
        ("שם משפחה",),
        ("שם פרטי",),
        ("Surname",),
        ("First Name",),
    ):
        header_at = _header_index(rows, *needles)
        if header_at is not None:
            break
    if header_at is None:
        raise SourceError(
            f"לא נמצאה שורת כותרת עם 'שם משפחה'/'שם פרטי' "
            f"(or 'Surname'/'First Name') בקובץ {path.name}"
        )
    header = rows[header_at]
    if len(header) < 3:
        raise SourceError(f"קובץ הציונים {path.name} חסר עמודות בסיסיות.")

    next_row = rows[header_at + 1] if header_at + 1 < len(rows) else []
    date_row = (
        next_row
        if next_row and _clean(next_row[0]).lower() in _DATE_ROW_LABELS
        else []
    )
    points_index = header_at + (2 if date_row else 1)
    points_row = (
        rows[points_index]
        if points_index < len(rows)
        and rows[points_index]
        and _clean(rows[points_index][0]).lower() in _POINTS_ROW_LABELS
        else []
    )
    roster_start = header_at + 1 + (1 if date_row else 0) + (1 if points_row else 0)

    columns: list[ClassroomColumn] = []
    for index in range(3, len(header)):
        title = _clean(header[index])
        if not title:
            continue
        due = parse_hebrew_date(date_row[index]) if index < len(date_row) else None
        max_points = (
            parse_grade(points_row[index])
            if index < len(points_row)
            else None
        )
        columns.append(
            ClassroomColumn(
                index=index,
                title=title,
                due_at=due,
                max_points=max_points,
            )
        )

    students: list[ClassroomStudent] = []
    warnings: list[str] = []
    for offset, row in enumerate(rows[roster_start:], start=roster_start + 1):
        if not row:
            continue
        last = _clean(row[0]) if row else ""
        first = _clean(row[1]) if len(row) > 1 else ""
        email = _clean(row[2]).lower() if len(row) > 2 else ""
        if not (last or first or email):
            continue
        if not (last or first):
            warnings.append(f"שורה {offset}: רשומת ציונים בלי שם ({email}).")
            continue
        scores = {
            column.index: _clean(row[column.index])
            for column in columns
            if column.index < len(row)
        }
        students.append(
            ClassroomStudent(
                row_number=offset,
                last_name=last,
                first_name=first,
                email=email,
                scores=scores,
            )
        )
    if not students:
        raise SourceError(f"קובץ הציונים {path.name} לא הכיל תלמידים.")
    label = _clean(class_label) if class_label else path.parent.name
    return ClassroomSheet(
        path=path,
        class_label=label,
        columns=tuple(columns),
        students=tuple(students),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Folder discovery
# ---------------------------------------------------------------------------


def class_directories(root: Path, wanted: tuple[str, ...] | None = None) -> dict[str, Path]:
    """Map a normalised class name to its folder under ``root``."""
    from portal.sync.matching import class_key, normalize_class_name

    if not root.is_dir():
        raise SourceError(f"התיקייה לא נמצאה: {root}")
    found: dict[str, Path] = {}
    wanted_keys = {class_key(item) for item in wanted} if wanted else None
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        key = class_key(normalize_class_name(child.name))
        if wanted_keys is not None and key not in wanted_keys:
            continue
        found[key] = child
    return found


def _pick(folder: Path, suffix: str) -> list[Path]:
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() == suffix
    )


def load_class_folder(folder: Path, class_label: str) -> tuple[Alephon, list[ClassroomSheet]]:
    """Read the one alephon workbook and every Classroom CSV in a folder."""
    workbooks = _pick(folder, ".xlsx")
    if not workbooks:
        raise SourceError(f"לא נמצא קובץ אלפון (.xlsx) בתיקיית {class_label}.")
    if len(workbooks) > 1:
        raise SourceError(
            f"נמצאו {len(workbooks)} קבצי אלפון בתיקיית {class_label}; "
            "השאירו קובץ אחד."
        )
    alephon = parse_alephon(workbooks[0], class_label)
    return alephon, load_class_sheets(folder, class_label)


def load_class_sheets(folder: Path, class_label: str) -> list[ClassroomSheet]:
    """Read every Classroom CSV in a class folder (no alephon needed)."""
    csv_files = _pick(folder, ".csv")
    if not csv_files:
        raise SourceError(
            f"לא נמצא קובץ ציונים (.csv) בתיקיית {class_label}."
        )
    return [parse_classroom_csv(path, class_label) for path in csv_files]


__all__ = [
    "Alephon",
    "ClassroomColumn",
    "ClassroomSheet",
    "ClassroomStudent",
    "RosterStudent",
    "SourceError",
    "class_directories",
    "load_class_folder",
    "load_class_sheets",
    "parse_alephon",
    "parse_classroom_csv",
    "parse_grade",
    "parse_hebrew_date",
    "read_xlsx_rows",
]
