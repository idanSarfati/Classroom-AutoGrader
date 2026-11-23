"""Write the class password sheets the teacher hands to students.

Every student's password is ``<given name><national id>`` (see
:mod:`portal.scripts.set_student_passwords`), so the sheet can be produced from
the live database. Before anything is written, every password is checked
against the **stored hash**: if one student was given a different password, the
export stops instead of handing out a wrong one.

Outputs (all private, git-ignored):

- ``student_credentials.csv`` - one row per student, Excel-friendly UTF-8.
- ``index.html`` - links to one sheet per class.
- ``class-<id>.html`` - RTL printable cards with a name filter, so the teacher
  can show one student at a time or print a whole class.

Usage::

    python -m portal.scripts.export_student_credentials
    python -m portal.scripts.export_student_credentials --out /tmp/sheets
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.auth.security import verify_password
from portal.config import load_settings
from portal.db import build_engine, build_session_factory, session_scope
from portal.models import (
    ROLE_STUDENT,
    ClassGroup,
    Enrollment,
    StudentRecord,
    User,
)
from portal.paths import DATA_DIR
from portal.scripts.set_student_passwords import _identifier, student_password

UNASSIGNED = "ללא כיתה"


@dataclass(frozen=True)
class Credential:
    """One student's line on a sheet. The hash is never rendered."""

    user_id: int
    class_id: int | None
    class_name: str
    display_name: str
    username: str
    password: str
    password_hash: str = field(repr=False)
    verified: bool = False


@dataclass(frozen=True)
class SkippedStudent:
    display_name: str
    reason: str


def build_credentials(
    db: Session,
) -> tuple[list[Credential], list[SkippedStudent]]:
    """One entry per student per class, in class and name order."""
    rows = db.execute(
        select(User, StudentRecord, ClassGroup)
        .outerjoin(StudentRecord, StudentRecord.student_id == User.id)
        .outerjoin(Enrollment, Enrollment.student_id == User.id)
        .outerjoin(ClassGroup, ClassGroup.id == Enrollment.class_id)
        .where(User.role == ROLE_STUDENT)
        .order_by(ClassGroup.name, User.display_name, User.id)
    ).all()

    entries: list[Credential] = []
    skipped: list[SkippedStudent] = []
    seen: set[tuple[int, int | None]] = set()
    skipped_users: set[int] = set()
    for user, record, group in rows:
        key = (user.id, group.id if group is not None else None)
        if key in seen:  # a duplicated enrollment row, not a second class
            continue
        seen.add(key)
        identifier = _identifier(user, record)
        password = student_password(user.display_name, identifier)
        if password is None:
            if user.id not in skipped_users:
                skipped_users.add(user.id)
                reason = "אין ת.ז." if not identifier else "אין שם פרטי בשם התצוגה"
                skipped.append(SkippedStudent(user.display_name, reason))
            continue
        entries.append(
            Credential(
                user_id=user.id,
                class_id=group.id if group is not None else None,
                class_name=group.name if group is not None else UNASSIGNED,
                display_name=user.display_name,
                username=user.username,
                password=password,
                password_hash=user.password_hash,
            )
        )
    return entries, skipped


def verify_credentials(
    entries: list[Credential],
) -> tuple[list[Credential], list[int]]:
    """Check every password against its stored hash.

    Returns the entries with ``verified`` set and the ids that do not match
    (those students were given a different password since the last reset).
    """
    checked: list[Credential] = []
    mismatched: list[int] = []
    for entry in entries:
        ok = verify_password(entry.password_hash, entry.password)
        if not ok:
            mismatched.append(entry.user_id)
        checked.append(replace(entry, verified=ok))
    return checked, mismatched


def _by_class(entries: list[Credential]) -> dict[tuple[int | None, str], list[Credential]]:
    groups: dict[tuple[int | None, str], list[Credential]] = {}
    for entry in entries:
        groups.setdefault((entry.class_id, entry.class_name), []).append(entry)
    for items in groups.values():
        items.sort(key=lambda item: (item.display_name, item.user_id))
    return dict(sorted(groups.items(), key=lambda item: (item[0][1], item[0][0] or 0)))


def write_csv(path: Path, entries: list[Credential]) -> None:
    """UTF-8 with BOM and CRLF so Excel opens Hebrew correctly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["כיתה", "שם מלא", "שם משתמש (ת.ז.)", "סיסמה"])
    for entry in entries:
        writer.writerow(
            [entry.class_name, entry.display_name, entry.username, entry.password]
        )
    # newline="" keeps the CRLF pairs written above intact on Windows too.
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    path.chmod(0o600)


_PAGE = """<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>
  :root {{
    --ink: #1c1b19;
    --muted: #6b665c;
    --paper: #faf8f3;
    --card: #ffffff;
    --line: #c9c3b6;
    --accent: #1f5e3d;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 20px;
    background: var(--paper);
    color: var(--ink);
    font-family: "Segoe UI", "Arial Hebrew", Arial, sans-serif;
    line-height: 1.35;
  }}
  header {{ max-width: 1100px; margin: 0 auto 14px; }}
  h1 {{ margin: 0 0 4px; font-size: 1.6rem; }}
  .meta {{ color: var(--muted); font-size: .95rem; }}
  .warning {{
    max-width: 1100px;
    margin: 0 auto 18px;
    padding: 10px 14px;
    border-inline-start: 5px solid #a33;
    background: #fbecec;
    font-size: .95rem;
  }}
  .toolbar {{
    position: sticky;
    top: 0;
    z-index: 2;
    max-width: 1100px;
    margin: 0 auto 18px;
    padding: 8px 0;
    background: var(--paper);
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .toolbar input {{
    flex: 1 1 260px;
    padding: 12px 14px;
    font-size: 1.1rem;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--card);
  }}
  .toolbar button {{
    padding: 12px 18px;
    font-size: 1rem;
    border: 0;
    border-radius: 10px;
    background: var(--accent);
    color: #fff;
    cursor: pointer;
  }}
  .count {{ color: var(--muted); font-size: .95rem; }}
  .cards {{
    max-width: 1100px;
    margin: 0 auto;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 12px;
  }}
  .card {{
    background: var(--card);
    border: 1px dashed var(--line);
    border-radius: 12px;
    padding: 14px 16px;
    break-inside: avoid;
  }}
  .card h2 {{ margin: 0 0 10px; font-size: 1.25rem; }}
  .row {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }}
  .row + .row {{ margin-top: 6px; }}
  .label {{ color: var(--muted); font-size: .9rem; }}
  .value {{ font-weight: 600; direction: ltr; unicode-bidi: plaintext; }}
  .password .value {{ font-size: 1.45rem; letter-spacing: .3px; color: var(--accent); }}
  .hint {{ max-width: 1100px; margin: 18px auto 0; color: var(--muted); font-size: .9rem; }}
  .classes {{ max-width: 1100px; margin: 0 auto; display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }}
  .classes a {{
    display: block;
    padding: 18px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    color: var(--ink);
    text-decoration: none;
    font-size: 1.2rem;
    font-weight: 600;
  }}
  .classes a span {{ display: block; margin-top: 4px; color: var(--muted); font-size: .9rem; font-weight: 400; }}
  @media print {{
    @page {{ size: A4; margin: 9mm; }}
    body {{ background: #fff; padding: 0; }}
    .toolbar, .warning .no-print, .hint {{ display: none; }}
    header {{ margin-bottom: 8mm; }}
    .cards {{ grid-template-columns: 1fr 1fr; gap: 5mm; }}
    .card {{ padding: 4mm; border: 1px dashed #666; }}
    .card h2 {{ font-size: 13pt; }}
    .password .value {{ font-size: 15pt; }}
  }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _search_key(entry: Credential) -> str:
    return f"{entry.display_name} {entry.username}"


def _class_filename(class_id: int | None) -> str:
    return f"class-{class_id}.html" if class_id is not None else "class-unassigned.html"


def _class_sheet(
    class_name: str, entries: list[Credential], today: str
) -> str:
    cards = "\n".join(
        f"""<article class="card" data-search="{html.escape(_search_key(entry))}">
  <h2>{html.escape(entry.display_name)}</h2>
  <div class="row"><span class="label">שם משתמש (ת.ז.)</span><span class="value">{html.escape(entry.username)}</span></div>
  <div class="row password"><span class="label">סיסמה</span><span class="value">{html.escape(entry.password)}</span></div>
</article>"""
        for entry in entries
    )
    body = f"""<header>
  <h1>סיסמאות התחברות — {html.escape(class_name)}</h1>
  <div class="meta">עודכן ב-{html.escape(today)} · {len(entries)} תלמידים</div>
</header>
<div class="warning">קובץ פרטי: הוא כולל סיסמאות של תלמידים. אין להעלות אותו לרשת, לשלוח בקבוצה או להשאיר אותו פתוח על מסך משותף.</div>
<div class="toolbar no-print">
  <input type="search" placeholder="חיפוש לפי שם או ת.ז." aria-label="חיפוש תלמיד" data-filter>
  <button type="button" onclick="window.print()">הדפסה</button>
  <span class="count" data-count>{len(entries)} מתוך {len(entries)}</span>
</div>
<main class="cards">
{cards}
</main>
<p class="hint">הסיסמה מורכבת מהשם הפרטי ומספר תעודת הזהות. אם הסיסמה אינה מתקבלת, פנו למורה — אין לנסות שוב ושוב.</p>
<script>
(() => {{
  const input = document.querySelector('[data-filter]');
  const cards = Array.from(document.querySelectorAll('.card'));
  const count = document.querySelector('[data-count]');
  const update = () => {{
    const query = input.value.trim();
    let shown = 0;
    cards.forEach((card) => {{
      const hit = !query || card.dataset.search.includes(query);
      card.hidden = !hit;
      if (hit) shown += 1;
    }});
    count.textContent = shown + ' מתוך ' + cards.length;
  }};
  input.addEventListener('input', update);
}})();
</script>"""
    return _PAGE.format(title=f"סיסמאות — {html.escape(class_name)}", body=body)


def _index_sheet(
    groups: dict[tuple[int | None, str], list[Credential]], today: str, total: int
) -> str:
    links = "\n".join(
        f'<a href="{html.escape(_class_filename(class_id))}">{html.escape(class_name)}'
        f"<span>{len(items)} תלמידים</span></a>"
        for (class_id, class_name), items in groups.items()
    )
    body = f"""<header>
  <h1>גיליונות סיסמאות</h1>
  <div class="meta">עודכן ב-{html.escape(today)} · {total} תלמידים ב-{len(groups)} כיתות</div>
</header>
<div class="warning">קובץ פרטי: הוא כולל סיסמאות של תלמידים. אין להעלות אותו לרשת, לשלוח בקבוצה או להשאיר אותו פתוח על מסך משותף.</div>
<main class="classes">
{links}
</main>
<p class="hint">לכל כיתה יש גיליון נפרד עם חיפוש והדפסה. הגיליון כולל גם קובץ CSV מרוכז: <a href="student_credentials.csv">student_credentials.csv</a>.</p>"""
    return _PAGE.format(title="גיליונות סיסמאות", body=body)


def write_sheets(out_dir: Path, entries: list[Credential], today: str) -> list[Path]:
    """Write the CSV, one printable page per class and the index."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    csv_path = out_dir / "student_credentials.csv"
    write_csv(csv_path, entries)
    written.append(csv_path)

    groups = _by_class(entries)
    for (class_id, class_name), items in groups.items():
        path = out_dir / _class_filename(class_id)
        path.write_text(_class_sheet(class_name, items, today), encoding="utf-8")
        path.chmod(0o600)
        written.append(path)

    index = out_dir / "index.html"
    index.write_text(_index_sheet(groups, today, len(entries)), encoding="utf-8")
    index.chmod(0o600)
    written.insert(0, index)
    return written


def _today(zone: str) -> str:
    try:
        from zoneinfo import ZoneInfo

        moment = datetime.now(ZoneInfo(zone))
    except Exception:  # pragma: no cover - exotic host without tzdata
        moment = datetime.now()
    return moment.strftime("%d.%m.%Y")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR / "credentials",
        help="Directory for the sheets (default: portal/data/credentials).",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    settings = load_settings()
    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)

    with session_scope(factory) as db:
        entries, skipped = build_credentials(db)

    print(f"database: {settings.database_url}")
    print(f"students on the sheets: {len(entries)}")
    print(f"skipped: {len(skipped)}")
    for item in skipped:
        print(f"  - user: {item.reason}")

    if not entries:
        print("nothing to write: no student has both a name and an id.")
        return 0

    checked, mismatched = verify_credentials(entries)
    if mismatched:
        print(
            "verification failed for "
            f"{len(mismatched)} students (ids: {', '.join(map(str, mismatched))}).",
            file=sys.stderr,
        )
        print(
            "Their stored password is not the name+id rule. Nothing was written. "
            "Run set_student_passwords --apply first, or reset those students "
            "from the teacher screen.",
            file=sys.stderr,
        )
        return 1

    written = write_sheets(args.out, checked, _today(settings.timezone))
    print(f"verified: {len(checked)} of {len(checked)}")
    print(f"written: {len(written)} files in {args.out}")
    print(f"open: {args.out / 'index.html'}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
