"""Phase 5 tests: grade matrix, CSV export and teacher-only access.

The fixture builds a small, fully-controlled class:

* a published assignment due 1 Nov and one due 5 Nov, plus a draft;
* a student with an old grade (revision 1) and a new one (revision 2);
* a queued late hand-in whose old grade must not leak;
* a failed hand-in and a student who never handed in;
* a late grade whose penalty is the only clue that it was late.

Nothing here calls the grading engine; grades are inserted directly, exactly
as the worker would persist them.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

import pytest
from sqlalchemy import select

from portal.gradebook import (
    CELL_FAILED,
    CELL_GRADED,
    CELL_MISSING,
    CELL_QUEUED,
    build_gradebook,
    build_student_profile,
)
from portal.gradebook.exporter import export_filename, gradebook_to_csv
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    SUBMISSION_QUEUED,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    Submission,
    User,
)
from portal.tests.helpers import create_user, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"
OTHER_PASSWORD = "OtherPass123"

A1_TITLE = "מטלה 1 – משתנים"
A2_TITLE = "מטלה 2 – לולאות"
DRAFT_TITLE = "טיוטה – לא לפרסום"


def _db(app):
    return app.state.session_factory()


def _seed(app):
    teacher_id = create_user(
        app,
        "mori",
        TEACHER_PASSWORD,
        role=ROLE_TEACHER,
        display_name="מורי כהן",
    )
    other_teacher_id = create_user(
        app,
        "other",
        OTHER_PASSWORD,
        role=ROLE_TEACHER,
        display_name="מורה אחר",
    )
    noa_id = create_user(
        app, "noa", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="נועה כהן"
    )
    amit_id = create_user(
        app, "amit", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="עמית לוי"
    )
    yael_id = create_user(
        app, "yael", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="יעל דוד"
    )
    dana_id = create_user(
        app, "dana", STUDENT_PASSWORD, role=ROLE_STUDENT, display_name="דנה מזרחי"
    )

    due_1 = datetime(2026, 11, 1, 12, 0)
    due_2 = datetime(2026, 11, 5, 12, 0)
    with _db(app) as db:
        group = ClassGroup(
            name="כיתה י׳ 1", code="GRD123", teacher_id=teacher_id
        )
        other_group = ClassGroup(
            name="כיתה אחרת", code="OTH123", teacher_id=other_teacher_id
        )
        db.add_all([group, other_group])
        db.flush()
        db.add_all(
            [
                Enrollment(class_id=group.id, student_id=noa_id),
                Enrollment(class_id=group.id, student_id=amit_id),
                Enrollment(class_id=group.id, student_id=yael_id),
                Enrollment(class_id=other_group.id, student_id=dana_id),
            ]
        )

        a1 = Assignment(
            class_id=group.id,
            title=A1_TITLE,
            instructions="כתבו תוכנית.",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="a" * 64,
            due_at=due_1,
            published=True,
        )
        a2 = Assignment(
            class_id=group.id,
            title=A2_TITLE,
            instructions="כתבו לולאה.",
            rubric_path="assignments/lesson_2.json",
            rubric_sha256="b" * 64,
            due_at=due_2,
            published=True,
        )
        draft = Assignment(
            class_id=group.id,
            title=DRAFT_TITLE,
            instructions="עדיין לא פורסם.",
            rubric_path="assignments/lesson_3.json",
            rubric_sha256="c" * 64,
            published=False,
        )
        db.add_all([a1, a2, draft])
        db.flush()

        noa_a1 = Submission(
            assignment_id=a1.id,
            student_id=noa_id,
            text="print('v2')",
            revision=2,
            status=SUBMISSION_GRADED,
            first_submitted_at=datetime(2026, 10, 30, 9, 0),
            submitted_at=datetime(2026, 10, 31, 9, 0),
        )
        noa_a2 = Submission(
            assignment_id=a2.id,
            student_id=noa_id,
            text="for i in range(3): print(i)",
            revision=1,
            status=SUBMISSION_QUEUED,
            # After the 5 Nov deadline: queued *and* late.
            first_submitted_at=datetime(2026, 11, 6, 9, 0),
            submitted_at=datetime(2026, 11, 6, 9, 0),
        )
        amit_a1 = Submission(
            assignment_id=a1.id,
            student_id=amit_id,
            text="print('x')",
            revision=1,
            status=SUBMISSION_FAILED,
            first_submitted_at=datetime(2026, 10, 31, 8, 0),
            submitted_at=datetime(2026, 10, 31, 8, 0),
        )
        amit_a2 = Submission(
            assignment_id=a2.id,
            student_id=amit_id,
            text="for i in range(2): print(i)",
            revision=1,
            status=SUBMISSION_GRADED,
            # Handed in on time; only the stored penalty reveals the lateness.
            first_submitted_at=datetime(2026, 11, 2, 8, 0),
            submitted_at=datetime(2026, 11, 2, 8, 0),
        )
        noa_draft = Submission(
            assignment_id=draft.id,
            student_id=noa_id,
            text="print('draft')",
            revision=1,
            status=SUBMISSION_GRADED,
            first_submitted_at=datetime(2026, 10, 30, 8, 0),
            submitted_at=datetime(2026, 10, 30, 8, 0),
        )
        db.add_all([noa_a1, noa_a2, amit_a1, amit_a2, noa_draft])
        db.flush()

        db.add_all(
            [
                # Old revision: must never appear in the matrix.
                Grade(
                    submission_id=noa_a1.id,
                    revision=1,
                    score=40,
                    feedback_hebrew="ציון ישן",
                    review_note="הערה ישנה",
                ),
                Grade(
                    submission_id=noa_a1.id,
                    revision=2,
                    score=90,
                    feedback_hebrew="עבודה מצוינת",
                    audit_needs_review=False,
                ),
                Grade(
                    submission_id=amit_a2.id,
                    revision=1,
                    score=70,
                    feedback_hebrew="כמעט שם",
                    late_penalty_points=5,
                    audit_needs_review=True,
                    review_note="לבדוק שוב",
                ),
                Grade(
                    submission_id=noa_draft.id,
                    revision=1,
                    score=100,
                    feedback_hebrew="טיוטה",
                ),
            ]
        )
        db.commit()
        return {
            "teacher": teacher_id,
            "other_teacher": other_teacher_id,
            "noa": noa_id,
            "amit": amit_id,
            "yael": yael_id,
            "dana": dana_id,
            "class": group.id,
            "other_class": other_group.id,
            "a1": a1.id,
            "a2": a2.id,
            "draft": draft.id,
        }


@pytest.fixture
def world(app, client):
    return _seed(app)


def _login(client, username: str, password: str):
    response = login(client, username, password)
    assert response.status_code == 303, response.text[:300]
    return response


def _gradebook(app, class_id: int):
    with _db(app) as db:
        group = db.get(ClassGroup, class_id)
        assert group is not None
        return build_gradebook(db, group)


# ------------------------------------------------------------------ matrix


def test_published_assignments_and_statuses(app, world):
    book = _gradebook(app, world["class"])

    assert [assignment.id for assignment in book.assignments] == [
        world["a1"],
        world["a2"],
    ]
    assert world["draft"] not in [a.id for a in book.assignments]

    rows = {row.student.id: row for row in book.rows}
    assert set(rows) == {world["noa"], world["amit"], world["yael"]}

    noa = rows[world["noa"]]
    assert [cell.status for cell in noa.cells] == [CELL_GRADED, CELL_QUEUED]
    # The current revision's grade (90) wins over the old 40.
    assert noa.cells[0].score == 90
    assert noa.cells[0].submission_id is not None
    assert noa.cells[1].score is None
    assert noa.cells[1].late is True
    assert noa.average == 90.0
    assert noa.graded_count == 1
    assert noa.submitted_count == 2
    assert noa.missing_count == 0
    assert noa.late_count == 1

    amit = rows[world["amit"]]
    assert [cell.status for cell in amit.cells] == [CELL_FAILED, CELL_GRADED]
    assert amit.cells[1].score == 70
    # The score says late even though the hand-in was before the deadline.
    assert amit.cells[1].late is True
    assert amit.cells[1].needs_review is True
    assert amit.cells[1].review_note == "לבדוק שוב"
    assert amit.average == 70.0
    assert amit.failed_count == 1
    assert amit.needs_review_count == 1

    yael = rows[world["yael"]]
    assert [cell.status for cell in yael.cells] == [CELL_MISSING, CELL_MISSING]
    assert all(cell.submission_id is None for cell in yael.cells)
    assert yael.average is None
    assert yael.missing_count == 2
    assert yael.late_count == 0


def test_class_and_assignment_statistics(app, world):
    book = _gradebook(app, world["class"])

    stats = book.stats
    assert stats.students == 3
    assert stats.assignments == 2
    assert stats.possible_submissions == 6
    assert stats.graded == 2
    assert stats.queued == 1
    assert stats.failed == 1
    assert stats.missing == 2
    assert stats.submitted == 4
    assert stats.late == 2
    assert stats.needs_review == 1
    assert stats.average == 80.0
    assert stats.completion_percent == 66.7

    by_id = {column.assignment.id: column for column in book.assignment_stats}
    a1 = by_id[world["a1"]]
    assert (a1.graded, a1.queued, a1.failed, a1.missing) == (1, 0, 1, 1)
    assert (a1.submitted, a1.late, a1.average) == (2, 0, 90.0)

    a2 = by_id[world["a2"]]
    assert (a2.graded, a2.queued, a2.failed, a2.missing) == (1, 1, 0, 1)
    assert (a2.submitted, a2.late, a2.needs_review, a2.average) == (2, 2, 1, 70.0)


def test_student_profile_spans_only_owned_classes(app, world):
    with _db(app) as db:
        teacher = db.get(User, world["teacher"])
        noa = db.get(User, world["noa"])
        profile = build_student_profile(db, teacher, noa)

    assert profile is not None
    assert [item.group.id for item in profile.classes] == [world["class"]]
    assert profile.average == 90.0
    assert profile.graded == 1
    assert profile.submitted == 2
    assert profile.late == 1
    assert [entry.assignment.id for entry in profile.classes[0].entries] == [
        world["a1"],
        world["a2"],
    ]
    assert profile.classes[0].entries[0].cell.feedback == "עבודה מצוינת"

    with _db(app) as db:
        other_teacher = db.get(User, world["other_teacher"])
        dana = db.get(User, world["dana"])
        assert build_student_profile(db, other_teacher, dana) is not None
        # The first teacher must not see a student from the other class.
        assert build_student_profile(db, other_teacher, noa) is None
        assert build_student_profile(db, teacher, dana) is None


# -------------------------------------------------------------------- CSV


def _csv_rows(payload: bytes):
    text = payload.decode("utf-8-sig")
    assert "\r\n" in text
    assert "\n" not in text.replace("\r\n", "")
    return list(csv.reader(io.StringIO(text)))


def test_csv_has_bom_crlf_and_hebrew(app, world):
    payload = gradebook_to_csv(_gradebook(app, world["class"]))

    assert payload.startswith(b"\xef\xbb\xbf")
    rows = _csv_rows(payload)
    header, *body = rows

    assert header[0] == "שם תלמיד"
    assert header[1] == "שם משתמש"
    assert A1_TITLE in header
    assert A2_TITLE in header
    assert DRAFT_TITLE not in header
    assert header[-1] == "הערות בדיקה"

    noa = next(row for row in body if row[0] == "נועה כהן")
    a1_col = header.index(A1_TITLE)
    a2_col = header.index(A2_TITLE)
    assert noa[a1_col] == "90"
    assert noa[a2_col] == "ממתין לבדיקה"
    assert noa[header.index("ממוצע")] == "90.0"
    assert noa[header.index("חסרים")] == "0"
    assert noa[header.index("באיחור")] == "1"
    assert noa[header.index("הערות בדיקה")] == ""

    yael = next(row for row in body if row[0] == "יעל דוד")
    assert yael[a1_col] == "לא הוגש"
    assert yael[a2_col] == "לא הוגש"
    assert yael[header.index("ממוצע")] == ""

    amit = next(row for row in body if row[0] == "עמית לוי")
    assert amit[a1_col] == "הבדיקה נכשלה"
    assert amit[header.index("באיחור")] == "1"
    notes = amit[header.index("הערות בדיקה")]
    assert f"{A2_TITLE}: לבדוק שוב" in notes


def test_csv_neutralises_formula_injection(app):
    teacher_id = create_user(
        app,
        "inject",
        TEACHER_PASSWORD,
        role=ROLE_TEACHER,
        display_name="מורה",
    )
    student_id = create_user(
        app,
        "evil",
        STUDENT_PASSWORD,
        role=ROLE_STUDENT,
        display_name='=HYPERLINK("http://example.invalid")',
    )
    with _db(app) as db:
        group = ClassGroup(name="כיתה", code="INJ123", teacher_id=teacher_id)
        db.add(group)
        db.flush()
        db.add(Enrollment(class_id=group.id, student_id=student_id))
        assignment = Assignment(
            class_id=group.id,
            title="=SUM(1,1)",
            instructions="",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="d" * 64,
            published=True,
        )
        db.add(assignment)
        db.flush()
        submission = Submission(
            assignment_id=assignment.id,
            student_id=student_id,
            text="print(1)",
            revision=1,
            status=SUBMISSION_GRADED,
            first_submitted_at=datetime(2026, 11, 1, 8, 0),
            submitted_at=datetime(2026, 11, 1, 8, 0),
        )
        db.add(submission)
        db.flush()
        db.add(
            Grade(
                submission_id=submission.id,
                revision=1,
                score=80,
                review_note="=1+1",
            )
        )
        db.commit()

        payload = gradebook_to_csv(build_gradebook(db, group))

    rows = _csv_rows(payload)
    header, body = rows[0], rows[1]
    assert header[2].startswith("'=")
    assert body[0].startswith("'=")
    # The note is embedded after the assignment title, and the cell as a
    # whole is neutralised because that title itself starts with "=".
    assert body[-1].startswith("'=")
    assert body[-1].endswith(": =1+1")


def test_export_filename_is_ascii():
    group = ClassGroup(name="כיתה", code="GRD123", teacher_id=1)
    assert (
        export_filename(group, now=datetime(2026, 11, 8))
        == "gradebook-GRD123-20261108.csv"
    )


# ----------------------------------------------------------------- routes


def test_gradebook_page_content(client, world):
    _login(client, "mori", TEACHER_PASSWORD)
    response = client.get(f"/teacher/classes/{world['class']}/gradebook")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    html = response.text
    assert "ספר ציונים" in html
    assert "כיתה י׳ 1" in html
    assert "נועה כהן" in html
    assert "עמית לוי" in html
    assert "יעל דוד" in html
    assert A1_TITLE in html
    assert A2_TITLE in html
    assert DRAFT_TITLE not in html
    assert f"/teacher/students/{world['noa']}/profile" in html
    assert f"/teacher/classes/{world['class']}/export" in html


def test_export_route_downloads_csv(client, world):
    _login(client, "mori", TEACHER_PASSWORD)
    response = client.get(f"/teacher/classes/{world['class']}/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert disposition.endswith('.csv"')
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "נועה כהן" in response.content.decode("utf-8-sig")


def test_student_profile_page(client, world):
    _login(client, "mori", TEACHER_PASSWORD)

    response = client.get(f"/teacher/students/{world['noa']}/profile")
    assert response.status_code == 200
    html = response.text
    assert "נועה כהן" in html
    assert "כיתה י׳ 1" in html
    assert A1_TITLE in html
    assert "עבודה מצוינת" in html
    assert DRAFT_TITLE not in html

    amit = client.get(f"/teacher/students/{world['amit']}/profile")
    assert amit.status_code == 200
    assert "לבדוק שוב" in amit.text
    assert "באיחור" in amit.text

    yael = client.get(f"/teacher/students/{world['yael']}/profile")
    assert yael.status_code == 200
    assert "לא הוגש" in yael.text


def test_anonymous_is_redirected_to_login(client, world):
    for path in (
        f"/teacher/classes/{world['class']}/gradebook",
        f"/teacher/classes/{world['class']}/export",
        f"/teacher/students/{world['noa']}/profile",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")


def test_student_is_forbidden(client, world):
    _login(client, "noa", STUDENT_PASSWORD)
    for path in (
        f"/teacher/classes/{world['class']}/gradebook",
        f"/teacher/classes/{world['class']}/export",
        f"/teacher/students/{world['noa']}/profile",
    ):
        response = client.get(path)
        assert response.status_code == 403


def test_foreign_ids_are_404(client, world):
    _login(client, "other", OTHER_PASSWORD)

    assert client.get(
        f"/teacher/classes/{world['class']}/gradebook"
    ).status_code == 404
    assert client.get(
        f"/teacher/classes/{world['class']}/export"
    ).status_code == 404
    # Dana belongs to the other teacher, Noa does not.
    assert client.get(
        f"/teacher/students/{world['dana']}/profile"
    ).status_code == 200
    assert client.get(
        f"/teacher/students/{world['noa']}/profile"
    ).status_code == 404
    assert client.get(
        f"/teacher/students/{world['teacher']}/profile"
    ).status_code == 404
    assert client.get("/teacher/classes/999999/gradebook").status_code == 404
    assert client.get("/teacher/students/999999/profile").status_code == 404


def test_class_with_only_a_draft_shows_empty_state(client, world):
    # Reuse the seeded teacher and class; unpublish both assignments.
    with _db(client.app) as db:
        for assignment in db.scalars(
            select(Assignment).where(Assignment.class_id == world["class"])
        ):
            assignment.published = False
        db.commit()

    _login(client, "mori", TEACHER_PASSWORD)
    response = client.get(f"/teacher/classes/{world['class']}/gradebook")
    assert response.status_code == 200
    assert "אין מטלות מפורסמות" in response.text
    assert DRAFT_TITLE not in response.text
