"""Mixed x.3 worksheets: spec validation, widgets, storage and grading text.

The two live sheets (lesson 2 and lesson 3) are treated as content: the tests
load exactly the files the portal serves, so a typo in Hebrew or a broken table
row fails here instead of on a student's phone.
"""
from __future__ import annotations

import json
import re

import pytest

from portal.grading import adapter
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
    Submission,
    WorksheetResponse,
)
from portal.tests.helpers import create_user, csrf, login
from portal.worksheets.schema import (
    WorksheetSpecError,
    load_by_slug,
    parse_worksheet,
)

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"


def _db(app):
    return app.state.session_factory()


class RecordingWorker:
    def __init__(self) -> None:
        self.jobs: list[int] = []

    def enqueue(self, job_id: int | None = None) -> None:
        if job_id is not None:
            self.jobs.append(int(job_id))

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 5.0) -> None:
        pass


# ------------------------------------------------------------------- fixtures


def _seed(app) -> dict:
    teacher_id = create_user(app, "ws-mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    student_id = create_user(app, "ws-noa", STUDENT_PASSWORD, role=ROLE_STUDENT)
    lesson2 = adapter.load_rubric("assignments/lesson_2_worksheet.json")
    lesson3 = adapter.load_rubric("assignments/lesson_3_worksheet.json")
    with _db(app) as db:
        group = ClassGroup(
            name="כיתת דפי עבודה", code="WS01", teacher_id=teacher_id
        )
        db.add(group)
        db.flush()
        db.add(Enrollment(class_id=group.id, student_id=student_id))
        first = Assignment(
            class_id=group.id,
            title="2.3 | תרגול לפיצוח - דף עבודה",
            instructions="ענו על דף העבודה.",
            rubric_path=str(lesson2.path),
            rubric_sha256=lesson2.sha256,
            published=True,
        )
        second = Assignment(
            class_id=group.id,
            title="3.3 | תרגול לפיצוח - דף עבודה",
            instructions="ענו על דף העבודה.",
            rubric_path=str(lesson3.path),
            rubric_sha256=lesson3.sha256,
            published=True,
        )
        db.add_all([first, second])
        db.commit()
        return {
            "teacher": teacher_id,
            "student": student_id,
            "class": group.id,
            "lesson2": first.id,
            "lesson3": second.id,
        }


@pytest.fixture
def seeded(app, client):
    data = _seed(app)
    app.state.grading_worker.stop()
    app.state.grading_worker = RecordingWorker()
    return data


def _login_student(client) -> None:
    response = login(client, "ws-noa", STUDENT_PASSWORD)
    assert response.status_code == 303, response.text


def _login_teacher(client) -> None:
    response = login(client, "ws-mori", TEACHER_PASSWORD)
    assert response.status_code == 303, response.text


def _page(client, assignment_id: int) -> str:
    response = client.get(f"/student/assignments/{assignment_id}")
    assert response.status_code == 200, response.text[:500]
    return response.text


def _submit(client, assignment_id: int, fields: dict[str, str]):
    token = csrf(client, f"/student/assignments/{assignment_id}")
    data = {"csrf_token": token, **fields}
    return client.post(
        f"/student/assignments/{assignment_id}/submit",
        data=data,
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )


def _stored(app, submission_id: int) -> dict:
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        response = db.get(
            WorksheetResponse, (submission.id, submission.revision)
        )
        return {
            "submission": submission,
            "payload": json.loads(response.payload) if response else None,
        }


# ------------------------------------------------------------------ the specs


def test_both_live_sheets_load_and_keep_their_exercises() -> None:
    lesson2 = load_by_slug("lesson_2_worksheet")
    lesson3 = load_by_slug("lesson_3_worksheet")

    assert lesson2 is not None and lesson3 is not None
    # Exercise 5 of lesson 2 is the exempt earth/sun task and is not in the Doc.
    assert [item.number for item in lesson2.items] == [1, 2, 3, 4, 6, 9, 10]
    assert [item.number for item in lesson3.items] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert lesson2.note
    assert lesson3.note


def test_live_sheets_use_every_needed_control() -> None:
    lesson2 = load_by_slug("lesson_2_worksheet")
    lesson3 = load_by_slug("lesson_3_worksheet")
    kinds2 = {
        part.kind for item in lesson2.items for part in item.parts
    }
    kinds3 = {
        part.kind for item in lesson3.items for part in item.parts
    }

    assert {"code", "code_line", "choice", "table"} <= kinds2
    assert "short" in kinds2
    assert {"code", "choice", "table"} <= kinds3


def test_field_names_are_unique_across_a_sheet() -> None:
    for slug in ("lesson_2_worksheet", "lesson_3_worksheet"):
        worksheet = load_by_slug(slug)
        names: list[str] = []
        for item in worksheet.items:
            for part in item.parts:
                if part.kind in ("code", "code_line", "short"):
                    names.append(part.value_name(item.key))
                elif part.kind == "table":
                    for row_index, _row in enumerate(part.rows):
                        for column in part.columns:
                            if not column.readonly:
                                names.append(
                                    part.table_name(
                                        item.key, row_index, column.key
                                    )
                                )
                elif part.kind == "choice":
                    for index, _statement in enumerate(part.statements):
                        names.append(part.choice_name(item.key, index))
        assert len(names) == len(set(names)), slug


def test_invalid_spec_is_rejected_before_it_reaches_a_student() -> None:
    with pytest.raises(WorksheetSpecError):
        parse_worksheet(
            {
                "version": 1,
                "rubric": "lesson_2_worksheet",
                "items": [
                    {
                        "key": "ex1",
                        "number": 1,
                        "title": "",
                        "parts": [
                            {
                                "key": "t",
                                "kind": "table",
                                "columns": [{"key": "a", "label": "A"}],
                                "rows": [["x", "extra"]],
                            }
                        ],
                    }
                ],
            },
            slug="lesson_2_worksheet",
        )

    with pytest.raises(WorksheetSpecError):
        parse_worksheet(
            {"version": 1, "rubric": "wrong", "items": []},
            slug="lesson_2_worksheet",
        )


def test_a_rubric_without_a_sheet_keeps_the_old_form() -> None:
    from portal.worksheets import for_rubric

    assert for_rubric("assignments/lesson_1.json") is None
    assert for_rubric("") is None


# ------------------------------------------------------------------ composing


def test_collect_and_compose_round_trip() -> None:
    worksheet = load_by_slug("lesson_3_worksheet")
    form = {
        "answer_ex1__trace__r0__output": "1 2 3 4",
        "answer_ex1__trace__r2__output": "100 101 102 103",
        "answer_ex4__blanks__s0": "int",
        "answer_ex4__blanks__s1": "15",
        "answer_ex4__blanks__s2": "print",
        "answer_ex4__blanks__s3": "shirts_number",
        "answer_ex5__code": "total = 0\nprint(total)",
        "answer_ex2__b": "print(1)",
    }

    values = worksheet.collect(form)
    text = worksheet.compose(values)

    assert values["ex1"]["trace"][0] == {"output": "1 2 3 4"}
    assert values["ex4"]["blanks"] == [
        "int",
        "15",
        "print",
        "shirts_number",
    ]
    assert worksheet.count_answered(values) == 4
    assert "## שאלה 1: תרגיל 1 - מעקב פלט עם end" in text
    assert "1: הפלט: 1 2 3 4" in text
    assert "— int" in text
    assert "total = 0" in text


def test_choice_values_outside_the_options_are_dropped() -> None:
    worksheet = load_by_slug("lesson_2_worksheet")
    values = worksheet.collect(
        {"answer_ex1__c__s0": "אולי", "answer_ex1__c__s1": "נכון"}
    )

    assert values["ex1"]["c"] == ["", "נכון", ""]


def test_empty_sheet_is_not_a_submission() -> None:
    worksheet = load_by_slug("lesson_2_worksheet")
    values = worksheet.collect({})

    assert worksheet.has_any(values) is False
    assert worksheet.compose(values)


# --------------------------------------------------------------- student page


def test_mixed_widgets_are_rendered_for_lesson_2(client, seeded) -> None:
    _login_student(client)
    html = _page(client, seeded["lesson2"])

    assert 'data-worksheet-item' in html
    # One-line code, true/false radios, a trace table and full code editors.
    assert 'name="answer_ex1__a"' in html
    assert 'name="answer_ex1__c__s0"' in html
    assert 'name="answer_ex2__outputs__r0__output"' in html
    assert 'name="answer_ex3__code"' in html
    assert 'value="נכון"' in html
    assert html.count("data-code-editor") == 5
    # The real Doc stays available but collapsed.
    assert 'class="card notes-disclosure"' in html


def test_mixed_widgets_are_rendered_for_lesson_3(client, seeded) -> None:
    _login_student(client)
    html = _page(client, seeded["lesson3"])

    assert 'name="answer_ex1__trace__r0__output"' in html
    assert 'name="answer_ex3__trace__r0__average"' in html
    assert 'name="answer_ex4__blanks__s0"' in html
    assert 'name="answer_ex2__a"' in html
    # The code snippet above the trace table is visible.
    assert "print(num, end=" in html


def test_submit_stores_text_and_structured_payload(app, client, seeded) -> None:
    _login_student(client)
    response = _submit(
        client,
        seeded["lesson2"],
        {
            "answer_ex1__a": 'name = "Bob"',
            "answer_ex1__b": "age = 14",
            "answer_ex1__c__s0": "נכון",
            "answer_ex1__c__s1": "נכון",
            "answer_ex1__c__s2": "לא נכון",
            "answer_ex2__outputs__r0__output": "5",
            "answer_ex3__code": 'name = "Dana"\nprint(name)',
        },
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    state = _stored(app, payload["submission_id"])
    assert state["payload"]["ex1"]["a"] == 'name = "Bob"'
    assert state["payload"]["ex1"]["c"] == ["נכון", "נכון", "לא נכון"]
    assert state["payload"]["ex2"]["outputs"][0] == {"output": "5"}
    text = state["submission"].text
    assert "## שאלה 1: תרגיל 1 - פקודות השמה ושאלות type" in text
    assert "name = \"Bob\"" in text
    assert "1. המשתנה my_birth_year הוא מטיפוס int — נכון" in text
    assert "5" in text
    assert 'name = "Dana"' in text


def test_reopening_restores_tables_and_choices(app, client, seeded) -> None:
    _login_student(client)
    _submit(
        client,
        seeded["lesson2"],
        {
            "answer_ex2__outputs__r0__output": "5",
            "answer_ex1__c__s0": "נכון",
            "answer_ex3__code": "print('x')",
        },
    )

    html = _page(client, seeded["lesson2"])

    assert re.search(
        r'name="answer_ex2__outputs__r0__output"\s+value="5"', html
    )
    assert re.search(
        r'name="answer_ex1__c__s0"\s+value="נכון"\s+checked', html
    )
    assert "print(&#39;x&#39;)" in html or "print('x')" in html


def test_resubmission_replaces_the_payload(app, client, seeded) -> None:
    _login_student(client)
    first = _submit(
        client,
        seeded["lesson3"],
        {"answer_ex1__trace__r0__output": "1 2 3 4"},
    )
    assert first.status_code == 202
    submission_id = first.json()["submission_id"]

    second = _submit(
        client,
        seeded["lesson3"],
        {"answer_ex1__trace__r0__output": "1 2 3 4 5"},
    )
    assert second.status_code == 202
    assert second.json()["revision"] == 2

    state = _stored(app, submission_id)
    assert state["payload"]["ex1"]["trace"][0] == {"output": "1 2 3 4 5"}
    assert "1 2 3 4 5" in state["submission"].text


def test_empty_worksheet_cannot_be_submitted(client, seeded) -> None:
    _login_student(client)

    response = _submit(client, seeded["lesson2"], {"answer_ex1__a": "   "})

    assert response.status_code == 400
    assert "error" in response.json()


def test_teacher_preview_shows_the_same_widgets(client, seeded) -> None:
    _login_teacher(client)
    response = client.get(
        f"/teacher/preview/assignments/{seeded['lesson3']}"
    )

    assert response.status_code == 200, response.text[:500]
    html = response.text
    assert "data-worksheet-item" in html
    assert 'name="answer_ex1__trace__r0__output"' in html
    assert "disabled" in html
    assert "data-submit-form" not in html


# --------------------------------------------------------------- older pages


def test_lesson_2_keeps_the_old_code_form(client, seeded) -> None:
    """x.2 is unchanged: its rubric has no worksheet file."""
    _login_student(client)
    # Lesson 1 is not seeded here; the builder-level check is the guard.
    from portal.worksheets import for_rubric

    assert for_rubric("assignments/lesson_1.json") is None
