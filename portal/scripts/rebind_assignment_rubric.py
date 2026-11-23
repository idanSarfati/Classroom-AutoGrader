"""Rebind one assignment to a different rubric file (teacher-authorized).

The importer's fuzzy matcher once bound "4.2 | הגשת משימות" to the lesson 2
variables rubric. This CLI fixes a single assignment's binding after the
teacher explicitly approves it: it validates the new rubric through the same
adapter the web app uses, records the new fingerprint, and prints the row
before and after. It never touches submissions, drafts or grades.

Usage (server)::

    sudo -u portal /opt/clasroom-autograder/.venv/bin/python \
        -m portal.scripts.rebind_assignment_rubric \
        --assignment-id 71 --rubric assignments/lesson_4.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from portal.config import load_settings
from portal.grading import adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignment-id", type=int, required=True)
    parser.add_argument("--rubric", required=True, help="path under assignments/")
    args = parser.parse_args(argv)

    binding = adapter.load_rubric(args.rubric)
    new_path = str(binding.path)

    cfg = load_settings()
    url = cfg.database_url
    if not url.startswith("sqlite:///"):
        print("This tool only supports the portal SQLite database.", file=sys.stderr)
        return 2
    db_path = url.removeprefix("sqlite:///")

    db = sqlite3.connect(db_path, timeout=30)
    try:
        db.execute("PRAGMA busy_timeout=30000")
        before = db.execute(
            "SELECT id, class_id, title, rubric_path, rubric_sha256, published "
            "FROM portal_assignments WHERE id=?",
            (args.assignment_id,),
        ).fetchone()
        if before is None:
            print(f"Assignment {args.assignment_id} not found.", file=sys.stderr)
            return 1
        counts = {
            "submissions": db.execute(
                "SELECT COUNT(*) FROM portal_submissions WHERE assignment_id=?",
                (args.assignment_id,),
            ).fetchone()[0],
            "drafts": db.execute(
                "SELECT COUNT(*) FROM portal_answer_drafts WHERE assignment_id=?",
                (args.assignment_id,),
            ).fetchone()[0],
        }
        cur = db.execute(
            "UPDATE portal_assignments SET rubric_path=?, rubric_sha256=? WHERE id=?",
            (new_path, binding.sha256, args.assignment_id),
        )
        db.commit()
        after = db.execute(
            "SELECT id, rubric_path, rubric_sha256 FROM portal_assignments WHERE id=?",
            (args.assignment_id,),
        ).fetchone()
        print(json.dumps({
            "before": before,
            "related_rows_untouched": counts,
            "rows_changed": cur.rowcount,
            "after": after,
        }, ensure_ascii=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())