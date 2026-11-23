# Phase 5 — gradebook, CSV export and student profiles

Phase 5 adds a teacher-only view of the whole class and a downloadable report.
It is still a **read-only projection** of Phase 3 data: nothing here grades,
changes a score or calls the engine. A screen and its CSV are built from one
snapshot, so the download always matches what the teacher just saw.

## Files

| File | Purpose |
| --- | --- |
| `portal/gradebook/matrix.py` | Students × published assignments, statuses, lateness, averages, class/column totals and the cross-class student profile |
| `portal/gradebook/exporter.py` | UTF-8-with-BOM CSV bytes, CRLF rows, review notes, formula-injection guard |
| `portal/web/gradebook_routes.py` | `GET /teacher/classes/{id}/gradebook`, `GET /teacher/classes/{id}/export`, `GET /teacher/students/{id}/profile` |
| `portal/templates/teacher_gradebook.html` | Responsive matrix with a sticky student column and a totals row |
| `portal/templates/teacher_student_profile.html` | Per-class performance, feedback and review notes |
| `portal/tests/test_phase5_gradebook.py` | Matrix, statistics, CSV encoding, profile and access tests |

## What counts as a column

Only `Assignment.published = true` assignments are columns, ordered by
`due_at` (undated last), then creation time. A draft is invisible to students,
so including it would imply work they were never given.

## What counts as a cell

The portal keeps one `Submission` per (assignment, student) and its
`revision`. A cell reads the grade for **that current revision only**
(`portal.grading.service.current_grade`). Consequences:

- resubmitting hides the old score until the new revision is graded;
- an old `Grade` row can never leak next to new text;
- the status is one of four normalised values:

| Status | Meaning | Badge |
| --- | --- | --- |
| `graded` | The current revision has a stored grade | green |
| `queued` | Handed in; `queued`, `grading` or `deferred` | amber |
| `failed` | The worker could not grade this revision | red |
| `missing` | No hand-in at all | grey |

## Lateness

A cell is late when either:

1. the stored grade carries `late_penalty_points > 0`, or
2. `first_submitted_at > assignment.due_at`.

The first rule preserves a penalty already reflected in the score even if the
deadline is edited later. The second uses the locked first hand-in, matching
`src.late_policy`; a late resubmission of on-time work is not marked late.

## Averages and totals

- A student average is the mean of **graded** scores in that class
  (ungraded/failed/missing cells are excluded). No graded work ⇒ `—`.
- A class average is the mean of all graded cells, not the mean of student
  averages. A student with two graded assignments therefore contributes two
  values.
- Column statistics count graded, queued, failed, missing, submitted, late and
  needs-review cells and show the column average.
- “הוגשו” counts any hand-in, including failed and queued ones; “הושלמו”
  divides that by `students × assignments`.
- Scores are rounded to one decimal for display and CSV.

There is no weighting: every published assignment counts equally. Weighted
grades would require a schema change and are intentionally out of scope.

## CSV export

- Encoding: **UTF-8 with BOM** (`utf-8-sig`) so Excel on Windows reads Hebrew
  names correctly.
- Line endings: CRLF, as Excel expects.
- One header row, one row per enrolled student:
  `שם תלמיד`, `שם משתמש`, one column per published assignment, then `ממוצע`,
  `הוגשו`, `חסרים`, `ממתינים לבדיקה`, `נכשלו`, `באיחור`, `לסקירה` and
  `הערות בדיקה`.
- Graded cells are numeric, so Excel can average them. Ungraded cells carry
  one of the Hebrew statuses instead.
- Duplicate assignment titles get ` (2)`, ` (3)` suffixes so columns stay
  distinguishable.
- Review notes are aggregated per student as `assignment: note`, joined with
  ` | `, with line breaks collapsed to spaces.
- Text cells starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with an
  apostrophe to prevent spreadsheet formula injection.
- The download is named `gradebook-<class code>-<YYYYMMDD>.csv` (ASCII only,
  because HTTP headers cannot carry Hebrew here).
- `Cache-Control: no-store` is set on the CSV and both HTML pages.

## Student profile

`/teacher/students/{id}/profile` lists every class owned by the signed-in
teacher in which the student is enrolled, with each published assignment's
status, score, lateness, feedback and review note. The overall average spans
all those classes. A student enrolled only in another teacher's class yields
**404**, as does an unknown or non-student id.

## Access rules

- All three routes require `require_teacher`; a signed-in student gets 403 and
  an anonymous visitor is redirected to `/login?next=…` (303).
- Class and student ids are checked against the teacher's own classes. Foreign
  ids return 404, never 403, so ids cannot be probed.
- No rubric path, hash, answer key or model name appears in either template or
  in the CSV.

## Tests

`portal/tests/test_phase5_gradebook.py` covers:

- published-only columns and draft exclusion;
- current-revision isolation (an old score of 40 never replaces 90);
- graded/queued/failed/missing statuses, queued-late and penalty-late;
- class, column and student averages, counts and completion percentage;
- missing students (no submission) and students with no graded work;
- profile scope across owned classes and 404 for foreign/unknown students;
- BOM, CRLF, Hebrew headers, numeric scores, status text, review notes and
  duplicate-safe columns;
- formula-injection neutralisation for names, titles and notes;
- teacher access, student 403, anonymous 303 and foreign-id 404.
