"""The intro-topic cleanup removes only 0.x items and the stale 1.1 intro deck."""
from __future__ import annotations

import sqlite3

from portal.scripts.cleanup_intro_topic import (
    collect,
    is_intro_assignment,
    is_intro_lesson,
)


def test_zero_prefix_lessons_and_assignments_are_intro() -> None:
    assert is_intro_lesson("0.1 | פייתון א | מצגת השיעור | היכרות")
    assert is_intro_lesson("1.1 | היכרות")
    assert not is_intro_lesson("1.1 מצגת השיעור _ מבוא לאלגוריתמיקה והדפסה")
    assert is_intro_assignment("0.3 | משימת התנסות | מצב הרוח שלי :)")
    assert not is_intro_assignment("1.2 | הגשת משימות")


def _seed(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE portal_classes (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE portal_lessons (
            id INTEGER PRIMARY KEY, class_id INTEGER, title TEXT, published INTEGER
        );
        CREATE TABLE portal_assignments (
            id INTEGER PRIMARY KEY, class_id INTEGER, title TEXT, published INTEGER
        );
        CREATE TABLE portal_lesson_sources (lesson_id INTEGER, source_id TEXT);
        CREATE TABLE portal_lesson_slides (id INTEGER PRIMARY KEY, lesson_id INTEGER);
        CREATE TABLE portal_slide_content (slide_id INTEGER, image_blob_sha TEXT);
        CREATE TABLE portal_submissions (id INTEGER PRIMARY KEY, assignment_id INTEGER);
        CREATE TABLE portal_grades (id INTEGER PRIMARY KEY, submission_id INTEGER);
        CREATE TABLE portal_assignment_content (assignment_id INTEGER);
        CREATE TABLE portal_assignment_attachments (assignment_id INTEGER);
        INSERT INTO portal_classes VALUES (1, 'ז1');
        INSERT INTO portal_lessons VALUES (1, 1, '0.1 | היכרות', 1);
        INSERT INTO portal_lessons VALUES (2, 1, '1.1 | היכרות', 1);
        INSERT INTO portal_lessons VALUES (3, 1, '1.1 מצגת השיעור', 1);
        INSERT INTO portal_assignments VALUES (1, 1, '0.3 | מצב הרוח', 1);
        INSERT INTO portal_assignments VALUES (2, 1, '1.2 | הגשת משימות', 1);
        INSERT INTO portal_lesson_slides VALUES (10, 1);
        INSERT INTO portal_slide_content VALUES (10, 'sha');
        INSERT INTO portal_submissions VALUES (20, 1);
        INSERT INTO portal_grades VALUES (30, 20);
        """
    )


def test_collect_includes_children_but_not_regression_rows() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _seed(connection)

    data = collect(connection)

    assert [row["id"] for row in data["lessons"]] == [1, 2]
    assert [row["id"] for row in data["assignments"]] == [1]
    assert [row["id"] for row in data["lesson_slides"]] == [10]
    assert [row["slide_id"] for row in data["slide_content"]] == [10]
    assert [row["id"] for row in data["submissions"]] == [20]
    assert [row["id"] for row in data["grades"]] == [30]
    connection.close()