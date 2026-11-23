"""Apply the teacher's manual name-mapping corrections.

The teacher reviewed the generated report and supplied the real Classroom
identity for a number of students.  This script treats that review as the
source of truth:

* rows the teacher identified are assigned to the correct national id and
  confirmed;
* rows that the matcher attached to the wrong student are detached, so they
  can never leak another family member's grades into a profile;
* every remaining student whose status was ``לבדיקה`` or ``התאמה חלשה`` is
  confirmed, because the teacher explicitly said that an unmentioned suspect
  is correct.

The mapping file is rewritten in the stable UTF-8-BOM CSV format used by the
grade importer.  Nothing here touches the database; run ``import_grades.py``
afterwards to refresh the historical grades.

Usage::

    python -m portal.scripts.apply_mapping_corrections --dry-run
    python -m portal.scripts.apply_mapping_corrections --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from portal.imports.matching import (
    CONFIDENCE_HIGH,
    CONFIDENCE_NONE,
    MappingRow,
    read_mapping,
    write_mapping,
)
from portal.imports.sources import class_directories, load_class_folder
from portal.paths import DATA_DIR
from portal.sync.matching import class_key

DEFAULT_ROOT = Path(r"C:/Users/Idan Sarfati/Desktop/אלפונים")
DEFAULT_MAPPING = DATA_DIR / "student_mapping.csv"
DEFAULT_REPORT = DATA_DIR / "student_mapping_report.json"

MANUAL_MARK = "אושר ידנית על ידי המורה"
REMOVED_MARK = "הוסר ידנית על ידי המורה"

# (class, classroom e-mail) -> national id of the student who really owns it.
# E-mail + class is the only safe key: the same address can appear in more than
# one class export (for example the shared לוי מוריה line in ז3 and ז4).
ASSIGN: dict[tuple[str, str], str] = {
    # ז3
    ("ז3", "miarool260114@gmail.com"): "338672520",       # Mia -> טולצינסקי מיה
    ("ז3", "noaeliyahu23@gmail.com"): "338968464",        # Noa Eliyahu -> אליהו נועה
    ("ז3", "danayarom111@gmail.com"): "338671472",        # Yarom Dana -> ירום דניאלה
    ("ז3", "elacohen450@gmail.com"): "221893209",         # Cohen Ela -> כהן אלה
    ("ז3", "kuzmenko.ks90@gmail.com"): "222662470",       # mother
    ("ז3", "m33431082@gmail.com"): "222662470",           # Michael
    ("ז3", "michaelkuzmenko2314@gmail.com"): "222662470", # Michael
    ("ז3", "stanislav1283@gmail.com"): "222662470",       # father
    ("ז3", "ronenelior2014@gmail.com"): "330047457",      # Ronen Elior
    ("ז3", "irishashifer@gmail.com"): "221545429",        # mother
    ("ז3", "nikolsh21@gmail.com"): "221545429",           # Nikol
    ("ז3", "kairapoport2711@gmail.com"): "223441650",     # Kai Rapoport (belongs to ז3)
    # ז4
    ("ז4", "zoyemoscovich@gmail.com"): "339022477",       # Zoey Moscovich
    ("ז4", "noam0320142@gmail.com"): "338245467",         # Noam6231d
    ("ז4", "romasayag4@gmail.com"): "338719289",          # father
    ("ז4", "rom.asayag@gmail.com"): "338719289",          # father
    ("ז4", "nevo09co@gmail.com"): "339029472",            # כהן נבו
    ("ז4", "amarroyal888@gmail.com"): "340344787",        # עמר רויאל
    ("ז4", "ronenamit90@gmail.com"): "338670243",         # Ronen0801 Amit
    # ח3
    ("ח3", "hilaomid767@gmail.com"): "338732357",         # Omid Hila
    # ח4
    ("ח4", "olga6984@gmail.com"): "222214702",            # first account
    ("ח4", "rozalin.yuhanov@gmail.com"): "222214702",     # second account
    # ח6
    ("ח6", "midukoflom@gmail.com"): "1556833002",         # Koflom Midu -> אייץ
    ("ח6", "yoelkuflom@gmail.com"): "1556832996",         # Kuflom Yoel -> יואל
    # ט2
    ("ט2", "andorafi306@gmail.com"): "1559178660",        # rafi ando -> גיאדו רפאל
    ("ט2", "ateretabergel@gmail.com"): "336006986",       # אברגל עטרת
    ("ט2", "emilgalperin5191@gmail.com"): "220004147",    # Galperin Emil
    ("ט2", "phallel2003@gmail.com"): "337296222",         # פרץ הלל
    # ט3
    ("ט3", "yuvalm2805@gmail.com"): "335903811",          # yuvalmr -> מרדכייב יובל
    ("ט3", "orielbaz07@gmail.com"): "221336829",          # אלבז אורי
    ("ט3", "eitan.elkaya@gmail.com"): "335435889",        # אלקיים איתן
    ("ט3", "omalka160785@gmail.com"): "337334163",        # first account
    ("ט3", "annmalka456@gmail.com"): "337334163",         # second account
    # ט4
    ("ט4", "kvolfzon@gmail.com"): "221369242",            # Mogger -> וולפזון
    ("ט4", "migirovstav@gmail.com"): "336648514",         # Мигиров Став
    ("ט4", "alona.itzik@gmail.com"): "221179468",         # first user
    ("ט4", "mayapeer.itzik0510@gmail.com"): "221179468",  # second user
    ("ט4", "amirsabag2102@gmail.com"): "221179468",       # third user
    ("ט4", "nizriking@gmail.com"): "220021067",           # first user
    ("ט4", "tairnizri12@gmail.com"): "220021067",         # second user
    ("ט4", "tairnizri67@gmail.com"): "220021067",         # third user
    ("ט4", "tyrnyzry@gmail.com"): "220021067",            # fourth user
    ("ט4", "yaelperetz13@gmail.com"): "336595178",        # פרץ יהל
}

# Rows the matcher attached to the wrong student, or duplicate exports that
# must not feed a profile.  They keep their text for the audit trail but lose
# the student id and confirmation.
UNASSIGN: set[tuple[str, str]] = {
    ("ז3", "idan.sarfati@gmail.com"),          # teacher, wrongly attached to ירום
    ("ז3", "morsigron87@gmail.com"),           # סגרון מור, another student
    ("ז4", "moryalevi9@gmail.com"),            # duplicate of the ז3 לוי מוריה line
    ("ח3", "oripahima7@gmail.com"),            # פחימה אורי, another student
    ("ט2", "nptlyybtmyr@gmail.com"),           # נפתלייב תמיר, another student
    ("ט2", "ofrigal2012@gmail.com"),           # גל עפרי, another student
    ("ט2", "twixle464@gmail.com"),             # not אברגל
    ("ט2", "milamilamila258@gmail.com"),       # not גלפרין
    ("ט3", "alinkrumer@gmail.com"),            # not אליקם
    ("ט4", "mdkkfm1@gmail.com"),               # not פרץ יהל
}

# Students the teacher says have no active Classroom account.
NO_ACCOUNT = {
    "222270951",  # אבוטבול הראל ציון
    "339409112",  # דוידוב אוה
    "220008973",  # אוחנה מריאל נינט
    "220288070",  # פוריאן הראל
    "217494434",  # מסס תמר
}


def _load_roster(root: Path) -> dict[str, tuple[str, str]]:
    """national id -> (display name, alephon class)."""
    roster: dict[str, tuple[str, str]] = {}
    for label, folder in class_directories(root).items():
        alephon, _sheets = load_class_folder(folder, label)
        for student in alephon.students:
            if student.national_id:
                roster[student.national_id] = (
                    student.display_name,
                    student.class_label,
                )
    return roster


def _review_ids(report_path: Path) -> set[str]:
    if not report_path.is_file():
        return set()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        str(row[1])
        for row in report.get("students", [])
        if row[3] in {"לבדיקה", "התאמה חלשה"}
    }


def _append_note(note: str, addition: str) -> str:
    if addition in note:
        return note
    return f"{note} · {addition}".strip(" ·")


def apply(mapping_path: Path, root: Path, report_path: Path, *, write: bool) -> int:
    roster = _load_roster(root)
    confirm_ids = (_review_ids(report_path) | set(ASSIGN.values())) - NO_ACCOUNT
    rows = read_mapping(mapping_path)

    changes: list[str] = []
    new_rows: list[MappingRow] = []
    for row in rows:
        key = (class_key(row.class_label), row.classroom_email.casefold())
        if key in UNASSIGN:
            if row.student_id or row.confirmed:
                changes.append(
                    f"הסרה: {row.class_label} · {row.classroom_name} "
                    f"({row.student_name or 'ללא שיוך'})"
                )
            new_rows.append(
                replace(
                    row,
                    student_id="",
                    student_name="",
                    confidence=CONFIDENCE_NONE,
                    confirmed=False,
                    note=_append_note(row.note, REMOVED_MARK),
                )
            )
            continue

        target_id = ASSIGN.get(key)
        if target_id is not None:
            if target_id not in roster:
                raise ValueError(
                    f"ת.ז {target_id} לא נמצאה באלפון עבור {row.classroom_name}"
                )
            name, _label = roster[target_id]
            if (row.student_id, row.confirmed) != (target_id, True):
                changes.append(
                    f"שיוך: {row.class_label} · {row.classroom_name} -> {name}"
                )
            new_rows.append(
                replace(
                    row,
                    student_id=target_id,
                    student_name=name,
                    confidence=CONFIDENCE_HIGH,
                    confirmed=True,
                    note=_append_note(row.note, MANUAL_MARK),
                )
            )
            continue

        if row.student_id and row.student_id in confirm_ids and not row.confirmed:
            changes.append(
                f"אישור: {row.class_label} · {row.classroom_name} "
                f"({row.student_name})"
            )
            new_rows.append(
                replace(
                    row,
                    confidence=CONFIDENCE_HIGH,
                    confirmed=True,
                    note=_append_note(row.note, MANUAL_MARK),
                )
            )
            continue

        new_rows.append(row)

    confirmed = sum(1 for row in new_rows if row.usable)
    print(f"שורות בקובץ: {len(new_rows)} · מאושרות: {confirmed}")
    print(f"שינויים: {len(changes)}")
    for line in changes:
        print(f"  • {line}")

    if write:
        count = write_mapping(mapping_path, new_rows)
        print(f"נכתב {mapping_path} ({count} שורות).")
    else:
        print("תצוגה מקדימה בלבד — הקובץ לא שונה.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        return apply(args.mapping, args.root, args.report, write=args.apply)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
