# Phase 7 — alephon and historical grade import

Phase 7 fills the portal with the real school data created before the portal
existed: the student directory (אלפון) and the grade columns exported from
Google Classroom. Both imports are read-only until `--apply`, and rerunning
them is safe.

## Input layout

One folder per class, with the class name exactly as the portal class (the
login-safe form without punctuation is fine — `ז3` matches `ז'3`):

```
אלפונים/
  ז3/
    אלפון.xlsx                 # school directory export
    <classroom export>.csv     # Students tab export, one or more files
  ז4/
  ח3/
  ח4/
  ח6/
  ט2/
  ט3/
  ט4/
```

The alephon is read with the standard library only (no `openpyxl` on the
school network). The header row is found by content (`ת.ז` + `שם`), so extra
title rows are harmless. Columns used: national id, student name, class,
student e-mail/phone, parent 1/2 name, phone and e-mail. The folder name is
the authoritative class; the workbook's `שכבה` column only says `ז`.

The Classroom CSV is the normal *Students* export: a header row
(`שם משפחה`, `שם פרטי`, `כתובת אימייל`, one column per assignment), optional
`תאריך` and `נקודות` rows, and duplicate `X` / `X [Auto]` columns. Both
duplicates are kept; the grade importer merges them.

## Step 1 — students

Preview (writes only the review CSV, never the database):

```powershell
.venv\Scripts\python -m portal.scripts.import_students --source "C:\Users\...\אלפונים"
```

Apply:

```powershell
.venv\Scripts\python -m portal.scripts.import_students --source "C:\Users\...\אלפונים" --apply
```

Options: `--classes "ז3,ח3"`, `--mapping-out`, `--credentials-out`,
`--overwrite-mapping`, `--verbose`.

Identity rules:

- The **national id is the stable key**, stored on `portal_student_records`.
  A new name or a moved class never creates a second account.
- The login name is the national id (digits, valid for the portal rules). If it
  is missing or already taken by another role, a random `s########` username
  is used and reported.
- An existing account with that username is reused, even before it has a
  student record. Its password is never reset.
- New accounts get a random 10-character password, hashed with Argon2id. The
  plaintext exists only in `portal/data/student_credentials.csv` (UTF-8 BOM,
  appended per batch, git-ignored). The CLI never prints it.
- The alephon is authoritative for display name, class and contacts. Empty
  cells never erase a value already stored.
- Each alephon student is enrolled in their class once. A duplicate national
  id inside the source aborts that row with a warning.

## Step 2 — review the name mapping

The students step writes `portal/data/student_mapping.csv`. Classroom and the
alephon disagree on spelling, so automatic matching is conservative:

| confidence | meaning | confirmed |
| --- | --- | --- |
| `high` | complete name on both sides (Hebrew, Latin or Cyrillic transliteration) | `yes` |
| `medium` | partial name, e-mail hint, or a close but not certain name | `no` |
| `low` / `none` | not enough information | `no` |

Columns:

```
class, csv_file, csv_row, classroom_name, classroom_email,
student_id, student_name, confidence, confirmed, note
```

The teacher edits `student_id` (national id) and `confirmed` (`yes`/`no`).
Only rows with a national id **and** `confirmed=yes` import grades. Duplicate
Classroom rows for one student stay in the file with `confirmed=no` and a
`שורת כפילות` note; they are never imported twice.

An existing mapping file is **not** overwritten by a rerun — pass
`--overwrite-mapping` only when you intentionally want a fresh review.

## Step 3 — historical grades

Preview:

```powershell
.venv\Scripts\python -m portal.scripts.import_grades --source "C:\Users\...\אלפונים"
```

Apply:

```powershell
.venv\Scripts\python -m portal.scripts.import_grades --source "C:\Users\...\אלפונים" --apply
```

Options: `--classes`, `--mapping`, `--verbose`.

Rules:

- A CSV column is imported only when its title matches a portal assignment in
  the same class, using the same normalised `title_key` as the content
  importer. Quarter exams, letter-grade columns and renamed work are listed
  as **skipped columns** with their numeric-cell count; they are never guessed
  into an assignment.
- Scores are scaled to 0–100 using the column's `נקודות` row (default 100),
  rounded and clamped.
- Duplicate `X` / `X [Auto]` columns are merged. If they hold different
  numeric values the row becomes a conflict instead of a silent choice.
- One `portal_submissions` row (empty text, `graded`, current revision) and one
  `portal_grades` row are created per student/assignment.
- `graded_at` is the column due date at 12:00, then the portal assignment
  deadline, then now. The result keeps the raw score and source file/row in
  `result_json.historical_import`.
- Imported grades use `configured_model = "classroom-import"`. They are marked
  `historical` in the gradebook, profile and both result pages. The engine
  never grades them and the UI refuses a regrade. Classroom exports have no
  hand-in time, so historical cells are never marked late.
- A real portal grade (engine output) is **never** overwritten; the row is
  reported as a conflict. A submission with text but no grade is also left
  alone.
- Rerunning refreshes a changed score in place and reports unchanged rows;
  it does not create duplicates.

## Where the data appears

- `/teacher/classes/{id}/gradebook` includes historical scores in cells,
  averages and counts. Draft assignments are not columns, so their imported
  grades are stored but wait for publication.
- `/teacher/students/{id}/profile` adds the alephon contact card, a historical
  notice, a recent-score trend (including historical entries) and a
  `היסטורי` badge on each imported assignment.
- `/student/submissions/{id}` shows the score and the historical notice, and
  omits the empty "submitted code" card.
- `/teacher/submissions/{id}` replaces the model/rubric/audit details with a
  historical explanation and hides the regrade button.

## Limitations

- Grades are only imported for assignments that already exist in the portal.
  Publish the class content (Phase 6) before importing, or the columns are
  listed as skipped.
- A class with no published assignments stores its grades but shows nothing in
  the gradebook yet. This is the normal state for ז'3 until its assignments
  are published.
- The Classroom export is a snapshot. Upload a fresh CSV and rerun to pick up
  new columns or corrected scores; the mapping file can be reused.
- The importer never writes to Google Classroom and never calls the grading
  engine.
- Passwords are only in the credentials CSV; there is still no in-app password
  reset. Delete the file after distributing the slips.
