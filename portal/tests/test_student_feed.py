"""The student class feed: topic grouping, kind badges and status badges."""
from __future__ import annotations

from portal.models import (
    SUBMISSION_GRADED,
    SUBMISSION_QUEUED,
    Assignment,
    ClassGroup,
    Lesson,
)
from portal.web.student_feed import build_class_feed, item_number, status_badge


def test_item_number_handles_lessons_and_practice() -> None:
    assert item_number("3.2 | הגשת משימות") == (3, 2)
    assert item_number("תרגול יחידות 1-2") == (2, 99)
    assert item_number("כותרת ללא מספר") is None


def test_status_badge_wording() -> None:
    assert status_badge(None) == ("לא הוגש", "badge-missing")
    assert status_badge({"status": SUBMISSION_QUEUED}) == (
        "ממתין לציון",
        "badge-draft",
    )
    assert status_badge({"status": SUBMISSION_GRADED, "score": 92}) == (
        "ציון 92",
        "badge-published",
    )


def test_feed_groups_lessons_and_assignments_by_unit() -> None:
    group = ClassGroup(name="כיתה ז׳ 1")
    lessons = [
        Lesson(id=1, title="0.1 | מצגת השיעור", summary="", class_id=1),
        Lesson(id=2, title="1.1 | מבוא", summary="", class_id=1),
    ]
    assignments = [
        Assignment(
            id=10,
            title="0.3 | משימת התנסות",
            class_id=1,
            rubric_path="assignments/lesson_1.json",
        ),
        Assignment(
            id=11,
            title="3.2 | הגשת משימות",
            class_id=1,
            rubric_path="assignments/lesson_3.json",
        ),
        Assignment(
            id=12,
            title="תרגול יחידות 1-2",
            class_id=1,
            rubric_path="assignments/practice.json",
        ),
    ]
    topics = build_class_feed(
        None,
        group,
        lessons,
        assignments,
        {1: 16, 2: 15},
        {11: {"status": SUBMISSION_QUEUED}},
        "/student",
    )

    # Units descend: the higher unit and the newer item sit on top.
    assert [topic.label for topic in topics] == [
        "יחידה 3",
        "יחידה 2",
        "יחידה 1",
        "יחידה 0",
    ]
    oldest = topics[3].items
    assert [item.kind for item in oldest] == ["assignment", "lesson"]
    assert oldest[0].kind_label == "מטלה"
    assert oldest[1].kind_label == "שיעור"
    assert oldest[0].status_label == "לא הוגש"
    assert topics[1].items[0].title == "תרגול יחידות 1-2"
    assert topics[0].items[0].status_label == "ממתין לציון"


def test_feed_orders_by_unit_then_newest_item() -> None:
    group = ClassGroup(name="כיתה ז׳ 1")
    lessons = [
        Lesson(id=1, title="1.1 | מבוא", class_id=1),
        Lesson(id=2, title="1.3 | המשך", class_id=1),
        Lesson(id=3, title="כותרת ללא מספר", class_id=1),
    ]
    assignments = [
        Assignment(id=10, title="1.2 | הגשה", class_id=1),
        Assignment(id=11, title="3.1 | הגשה", class_id=1),
    ]
    topics = build_class_feed(
        None, group, lessons, assignments, {}, {}, "/student"
    )

    assert [topic.label for topic in topics] == [
        "יחידה 3",
        "יחידה 1",
        "כללי",
    ]
    assert [item.title for item in topics[1].items] == [
        "1.3 | המשך",
        "1.2 | הגשה",
        "1.1 | מבוא",
    ]


def test_practice_revision_closes_its_unit() -> None:
    group = ClassGroup(name="כיתה ז׳ 1")
    assignments = [
        Assignment(id=20, title="2.2 | הגשה", class_id=1),
        Assignment(id=21, title="תרגול יחידות 1-2", class_id=1),
    ]
    topics = build_class_feed(
        None, group, [], assignments, {}, {}, "/student"
    )

    assert [topic.label for topic in topics] == ["יחידה 2"]
    assert [item.title for item in topics[0].items] == [
        "2.2 | הגשה",
        "תרגול יחידות 1-2",
    ]


def test_class_preview_shows_topics_and_badges(app, client) -> None:
    from sqlalchemy import select

    from portal.models import ROLE_TEACHER
    from portal.tests.helpers import create_user, csrf, login

    create_user(
        app, "mori2", "TeacherPass123", role=ROLE_TEACHER, display_name="מורי"
    )
    login(client, "mori2", "TeacherPass123")
    token = csrf(client, "/teacher")
    response = client.post(
        "/teacher/classes",
        data={"csrf_token": token, "name": "כיתה ח׳ 2"},
        follow_redirects=False,
    )
    class_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        teacher_id = db.scalar(
            select(ClassGroup.teacher_id).where(ClassGroup.id == class_id)
        )
        db.add(
            Lesson(
                teacher_id=teacher_id,
                class_id=class_id,
                title="1.1 | מבוא לאלגוריתמיקה",
                published=True,
            )
        )
        db.add(
            Assignment(
                class_id=class_id,
                title="1.2 | הגשת משימות",
                instructions="הגישו קוד",
                rubric_path="assignments/lesson_1.json",
                rubric_sha256="0" * 64,
                published=True,
            )
        )
        db.commit()

    page = client.get(f"/teacher/preview/classes/{class_id}")
    assert page.status_code == 200
    html = page.text
    assert "יחידה 1" in html
    assert "badge-lesson" in html and ">שיעור<" in html
    assert "badge-assignment" in html and ">מטלה<" in html
    assert "לא הוגש" in html
    assert f"/teacher/preview/lessons/" in html
    assert f"/teacher/preview/assignments/" in html


def test_teacher_class_page_merges_lessons_and_assignments(app, client) -> None:
    from sqlalchemy import select

    from portal.models import ROLE_TEACHER
    from portal.tests.helpers import create_user, csrf, login

    create_user(
        app, "mori3", "TeacherPass123", role=ROLE_TEACHER, display_name="מורי"
    )
    login(client, "mori3", "TeacherPass123")
    token = csrf(client, "/teacher")
    response = client.post(
        "/teacher/classes",
        data={"csrf_token": token, "name": "כיתה ח׳ 3"},
        follow_redirects=False,
    )
    class_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])

    with app.state.session_factory() as db:
        teacher_id = db.scalar(
            select(ClassGroup.teacher_id).where(ClassGroup.id == class_id)
        )
        db.add(
            Lesson(
                teacher_id=teacher_id,
                class_id=class_id,
                title="1.1 | מבוא לאלגוריתמיקה",
                published=True,
            )
        )
        # Drafts stay on the teacher page even though students cannot see them.
        db.add(
            Lesson(
                teacher_id=teacher_id,
                class_id=class_id,
                title="2.1 | טיוטת שיעור",
                published=False,
            )
        )
        db.add(
            Assignment(
                class_id=class_id,
                title="1.2 | הגשת משימות",
                instructions="הגישו קוד",
                rubric_path="assignments/lesson_1.json",
                rubric_sha256="0" * 64,
                published=True,
            )
        )
        db.commit()

    page = client.get(f"/teacher/classes/{class_id}")
    assert page.status_code == 200
    html = page.text

    # One merged list, not a lessons section plus an assignments section.
    assert "תוכן הכיתה" in html
    assert "יחידה 1" in html and "יחידה 2" in html
    assert "טיוטת שיעור" in html
    assert "1.2 | הגשת משימות" in html
    # Newer item sits above the earlier lesson inside the same unit.
    assert html.index("1.2 | הגשת משימות") < html.index("1.1 | מבוא לאלגוריתמיקה")