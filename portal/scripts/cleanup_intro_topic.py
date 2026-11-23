"""Remove the introductory ("היכרות") lessons and assignments from every class.

The teacher asked to drop the whole 0.x prefix because those items belong to the
old "היכרות" topic. The leftover lesson titled ``1.1 | היכרות`` holds the same
intro deck (its slides say "מצגת היכרות") and would clash with the real 1.1
presentation, so it is removed as well.

Assignments may already carry graded submissions. Deleting an assignment
cascades to its submissions and grades, so every affected row is exported to a
JSON file under ``portal/data/`` (git-ignored) first, and the whole live
database is expected to be backed up before an ``--apply`` run.

Run against a copy first (``--db``), then against the live database with
``--apply`` while the portal server is stopped.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_DB = PROJECT_ROOT / "portal" / "data" / "portal.db"
DEFAULT_EXPORT = (
    PROJECT_ROOT
    / "portal"
    / "data"
    / f"removed-intro-topic-{datetime.now(timezone.utc):%Y-%m-%d}.json"
)

_INTRO_PREFIX_RE = re.compile(r"^\s*0\.\d")
_INTRO_LESSON_TITLES = {"1.1 | היכרות"}


def is_intro_lesson(title: str) -> bool:
    cleaned = (title or "").strip()
    return bool(_INTRO_PREFIX_RE.match(cleaned)) or cleaned in _INTRO_LESSON_TITLES


def is_intro_assignment(title: str) -> bool:
    return bool(_INTRO_PREFIX_RE.match((title or "").strip()))


def _rows(connection: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def collect(connection: sqlite3.Connection) -> dict:
    lessons = [
        row
        for row in _rows(
            connection,
            """
            SELECT l.id, l.class_id, c.name AS class_name, l.title, l.published
            FROM portal_lessons l
            LEFT JOIN portal_classes c ON c.id = l.class_id
            ORDER BY l.class_id, l.id
            """,
        )
        if is_intro_lesson(row["title"])
    ]
    assignments = [
        row
        for row in _rows(
            connection,
            """
            SELECT a.id, a.class_id, c.name AS class_name, a.title, a.published
            FROM portal_assignments a
            LEFT JOIN portal_classes c ON c.id = a.class_id
            ORDER BY a.class_id, a.id
            """,
        )
        if is_intro_assignment(row["title"])
    ]

    lesson_ids = [row["id"] for row in lessons]
    assignment_ids = [row["id"] for row in assignments]
    placeholders = ",".join("?" for _ in lesson_ids) or "NULL"
    slides = (
        _rows(
            connection,
            f"SELECT * FROM portal_lesson_slides WHERE lesson_id IN ({placeholders})",
            lesson_ids,
        )
        if lesson_ids
        else []
    )
    slide_contents = (
        _rows(
            connection,
            "SELECT * FROM portal_slide_content WHERE slide_id IN "
            f"({','.join('?' for _ in slides)})",
            [row["id"] for row in slides],
        )
        if slides
        else []
    )
    sources = (
        _rows(
            connection,
            f"SELECT * FROM portal_lesson_sources WHERE lesson_id IN ({placeholders})",
            lesson_ids,
        )
        if lesson_ids
        else []
    )

    a_placeholders = ",".join("?" for _ in assignment_ids) or "NULL"
    submissions = (
        _rows(
            connection,
            "SELECT * FROM portal_submissions WHERE assignment_id IN "
            f"({a_placeholders})",
            assignment_ids,
        )
        if assignment_ids
        else []
    )
    grades = (
        _rows(
            connection,
            "SELECT * FROM portal_grades WHERE submission_id IN "
            f"({','.join('?' for _ in submissions)})",
            [row["id"] for row in submissions],
        )
        if submissions
        else []
    )
    contents = (
        _rows(
            connection,
            f"SELECT * FROM portal_assignment_content WHERE assignment_id IN ({a_placeholders})",
            assignment_ids,
        )
        if assignment_ids
        else []
    )
    attachments = (
        _rows(
            connection,
            f"SELECT * FROM portal_assignment_attachments WHERE assignment_id IN ({a_placeholders})",
            assignment_ids,
        )
        if assignment_ids
        else []
    )

    return {
        "lessons": lessons,
        "lesson_sources": sources,
        "lesson_slides": slides,
        "slide_content": slide_contents,
        "assignments": assignments,
        "submissions": submissions,
        "grades": grades,
        "assignment_content": contents,
        "assignment_attachments": attachments,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=LIVE_DB)
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        data = collect(connection)
        lessons = data["lessons"]
        assignments = data["assignments"]

        print("== lessons to remove")
        for row in lessons:
            print(
                f"  {row['class_name']:<5} lesson {row['id']:>3} "
                f"({'published' if row['published'] else 'draft'}) {row['title']}"
            )
        print("== assignments to remove")
        for row in assignments:
            print(
                f"  {row['class_name']:<5} assignment {row['id']:>3} "
                f"({'published' if row['published'] else 'draft'}) {row['title']}"
            )
        print("\nSlides:", len(data["lesson_slides"]))
        print("Submissions:", len(data["submissions"]))
        print("Grades:", len(data["grades"]))
        print("Attachments:", len(data["assignment_attachments"]))

        if not lessons and not assignments:
            print("\nNothing to remove.")
            return 0
        if not args.apply:
            print("\nDry run only; re-run with --apply to write.")
            return 0

        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(
            json.dumps(
                {"exported_at": datetime.now(timezone.utc).isoformat(), **data},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nExported removed rows to {args.export}")

        lesson_ids = [row["id"] for row in lessons]
        assignment_ids = [row["id"] for row in assignments]
        slide_ids = [row["id"] for row in data["lesson_slides"]]
        submission_ids = [row["id"] for row in data["submissions"]]

        with connection:
            if slide_ids:
                connection.execute(
                    "DELETE FROM portal_slide_content WHERE slide_id IN "
                    f"({','.join('?' for _ in slide_ids)})",
                    slide_ids,
                )
            if lesson_ids:
                lph = ",".join("?" for _ in lesson_ids)
                connection.execute(
                    f"DELETE FROM portal_lesson_slides WHERE lesson_id IN ({lph})",
                    lesson_ids,
                )
                connection.execute(
                    f"DELETE FROM portal_lesson_sources WHERE lesson_id IN ({lph})",
                    lesson_ids,
                )
            if submission_ids:
                sph = ",".join("?" for _ in submission_ids)
                connection.execute(
                    f"DELETE FROM portal_grades WHERE submission_id IN ({sph})",
                    submission_ids,
                )
            if assignment_ids:
                aph = ",".join("?" for _ in assignment_ids)
                connection.execute(
                    f"DELETE FROM portal_submissions WHERE assignment_id IN ({aph})",
                    assignment_ids,
                )
                connection.execute(
                    f"DELETE FROM portal_assignment_attachments "
                    f"WHERE assignment_id IN ({aph})",
                    assignment_ids,
                )
                connection.execute(
                    f"DELETE FROM portal_assignment_content "
                    f"WHERE assignment_id IN ({aph})",
                    assignment_ids,
                )
            if lesson_ids:
                connection.execute(
                    f"DELETE FROM portal_lessons WHERE id IN "
                    f"({','.join('?' for _ in lesson_ids)})",
                    lesson_ids,
                )
            if assignment_ids:
                connection.execute(
                    f"DELETE FROM portal_assignments WHERE id IN "
                    f"({','.join('?' for _ in assignment_ids)})",
                    assignment_ids,
                )
            if lesson_ids:
                connection.execute(
                    f"UPDATE portal_import_items SET portal_id = NULL "
                    f"WHERE portal_type = 'lesson' AND portal_id IN "
                    f"({','.join('?' for _ in lesson_ids)})",
                    lesson_ids,
                )
            if assignment_ids:
                connection.execute(
                    f"UPDATE portal_import_items SET portal_id = NULL "
                    f"WHERE portal_type = 'assignment' AND portal_id IN "
                    f"({','.join('?' for _ in assignment_ids)})",
                    assignment_ids,
                )

        print(
            f"Removed {len(lessons)} lesson(s) and {len(assignments)} "
            f"assignment(s) from {args.db}"
        )
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())