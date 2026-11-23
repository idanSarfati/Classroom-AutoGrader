"""Align each class's visible content with the teacher's reported progress.

Every numbered lesson/assignment whose leading number (``3.2``) is less than or
equal to the class target is published; anything above the target stays or
becomes a draft. The unnumbered "תרגול יחידות 1-2" practice belongs to unit 2,
so it is published once the class has reached 2.x.

Run against a copy first (``--db``), then against the live database with
``--apply`` while the portal server is stopped.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_DB = PROJECT_ROOT / "portal" / "data" / "portal.db"

# The teacher's progress per class: everything up to and including this lesson.
PROGRESS: dict[str, str] = {
    "ז'3": "3.2",
    "ז'4": "3.2",
    "ח'3": "3.3",
    "ח'4": "3.3",
    "ח'6": "2.3",
    "ט'2": "3.2",
    "ט'3": "3.3",
    "ט'4": "3.3",
}

_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?!\d)")
_PRACTICE_PREFIX = "תרגול יחידות 1-2"


def leading_number(title: str) -> tuple[int, int] | None:
    match = _NUMBER_RE.match(title or "")
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_target(text: str) -> tuple[int, int]:
    number = leading_number(text)
    if number is None:
        raise ValueError(f"Invalid lesson target: {text!r}")
    return number


def is_at_most(number: tuple[int, int], target: tuple[int, int]) -> bool:
    return number <= target


def decide(title: str, target: tuple[int, int]) -> tuple[bool, str]:
    """Return (should_be_published, reason) for one item title."""
    number = leading_number(title)
    if number is not None:
        ok = is_at_most(number, target)
        return ok, f"lesson {number[0]}.{number[1]}"
    if (title or "").strip().startswith(_PRACTICE_PREFIX):
        # A revision of units 1-2; every class here is past unit 2.
        return target >= (2, 0), "units 1-2 practice"
    return False, "unnumbered (unchanged)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=LIVE_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        classes = {
            row["name"]: row["id"]
            for row in connection.execute("SELECT id, name FROM portal_classes")
        }
        missing = sorted(set(PROGRESS) - set(classes))
        if missing:
            print("Missing classes in DB:", missing)
            return 2

        updates: list[tuple[str, bool, int]] = []
        untouched: list[str] = []
        for name, target_text in PROGRESS.items():
            target = parse_target(target_text)
            class_id = classes[name]
            print(f"\n== {name} — target {target_text}")
            for table, label in (
                ("portal_lessons", "lesson"),
                ("portal_assignments", "assignment"),
            ):
                rows = connection.execute(
                    f"SELECT id, title, published FROM {table} "
                    "WHERE class_id = ? ORDER BY id",
                    (class_id,),
                ).fetchall()
                for row in rows:
                    desired, reason = decide(row["title"], target)
                    if reason == "unnumbered (unchanged)":
                        untouched.append(f"{name} {label} {row['id']} {row['title']}")
                        continue
                    current = bool(row["published"])
                    if desired and not current:
                        updates.append((table, True, row["id"]))
                        action = "PUBLISH"
                    elif not desired and current:
                        updates.append((table, False, row["id"]))
                        action = "UNPUBLISH"
                    else:
                        action = "ok"
                    print(
                        f"  {label:10} {row['id']:>3} "
                        f"{'published' if current else 'draft    '} -> "
                        f"{'published' if desired else 'draft    '} "
                        f"[{action:9}] {row['title']}  ({reason})"
                    )

        print(f"\nPlanned changes: {len(updates)}")
        if untouched:
            print("Left untouched (unnumbered):")
            for line in untouched:
                print("  ", line)

        if not args.apply:
            print("\nDry run only; re-run with --apply to write.")
            return 0

        with connection:
            for table, desired, item_id in updates:
                if table == "portal_lessons":
                    connection.execute(
                        "UPDATE portal_lessons SET published = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (1 if desired else 0, item_id),
                    )
                else:
                    connection.execute(
                        "UPDATE portal_assignments SET published = ? WHERE id = ?",
                        (1 if desired else 0, item_id),
                    )
        print(f"Applied {len(updates)} changes to {args.db}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())