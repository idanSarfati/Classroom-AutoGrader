"""Exam/quiz grade import: workbook parsing, planning, applying and the UI.

A grade-only assignment has no rubric, so the student page must show a score
(and never a submission form) while the class feed says "לא הוזן ציון"
instead of "לא הוגש" for a student the teacher has not graded yet.
"""
from __future__ import annotations

import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from sqlalchemy import select

from portal.imports.exams import (
    ACTION_CONFLICT,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    apply_exam_plan,
    build_exam_plan,
    discover_exam_sheets,
    parse_exam_workbook,
)
from portal.models import (
    GRADE_SOURCE_CLASSROOM,
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    StudentRecord,
    Submission,
)
from portal.tests.helpers import create_user, csrf, login
from portal.web.student_feed import build_class_feed

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"
CLASS_NAME = "ז'3"
FOLDER_NAME = "ז3"
EVENT = "מבחן רבעון א"
EXAM_TITLE = f"{EVENT} - 22/10/2026"
NOA_ID = "111111118"
AMIT_ID = "222222226"
YAEL_ID = "333333334"


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


HEADER = [
    "מס'",
    "ת.ז התלמיד",
    "שם התלמיד",
    "שכבה",
    "כיתה",
    "ציון מספרי",
    "ציון מילולי",
    "הערה חופשית",
]


def exam_rows(
    *,
    noa: int | None = 88,
    amit: int | None = None,
    yael: int | None = 91,
    unknown: bool = True,
) -> list[list[object]]:
    rows: list[list[object]] = [
        [f"הזנת ציונים לאירוע ציון: {EVENT} - 22/10/2026"],
        [],
        HEADER,
        [1, NOA_ID, "כהן נועה", "ז", "3", noa if noa is not None else "", "", "טוב"],
        [2, AMIT_ID, "לוי עמית", "ז", "3", amit if amit is not None else "", "", ""],
        [3, YAEL_ID, "דוד יעל", "ז", "3", yael if yael is not None else "", "", ""],
        ["", "", "ממוצע", "", "", 89.5, "", ""],
    ]
    if unknown:
        rows.append([4, "999999999", "לא קיים", "ז", "3", 77, "", ""])
    return rows


def write_exam_folder(root: Path, **overrides) -> Path:
    folder = root / FOLDER_NAME
    folder.mkdir(parents=True, exist_ok=True)
    write_xlsx(
        folder / f"הזנת ציונים לאירוע ציון_ {EVENT} - 22_10_2026.xlsx",
        exam_rows(**overrides),
    )
    return folder


def _db(app):
    return app.state.session_factory()


def _seed(app) -> dict[str, int]:
    teacher_id = create_user(
        app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורי כהן"
    )
    students = {}
    for username, display, national_id in (
        (NOA_ID, "כהן נועה", NOA_ID),
        (AMIT_ID, "לוי עמית", AMIT_ID),
        (YAEL_ID, "דוד יעל", YAEL_ID),
    ):
        user_id = create_user(
            app,
            username,
            STUDENT_PASSWORD,
            role=ROLE_STUDENT,
            display_name=display,
        )
        students[display] = user_id
        with _db(app) as db:
            db.add(
                StudentRecord(
                    student_id=user_id,
                    national_id=national_id,
                    class_label=FOLDER_NAME,
                )
            )
            db.commit()
    with _db(app) as db:
        group = ClassGroup(name=CLASS_NAME, code="Z3", teacher_id=teacher_id)
        db.add(group)
        db.flush()
        for user_id in students.values():
            db.add(Enrollment(class_id=group.id, student_id=user_id))
        db.commit()
        return {"teacher": teacher_id, "class": group.id, **students}


@pytest.fixture
def world(app, tmp_path):
    ids = _seed(app)
    folder = write_exam_folder(tmp_path / "אלפונים")
    return {"ids": ids, "folder": folder, "root": tmp_path / "אלפונים"}


# ---------------------------------------------------------------------------
# Reader and planner
# ---------------------------------------------------------------------------


def test_parse_exam_workbook_skips_summary_and_keeps_blank_scores(world):
    path = next(world["folder"].glob("*.xlsx"))

    sheet = parse_exam_workbook(path, FOLDER_NAME)

    assert sheet.class_label == FOLDER_NAME
    assert sheet.event_name == EVENT
    assert sheet.exam_date == date(2026, 10, 22)
    assert sheet.title == EXAM_TITLE
    # The "ממוצע" row is not a student; the blank score is kept as an absent
    # grade, not dropped. Portal-unknown ids are reported later, by the plan.
    assert [(row.national_id, row.score) for row in sheet.rows] == [
        (NOA_ID, 88.0),
        (AMIT_ID, None),
        (YAEL_ID, 91.0),
        ("999999999", 77.0),
    ]


def test_plan_and_apply_create_grade_only_assignments(app, world):
    sheets = discover_exam_sheets(world["root"])
    assert len(sheets) == 1

    with _db(app) as db:
        plan = build_exam_plan(db, sheets)

    assert len(plan.assignments) == 1
    assert plan.blank_scores == 1
    assert plan.created == 2
    assert all(issue for issue in plan.issues)  # the unknown id is reported

    with _db(app) as db:
        counts = apply_exam_plan(db, plan)
    assert counts["assignments"] == 1
    assert counts["created"] == 2

    with _db(app) as db:
        assignment = db.scalar(
            select(Assignment).where(Assignment.title == EXAM_TITLE)
        )
        assert assignment is not None
        assert assignment.rubric_path == ""
        assert assignment.rubric_sha256 == ""
        assert assignment.published is True
        assert assignment.due_at == datetime(2026, 10, 22, 12, 0)
        assert "אירוע ציון" in assignment.instructions

        grades = db.scalars(select(Grade)).all()
        assert {grade.score for grade in grades} == {88, 91}
        assert all(
            grade.configured_model == GRADE_SOURCE_CLASSROOM for grade in grades
        )
        assert all(grade.late_penalty_points == 0 for grade in grades)
        submissions = db.scalars(select(Submission)).all()
        assert len(submissions) == 2
        assert all(submission.text == "" for submission in submissions)


def test_rerun_is_idempotent_and_updates_a_changed_score(app, world):
    sheets = discover_exam_sheets(world["root"])
    with _db(app) as db:
        apply_exam_plan(db, build_exam_plan(db, sheets))

    with _db(app) as db:
        plan = build_exam_plan(db, sheets)
    assert plan.assignments[0].action == ACTION_UNCHANGED
    assert plan.created == 0
    assert plan.unchanged == 2

    second = write_exam_folder(
        world["root"].parent / "שנית", noa=95, yael=91, unknown=False
    )
    changed = parse_exam_workbook(next(second.glob("*.xlsx")), FOLDER_NAME)
    with _db(app) as db:
        plan = build_exam_plan(db, [changed])
        actions = {item.score: item.action for item in plan.grades}
    assert plan.assignments[0].assignment_id != 0
    assert actions[95] == ACTION_UPDATE
    assert actions[91] == ACTION_UNCHANGED

    with _db(app) as db:
        counts = apply_exam_plan(db, plan)
    assert counts["updated"] == 1
    with _db(app) as db:
        assert sorted(
            grade.score for grade in db.scalars(select(Grade)).all()
        ) == [91, 95]


def test_engine_grade_or_hand_in_is_never_overwritten(app, world):
    sheets = discover_exam_sheets(world["root"])
    with _db(app) as db:
        apply_exam_plan(db, build_exam_plan(db, sheets))

    with _db(app) as db:
        assignment = db.scalar(
            select(Assignment).where(Assignment.title == EXAM_TITLE)
        )
        submission = db.scalar(
            select(Submission).where(Submission.assignment_id == assignment.id)
        )
        submission.text = "print('real code')"
        grade = db.scalar(
            select(Grade).where(Grade.submission_id == submission.id)
        )
        grade.configured_model = "gpt-test"
        db.commit()

    with _db(app) as db:
        plan = build_exam_plan(db, sheets)
    assert any(item.action == ACTION_CONFLICT for item in plan.grades)
    assert plan.conflicts == 1


# ---------------------------------------------------------------------------
# Student feed and pages
# ---------------------------------------------------------------------------


def test_feed_labels_exam_without_grade_as_not_entered(app, world):
    sheets = discover_exam_sheets(world["root"])
    with _db(app) as db:
        apply_exam_plan(db, build_exam_plan(db, sheets))
        group = db.get(ClassGroup, world["ids"]["class"])
        assignment = db.scalar(
            select(Assignment).where(Assignment.title == EXAM_TITLE)
        )
        topics = build_class_feed(
            db, group, [], [assignment], {}, {}, "/student"
        )

    items = [item for topic in topics for item in topic.items]
    assert len(items) == 1
    item = items[0]
    assert item.kind_label == "אירוע ציון"
    assert item.kind_badge == "badge-exam"
    assert item.status_label == "לא הוזן ציון"


def test_student_exam_page_shows_score_and_has_no_submission_form(
    app, client, world
):
    sheets = discover_exam_sheets(world["root"])
    with _db(app) as db:
        apply_exam_plan(db, build_exam_plan(db, sheets))
        assignment = db.scalar(
            select(Assignment).where(Assignment.title == EXAM_TITLE)
        )
        assignment_id = assignment.id

    # Amit has no score in the workbook: still a valid, read-only page.
    login(client, AMIT_ID, STUDENT_PASSWORD)
    response = client.get(f"/student/assignments/{assignment_id}")
    assert response.status_code == 200
    assert "הציון טרם הוזן" in response.text
    assert 'name="answer_1"' not in response.text
    assert "data-answer-editor" not in response.text

    login(client, NOA_ID, STUDENT_PASSWORD)
    response = client.get(f"/student/assignments/{assignment_id}")
    assert response.status_code == 200
    assert "הציון שלכם" in response.text
    assert 'aria-label="ציון 88"' in response.text
    assert "אירוע ציון" in response.text
    assert "הגשת גרסה חדשה" not in response.text


def test_student_cannot_submit_to_an_exam_event(app, client, world):
    sheets = discover_exam_sheets(world["root"])
    with _db(app) as db:
        apply_exam_plan(db, build_exam_plan(db, sheets))
        assignment = db.scalar(
            select(Assignment).where(Assignment.title == EXAM_TITLE)
        )
        assignment_id = assignment.id

    login(client, NOA_ID, STUDENT_PASSWORD)
    token = csrf(client, f"/student/assignments/{assignment_id}")
    response = client.post(
        f"/student/assignments/{assignment_id}/submit",
        data={"csrf_token": token, "answer_1": "print(1)"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 400
    assert "אירוע ציון" in response.json()["error"]

    with _db(app) as db:
        submission = db.scalar(
            select(Submission).where(
                Submission.assignment_id == assignment_id,
                Submission.text == "print(1)",
            )
        )
        assert submission is None


def test_teacher_class_page_and_preview_show_grade_event(app, client, world):
    sheets = discover_exam_sheets(world["root"])
    with _db(app) as db:
        apply_exam_plan(db, build_exam_plan(db, sheets))
        assignment = db.scalar(
            select(Assignment).where(Assignment.title == EXAM_TITLE)
        )
        assignment_id = assignment.id

    login(client, "mori", TEACHER_PASSWORD)
    class_page = client.get(f"/teacher/classes/{world['ids']['class']}")
    assert class_page.status_code == 200
    assert EXAM_TITLE in class_page.text
    assert "אירוע ציון" in class_page.text
    assert "אירוע ציון — הציונים הוזנו על ידי המורה" in class_page.text

    preview = client.get(f"/teacher/preview/assignments/{assignment_id}")
    assert preview.status_code == 200
    assert "תצוגת תלמיד" in preview.text
    assert "אירוע ציון" in preview.text
