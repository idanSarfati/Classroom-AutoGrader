"""End-to-end exam behaviour: versions, shuffle, window, one attempt, marking.

These tests use the real exam definition in ``portal/exams/units_1_3.json`` and
the real check file, because the point of both is that they describe every
paper exactly. A change to a question without its answer key fails here.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta

import pytest

from portal.exams import checker
from portal.exams import config as exam_config
from portal.exams import llm_check as exam_llm_check
from portal.exams import manual as exam_manual
from portal.exams import service as exam_service
from portal import drafts, worksheets
from portal.exams.paper import load_paper, paper_for, paper_for_allocation
from portal.grading import service as grading_service
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
    ExamAllocation,
    ExamRecheckPreview,
    ExamStudentWindow,
    Grade,
    GradingJob,
    Submission,
    User,
)
from portal.scripts import exam_tools
from portal.tests.helpers import create_user, csrf, login
from portal.timeutil import format_local, parse_local_datetime, utcnow

TEACHER_PASSWORD = "ExTeacher123"
STUDENT_PASSWORD = "ExStudent123"
EXAM = "units_1_3"
STUDENTS = ("exam-noa", "exam-dan", "exam-maya", "exam-eden")


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
    exam_config.clear_cache()
    teacher_id = create_user(app, "exam-mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    student_ids = [
        create_user(
            app,
            name,
            STUDENT_PASSWORD,
            role=ROLE_STUDENT,
            display_name=name.replace("exam-", ""),
        )
        for name in STUDENTS
    ]
    config = exam_config.load_exam(EXAM)
    assert config is not None
    first = config.versions[0]
    now = utcnow()
    with _db(app) as db:
        group = ClassGroup(name="כיתת מבחן", code="EX01", teacher_id=teacher_id)
        db.add(group)
        db.flush()
        for student_id in student_ids:
            db.add(Enrollment(class_id=group.id, student_id=student_id))
        assignment = Assignment(
            class_id=group.id,
            title="3.9 | מבחן מסכם - יחידות 1-3",
            instructions="ענו על כל השאלות.",
            rubric_path=str(first.rubric_path),
            rubric_sha256="",
            exam_config=EXAM,
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            published=True,
        )
        db.add(assignment)
        db.commit()
        return {
            "teacher": teacher_id,
            "students": student_ids,
            "class": group.id,
            "assignment": assignment.id,
        }


@pytest.fixture
def seeded(app, client):
    # ``client`` starts the lifespan, which is where the live worker exists.
    # It is stopped and replaced so no background pass can grade behind a test.
    data = _seed(app)
    real = app.state.grading_worker
    real.stop()
    app.state.grading_worker = RecordingWorker()
    try:
        yield {**data, "real_worker": real}
    finally:
        real.stop()


def _login(client, username: str) -> None:
    response = login(client, username, STUDENT_PASSWORD)
    assert response.status_code == 303, response.text


def _page(client, assignment_id: int) -> str:
    response = client.get(f"/student/assignments/{assignment_id}")
    assert response.status_code == 200, response.text[:600]
    return response.text


def _perfect_fields(version_key: str) -> dict[str, str]:
    """The form fields of an all-correct sheet for one version."""
    config = exam_config.load_exam(EXAM)
    version = config.version(version_key)
    answers = exam_tools._perfect_answers(
        config,
        version,
        checker.version_rules(checker.load_checks(config), version_key),
    )
    paper = load_paper(version)
    fields: dict[str, str] = {}
    for item in paper.items:
        item_values = answers[item.key]
        for part in item.parts:
            if part.given:
                continue
            value = item_values.get(part.key)
            if part.kind in {"code", "code_plain", "code_line", "short"}:
                fields[part.value_name(item.key)] = str(value or "")
            elif part.kind == "table":
                for row_index, row in enumerate(part.rows):
                    for column in part.columns:
                        if column.readonly:
                            continue
                        fields[part.table_name(item.key, row_index, column.key)] = ""
            elif part.kind == "choice":
                for index, chosen in enumerate(value or []):
                    fields[part.choice_name(item.key, index)] = str(chosen or "")
    return fields


def _submit(client, assignment_id: int, fields: dict[str, str]):
    token = csrf(client, f"/student/assignments/{assignment_id}")
    return client.post(
        f"/student/assignments/{assignment_id}/submit",
        data={"csrf_token": token, **fields},
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )


def _allocation(app, assignment_id: int, student_id: int) -> ExamAllocation | None:
    with _db(app) as db:
        return db.query(ExamAllocation).filter_by(
            assignment_id=assignment_id, student_id=student_id
        ).one_or_none()


def _release(app, assignment_id: int, state: str) -> None:
    """Set the teacher's release state directly, for tests about the page."""
    with _db(app) as db:
        db.get(Assignment, assignment_id).exam_release = state
        db.commit()


def _release_via_page(client, assignment_id: int, state: str):
    """Press one of the teacher's release buttons from the class page."""
    token = csrf(client, "/teacher")
    return client.post(
        f"/teacher/assignments/{assignment_id}/exam-release",
        data={"csrf_token": token, "release": state},
        follow_redirects=False,
    )


# ------------------------------------------------------------------ allocation


def test_each_student_gets_one_stable_version_and_an_own_order(app, seeded) -> None:
    config = exam_config.load_exam(EXAM)
    with _db(app) as db:
        assignment = db.get(Assignment, seeded["assignment"])
        papers = []
        for student_id in seeded["students"]:
            student = db.get(User, student_id)
            first = exam_service.student_paper(db, assignment, student, config)
            again = exam_service.student_paper(db, assignment, student, config)
            papers.append(first)
            assert (first.version.key, first.seed) == (again.version.key, again.seed)
            assert [item.key for item in first.display.items] == [
                item.key for item in again.display.items
            ]
    assert {paper.version.key for paper in papers} <= set(config.version_keys)
    # Seeds are drawn from a huge range, so two students sharing one is not
    # something a class should ever see.
    assert len({paper.seed for paper in papers}) == len(papers)


def test_question_order_is_shuffled_inside_each_scored_part(app) -> None:
    config = exam_config.load_exam(EXAM)
    version = config.versions[0]
    canonical = load_paper(version)
    shuffled = paper_for(config, version, 424242)

    moved = False
    for (name, original), (shown_name, shown) in zip(
        canonical.sections(), shuffled.display.sections()
    ):
        assert name == shown_name
        assert sorted(item.key for item in original) == sorted(
            item.key for item in shown
        )
        if len(original) > 1 and [item.key for item in original] != [
            item.key for item in shown
        ]:
            moved = True
    assert moved, "a five-question part should not keep the canonical order"

    # Numbers run 1..N across the whole paper, in the shown order.
    assert [item.number for item in shuffled.display.items] == list(
        range(1, len(shuffled.display.items) + 1)
    )
    # The stored hand-in follows the student's numbering, so the review screen
    # reads in the same order the student worked in.
    assert [item.label for item in shuffled.display.items][:3] == [
        "שאלה 1",
        "שאלה 2",
        "שאלה 3",
    ]


def test_choice_options_are_shuffled_for_the_student_but_not_the_key(app) -> None:
    config = exam_config.load_exam(EXAM)
    version = config.versions[0]
    canonical = load_paper(version)
    answers = exam_tools._perfect_answers(
        config,
        version,
        checker.version_rules(checker.load_checks(config), version.key),
    )
    # The key is expressed in option text, so a shuffled display and the
    # canonical paper accept exactly the same answers.
    paper = exam_service.canonical_paper(config, version.key)
    assert paper.display.total_points == canonical.total_points == 100
    report = checker.check(config, version, answers)
    assert report.score == 100

    # A different seed is allowed to reorder options, but it must not change
    # which answers are correct.
    shuffled = paper_for(config, version, 987654321)
    by_key = {item.key: item for item in shuffled.display.items}
    for item in canonical.items:
        shown = by_key[item.key]
        for part, shown_part in zip(item.parts, shown.parts):
            if part.kind != "choice":
                continue
            assert sorted(part.options_for(part.statements[0])) == sorted(
                shown_part.options_for(shown_part.statements[0])
            )


# ---------------------------------------------------------------------- window


def test_exam_is_not_served_before_it_opens(client, app, seeded) -> None:
    _login(client, STUDENTS[0])
    with _db(app) as db:
        assignment = db.get(Assignment, seeded["assignment"])
        assignment.opens_at = utcnow() + timedelta(hours=1)
        db.commit()
    page = _page(client, seeded["assignment"])
    assert "טרם נפתח" in page
    assert "data-worksheet-item" not in page
    assert _allocation(app, seeded["assignment"], seeded["students"][0]) is None

    fields = _perfect_fields(exam_config.load_exam(EXAM).versions[0].key)
    response = _submit(client, seeded["assignment"], fields)
    assert response.status_code == 403
    assert "טרם נפתח" in response.json()["error"]


def test_exam_refuses_a_hand_in_after_it_closes(client, app, seeded) -> None:
    _login(client, STUDENTS[0])
    with _db(app) as db:
        assignment = db.get(Assignment, seeded["assignment"])
        assignment.closes_at = utcnow() - timedelta(minutes=1)
        db.commit()
    page = _page(client, seeded["assignment"])
    assert "נסגר" in page
    assert "data-worksheet-item" not in page

    fields = _perfect_fields(exam_config.load_exam(EXAM).versions[0].key)
    response = _submit(client, seeded["assignment"], fields)
    assert response.status_code == 403
    assert "נסגר" in response.json()["error"]


def test_open_exam_serves_the_students_own_paper_only(client, app, seeded) -> None:
    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    assert allocation is not None
    config = exam_config.load_exam(EXAM)
    label = config.version(allocation.version).label
    assert "פרטי המבחן" in page
    assert label in html.unescape(page)
    assert page.count("data-worksheet-item") == 11
    # The exam is an in-site paper: no "Run code" IDE anywhere.
    assert "data-run-code" not in page
    # Subject and learner identity are never written into the page.
    for leaked in ("correct_answer", "corrected_code", "rubric_path"):
        assert leaked not in page


# --------------------------------------------------------------- one attempt


def test_a_perfect_sheet_is_marked_100_by_the_worker(client, app, seeded) -> None:
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])  # allocates the version
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    fields = _perfect_fields(allocation.version)

    response = _submit(client, seeded["assignment"], fields)
    assert response.status_code == 202, response.text[:600]
    body = response.json()
    assert body["accepted"] is True and body["queued"] is True

    # The exam path is local: one worker pass marks it, no model call.
    worker = seeded["real_worker"]
    assert worker.process_next(pause=False) is True

    with _db(app) as db:
        submission = db.get(Submission, body["submission_id"])
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        assert submission.status == "graded"
        assert grade is not None
        assert grade.score == 100
        assert grade.configured_model == "exam-checker"
        assert grade.late_penalty_points == 0

    # Marked already, but the teacher still holds the result: the student sees
    # a confirmation and not the number.
    page = _page(client, seeded["assignment"])
    assert "המבחן הוגש" in page
    assert "score-ring" not in page
    assert 'aria-label="ציון' not in page

    # Once released in full the same page shows the score and the paper.
    _release(app, seeded["assignment"], exam_service.RELEASE_FULL)
    page = _page(client, seeded["assignment"])
    assert 'aria-label="ציון 100"' in page
    assert "אי אפשר לשנות" in page
    assert page.count("data-worksheet-item") == 11


def test_second_hand_in_is_refused_and_the_paper_becomes_read_only(
    client, app, seeded
) -> None:
    _login(client, STUDENTS[1])
    _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][1])
    fields = _perfect_fields(allocation.version)

    first = _submit(client, seeded["assignment"], fields)
    assert first.status_code == 202, first.text[:600]

    second = _submit(client, seeded["assignment"], fields)
    assert second.status_code == 409
    assert "פעם אחת" in second.json()["error"]

    # Before a release the page is the hand-in confirmation, not the paper.
    page = _page(client, seeded["assignment"])
    assert 'action="/student/assignments/' not in page
    assert "אי אפשר לשנות" in page
    assert "data-worksheet-item" not in page
    # After a full release the answers are visible in the locked paper.
    _release(app, seeded["assignment"], exam_service.RELEASE_FULL)
    page = _page(client, seeded["assignment"])
    assert "data-worksheet-item" in page
    assert 'action="/student/assignments/' not in page


def test_a_failed_check_can_be_handed_in_again(client, app, seeded) -> None:
    """A checker failure is the portal's fault, so it must not burn the try."""
    _login(client, STUDENTS[2])
    _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][2])
    fields = _perfect_fields(allocation.version)
    first = _submit(client, seeded["assignment"], fields)
    assert first.status_code == 202

    with _db(app) as db:
        submission = db.get(Submission, first.json()["submission_id"])
        submission.status = "failed"
        db.commit()

    again = _submit(client, seeded["assignment"], fields)
    assert again.status_code == 202, again.text[:600]


# -------------------------------------------------------------------- checker


def test_checker_deducts_for_a_wrong_choice_and_keeps_the_rest(app) -> None:
    config = exam_config.load_exam(EXAM)
    version = config.version("a")
    answers = exam_tools._perfect_answers(
        config,
        version,
        checker.version_rules(checker.load_checks(config), "a"),
    )
    # Flip the first multiple-choice answer to a wrong option.
    wrong = "99"
    answers["a1"] = {"response": [wrong]}
    report = checker.check(config, version, answers)
    assert report.score == 100 - 5
    assert next(q for q in report.questions if q.key == "a1").status == checker.INCORRECT
    assert all(
        q.status == checker.CORRECT for q in report.questions if q.key != "a1"
    )


def test_a_hardcoded_print_fails_the_second_input_of_a_program(app) -> None:
    config = exam_config.load_exam(EXAM)
    version = config.version("a")
    answers = exam_tools._perfect_answers(
        config,
        version,
        checker.version_rules(checker.load_checks(config), "a"),
    )
    answers["d1"] = {"code": 'print("Next year you will be 21")'}
    report = checker.check(config, version, answers)
    question = next(q for q in report.questions if q.key == "d1")
    assert question.earned < question.points
    assert question.status != checker.CORRECT
    assert report.score < 100


def _graded(version_key: str, overrides: dict[str, dict] | None = None):
    """A perfect sheet with chosen parts replaced, graded by the real checker."""
    config = exam_config.load_exam(EXAM)
    version = config.version(version_key)
    rules = checker.version_rules(checker.load_checks(config), version_key)
    answers = exam_tools._perfect_answers(config, version, rules)
    for key, values in (overrides or {}).items():
        answers[key].update(values)
    report = checker.check(config, version, answers)
    return report, {question.key: question for question in report.questions}


def test_tracing_lines_earn_points_one_by_one_in_order() -> None:
    """The rubric names each output line and its own points."""
    _report, questions = _graded("a", {"b2": {"response": "a = 19"}})
    assert questions["b2"].earned == 6
    assert questions["b2"].status == checker.PARTIALLY_CORRECT

    # Ignoring spaces does not glue the whole answer together.
    _report, questions = _graded("a", {"b2": {"response": "a=19\nb=15"}})
    assert questions["b2"].earned == 12
    assert questions["b2"].status == checker.CORRECT

    # A line written twice cannot pay for the missing one.
    _report, questions = _graded("a", {"b2": {"response": "a = 19\na = 19"}})
    assert questions["b2"].earned == 6

    # Swapped lines keep the credit of the value the student did write.
    _report, questions = _graded("a", {"b2": {"response": "b = 15\na = 19"}})
    assert questions["b2"].earned == 6
    assert questions["b2"].status == checker.PARTIALLY_CORRECT

    # A typo in the number loses that line and only that line.
    _report, questions = _graded("a", {"b2": {"response": "a = 19\nb = 51"}})
    assert questions["b2"].earned == 6
    assert "1 מתוך 2" in questions["b2"].notes


def test_type_line_accepts_str_as_well_as_the_repr() -> None:
    _report, questions = _graded("a", {"b3": {"response": "str\n60"}})
    assert questions["b3"].earned == 11
    _report, questions = _graded("a", {"b3": {"response": "<class 'str'>\n60"}})
    assert questions["b3"].earned == 11
    _report, questions = _graded("a", {"b3": {"response": "<class 'str'>\n50"}})
    assert questions["b3"].earned == 5


def test_a_hebrew_explanation_of_the_error_earns_the_whole_part() -> None:
    """Naming the cause and what goes wrong earns every explanation point."""
    answers = {
        ("a", "c1"): "הקלט הוא מחרוזת, ולכן אי אפשר לחבר אליו 1",
        ("c", "c2"): "input מחזיר טקסט ולא מספר, ולכן המחרוזת מוכפלת",
        ("a", "c2"): "שם המשתנה נמצא בתוך המרכאות ולכן הוא מודפס כמו שהוא",
        ("b", "c1"): "המשתנה לא מחובר להודעה אלא כתוב בתוך המחרוזת",
    }
    for (version_key, item_key), answer in answers.items():
        report, questions = _graded(version_key, {item_key: {"explain": answer}})
        assert questions[item_key].status == checker.CORRECT, (
            answer,
            questions[item_key].notes,
        )
        assert report.score == 100


def test_a_cause_without_the_result_loses_one_point_and_asks_for_review() -> None:
    """The cause is the question; the missing consequence costs the last point."""
    _report, questions = _graded(
        "b", {"c1": {"explain": "כל הפרינט בגרשיים"}}
    )
    assert questions["c1"].earned == 4 + 5  # 4 explanation + the perfect code
    assert questions["c1"].needs_review


def test_a_wrong_diagnosis_loses_the_whole_explanation() -> None:
    """A shared word with the key must not pay when the diagnosis is reversed."""
    wrong = {
        "b": "לא שמו item_name מרכאות",
        "c": "השגיאה היא שיש גרשיים על המשתנה total_weight",
    }
    # ``c`` is a correct diagnosis; keep it here to prove the rejection is not
    # a blanket "mentions quotes" rule.
    _report, questions = _graded("c", {"c1": {"explain": wrong["c"]}})
    assert questions["c1"].earned >= 4
    _report, questions = _graded("b", {"c1": {"explain": wrong["b"]}})
    assert questions["c1"].earned == 5  # only the corrected code
    assert questions["c1"].needs_review
    # Quoting only the printable sentence is the right fix, not a reversed
    # diagnosis; it earns the cause share even though it is terse.
    _report, questions = _graded(
        "b", {"c1": {"explain": "צריך לשים בגרשיים רק את המשפט Product name:"}}
    )
    assert questions["c1"].earned == 4 + 5
    assert questions["c1"].needs_review
    _report, questions = _graded(
        "a", {"c1": {"explain": 'the correct type is string'}}
    )
    assert questions["c1"].earned == 5
    assert questions["c1"].needs_review


def test_an_explanation_that_names_the_needed_conversion_counts() -> None:
    """A terse fix shows the cause, so it earns the cause share and one to go."""
    for version_key, item_key, answer in (
        ("a", "c1", "צריך להמיר את הקלט ל-int"),
        ("b", "c2", "הגובה צריך float"),
        ("c", "c1", "אפשר לחבר את המשתנה עם + להודעה"),
    ):
        _report, questions = _graded(version_key, {item_key: {"explain": answer}})
        assert questions[item_key].earned == questions[item_key].points - 1, (
            answer,
            questions[item_key].notes,
        )
        assert questions[item_key].needs_review


def test_an_explanation_that_misses_the_error_is_left_for_review() -> None:
    # ``print`` contains ``int``; a word inside another word must not count.
    _report, questions = _graded("a", {"c1": {"explain": "השגיאה בשורה עם print"}})
    assert questions["c1"].earned == 5  # only the corrected code
    assert questions["c1"].status == checker.PARTIALLY_CORRECT
    assert questions["c1"].needs_review

    _report, questions = _graded(
        "a", {"c2": {"explain": "השגיאה נמצאת בתחתית הקוד"}}
    )
    assert questions["c2"].earned == 5
    assert questions["c2"].needs_review


def test_corrected_program_criteria_are_independent() -> None:
    """Running, printing the value and converting are three separate slices."""
    fixed_explain = {"explain": "מחרוזת צריך להמיר, אחרת אי אפשר לחבר"}
    buggy = (
        'age = input("Enter your age: ")\nnext_year = age + 1\n'
        'print("Next year you will be", next_year)'
    )
    hardcoded = 'print("Next year you will be 21")'
    converted_wrong_print = (
        'age = int(input("Enter your age: "))\n'
        'print("Next year you will be", age)'
    )
    for code, expected in (
        (buggy, 0),
        (hardcoded, 3),
        (converted_wrong_print, 3),
    ):
        _report, questions = _graded("a", {"c1": {**fixed_explain, "code": code}})
        assert questions["c1"].earned == 5 + expected, (code, questions["c1"].notes)


def test_full_program_loses_the_shares_it_missed() -> None:
    """Two inputs make a hardcoded print fail; each requirement has its share."""
    three_prints = (
        'name = input("Enter your name: ")\n'
        'age = int(input("Enter your age: "))\n'
        'price = float(input("Enter ticket price: "))\n'
        "print(name)\nprint(age)\nprint(price)"
    )
    hardcoded = 'print("Hello Dan you are 12 and the ticket costs 45.5 shekels")'
    missing_float = (
        'name = input("Enter your name: ")\n'
        'age = int(input("Enter your age: "))\n'
        'price = input("Enter ticket price: ")\n'
        'print("Hello", name, "you are", age, "and the ticket costs", price, "shekels")'
    )
    for code, earned in ((three_prints, 12), (hardcoded, 8), (missing_float, 16)):
        report, questions = _graded("a", {"d1": {"code": code}})
        assert questions["d1"].earned == earned, (code, questions["d1"].notes)
        assert report.score == 100 - (20 - earned)


def test_live_exam_files_verify_end_to_end() -> None:
    """The paper, the key and the rubric are one consistent set."""
    assert exam_tools.verify(EXAM) == 0


# ------------------------------------------------------------------- teacher


def test_teacher_class_page_reads_the_exam_and_checks_a_hand_in(
    client, app, seeded
) -> None:
    """The class page is the teacher's control panel for the exam."""
    login(client, "exam-mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/classes/{seeded['class']}")
    assert page.status_code == 200, page.text[:600]
    text = html.unescape(page.text)
    assert "פתוח להגשה" in text
    assert "נפתחו 0 מתוך 4" in text
    assert "בדיקת הגשות" in text
    assert "בדיקה מחדש עם השוואה" not in text
    assert "בדיקה עם המודל" not in text
    # The generated exam rubric is not offered as an ordinary homework rubric.
    assert "exam_units_1_3_a.json" not in text

    assignment_id = seeded["assignment"]
    _login(client, STUDENTS[1])
    _page(client, assignment_id)
    allocation = _allocation(app, assignment_id, seeded["students"][1])
    fields = _perfect_fields(allocation.version)
    submitted = _submit(client, assignment_id, fields)
    assert submitted.status_code == 202, submitted.text[:600]
    submission_id = submitted.json()["submission_id"]
    assert seeded["real_worker"].process_next(pause=False) is True

    login(client, "exam-mori", TEACHER_PASSWORD)
    text = html.unescape(client.get(f"/teacher/classes/{seeded['class']}").text)
    assert "נפתחו 1 מתוך 4" in text
    assert "הוגשו 1" in text
    assert "נבדקו 1" in text
    label = exam_config.load_exam(EXAM).version(allocation.version).label
    assert label in text

    # The single "בדיקת הגשות" screen replaces the old whole-class re-check.
    # With the model off it marks deterministically; selecting the one hand-in
    # and applying it must leave the same perfect sheet worth 100.
    base = f"/teacher/assignments/{assignment_id}/llm-check"
    text = html.unescape(client.get(base).text)
    assert "בחירת הגשות לבדיקה" in text
    assert f'value="{submission_id}"' in text

    with _db(app) as db:
        preview = exam_llm_check.start(
            db, db.get(Assignment, assignment_id), [submission_id],
            teacher_id=seeded["teacher"],
        )
        preview_id = preview.id
    assert exam_llm_check.run(app.state.session_factory, preview_id) == exam_llm_check.READY
    with _db(app) as db:
        row = exam_llm_check.payload(db.get(ExamRecheckPreview, preview_id))["rows"][0]
        assert row["llm_state"] == exam_llm_check.OFF
        assert row["new_score"] == 100

    token = csrf(client, base)
    applied = client.post(
        f"{base}/apply", data={"csrf_token": token}, follow_redirects=False
    )
    assert applied.status_code == 303
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        assert grade is not None and grade.score == 100
        assert db.query(ExamRecheckPreview).count() == 0


def test_check_preview_shows_old_and_new_and_keeps_manual_points(
    client, app, seeded
) -> None:
    """A dry run never writes; applying it writes the shown numbers."""
    assignment_id = seeded["assignment"]
    _login(client, STUDENTS[1])
    _page(client, assignment_id)
    allocation = _allocation(app, assignment_id, seeded["students"][1])
    fields = _perfect_fields(allocation.version)
    # The cause-only explanation is now worth 4 of 5: a diff must appear.
    fields["answer_c1__explain"] = "כל הפרינט בגרשיים"
    submitted = _submit(client, assignment_id, fields)
    assert submitted.status_code == 202, submitted.text[:600]
    submission_id = submitted.json()["submission_id"]
    assert seeded["real_worker"].process_next(pause=False) is True

    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        assert grade is not None
        # Pretend the old key awarded the whole explanation, and the teacher
        # had already judged one other question by hand.
        record = json.loads(grade.result_json)
        for question in record["exam"]["questions"]:
            if question["key"] == "c1":
                question["earned"] = question["points"]
                question["status"] = checker.CORRECT
        grade.score = 100
        grade.result_json = json.dumps(record, ensure_ascii=False)
        exam_manual.adjust(grade, "a1", points=0, teacher_id=seeded["teacher"])
        db.commit()
        preview = exam_llm_check.start(
            db, db.get(Assignment, assignment_id), [submission_id],
            teacher_id=seeded["teacher"],
        )
        preview_id = preview.id

    # The route starts a thread; the test runs the same work directly. With the
    # model off this is a deterministic-only ("off") preview, still applicable.
    assert exam_llm_check.run(app.state.session_factory, preview_id) == exam_llm_check.READY

    login(client, "exam-mori", TEACHER_PASSWORD)
    base = f"/teacher/assignments/{assignment_id}/llm-check"
    page = client.get(base)
    assert page.status_code == 200, page.text[:600]
    text = html.unescape(page.text)
    assert "100" in text and "ניקוד ידני נשמר" in text

    token = csrf(client, base)
    applied = client.post(
        f"{base}/apply",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert applied.status_code == 303
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        record = json.loads(grade.result_json)
        questions = {q["key"]: q for q in record["exam"]["questions"]}
        # The corrected explanation lost its extra point...
        assert questions["c1"]["earned"] < questions["c1"]["points"]
        # ...while the teacher's manual zero on a1 is still zero.
        assert questions["a1"]["earned"] == 0
        assert questions["a1"]["manual"]["points"] == 0
        assert record["score"] < 100
        assert db.query(ExamRecheckPreview).count() == 0


def test_leaked_free_text_answer_matches_the_rendered_document(app, seeded) -> None:
    """A perfect hand-in composes a document that names every question once."""
    config = exam_config.load_exam(EXAM)
    with _db(app) as db:
        assignment = db.get(Assignment, seeded["assignment"])
        student = db.get(User, seeded["students"][0])
        paper = exam_service.student_paper(db, assignment, student, config)
    answers = exam_tools._perfect_answers(
        config,
        paper.version,
        checker.version_rules(checker.load_checks(config), paper.version.key),
    )
    document = paper.display.compose(answers)
    headings = re.findall(r"^## שאלה (\d+):", document, flags=re.MULTILINE)
    assert headings == [str(number) for number in range(1, 12)]


# --------------------------------------------------------------- exam window


def _teacher_login(client) -> None:
    response = login(client, "exam-mori", TEACHER_PASSWORD)
    assert response.status_code == 303, response.text


def test_teacher_moves_the_exam_window_from_the_class_page(
    app, client, seeded
) -> None:
    """The window is movable without unpublishing the exam mid-lesson."""
    _teacher_login(client)
    class_id = seeded["class"]
    assignment_id = seeded["assignment"]
    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/assignments/{assignment_id}/exam-window",
        data={
            "csrf_token": token,
            "opens_at": "2026-11-11T08:00",
            "closes_at": "2026-11-11T10:30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:600]
    assert response.headers["location"] == f"/teacher/classes/{class_id}"

    zone = app.state.portal_settings.timezone
    with _db(app) as db:
        assignment = db.get(Assignment, assignment_id)
        assert assignment.opens_at == parse_local_datetime(
            "2026-11-11T08:00", zone
        )
        assert assignment.closes_at == parse_local_datetime(
            "2026-11-11T10:30", zone
        )
        # The class keeps the exam: same title, same paper, still published.
        assert assignment.published is True
        assert assignment.exam_config == EXAM

    page = html.unescape(client.get(f"/teacher/classes/{class_id}").text)
    assert "חלון ההגשה של" in page and "עודכן" in page
    # The class page reports the times in the school's zone, 08:00 and 10:30,
    # and the editor is pre-filled with exactly those values.
    assert "11/11/2026 08:00" in page
    assert "11/11/2026 10:30" in page
    assert f"/teacher/assignments/{assignment_id}/exam-window" in page
    assert "שמירת החלון" in page
    assert 'value="2026-11-11T08:00"' in page
    assert 'value="2026-11-11T10:30"' in page


def test_exam_window_rejects_incomplete_or_impossible_values(
    app, client, seeded
) -> None:
    _teacher_login(client)
    class_id = seeded["class"]
    assignment_id = seeded["assignment"]
    url = f"/teacher/assignments/{assignment_id}/exam-window"
    with _db(app) as db:
        assignment = db.get(Assignment, assignment_id)
        original = (assignment.opens_at, assignment.closes_at)

    cases = [
        ("2026-11-11T12:00", "2026-11-11T11:00", "מועד הסגירה חייב להיות אחרי מועד הפתיחה"),
        ("2026-11-11T12:00", "2026-11-11T12:00", "מועד הסגירה חייב להיות אחרי מועד הפתיחה"),
        ("2026-11-11T08:00", "", "גם מועד פתיחה וגם מועד סגירה"),
        ("", "2026-11-11T10:30", "גם מועד פתיחה וגם מועד סגירה"),
        ("11/11/2026 08:00", "2026-11-11T10:30", "שעת הפתיחה או הסגירה אינה תקינה"),
    ]
    for opens, closes, message in cases:
        token = csrf(client, f"/teacher/classes/{class_id}")
        response = client.post(
            url,
            data={"csrf_token": token, "opens_at": opens, "closes_at": closes},
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = html.unescape(client.get(f"/teacher/classes/{class_id}").text)
        assert message in page, (opens, closes)
        with _db(app) as db:
            assignment = db.get(Assignment, assignment_id)
            # A rejected edit never moves half of the window.
            assert (assignment.opens_at, assignment.closes_at) == original


def test_exam_window_refuses_an_ordinary_assignment(app, client, seeded) -> None:
    """Homework has a deadline, not a sitting window."""
    with _db(app) as db:
        homework = Assignment(
            class_id=seeded["class"],
            title="3.1 | תרגול",
            instructions="הגישו את הקוד.",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="",
            published=True,
        )
        db.add(homework)
        db.commit()
        homework_id = homework.id

    _teacher_login(client)
    token = csrf(client, f"/teacher/classes/{seeded['class']}")
    response = client.post(
        f"/teacher/assignments/{homework_id}/exam-window",
        data={
            "csrf_token": token,
            "opens_at": "2026-11-11T08:00",
            "closes_at": "2026-11-11T10:30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _db(app) as db:
        homework = db.get(Assignment, homework_id)
        assert homework.opens_at is None and homework.closes_at is None
    page = html.unescape(client.get(f"/teacher/classes/{seeded['class']}").text)
    assert "אפשר לקבוע חלון הגשה רק למטלה שהיא מבחן" in page


# ------------------------------------------------------- personal make-up window


def _local_input(value: datetime, zone: str) -> str:
    return format_local(value, zone, "%Y-%m-%dT%H:%M")


def _grant(app, assignment_id: int, student_id: int, opens_at, closes_at) -> None:
    with _db(app) as db:
        db.add(
            ExamStudentWindow(
                assignment_id=assignment_id,
                student_id=student_id,
                opens_at=opens_at,
                closes_at=closes_at,
            )
        )
        db.commit()


def _close_the_class_window(app, assignment_id: int) -> None:
    with _db(app) as db:
        assignment = db.get(Assignment, assignment_id)
        assignment.opens_at = utcnow() - timedelta(hours=3)
        assignment.closes_at = utcnow() - timedelta(hours=1)
        db.commit()


def test_personal_window_overrides_the_class_window_for_one_student(
    app, seeded
) -> None:
    """The grant replaces both ends for its student and nobody else."""
    now = utcnow()
    _close_the_class_window(app, seeded["assignment"])
    first, second = seeded["students"][0], seeded["students"][1]
    _grant(
        app,
        seeded["assignment"],
        first,
        now - timedelta(minutes=5),
        now + timedelta(minutes=55),
    )
    with _db(app) as db:
        assignment = db.get(Assignment, seeded["assignment"])
        one = db.get(User, first)
        other = db.get(User, second)
        assert exam_service.window_state(assignment) == exam_service.CLOSED
        window = exam_service.student_window(db, assignment, one)
        assert window.personal is True
        assert exam_service.student_window_state(db, assignment, one) == (
            exam_service.OPEN
        )
        # A classmate keeps the class window, closed.
        assert exam_service.student_window(db, assignment, other).personal is False
        assert exam_service.student_window_state(db, assignment, other) == (
            exam_service.CLOSED
        )


def test_teacher_grants_a_make_up_window_from_the_class_page(
    app, client, seeded
) -> None:
    zone = app.state.portal_settings.timezone
    now = utcnow()
    closes = parse_local_datetime(_local_input(now + timedelta(minutes=45), zone), zone)
    _close_the_class_window(app, seeded["assignment"])
    first = seeded["students"][0]

    _teacher_login(client)
    token = csrf(client, f"/teacher/classes/{seeded['class']}")
    response = client.post(
        f"/teacher/assignments/{seeded['assignment']}/exam-window/student",
        data={
            "csrf_token": token,
            "student_id": str(first),
            "opens_at": _local_input(now - timedelta(minutes=5), zone),
            "closes_at": _local_input(now + timedelta(minutes=45), zone),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:600]
    with _db(app) as db:
        grant = db.query(ExamStudentWindow).filter_by(
            assignment_id=seeded["assignment"], student_id=first
        ).one()
        assert grant.opens_at < now < grant.closes_at
        # The class window is untouched by a personal grant.
        assignment = db.get(Assignment, seeded["assignment"])
        assert assignment.closes_at < now

    page = html.unescape(client.get(f"/teacher/classes/{seeded['class']}").text)
    assert "נקבע מועד אישי" in page
    assert "noa" in page
    assert (
        f"/teacher/assignments/{seeded['assignment']}"
        f"/exam-window/student/{first}/clear"
    ) in page

    # The granted student gets the paper; the countdown uses the personal end.
    _login(client, STUDENTS[0])
    paper = _page(client, seeded["assignment"])
    assert "פרטי המבחן" in paper
    assert (
        f'data-deadline="{closes.strftime("%Y-%m-%dT%H:%M:%SZ")}"' in paper
    )

    # A classmate still sees the closed gate, with no hint of the grant.
    _login(client, STUDENTS[1])
    gate = html.unescape(_page(client, seeded["assignment"]))
    assert "נסגר" in gate
    assert "data-worksheet-item" not in gate
    assert "מועד אישי" not in gate


def test_teacher_grants_one_make_up_window_to_several_students_at_once(
    app, client, seeded
) -> None:
    """Ticking several students gives them all the same window in one save."""
    zone = app.state.portal_settings.timezone
    now = utcnow()
    _close_the_class_window(app, seeded["assignment"])
    group = seeded["students"][:3]
    outsider = seeded["students"][3]

    _teacher_login(client)
    token = csrf(client, f"/teacher/classes/{seeded['class']}")
    response = client.post(
        f"/teacher/assignments/{seeded['assignment']}/exam-window/student",
        data={
            "csrf_token": token,
            "student_ids": [str(sid) for sid in group],
            "opens_at": _local_input(now - timedelta(minutes=5), zone),
            "closes_at": _local_input(now + timedelta(minutes=45), zone),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:600]
    with _db(app) as db:
        grants = db.query(ExamStudentWindow).filter_by(
            assignment_id=seeded["assignment"]
        ).all()
        assert {g.student_id for g in grants} == set(group)
        # One shared window, not three different ones.
        assert len({(g.opens_at, g.closes_at) for g in grants}) == 1
        assert grants[0].opens_at < now < grants[0].closes_at
        # The class window is untouched by the group grant.
        assignment = db.get(Assignment, seeded["assignment"])
        assert assignment.closes_at < now
        for sid in group:
            student = db.get(User, sid)
            assert exam_service.student_window_state(
                db, assignment, student
            ) == exam_service.OPEN
        other = db.get(User, outsider)
        assert exam_service.student_window_state(
            db, assignment, other
        ) == exam_service.CLOSED

    page = html.unescape(client.get(f"/teacher/classes/{seeded['class']}").text)
    assert "נקבע מועד אישי ל־3 תלמידים" in page
    # The picker is a tick list with a master checkbox, and the students who
    # already hold a grant are marked so a second group save is informed.
    assert 'name="student_ids"' in page
    assert 'data-check-all="student_ids"' in page
    assert "כבר נקבע מועד" in page


def test_personal_window_group_save_updates_existing_and_rejects_bad_groups(
    app, client, seeded
) -> None:
    zone = app.state.portal_settings.timezone
    now = utcnow()
    url = f"/teacher/assignments/{seeded['assignment']}/exam-window/student"
    class_page = f"/teacher/classes/{seeded['class']}"
    first, second = seeded["students"][:2]
    old_open = now - timedelta(hours=2)
    old_close = now - timedelta(hours=1)
    _grant(app, seeded["assignment"], first, old_open, old_close)
    _teacher_login(client)

    def post(ids):
        token = csrf(client, class_page)
        return client.post(
            url,
            data={
                "csrf_token": token,
                "student_ids": ids,
                "opens_at": _local_input(now - timedelta(minutes=5), zone),
                "closes_at": _local_input(now + timedelta(minutes=45), zone),
            },
            follow_redirects=False,
        )

    # An empty tick list saves nothing.
    assert post([]).status_code == 303
    page = html.unescape(client.get(class_page).text)
    assert "סמנו לפחות תלמיד/ה אחד/ת" in page
    with _db(app) as db:
        assert db.query(ExamStudentWindow).count() == 1

    # One invalid id rejects the WHOLE group - no partial grants.
    assert post([str(second), str(seeded["teacher"])]).status_code == 303
    page = html.unescape(client.get(class_page).text)
    assert "בחרו תלמיד/ה שרשומים בכיתה" in page
    with _db(app) as db:
        grants = db.query(ExamStudentWindow).all()
        assert len(grants) == 1
        assert grants[0].student_id == first
        assert grants[0].opens_at == old_open

    # Duplicates collapse, an existing grant is updated in place, and the
    # newly ticked classmate gets the same window.
    assert post([str(first), str(first), str(second)]).status_code == 303
    page = html.unescape(client.get(class_page).text)
    assert "נקבע מועד אישי ל־2 תלמידים" in page
    with _db(app) as db:
        grants = db.query(ExamStudentWindow).filter_by(
            assignment_id=seeded["assignment"]
        ).all()
        assert {g.student_id for g in grants} == {first, second}
        assert len({(g.opens_at, g.closes_at) for g in grants}) == 1
        assert grants[0].opens_at > old_open


def test_make_up_window_accepts_a_hand_in_after_the_class_window_closed(
    app, client, seeded
) -> None:
    now = utcnow()
    _close_the_class_window(app, seeded["assignment"])
    _grant(
        app,
        seeded["assignment"],
        seeded["students"][0],
        now - timedelta(minutes=5),
        now + timedelta(minutes=55),
    )
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])  # allocates the paper
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    fields = _perfect_fields(allocation.version)

    response = _submit(client, seeded["assignment"], fields)
    assert response.status_code == 202, response.text[:600]
    assert response.json()["accepted"] is True

    # The same hand-in from a classmate without a grant is refused.
    _login(client, STUDENTS[1])
    refused = _submit(client, seeded["assignment"], fields)
    assert refused.status_code == 403
    assert "נסגר" in refused.json()["error"]


def test_personal_window_rejects_bad_input_and_clears_to_the_class_window(
    app, client, seeded
) -> None:
    zone = app.state.portal_settings.timezone
    now = utcnow()
    url = f"/teacher/assignments/{seeded['assignment']}/exam-window/student"
    _teacher_login(client)
    class_page = f"/teacher/classes/{seeded['class']}"

    def post(**fields):
        token = csrf(client, class_page)
        return client.post(
            url,
            data={"csrf_token": token, **fields},
            follow_redirects=False,
        )

    cases = [
        (
            {
                "student_id": str(seeded["students"][0]),
                "opens_at": _local_input(now + timedelta(hours=2), zone),
                "closes_at": _local_input(now + timedelta(hours=1), zone),
            },
            "מועד הסגירה חייב להיות אחרי מועד הפתיחה",
        ),
        (
            {
                "student_id": str(seeded["students"][0]),
                "opens_at": _local_input(now, zone),
                "closes_at": "",
            },
            "גם שעת פתיחה וגם שעת סגירה",
        ),
        (
            {
                "student_id": str(seeded["teacher"]),
                "opens_at": _local_input(now, zone),
                "closes_at": _local_input(now + timedelta(hours=1), zone),
            },
            "בחרו תלמיד/ה שרשומים בכיתה",
        ),
    ]
    for data, message in cases:
        assert post(**data).status_code == 303
        page = html.unescape(client.get(class_page).text)
        assert message in page, data
        with _db(app) as db:
            assert db.query(ExamStudentWindow).count() == 0

    _grant(
        app,
        seeded["assignment"],
        seeded["students"][0],
        now - timedelta(minutes=5),
        now + timedelta(minutes=55),
    )
    token = csrf(client, class_page)
    cleared = client.post(
        f"{url}/{seeded['students'][0]}/clear",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    page = html.unescape(client.get(class_page).text)
    assert "המועד האישי הוסר" in page
    with _db(app) as db:
        assert db.query(ExamStudentWindow).count() == 0
        assignment = db.get(Assignment, seeded["assignment"])
        student = db.get(User, seeded["students"][0])
        assert exam_service.student_window(db, assignment, student).personal is False


def test_personal_window_is_refused_for_an_ordinary_assignment(
    app, client, seeded
) -> None:
    zone = app.state.portal_settings.timezone
    now = utcnow()
    with _db(app) as db:
        homework = Assignment(
            class_id=seeded["class"],
            title="3.1 | תרגול",
            instructions="הגישו את הקוד.",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="",
            published=True,
        )
        db.add(homework)
        db.commit()
        homework_id = homework.id

    _teacher_login(client)
    token = csrf(client, f"/teacher/classes/{seeded['class']}")
    response = client.post(
        f"/teacher/assignments/{homework_id}/exam-window/student",
        data={
            "csrf_token": token,
            "student_id": str(seeded["students"][0]),
            "opens_at": _local_input(now, zone),
            "closes_at": _local_input(now + timedelta(hours=1), zone),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = html.unescape(client.get(f"/teacher/classes/{seeded['class']}").text)
    assert "אפשר לקבוע מועד אישי רק למטלה שהיא מבחן" in page
    with _db(app) as db:
        assert db.query(ExamStudentWindow).count() == 0


# ------------------------------------------------------------ student clock


def test_open_exam_shows_a_countdown_to_the_servers_closing_time(
    app, client, seeded
) -> None:
    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    with _db(app) as db:
        closes = db.get(Assignment, seeded["assignment"]).closes_at
    assert "data-exam-clock" in page
    assert "נותר זמן למבחן" in page
    # The browser counts down to the stored UTC instant, so a device clock set
    # to the wrong hour cannot buy extra time.
    assert f'data-deadline="{closes.strftime("%Y-%m-%dT%H:%M:%SZ")}"' in page
    # The server's own clock is embedded too, so a fast phone cannot hand the
    # paper in early or a slow one think it has minutes to spare.
    match = re.search(r'data-server-now="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z"', page)
    assert match is not None
    rendered = datetime.fromisoformat(match.group(1))
    assert abs((utcnow() - rendered).total_seconds()) < 120
    # The student is told the paper is sent for them at the end.
    assert "ההגשה נשלחת אוטומטית בסוף הזמן" in page
    assert "יישלח אוטומטית" in html.unescape(page)

    # The script and stylesheet that drive the clock are actually served.
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "[data-exam-countdown]" in script.text
    # At the deadline the page sends itself; it never locks the button and
    # leaves the student sitting on an open paper (task 79).
    assert "form.requestSubmit()" in script.text
    assert "submit.disabled" not in script.text
    sheet = client.get("/static/style.css")
    assert sheet.status_code == 200
    assert ".exam-clock" in sheet.text


def test_exam_clock_is_shown_only_while_the_paper_is_open(
    app, client, seeded
) -> None:
    _login(client, STUDENTS[0])
    # Before the opening instant there is nothing to count down and no paper.
    with _db(app) as db:
        assignment = db.get(Assignment, seeded["assignment"])
        assignment.opens_at = utcnow() + timedelta(hours=1)
        db.commit()
    assert "data-exam-clock" not in _page(client, seeded["assignment"])

    with _db(app) as db:
        assignment = db.get(Assignment, seeded["assignment"])
        assignment.opens_at = utcnow() - timedelta(hours=1)
        db.commit()
    page = _page(client, seeded["assignment"])
    assert "data-exam-clock" in page

    # Once handed in the paper is read-only, so the clock goes away.
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    submitted = _submit(client, seeded["assignment"], _perfect_fields(allocation.version))
    assert submitted.status_code == 202, submitted.text[:600]
    page = _page(client, seeded["assignment"])
    assert "data-exam-clock" not in page
    assert "אי אפשר לשנות" in page


# --------------------------------------------------------------- manual points


def _typed_field(version_key: str, item_key: str, part_key: str) -> str:
    paper = load_paper(exam_config.load_exam(EXAM).version(version_key))
    item = next(item for item in paper.items if item.key == item_key)
    part = next(part for part in item.parts if part.key == part_key)
    return part.value_name(item.key)


def _stored_grade(app, submission_id: int):
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        result = json.loads(grade.result_json)
        return grade.score, result, grade.review_note


def test_teacher_overrides_one_question_and_can_restore_it(
    client, app, seeded
) -> None:
    """A typo in copied output costs points; the teacher can award them back."""
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    fields = _perfect_fields(allocation.version)
    rules = checker.version_rules(
        checker.load_checks(exam_config.load_exam(EXAM)), allocation.version
    )
    tracing = rules["b2"]["checks"][0]
    entries = tracing["lines"]
    expected = [str(entry["expected"]) for entry in entries]
    # Corrupt exactly one expected line, so the automatic score loses only that
    # line's share whatever version this student drew.
    fields[_typed_field(allocation.version, "b2", "response")] = "\n".join(
        [expected[0] + "?"] + expected[1:]
    )
    lost = int(entries[0]["points"])
    auto_score = 100 - lost

    submitted = _submit(client, seeded["assignment"], fields)
    assert submitted.status_code == 202, submitted.text[:600]
    submission_id = submitted.json()["submission_id"]
    assert seeded["real_worker"].process_next(pause=False) is True

    score, result, note = _stored_grade(app, submission_id)
    assert score == auto_score
    b2 = next(q for q in result["exam"]["questions"] if q["key"] == "b2")
    assert b2["earned"] == int(tracing["points"]) - lost

    login(client, "exam-mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/submissions/{submission_id}")
    assert page.status_code == 200, page.text[:600]
    assert "/questions/b2/adjust" in page.text
    assert "ניקוד מלא" in page.text

    token = csrf(client, f"/teacher/submissions/{submission_id}")
    adjusted = client.post(
        f"/teacher/submissions/{submission_id}/questions/b2/adjust",
        data={"csrf_token": token, "points": "12", "note": "שגיאת הקלדה"},
        follow_redirects=False,
    )
    assert adjusted.status_code == 303

    score, result, note = _stored_grade(app, submission_id)
    assert score == 100
    b2 = next(q for q in result["exam"]["questions"] if q["key"] == "b2")
    assert b2["earned"] == 12
    assert b2["manual"]["auto_points"] == int(tracing["points"]) - lost
    assert "עדכון ניקוד ידני" in note and "שגיאת הקלדה" in note
    task = next(
        item
        for item in result["task_evaluations"]
        if item["task_name"] == b2["title"]
    )
    assert "עודכן ידנית" in task["notes"]

    # The student sees the corrected score, not the automatic one, as soon as
    # the teacher publishes it.
    _release(app, seeded["assignment"], exam_service.RELEASE_SCORES)
    _login(client, STUDENTS[0])
    assert 'aria-label="ציון 100"' in _page(client, seeded["assignment"])

    # The teacher page names the override and its note.
    login(client, "exam-mori", TEACHER_PASSWORD)
    text = html.unescape(
        client.get(f"/teacher/submissions/{submission_id}").text
    )
    assert "הותאם ידנית" in text
    assert "שגיאת הקלדה" in text

    # Points outside 0..12 are refused and change nothing.
    token = csrf(client, f"/teacher/submissions/{submission_id}")
    refused = client.post(
        f"/teacher/submissions/{submission_id}/questions/b2/adjust",
        data={"csrf_token": token, "points": "13"},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    score, _, _ = _stored_grade(app, submission_id)
    assert score == 100

    token = csrf(client, f"/teacher/submissions/{submission_id}")
    restored = client.post(
        f"/teacher/submissions/{submission_id}/questions/b2/reset",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert restored.status_code == 303
    score, result, note = _stored_grade(app, submission_id)
    assert score == auto_score
    b2 = next(q for q in result["exam"]["questions"] if q["key"] == "b2")
    assert "manual" not in b2
    assert "בוטלה" in note

    # The one-click action gives the question its full points.
    token = csrf(client, f"/teacher/submissions/{submission_id}")
    full = client.post(
        f"/teacher/submissions/{submission_id}/questions/b2/adjust",
        data={"csrf_token": token, "full": "1"},
        follow_redirects=False,
    )
    assert full.status_code == 303
    score, result, _ = _stored_grade(app, submission_id)
    assert score == 100
    b2 = next(q for q in result["exam"]["questions"] if q["key"] == "b2")
    assert b2["earned"] == int(tracing["points"])


# ------------------------------------------------------------- release states


def _marked_exam_submission(client, app, seeded, student_index: int):
    """Hand in a perfect sheet and let the real worker mark it."""
    _login(client, STUDENTS[student_index])
    _page(client, seeded["assignment"])
    allocation = _allocation(
        app, seeded["assignment"], seeded["students"][student_index]
    )
    submitted = _submit(
        client, seeded["assignment"], _perfect_fields(allocation.version)
    )
    assert submitted.status_code == 202, submitted.text[:600]
    assert seeded["real_worker"].process_next(pause=False) is True
    return submitted.json()["submission_id"]


def test_hand_in_confirms_but_keeps_the_marked_result_hidden(
    client, app, seeded
) -> None:
    submission_id = _marked_exam_submission(client, app, seeded, 0)

    page = _page(client, seeded["assignment"])
    assert "המבחן הוגש" in page
    assert "הציון והפירוט יפורסמו על ידי המורה" in page
    assert "data-worksheet-item" not in page
    assert "score-ring" not in page
    assert 'aria-label="ציון' not in page

    # The submission page is not a side door to the same answer.
    result_page = client.get(f"/student/submissions/{submission_id}").text
    assert "הציון טרם פורסם" in result_page
    assert "score-ring" not in result_page
    assert "הערכת המשימות" not in result_page
    assert "הקוד שהוגש" not in result_page

    # The teacher's class page offers all three states and starts at held.
    login(client, "exam-mori", TEACHER_PASSWORD)
    class_page = client.get(f"/teacher/classes/{seeded['class']}").text
    assert "פרסום תוצאות" in class_page
    assert "התוצאות מוסתרות" in class_page
    assert "פרסום ציונים בלבד" in class_page
    assert "פרסום מלא - ציון, פירוט ודף המבחן" in class_page


def test_teacher_releases_scores_only_then_full(client, app, seeded) -> None:
    submission_id = _marked_exam_submission(client, app, seeded, 0)
    login(client, "exam-mori", TEACHER_PASSWORD)

    released = _release_via_page(
        client, seeded["assignment"], exam_service.RELEASE_SCORES
    )
    assert released.status_code == 303, released.text[:600]
    with _db(app) as db:
        assert (
            db.get(Assignment, seeded["assignment"]).exam_release
            == exam_service.RELEASE_SCORES
        )
    class_page = client.get(f"/teacher/classes/{seeded['class']}").text
    assert "ציונים בלבד" in class_page

    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    assert 'aria-label="ציון 100"' in page
    assert "המורה פרסמה את הציון בלבד" in page
    # The number is out, the paper is still not.
    assert "data-worksheet-item" not in page
    result_page = client.get(f"/student/submissions/{submission_id}").text
    assert 'aria-label="ציון 100"' in result_page
    assert "המורה פרסמה את הציון בלבד" in result_page
    assert "הערכת המשימות" not in result_page
    assert "ניכוי נקודות" not in result_page
    assert "הקוד שהוגש" not in result_page

    login(client, "exam-mori", TEACHER_PASSWORD)
    released = _release_via_page(
        client, seeded["assignment"], exam_service.RELEASE_FULL
    )
    assert released.status_code == 303

    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    assert 'aria-label="ציון 100"' in page
    assert page.count("data-worksheet-item") == 11
    result_page = client.get(f"/student/submissions/{submission_id}").text
    assert "הערכת המשימות" in result_page
    assert "הקוד שהוגש" in result_page

    # Hiding again puts the confirmation back, paper included.
    login(client, "exam-mori", TEACHER_PASSWORD)
    _release_via_page(client, seeded["assignment"], exam_service.RELEASE_HELD)
    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    assert "המבחן הוגש" in page
    assert "data-worksheet-item" not in page
    assert "score-ring" not in page


def test_release_refuses_an_ordinary_assignment_and_unknown_states(
    client, app, seeded
) -> None:
    config = exam_config.load_exam(EXAM)
    with _db(app) as db:
        ordinary = Assignment(
            class_id=seeded["class"],
            title="מטלה רגילה",
            instructions="",
            rubric_path=str(config.versions[0].rubric_path),
            rubric_sha256="",
            published=True,
        )
        db.add(ordinary)
        db.commit()
        ordinary_id = ordinary.id

    login(client, "exam-mori", TEACHER_PASSWORD)
    refused = _release_via_page(client, ordinary_id, exam_service.RELEASE_FULL)
    assert refused.status_code == 303
    with _db(app) as db:
        assert db.get(Assignment, ordinary_id).exam_release == exam_service.RELEASE_HELD

    unknown = _release_via_page(client, seeded["assignment"], "leak")
    assert unknown.status_code == 303
    with _db(app) as db:
        assert (
            db.get(Assignment, seeded["assignment"]).exam_release
            == exam_service.RELEASE_HELD
        )

    # Only the owning teacher can change it.
    create_user(app, "other-teacher", TEACHER_PASSWORD, role=ROLE_TEACHER)
    login(client, "other-teacher", TEACHER_PASSWORD)
    denied = _release_via_page(
        client, seeded["assignment"], exam_service.RELEASE_FULL
    )
    assert denied.status_code == 404
    with _db(app) as db:
        assert (
            db.get(Assignment, seeded["assignment"]).exam_release
            == exam_service.RELEASE_HELD
        )


def test_student_lists_hide_a_held_exam_score(client, app, seeded) -> None:
    """The class and dashboard lists must not print the number as a label."""
    _marked_exam_submission(client, app, seeded, 0)

    with _db(app) as db:
        row = grading_service.student_status_map(
            db, seeded["students"][0], [seeded["assignment"]]
        )[seeded["assignment"]]
        assert row["label"] == "הוגש"
        assert row["score"] is None
        assert row["needs_review"] is False

    _release(app, seeded["assignment"], exam_service.RELEASE_SCORES)
    with _db(app) as db:
        row = grading_service.student_status_map(
            db, seeded["students"][0], [seeded["assignment"]]
        )[seeded["assignment"]]
        assert row["score"] == 100
        assert row["label"].endswith("100")

    # The rendered dashboard uses that map, so the held label is what a
    # student actually reads before a release.
    _release(app, seeded["assignment"], exam_service.RELEASE_HELD)
    _login(client, STUDENTS[0])
    dashboard = client.get("/student")
    assert dashboard.status_code == 200
    assert "הוגש" in dashboard.text
    assert "100" not in dashboard.text


# --------------------------------------- marks published, paper kept back


def _hide_exam(app, assignment_id: int) -> None:
    """The teacher's move between two sittings: the marks go out, the exam stays
    unpublished so the next class cannot open it."""
    with _db(app) as db:
        db.get(Assignment, assignment_id).published = False
        db.commit()


def test_a_hidden_exam_opens_only_for_the_children_who_sat_it(
    client, app, seeded
) -> None:
    _marked_exam_submission(client, app, seeded, 0)
    _hide_exam(app, seeded["assignment"])

    # Hidden *and* held: even the student who sat has no page and no row. The
    # release, not the hiding, is what publishes a mark.
    _login(client, STUDENTS[0])
    assert client.get(
        f"/student/assignments/{seeded['assignment']}"
    ).status_code == 404
    assert "3.9 | מבחן מסכם" not in client.get(
        f"/student/classes/{seeded['class']}"
    ).text

    login(client, "exam-mori", TEACHER_PASSWORD)
    released = _release_via_page(
        client, seeded["assignment"], exam_service.RELEASE_SCORES
    )
    assert released.status_code == 303, released.text[:600]

    # The teacher reads exactly who is about to see what.
    class_page = client.get(f"/teacher/classes/{seeded['class']}").text
    assert "ציונים פורסמו למי שנבחן" in class_page
    assert "המבחן מוסתר מהתלמידים" in class_page

    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    assert 'aria-label="ציון 100"' in page
    assert "המורה פרסמה את הציון בלבד" in page
    # The number is out; the paper still is not.
    assert "data-worksheet-item" not in page

    listing = client.get(f"/student/classes/{seeded['class']}").text
    assert "3.9 | מבחן מסכם" in listing
    assert "ציון 100" in listing

    # A student who has not sat it is refused every door, not just the page.
    _login(client, STUDENTS[1])
    assert client.get(
        f"/student/assignments/{seeded['assignment']}"
    ).status_code == 404
    assert client.get(
        f"/student/assignments/{seeded['assignment']}/grading-status"
    ).status_code == 404
    hidden_listing = client.get(f"/student/classes/{seeded['class']}").text
    assert "3.9 | מבחן מסכם" not in hidden_listing
    assert "3.9 | מבחן מסכם" not in client.get("/student").text


def test_a_hidden_unreleased_exams_result_page_is_not_a_side_door(
    client, app, seeded
) -> None:
    """A direct /student/submissions/{id} link must not leak a hidden exam.

    The assignment page already 404s for everyone while the marks are held,
    but the result page only checked ownership. Without the same
    ``visible_to_student`` gate, a student who handed in the exam could open
    the result URL directly and see the title, their submitted text and - once
    marked - the feedback, for an exam nobody can open. Releasing the marks
    reopens both doors, for exactly the children who sat it.
    """
    submission_id = _marked_exam_submission(client, app, seeded, 0)
    _hide_exam(app, seeded["assignment"])

    _login(client, STUDENTS[0])
    hidden = client.get(f"/student/submissions/{submission_id}")
    assert hidden.status_code == 404, hidden.text[:400]
    assert "המבחן הוגש" not in hidden.text
    assert "הקוד שהוגש" not in hidden.text
    assert client.get(
        f"/student/submissions/{submission_id}/grading-status"
    ).status_code == 404

    _release(app, seeded["assignment"], exam_service.RELEASE_SCORES)
    released = client.get(f"/student/submissions/{submission_id}")
    assert released.status_code == 200, released.text[:400]
    assert 'aria-label="ציון 100"' in released.text
    assert client.get(
        f"/student/submissions/{submission_id}/grading-status"
    ).status_code == 200

    # The published case is untouched: a result page for a published exam
    # still opens, released or not.
    _release(app, seeded["assignment"], exam_service.RELEASE_HELD)
    with _db(app) as db:
        db.get(Assignment, seeded["assignment"]).published = True
        db.commit()
    assert client.get(f"/student/submissions/{submission_id}").status_code == 200


def test_a_hidden_exam_never_accepts_a_hand_in(client, app, seeded) -> None:
    """Publishing the marks must not reopen the sitting for the student who
    can now read their result."""
    _marked_exam_submission(client, app, seeded, 0)
    _hide_exam(app, seeded["assignment"])
    _release(app, seeded["assignment"], exam_service.RELEASE_SCORES)

    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    assert 'aria-label="ציון 100"' in page

    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    refused = _submit(
        client, seeded["assignment"], _perfect_fields(allocation.version)
    )
    assert refused.status_code == 404, refused.text[:400]


def test_a_fully_released_hidden_exam_shows_the_sat_paper_read_only(
    client, app, seeded
) -> None:
    _marked_exam_submission(client, app, seeded, 0)
    _hide_exam(app, seeded["assignment"])
    _release(app, seeded["assignment"], exam_service.RELEASE_FULL)

    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    assert 'aria-label="ציון 100"' in page
    # Their own paper, as they sat it - and nobody else's page to sit it on.
    assert page.count("data-worksheet-item") == 11

    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    refused = _submit(
        client, seeded["assignment"], _perfect_fields(allocation.version)
    )
    assert refused.status_code == 404, refused.text[:400]


def test_a_released_hidden_exam_keeps_its_gradebook_column(
    client, app, seeded
) -> None:
    """Hiding the paper must not cost the teacher the marks they published."""
    _marked_exam_submission(client, app, seeded, 0)
    _hide_exam(app, seeded["assignment"])

    login(client, "exam-mori", TEACHER_PASSWORD)
    gradebook = client.get(f"/teacher/classes/{seeded['class']}/gradebook").text
    assert "3.9 | מבחן מסכם" not in gradebook

    _release_via_page(
        client, seeded["assignment"], exam_service.RELEASE_SCORES
    )
    gradebook = client.get(f"/teacher/classes/{seeded['class']}/gradebook").text
    assert "3.9 | מבחן מסכם" in gradebook


def test_hand_in_popup_is_wired_for_the_fetch_and_form_paths(
    client, app, seeded
) -> None:
    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    # The dialog markup rides along with the open paper.
    assert "data-exam-submitted-dialog" in page
    assert "המבחן הוגש" in page

    script = client.get("/static/app.js").text
    assert "showExamSubmittedDialog" in script
    assert 'response.status === 202' in script
    assert "submitted=1" in script
    assert "history.replaceState" in script
    sheet = client.get("/static/style.css").text
    assert ".exam-submitted-dialog" in sheet

    # A plain form post (no JavaScript hand-off) lands on the confirmation
    # page with the flag that opens the same dialog on load.
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")
    posted = client.post(
        f"/student/assignments/{seeded['assignment']}/submit",
        data={"csrf_token": token, **_perfect_fields(allocation.version)},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"].endswith("?submitted=1#submitted")
    page = _page(client, seeded["assignment"])
    assert "data-exam-submitted-dialog" in page


def test_teacher_review_shows_the_students_paper_with_a_score_box_each(
    client, app, seeded
) -> None:
    """The review page must show the questions, not just their titles."""
    submission_id = _marked_exam_submission(client, app, seeded, 0)
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    paper = paper_for_allocation(exam_config.load_exam(EXAM), allocation)
    display = paper.display

    login(client, "exam-mori", TEACHER_PASSWORD)
    response = client.get(f"/teacher/submissions/{submission_id}")
    assert response.status_code == 200, response.text[:600]
    page = response.text
    text = html.unescape(page)

    assert "דף המבחן והתשובות" in text
    assert paper.version.label in text
    assert page.count(
        'class="question-card worksheet-question exam-review-item"'
    ) == len(display.items)
    # Every item carries one point editor per scored part: a single-part item
    # keeps one box, a multi-part item (explanation + code) gets one box per
    # part and its question total is their read-only sum. The task list does not
    # repeat any editor, so the teacher cannot adjust the same part twice.
    def _editors(item) -> int:
        scored = [part for part in item.parts if not part.given]
        return len(scored) if len(scored) > 1 else 1

    expected_forms = sum(_editors(item) for item in display.items)
    assert page.count("manual-grade exam-question-score") == len(display.items)
    assert page.count("manual-grade-form") == expected_forms
    assert page.count(
        f"/teacher/submissions/{submission_id}/questions/"
    ) == expected_forms

    # The paper is in the student's order, and the answers are the stored ones.
    # The question-number span is the unique anchor: several tracing questions
    # share a title, and "שאלה 1" is a prefix of "שאלה 10".
    positions = [
        page.index(f'<span class="question-number">{item.label}</span>')
        for item in display.items
    ]
    assert positions == sorted(positions)

    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        values = worksheets.load_values(db, submission)
    sample = next(
        value.strip().splitlines()[0]
        for item in display.items
        for part in item.parts
        if not part.given
        for value in [values.get(item.key, {}).get(part.key)]
        if isinstance(value, str) and value.strip()
    )
    assert sample in text


# ------------------------------------------------- per-answer marking reasons


def test_every_part_keeps_its_own_points_and_reason() -> None:
    """The reason a part lost points travels with that part, not the paper."""
    report, questions = _graded("a", {"b2": {"response": "a = 19"}})
    record = report.to_record("נועה")

    # Every question carries one mark per answered part, in part order, and
    # the marks add up to the question's own number.
    for question in report.questions:
        stored = next(
            q for q in record["exam"]["questions"] if q["key"] == question.key
        )
        assert stored["parts"] == question.parts
        assert question.parts, question.key
        assert sum(mark["points"] for mark in question.parts) == question.points
        assert sum(mark["earned"] for mark in question.parts) == question.earned
        assert all(mark["label"] and mark["note"] for mark in question.parts)

    # The tracing answer lost exactly the second line, and says so.
    b2 = questions["b2"]
    assert len(b2.parts) == 1
    mark = b2.parts[0]
    assert mark["part"] == "response"
    assert (mark["points"], mark["earned"]) == (12, 6)
    assert "1 מתוך 2" in mark["note"]

    # An open question splits its points between the explanation and the code.
    c1 = questions["c1"]
    assert [mark["part"] for mark in c1.parts] == ["explain", "code"]
    assert all(mark["points"] == 5 and mark["earned"] == 5 for mark in c1.parts)

    # An explanation the keywords cannot place earns nothing and is flagged,
    # and the flag belongs to that part alone.
    _report, questions = _graded("a", {"c1": {"explain": "???"}})
    explain, code = questions["c1"].parts
    assert explain["earned"] == 0
    assert explain["needs_review"] is True
    assert code["earned"] == 5
    assert code["needs_review"] is False


def _item_section(page: str, label: str) -> str:
    """One question card of the review page, cut at the next question."""
    start = page.index(f'<span class="question-number">{label}</span>')
    following = page.find('<span class="question-number">', start + 1)
    return page[start : following if following != -1 else len(page)]


def test_teacher_sees_the_reason_under_each_answer_not_at_the_end(
    client, app, seeded
) -> None:
    """A lost point is explained under the answer that lost it."""
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    fields = _perfect_fields(allocation.version)
    rules = checker.version_rules(
        checker.load_checks(exam_config.load_exam(EXAM)), allocation.version
    )
    entries = rules["b2"]["checks"][0]["lines"]
    expected = [str(entry["expected"]) for entry in entries]
    fields[_typed_field(allocation.version, "b2", "response")] = "\n".join(
        [expected[0] + "?"] + expected[1:]
    )
    lost = int(entries[0]["points"])

    submitted = _submit(client, seeded["assignment"], fields)
    assert submitted.status_code == 202, submitted.text[:600]
    submission_id = submitted.json()["submission_id"]
    assert seeded["real_worker"].process_next(pause=False) is True

    score, result, _note = _stored_grade(app, submission_id)
    assert score == 100 - lost
    b2_record = next(q for q in result["exam"]["questions"] if q["key"] == "b2")
    reason = b2_record["parts"][0]["note"]

    paper = paper_for_allocation(exam_config.load_exam(EXAM), allocation)
    display = paper.display
    answered_parts = sum(
        1 for item in display.items for part in item.parts if not part.given
    )
    b2_label = next(item.label for item in display.items if item.key == "b2")

    login(client, "exam-mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/submissions/{submission_id}").text
    assert page.count("exam-part-mark-head") == answered_parts
    text = html.unescape(page)
    # The paper already carries every reason; the old aggregate lists would
    # only repeat them far away from the answers they belong to.
    assert "הערכת משימות" not in text
    assert "פירוט ניכויים" not in text

    # Under the tracing answer: its own points, the loss and the reason -
    # all before the question's point editor, inside the same card.
    section = html.unescape(_item_section(page, b2_label))
    assert f"{12 - lost} מתוך 12 נק׳" in section
    assert f"−{lost} נק׳" in section
    assert reason in section
    assert section.index("exam-part-mark") < section.index(
        "manual-grade exam-question-score"
    )
    # A full-credit answer says so too, without a loss badge.
    a1_label = next(item.label for item in display.items if item.key == "a1")
    a1_section = html.unescape(_item_section(page, a1_label))
    assert "exam-part-full" in a1_section
    assert "exam-part-delta" not in a1_section

    # A manual override changes the question's number, not the automatic
    # detail under each answer: the teacher still sees why points were lost.
    token = csrf(client, f"/teacher/submissions/{submission_id}")
    adjusted = client.post(
        f"/teacher/submissions/{submission_id}/questions/b2/adjust",
        data={"csrf_token": token, "full": "1"},
        follow_redirects=False,
    )
    assert adjusted.status_code == 303
    score, _result, _note = _stored_grade(app, submission_id)
    assert score == 100
    page = client.get(f"/teacher/submissions/{submission_id}").text
    assert page.count("exam-part-mark-head") == answered_parts
    section = html.unescape(_item_section(page, b2_label))
    assert reason in section
    assert "הותאם ידנית" in section


def test_papers_marked_before_per_part_marks_keep_the_question_note(
    client, app, seeded
) -> None:
    """Old records have one note per question; it moves under the question."""
    submission_id = _marked_exam_submission(client, app, seeded, 0)
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        grade = grading_service.grade_for_revision(
            db, submission.id, submission.revision
        )
        result = json.loads(grade.result_json)
        for question in result["exam"]["questions"]:
            question.pop("parts", None)
        grade.result_json = json.dumps(result, ensure_ascii=False)
        db.commit()
    # The question's note lives in the task list, written in the same order.
    notes = {
        question["key"]: task["notes"]
        for question, task in zip(
            result["exam"]["questions"], result["task_evaluations"]
        )
    }

    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    paper = paper_for_allocation(exam_config.load_exam(EXAM), allocation)
    login(client, "exam-mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/submissions/{submission_id}").text
    text = html.unescape(page)
    assert "פירוט הניקוד" in text
    assert page.count("exam-question-note") == len(paper.display.items)
    assert "הערכת משימות" not in text
    for item in paper.display.items:
        section = html.unescape(_item_section(page, item.label))
        assert notes[item.key] in section


# ------------------------------------------------- reopen an accidental hand-in


def _b2_typo_fields(version_key: str):
    """A perfect sheet with one tracing line corrupted, and the points it costs."""
    fields = _perfect_fields(version_key)
    rules = checker.version_rules(
        checker.load_checks(exam_config.load_exam(EXAM)), version_key
    )
    tracing = rules["b2"]["checks"][0]
    expected = [str(entry["expected"]) for entry in tracing["lines"]]
    fields[_typed_field(version_key, "b2", "response")] = "\n".join(
        [expected[0] + "?"] + expected[1:]
    )
    return fields, int(tracing["lines"][0]["points"])


def _reopen(client, submission_id: int):
    token = csrf(client, f"/teacher/submissions/{submission_id}")
    return client.post(
        f"/teacher/submissions/{submission_id}/reopen",
        data={"csrf_token": token},
        follow_redirects=False,
    )


def test_reopen_suspends_the_attempt_and_restores_the_answers(
    client, app, seeded
) -> None:
    """An accidental hand-in can be continued: answers kept, attempt suspended."""
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    fields, lost = _b2_typo_fields(allocation.version)
    submitted = _submit(client, seeded["assignment"], fields)
    assert submitted.status_code == 202, submitted.text[:600]
    submission_id = submitted.json()["submission_id"]
    assert seeded["real_worker"].process_next(pause=False) is True
    auto_score = 100 - lost
    score, _, _ = _stored_grade(app, submission_id)
    assert score == auto_score

    # The teacher lets this one student continue.
    login(client, "exam-mori", TEACHER_PASSWORD)
    assert "אפשר המשך מבחן" in client.get(
        f"/teacher/submissions/{submission_id}"
    ).text
    assert _reopen(client, submission_id).status_code == 303

    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "reopened"
        assert submission.revision == 2
        # The accidental hand-in stays in history at the old revision; the
        # continuation revision has no grade yet.
        old = grading_service.grade_for_revision(db, submission_id, 1)
        assert old is not None and old.score == auto_score
        assert grading_service.grade_for_revision(db, submission_id, 2) is None
        # The student's structured answers are restored as an editable draft.
        draft = drafts.load(db, seeded["assignment"], seeded["students"][0])
        assert draft is not None and draft.kind == "worksheet"
        assert draft.values == worksheets.load_values(db, submission, revision=1)
        assert exam_service.attempt_taken(submission) is False

    # The teacher review keeps showing the preserved hand-in, read-only.
    page = client.get(f"/teacher/submissions/{submission_id}").text
    text = html.unescape(page)
    assert "המבחן פתוח מחדש להמשך" in text
    assert "זהו הציון של ההגשה הקודמת" in text
    assert f'aria-label="ציון {auto_score}"' in page
    assert page.count("exam-review-item") == 11      # previous answers still shown
    assert "manual-grade-form" not in page           # nothing to adjust yet
    assert "/regrade" not in page                    # no regrade of an empty slot
    assert "/reopen" not in page                     # cannot reopen twice

    # The student gets an editable paper with their own answers back.
    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    assert page.count("data-worksheet-item") == 11
    assert (
        f'action="/student/assignments/{seeded["assignment"]}/submit"' in page
    )
    assert "אי אפשר לשנות אותה או להגיש שוב" not in page
    assert "שחזרנו את הטיוטה" in html.unescape(page)
    restored = fields[_typed_field(allocation.version, "b2", "response")]
    assert html.escape(restored.splitlines()[0]) in page


def test_continuing_after_reopen_relocks_and_replaces_the_grade(
    client, app, seeded
) -> None:
    """The continuation fills the opened revision, is marked, and locks again."""
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    fields, lost = _b2_typo_fields(allocation.version)
    submitted = _submit(client, seeded["assignment"], fields)
    submission_id = submitted.json()["submission_id"]
    assert seeded["real_worker"].process_next(pause=False) is True
    assert _stored_grade(app, submission_id)[0] == 100 - lost

    login(client, "exam-mori", TEACHER_PASSWORD)
    assert _reopen(client, submission_id).status_code == 303

    # The student fixes the typo and hands in again.
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    again = _submit(
        client, seeded["assignment"], _perfect_fields(allocation.version)
    )
    assert again.status_code == 202, again.text[:600]
    body = again.json()
    assert body["submission_id"] == submission_id
    assert body["revision"] == 2          # the continuation, not a third revision
    assert body["status"] == "queued"
    assert seeded["real_worker"].process_next(pause=False) is True

    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "graded"
        assert submission.revision == 2
        # The new grade replaces the old one as the current grade; the accidental
        # hand-in remains in history at revision 1.
        assert grading_service.current_grade(db, submission).score == 100
        assert grading_service.grade_for_revision(db, submission_id, 1).score == (
            100 - lost
        )
        # The draft is gone once the hand-in is the source of truth.
        assert drafts.load(db, seeded["assignment"], seeded["students"][0]) is None

    # The paper is locked again: a further hand-in is refused.
    third = _submit(
        client, seeded["assignment"], _perfect_fields(allocation.version)
    )
    assert third.status_code == 409
    page = _page(client, seeded["assignment"])
    assert "המבחן הוגש" in html.unescape(page)        # back to the confirmation
    assert f'action="/student/assignments/{seeded["assignment"]}/submit"' not in page

    # The teacher review is editable again and shows the new current grade.
    login(client, "exam-mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/submissions/{submission_id}").text
    assert 'aria-label="ציון 100"' in page
    # 9 single-part questions keep one box; c1 and c2 grade two parts each.
    assert page.count("manual-grade-form") == 13
    assert "המבחן פתוח מחדש להמשך" not in html.unescape(page)


def test_reopen_continuation_with_identical_answers_still_relocks(
    client, app, seeded
) -> None:
    """Handing in the same answers again must still count and lock the exam."""
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    fields = _perfect_fields(allocation.version)
    submitted = _submit(client, seeded["assignment"], fields)
    submission_id = submitted.json()["submission_id"]
    assert seeded["real_worker"].process_next(pause=False) is True

    login(client, "exam-mori", TEACHER_PASSWORD)
    assert _reopen(client, submission_id).status_code == 303

    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    again = _submit(client, seeded["assignment"], fields)   # identical text
    assert again.status_code == 202, again.text[:600]
    assert again.json()["revision"] == 2
    assert seeded["real_worker"].process_next(pause=False) is True
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "graded"
        assert grading_service.current_grade(db, submission).score == 100
    assert _submit(client, seeded["assignment"], fields).status_code == 409


def test_reopen_does_not_override_the_closed_window(client, app, seeded) -> None:
    """Reopening lets the student continue only while their window is open."""
    submission_id = _marked_exam_submission(client, app, seeded, 0)
    login(client, "exam-mori", TEACHER_PASSWORD)
    assert _reopen(client, submission_id).status_code == 303

    # The class window closes after the reopen.
    _close_the_class_window(app, seeded["assignment"])
    _login(client, STUDENTS[0])
    page = _page(client, seeded["assignment"])
    assert "data-worksheet-item" not in page          # the gate, not the paper
    assert f'action="/student/assignments/{seeded["assignment"]}/submit"' not in page
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    refused = _submit(
        client, seeded["assignment"], _perfect_fields(allocation.version)
    )
    assert refused.status_code == 403
    assert "נסגר" in refused.json()["error"]
    with _db(app) as db:
        assert db.get(Submission, submission_id).status == "reopened"

    # A personal make-up window re-opens the continuation for this student only.
    now = utcnow()
    _grant(app, seeded["assignment"], seeded["students"][0],
           now - timedelta(minutes=5), now + timedelta(hours=1))
    page = _page(client, seeded["assignment"])
    assert page.count("data-worksheet-item") == 11
    assert (
        f'action="/student/assignments/{seeded["assignment"]}/submit"' in page
    )


def test_reopen_refuses_non_exam_uncounted_and_double(client, app, seeded) -> None:
    """Only a counted exam hand-in can be reopened, and only once."""
    login(client, "exam-mori", TEACHER_PASSWORD)

    # Homework is not an exam: the action is refused and nothing changes.
    with _db(app) as db:
        homework = Assignment(
            class_id=seeded["class"],
            title="3.1 | תרגול",
            instructions="הגישו את הקוד.",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="",
            published=True,
        )
        db.add(homework)
        db.flush()
        now = utcnow()
        homework_submission = Submission(
            assignment_id=homework.id,
            student_id=seeded["students"][0],
            text="print(1)",
            revision=1,
            status="graded",
            first_submitted_at=now,
            submitted_at=now,
        )
        db.add(homework_submission)
        db.commit()
        homework_submission_id = homework_submission.id
    assert _reopen(client, homework_submission_id).status_code == 303
    page = html.unescape(
        client.get(f"/teacher/submissions/{homework_submission_id}").text
    )
    assert "אפשר לפתוח מחדש רק הגשה של מבחן" in page
    with _db(app) as db:
        assert db.get(Submission, homework_submission_id).status == "graded"

    # A failed check is not a counted attempt, so there is nothing to reopen.
    failed_id = _marked_exam_submission(client, app, seeded, 1)
    with _db(app) as db:
        db.get(Submission, failed_id).status = "failed"
        db.commit()
    login(client, "exam-mori", TEACHER_PASSWORD)
    assert _reopen(client, failed_id).status_code == 303
    page = html.unescape(client.get(f"/teacher/submissions/{failed_id}").text)
    assert "אין הגשה שנקלטה לפתוח מחדש" in page
    with _db(app) as db:
        assert db.get(Submission, failed_id).status == "failed"

    # A second reopen is a no-op: the revision does not advance again.
    counted_id = _marked_exam_submission(client, app, seeded, 2)
    login(client, "exam-mori", TEACHER_PASSWORD)
    assert _reopen(client, counted_id).status_code == 303
    assert _reopen(client, counted_id).status_code == 303
    page = html.unescape(client.get(f"/teacher/submissions/{counted_id}").text)
    assert "המבחן כבר פתוח מחדש" in page
    with _db(app) as db:
        submission = db.get(Submission, counted_id)
        assert submission.status == "reopened"
        assert submission.revision == 2


def test_reopen_while_a_job_is_in_flight_cannot_be_relocked_by_it(
    client, app, seeded
) -> None:
    """A job waiting on the old revision is superseded, not allowed to re-lock."""
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    allocation = _allocation(app, seeded["assignment"], seeded["students"][0])
    submitted = _submit(
        client, seeded["assignment"], _perfect_fields(allocation.version)
    )
    submission_id = submitted.json()["submission_id"]
    # The hand-in is queued but not yet marked when the teacher reopens.
    with _db(app) as db:
        assert db.get(Submission, submission_id).status == "queued"
        old_job = grading_service.latest_job(db, db.get(Submission, submission_id))
        old_job_id = old_job.id

    login(client, "exam-mori", TEACHER_PASSWORD)
    assert _reopen(client, submission_id).status_code == 303

    # The waiting job runs now: it must supersede itself and leave the exam open.
    assert seeded["real_worker"].process_next(pause=False) is True
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "reopened"
        assert submission.revision == 2
        assert db.get(GradingJob, old_job_id).state == "superseded"
        assert grading_service.grade_for_revision(db, submission_id, 1) is None
        assert grading_service.grade_for_revision(db, submission_id, 2) is None

    # The student can still continue, and that hand-in is the one that grades.
    _login(client, STUDENTS[0])
    _page(client, seeded["assignment"])
    again = _submit(
        client, seeded["assignment"], _perfect_fields(allocation.version)
    )
    assert again.status_code == 202, again.text[:600]
    assert seeded["real_worker"].process_next(pause=False) is True
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "graded"
        assert grading_service.current_grade(db, submission).score == 100
