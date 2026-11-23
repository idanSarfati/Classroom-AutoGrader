"""Phase 7 tests: alephon import, Classroom grade import and profile views.

Everything runs against a temporary SQLite database and hand-built source
files. The XLSX is a minimal but valid OOXML archive, so the stdlib reader is
exercised exactly the way it reads the school's real workbook.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from sqlalchemy import select

from portal.gradebook import build_gradebook, build_student_profile
from portal.imports.grades import (
    ACTION_CONFLICT,
    ACTION_UPDATE,
    apply_grade_plan,
    build_grade_plan,
)
from portal.imports.matching import (
    CONFIDENCE_HIGH,
    MAPPING_HEADERS,
    match_sheet,
    read_mapping,
    write_mapping,
)
from portal.imports.sources import (
    load_class_folder,
    load_class_sheets,
    parse_alephon,
    parse_classroom_csv,
    parse_grade,
    parse_hebrew_date,
)
from portal.imports.students import (
    CREDENTIAL_HEADERS,
    apply_student_plan,
    build_student_plan,
    write_credentials,
)
from portal.models import (
    GRADE_SOURCE_CLASSROOM,
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    GradingJob,
    StudentRecord,
    Submission,
    User,
)
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"

CLASS_NAME = "ז'3"
FOLDER_NAME = "ז3"
TITLE = "3.2 | הגשת משימות"
NOA_ID = "111111118"
AMIT_ID = "222222226"
YAEL_ID = "333333334"


# ---------------------------------------------------------------------------
# Source-file builders
# ---------------------------------------------------------------------------


def _col(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def write_xlsx(path: Path, rows: list[list[object]]) -> None:
    """Write a one-sheet XLSX that the stdlib reader can open."""
    shared: list[str] = []
    indexes: dict[str, int] = {}
    sheet_rows: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column, value in enumerate(row):
            if value is None or value == "":
                continue
            reference = f"{_col(column)}{row_number}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            else:
                text = str(value)
                if text not in indexes:
                    indexes[text] = len(shared)
                    shared.append(text)
                cells.append(
                    f'<c r="{reference}" t="s"><v>{indexes[text]}</v></c>'
                )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')

    workbook = f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>{"".join(sheet_rows)}</sheetData>
</worksheet>"""
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{escape(item)}</t></si>" for item in shared)
        + "</sst>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/worksheets/sheet1.xml", workbook)


ALEPHON_ROWS = [
    [
        "ת.ז",
        "שם התלמיד",
        "שכבה",
        "דואר אלקטרוני של התלמיד",
        "סלולרי של התלמיד",
        "שם הורה 1",
        "סלולארי הורה 1",
        'דוא"ל הורה 1',
    ],
    [
        NOA_ID,
        "כהן נועה",
        "ז",
        "noa@example.com",
        "0501111111",
        "דוד כהן",
        "0502222222",
        "david@example.com",
    ],
    [AMIT_ID, "לוי עמית", "ז", "amit@example.com", "0503333333", "", "", ""],
    [YAEL_ID, "דוד יעל", "ז", "yael@example.com", "", "", "", ""],
]

CSV_HEADER = [
    "שם משפחה",
    "שם פרטי",
    "כתובת אימייל",
    TITLE,
    f"{TITLE} [Auto]",
    "מבחן רבעון",
]


def write_class_folder(root: Path, noa_auto: str = "95") -> Path:
    folder = root / FOLDER_NAME
    folder.mkdir(parents=True)
    write_xlsx(folder / "אלפון.xlsx", ALEPHON_ROWS)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    writer.writerow(["תאריך", "", "", "14 בספט׳ 2026", "14 בספט׳ 2026", ""])
    writer.writerow(["נקודות", "", "", "100", "80", ""])
    writer.writerow(["כהן", "נועה", "noa@example.com", "95", noa_auto, "70"])
    writer.writerow(["Levi", "Amit", "amit@example.com", "", "64", ""])
    writer.writerow(["דוד", "יעל", "yael@example.com", "לא הוגש", "", ""])
    (folder / "ציונים.csv").write_text(buffer.getvalue(), encoding="utf-8-sig")
    return folder


def _db(app):
    return app.state.session_factory()


def _seed(app, *, engine_grade_for_noa: bool = False) -> dict[str, int]:
    teacher_id = create_user(
        app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורי כהן"
    )
    # Noa already has an account (username = national id) with a known
    # password, so the import must attach to it rather than duplicate it.
    noa_user_id = create_user(
        app,
        NOA_ID,
        STUDENT_PASSWORD,
        role=ROLE_STUDENT,
        display_name="כהן נועה",
    )
    with _db(app) as db:
        group = ClassGroup(name=CLASS_NAME, code="Z3", teacher_id=teacher_id)
        db.add(group)
        db.flush()
        assignment = Assignment(
            class_id=group.id,
            title=TITLE,
            instructions="הגישו את התרגיל.",
            rubric_path="assignments/lesson_3.json",
            rubric_sha256="a" * 64,
            due_at=datetime(2026, 9, 14, 8, 0),
            published=True,
        )
        draft = Assignment(
            class_id=group.id,
            title="3.3 | הגשת משימות",
            instructions="טיוטה.",
            rubric_path="assignments/lesson_3.json",
            rubric_sha256="b" * 64,
            published=False,
        )
        db.add_all([assignment, draft])
        db.flush()
        db.add(Enrollment(class_id=group.id, student_id=noa_user_id))
        if engine_grade_for_noa:
            submission = Submission(
                assignment_id=assignment.id,
                student_id=noa_user_id,
                text="print('real code')",
                revision=1,
                status="graded",
                first_submitted_at=datetime(2026, 9, 10, 9, 0),
                submitted_at=datetime(2026, 9, 10, 9, 0),
            )
            db.add(submission)
            db.flush()
            db.add(
                Grade(
                    submission_id=submission.id,
                    revision=1,
                    score=100,
                    feedback_hebrew="ציון מהמנוע",
                    result_json="{}",
                    configured_model="gpt-test",
                    graded_at=datetime(2026, 9, 11, 9, 0),
                )
            )
        db.commit()
        return {
            "teacher": teacher_id,
            "noa": noa_user_id,
            "class": group.id,
            "assignment": assignment.id,
            "draft": draft.id,
        }


@pytest.fixture
def world(app, tmp_path):
    ids = _seed(app)
    folder = write_class_folder(tmp_path / "אלפונים")
    alephon, sheets = load_class_folder(folder, folder.name)
    return {"ids": ids, "folder": folder, "alephon": alephon, "sheets": sheets}


def _import_students(app, alephon):
    with _db(app) as db:
        plan = build_student_plan(db, [alephon])
        credentials = apply_student_plan(db, plan)
    return plan, credentials


def _import_grades(app, sheets, mapping):
    with _db(app) as db:
        plan = build_grade_plan(db, sheets, mapping)
        counts = apply_grade_plan(db, plan)
    return plan, counts


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def test_alephon_reader_keeps_ids_names_and_contacts(tmp_path):
    folder = write_class_folder(tmp_path)
    alephon = parse_alephon(folder / "אלפון.xlsx", folder.name)

    assert alephon.class_label == FOLDER_NAME
    assert [student.national_id for student in alephon.students] == [
        NOA_ID,
        AMIT_ID,
        YAEL_ID,
    ]
    noa = alephon.students[0]
    assert noa.display_name == "כהן נועה"
    assert noa.family_name == "כהן"
    assert noa.given_name == "נועה"
    assert noa.email == "noa@example.com"
    assert noa.phone == "0501111111"
    assert noa.parent_name == "דוד כהן"
    assert noa.parent_email == "david@example.com"
    # The workbook's שכבה column says only "ז"; the folder supplies "ז3".
    assert all(student.class_label == FOLDER_NAME for student in alephon.students)


def test_classroom_reader_keeps_duplicate_columns_and_dates(tmp_path):
    folder = write_class_folder(tmp_path)
    sheet = parse_classroom_csv(folder / "ציונים.csv", folder.name)

    titles = [column.title for column in sheet.columns]
    assert titles == [TITLE, f"{TITLE} [Auto]", "מבחן רבעון"]
    assert sheet.columns[0].due_at == parse_hebrew_date("14 בספט׳ 2026")
    assert sheet.columns[0].max_points == 100
    assert sheet.columns[1].max_points == 80

    noa = sheet.students[0]
    assert noa.display_name == "כהן נועה"
    assert noa.scores[3] == "95"
    assert noa.scores[4] == "95"
    assert parse_grade(noa.scores[5]) == 70
    assert parse_grade("לא הוגש") is None
    assert parse_grade("—") is None


def test_classroom_reader_accepts_english_headers(tmp_path):
    """An English Google export parses exactly like the Hebrew one.

    Google downloads the gradebook in the workspace language, so a school set
    to English yields ``Surname/First Name/Email`` with ``Date``/``Points``
    preamble rows. The reader must detect the header and both preamble rows in
    English too, and scale by the ``Points`` row just as with ``נקודות``.
    """
    folder = tmp_path / FOLDER_NAME
    folder.mkdir(parents=True)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Surname", "First Name", "Email Address", TITLE, f"{TITLE} [Auto]"]
    )
    writer.writerow(["Date", "", "", "14 Sept 2026", "14 Sept 2026"])
    writer.writerow(["Points", "", "", "100", "80"])
    writer.writerow(["Cohen", "Noa", "noa@example.com", "95", "70"])
    (folder / "grades.csv").write_text(buffer.getvalue(), encoding="utf-8-sig")

    sheet = parse_classroom_csv(folder / "grades.csv", folder.name)

    assert [c.title for c in sheet.columns] == [TITLE, f"{TITLE} [Auto]"]
    assert sheet.columns[0].due_at == date(2026, 9, 14)
    assert sheet.columns[0].max_points == 100
    assert sheet.columns[1].max_points == 80
    assert sheet.students[0].display_name == "Cohen Noa"
    assert sheet.students[0].scores[3] == "95"


def test_load_class_sheets_does_not_need_the_alephon(tmp_path):
    folder = write_class_folder(tmp_path)
    (folder / "אלפון.xlsx").unlink()
    sheets = load_class_sheets(folder, folder.name)
    assert len(sheets) == 1


# ---------------------------------------------------------------------------
# Name matching and the review file
# ---------------------------------------------------------------------------


def test_matching_confirms_complete_names_and_flags_duplicates(world):
    sheet = world["sheets"][0]
    match = match_sheet(world["alephon"].students, sheet)
    by_name = {row.classroom_name: row for row in match.rows}

    noa = by_name["כהן נועה"]
    assert noa.confidence == CONFIDENCE_HIGH
    assert noa.confirmed is True
    assert noa.student_id == NOA_ID

    # A Latin transliteration still matches a complete Hebrew name.
    amit = by_name["Levi Amit"]
    assert amit.confidence == CONFIDENCE_HIGH
    assert amit.confirmed is True
    assert amit.student_id == AMIT_ID

    yael = by_name["דוד יעל"]
    assert yael.confirmed is True
    assert yael.student_id == YAEL_ID


def test_duplicate_classroom_rows_are_kept_but_not_confirmed(world, tmp_path):
    sheet = world["sheets"][0]
    roster = world["alephon"].students
    duplicate = replace(
        sheet,
        students=sheet.students
        + (replace(sheet.students[0], row_number=99),),
    )
    match = match_sheet(roster, duplicate)
    noa_rows = [row for row in match.rows if row.classroom_name == "כהן נועה"]
    assert len(noa_rows) == 2
    assert sum(1 for row in noa_rows if row.confirmed) == 1
    extra = next(row for row in noa_rows if not row.confirmed)
    assert "שורת כפילות" in extra.note


def test_mapping_file_roundtrip_and_validation(world, tmp_path):
    sheet = world["sheets"][0]
    rows = list(match_sheet(world["alephon"].students, sheet).rows)
    path = tmp_path / "mapping.csv"
    write_mapping(path, rows)

    raw = path.read_text(encoding="utf-8-sig")
    assert raw.splitlines()[0] == ",".join(MAPPING_HEADERS)
    loaded = read_mapping(path)
    assert len(loaded) == len(rows)
    assert [row.confirmed for row in loaded] == [row.confirmed for row in rows]

    # A teacher may type the id with punctuation; read_mapping strips it.
    text = raw.replace(f",{NOA_ID},", f",{NOA_ID[:3]}-{NOA_ID[3:6]}-{NOA_ID[6:]},", 1)
    path.write_text(text, encoding="utf-8-sig")
    assert any(row.student_id == NOA_ID for row in read_mapping(path))

    path.write_text(
        text.replace("\nyes\n", "\nmaybe\n").replace(",yes,", ",maybe,"),
        encoding="utf-8-sig",
    )
    with pytest.raises(ValueError):
        read_mapping(path)


# ---------------------------------------------------------------------------
# Student import
# ---------------------------------------------------------------------------


def test_student_import_creates_accounts_and_enrollments(app, world):
    plan, credentials = _import_students(app, world["alephon"])

    assert plan.new_accounts == 2  # Noa already existed
    assert plan.new_enrollments == 2  # and was already enrolled
    assert len(credentials) == 2
    assert all(len(item.password) == 10 for item in credentials)

    with _db(app) as db:
        students = db.scalars(
            select(User).where(User.role == ROLE_STUDENT).order_by(User.username)
        ).all()
        records = db.scalars(select(StudentRecord)).all()
        enrollments = db.scalars(select(Enrollment)).all()

    assert {user.username for user in students} == {NOA_ID, AMIT_ID, YAEL_ID}
    assert len(records) == 3
    assert len(enrollments) == 3
    noa_record = next(record for record in records if record.national_id == NOA_ID)
    assert noa_record.parent_name == "דוד כהן"
    names = {user.username: user.display_name for user in students}
    assert names[AMIT_ID] == "לוי עמית"


def test_student_import_is_idempotent(app, world):
    _import_students(app, world["alephon"])
    with _db(app) as db:
        plan = build_student_plan(db, [world["alephon"]])
        assert plan.new_accounts == 0
        assert plan.new_enrollments == 0
        assert plan.updated == 0
        assert plan.unchanged == 3
        assert apply_student_plan(db, plan) == []


def test_student_import_updates_changed_contact_without_blanking(app, world):
    _import_students(app, world["alephon"])
    with _db(app) as db:
        record = db.scalar(
            select(StudentRecord).where(StudentRecord.national_id == AMIT_ID)
        )
        record.parent_name = "אבא עמית"
        record.phone = "0501111111"
        db.commit()

    changed = replace(
        world["alephon"],
        students=(
            replace(
                world["alephon"].students[1],
                phone="0509999999",
                parent_name="",
            ),
        )
        + world["alephon"].students[2:],
    )
    with _db(app) as db:
        plan = build_student_plan(db, [changed])
        assert any("טלפון" in entry.changes for entry in plan.entries)
        apply_student_plan(db, plan)

    with _db(app) as db:
        record = db.scalar(
            select(StudentRecord).where(StudentRecord.national_id == AMIT_ID)
        )
        assert record.phone == "0509999999"
        # An empty alephon cell never erases a value the teacher already has.
        assert record.parent_name == "אבא עמית"


def test_credentials_file_is_utf8_bom_and_appends(tmp_path):
    folder = write_class_folder(tmp_path / "src")
    alephon = parse_alephon(folder / "אלפון.xlsx", folder.name)
    from portal.imports.students import Credential

    first = [
        Credential(FOLDER_NAME, "לוי עמית", AMIT_ID, AMIT_ID, "Abcdef2345"),
    ]
    second = [
        Credential(FOLDER_NAME, "דוד יעל", YAEL_ID, YAEL_ID, "Zyxwvu9876"),
    ]
    path = tmp_path / "credentials.csv"
    write_credentials(path, first)
    write_credentials(path, second)
    payload = path.read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == ",".join(CREDENTIAL_HEADERS)
    assert len(lines) == 3
    assert sum("Abcdef2345" in line for line in lines) == 1
    assert sum("Zyxwvu9876" in line for line in lines) == 1


# ---------------------------------------------------------------------------
# Historical grade import
# ---------------------------------------------------------------------------


def _mapping_rows(world):
    return list(match_sheet(world["alephon"].students, world["sheets"][0]).rows)


def test_grade_plan_scales_by_max_points_and_reports_unmatched_columns(app, world):
    _import_students(app, world["alephon"])
    with _db(app) as db:
        plan = build_grade_plan(db, world["sheets"], _mapping_rows(world))

    by_student = {item.student_name: item for item in plan.grades}
    assert plan.created == 2
    assert by_student["כהן נועה"].score == 95
    # Amit's only value is in the 80-point [Auto] column.
    assert by_student["לוי עמית"].raw_score == 64
    assert by_student["לוי עמית"].max_points == 80
    assert by_student["לוי עמית"].score == 80
    assert "יעל דוד" not in by_student
    assert plan.matched_columns == 1
    skipped = {item.title: item for item in plan.skipped_columns}
    assert skipped["מבחן רבעון"].numeric_cells == 1
    assert by_student["כהן נועה"].graded_at == datetime(2026, 9, 14, 12, 0)


def test_grade_import_follows_the_email_when_an_export_resorts_rows(app, world):
    """A re-sorted export must not grade the child sitting on the old row.

    Google sorts the gradebook by display name, and a rename moves everyone
    below it. The mapping's ``csv_row`` is therefore only a hint; the recorded
    e-mail is what identifies a student across exports.
    """
    _import_students(app, world["alephon"])
    mapping = _mapping_rows(world)
    sheet = world["sheets"][0]
    reordered = replace(
        sheet,
        students=tuple(
            replace(student, row_number=student.row_number + 4)
            for student in reversed(sheet.students)
        ),
    )

    with _db(app) as db:
        plan = build_grade_plan(db, [reordered], mapping)

    by_student = {item.student_name: item.raw_score for item in plan.grades}
    assert by_student["כהן נועה"] == 95
    assert by_student["לוי עמית"] == 64
    assert any("אימייל" in issue for issue in plan.issues)


def test_grade_import_refuses_a_row_whose_email_left_the_export(app, world):
    """No e-mail match means no import - never a positional guess."""
    _import_students(app, world["alephon"])
    mapping = _mapping_rows(world)
    sheet = world["sheets"][0]
    noa = sheet.students[0]
    moved = replace(
        sheet,
        students=(replace(noa, email="newaddress@example.com"),)
        + sheet.students[1:],
    )

    with _db(app) as db:
        plan = build_grade_plan(db, [moved], mapping)

    assert [item.student_name for item in plan.grades] == ["לוי עמית"]
    assert any("כהן נועה" in issue for issue in plan.issues)


def test_grade_plan_exclude_titles_leaves_an_assignment_out(app, world):
    _import_students(app, world["alephon"])
    with _db(app) as db:
        plan = build_grade_plan(
            db, world["sheets"], _mapping_rows(world), exclude_titles=("3.2",)
        )

    assert plan.grades == ()
    assert plan.matched_columns == 0
    excluded = {item.title: item for item in plan.skipped_columns}
    assert "הוחרג" in excluded[TITLE].reason


def test_grade_import_writes_submissions_grades_and_gradebook(app, world):
    _import_students(app, world["alephon"])
    plan, counts = _import_grades(app, world["sheets"], _mapping_rows(world))
    assert counts == {"created": 2, "updated": 0, "unchanged": 0, "conflicts": 0}

    with _db(app) as db:
        submissions = db.scalars(select(Submission)).all()
        grades = db.scalars(select(Grade)).all()
        assert len(submissions) == 2
        assert all(submission.text == "" for submission in submissions)
        assert {grade.score for grade in grades} == {95, 80}
        assert {grade.configured_model for grade in grades} == {
            GRADE_SOURCE_CLASSROOM
        }
        assert all("היסטורי" in grade.feedback_hebrew for grade in grades)

        group = db.get(ClassGroup, world["ids"]["class"])
        book = build_gradebook(db, group)
        assert len(book.assignments) == 1  # the draft is not a column
        row = next(
            item for item in book.rows if item.student.display_name == "כהן נועה"
        )
        assert row.cells[0].score == 95
        assert row.cells[0].historical is True
        # The submitted timestamp is at noon UTC, after this 08:00 deadline;
        # a historical grade is still never flagged late.
        assert row.cells[0].late is False
        assert row.late_count == 0
        assert row.average == 95
        assert book.stats.average == pytest.approx(87.5)

        noa = db.get(User, world["ids"]["noa"])
        teacher = db.get(User, world["ids"]["teacher"])
        profile = build_student_profile(db, teacher, noa)
        assert profile.historical == 1
        assert profile.history[0].historical is True
        assert profile.history[0].assignment_title == TITLE


def test_grade_import_is_idempotent_and_updates_changed_scores(app, world):
    _import_students(app, world["alephon"])
    _import_grades(app, world["sheets"], _mapping_rows(world))

    with _db(app) as db:
        plan = build_grade_plan(db, world["sheets"], _mapping_rows(world))
    assert plan.created == 0
    assert plan.unchanged == 2

    sheet = world["sheets"][0]
    noa = sheet.students[0]
    edited = replace(
        sheet,
        students=(
            replace(noa, scores={**noa.scores, 3: "88", 4: "88"}),
        )
        + sheet.students[1:],
    )
    with _db(app) as db:
        plan = build_grade_plan(db, [edited], _mapping_rows(world))
        changed = [item for item in plan.grades if item.action == ACTION_UPDATE]
        assert len(changed) == 1
        assert changed[0].score == 88
        apply_grade_plan(db, plan)
        grade = db.scalar(
            select(Grade)
            .join(Submission, Submission.id == Grade.submission_id)
            .where(Submission.student_id == world["ids"]["noa"])
        )
        assert grade.score == 88


def test_grade_import_never_overwrites_engine_grade(app, tmp_path):
    ids = _seed(app, engine_grade_for_noa=True)
    folder = write_class_folder(tmp_path / "אלפונים")
    alephon, sheets = load_class_folder(folder, folder.name)
    plan, _credentials = _import_students(app, alephon)
    with _db(app) as db:
        grade_plan = build_grade_plan(db, sheets, _mapping_rows(
            {"alephon": alephon, "sheets": sheets}
        ))
        conflicts = [
            item for item in grade_plan.grades if item.action == ACTION_CONFLICT
        ]
        assert len(conflicts) == 1
        assert conflicts[0].student_name == "כהן נועה"
        assert "מהמנוע" in conflicts[0].note
        counts = apply_grade_plan(db, grade_plan)
        assert counts["conflicts"] == 1

        grade = db.scalar(
            select(Grade)
            .join(Submission, Submission.id == Grade.submission_id)
            .where(Submission.student_id == ids["noa"])
        )
        assert grade.score == 100
        assert grade.configured_model == "gpt-test"


def test_disagreeing_duplicate_columns_become_a_conflict(app, world):
    _import_students(app, world["alephon"])
    sheet = world["sheets"][0]
    noa = sheet.students[0]
    # Same title, two columns, two different scores: refuse to guess.
    edited = replace(
        sheet,
        students=(replace(noa, scores={**noa.scores, 3: "95", 4: "60"}),)
        + sheet.students[1:],
    )
    with _db(app) as db:
        plan = build_grade_plan(db, [edited], _mapping_rows(world))
        conflict = next(
            item
            for item in plan.grades
            if item.student_name == "כהן נועה"
        )
        assert conflict.action == ACTION_CONFLICT
        assert "אינן מסכימות" in conflict.note
        assert apply_grade_plan(db, plan)["conflicts"] == 1


# ---------------------------------------------------------------------------
# Web views
# ---------------------------------------------------------------------------


def _login(client, username: str, password: str):
    response = login(client, username, password, follow_redirects=True)
    assert response.status_code == 200
    return response


def _imported_world(app, world):
    _import_students(app, world["alephon"])
    _import_grades(app, world["sheets"], _mapping_rows(world))
    with _db(app) as db:
        submission = db.scalar(
            select(Submission).where(Submission.student_id == world["ids"]["noa"])
        )
        return submission.id


def test_teacher_profile_shows_contact_history_and_historical_badge(
    client, app, world
):
    _imported_world(app, world)
    _login(client, "mori", TEACHER_PASSWORD)

    response = client.get(f"/teacher/students/{world['ids']['noa']}/profile")
    assert response.status_code == 200
    html = response.text
    assert "פרטי תלמיד מהאלפון" in html
    assert NOA_ID in html
    assert "דוד כהן" in html
    assert "מגמת ציונים" in html
    assert "היסטוריים" in html
    assert "היסטורי" in html
    assert TITLE in html


def test_student_result_hides_the_empty_code_card(client, app, world):
    submission_id = _imported_world(app, world)
    _login(client, NOA_ID, STUDENT_PASSWORD)

    response = client.get(f"/student/submissions/{submission_id}")
    assert response.status_code == 200
    html = response.text
    assert "הקוד שהוגש" not in html
    assert "ציון היסטורי" in html
    assert "95" in html


def test_regrade_is_refused_for_a_historical_grade(client, app, world):
    submission_id = _imported_world(app, world)
    _login(client, "mori", TEACHER_PASSWORD)

    page = client.get(f"/teacher/submissions/{submission_id}")
    assert page.status_code == 200
    assert "בדיקה מחדש" not in page.text
    token = csrf(client, f"/teacher/submissions/{submission_id}")

    response = client.post(
        f"/teacher/submissions/{submission_id}/regrade",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "אין לו קוד לבדיקה מחדש" in response.text

    with _db(app) as db:
        assert db.scalars(select(GradingJob)).all() == []
