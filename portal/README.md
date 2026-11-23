# Portal (Phase 0 → Phase 7)

A mobile-first Hebrew learning portal that runs **beside** the existing
Classroom AutoGrader. It reads the existing grading engine; nothing in
`src/`, `app.py`, `config/` or `tests/` was modified.

## What exists now

- **Phase 0 — fidelity bridge**
  - `portal/grading/adapter.py` is the only module that imports `src/`.
    Every grade comes from `src.evaluator.core.evaluate_student_submission`
    (through the `src.llm_evaluator` facade) with the student's text verbatim.
  - Late points come from `src.late_policy.penalty_for` with
    `late_state=None`, `is_late=None`, and `first_submitted_at` locked at the
    first hand-in.
  - The deterministic audit sidecar is popped once, immediately, same thread,
    and stored on the `GradeArtifact`.
  - Rubrics are loaded through `src.main.load_rubric`, fingerprinted by
    SHA-256, and path-restricted to `assignments/` and `configs/`.
  - `portal/grading/baseline.json` records the engine file hashes, constants,
    git commit and measured existing-suite state. Regenerate with
    `python -m portal.grading.build_baseline`.
  - `portal/tests/test_parity.py` proves the adapter and a direct engine call
    produce identical grades, prompts, audits and late penalties.
- **Phase 1 — skeleton and auth**
  - FastAPI app (`portal/main.py`), SQLite via SQLAlchemy (`portal/db.py`,
    `portal/models.py`), Argon2id passwords, signed session cookies, CSRF on
    every state-changing form, and an in-memory login throttle.
  - Teacher-provisioned student accounts, classes with join codes,
    enrollments, assignment listings bound to rubric files, publish/unpublish
    with hash re-verification, rename/delete for classes and assignments.
  - A published assignment keeps its deadline editable (`תאריך יעד` on the
    class content tab): set, move or clear it without unpublishing the task.
    An empty date removes the deadline; a date means 23:59 in the school's
    zone, and submissions stay open afterwards with the usual late penalty.
  - One-shot password reset from the roster or the student profile
    (`POST /teacher/students/{id}/reset-password`). A random password is
    generated with the same alphabet as the roster import, only its Argon2id
    hash is stored, and the plaintext is rendered once in a `no-store`
    response — never in a flash/cookie, a URL or a log. Any login lockout for
    that username is cleared so the student can sign in immediately.
  - The login form can reveal the password while typing (the eye button beside
    the field). It switches the same input between `password` and `text` in
    `app.js`, so the typed value, autofill and the posted field name never
    change; the button reports its state with `aria-pressed` and a Hebrew
    label.
  - Mobile-first Hebrew RTL templates and CSS (no CDNs; works offline on the
    school network).
- **Phase 2 — lessons and student-safe rubric views**
  - Global or class-linked lessons (`portal_lessons`) with ordered slides
    (`portal_lesson_slides`). Students read them in a phone-first slide viewer
    at `/student/lessons/{id}?slide=N`; no Google Drive or external account is
    involved.
  - Teacher lesson management at `/teacher/lessons`: create/edit/delete,
    choose global vs. class scope, publish/unpublish, add/edit/delete slides,
    and reorder them up/down. Every route checks ownership; foreign ids return
    404.
  - A slide can carry a picture the teacher uploads from a phone or laptop
    (`portal/ingestion/images.py`). PNG/JPEG/WebP/GIF/BMP/TIFF up to 8 MB are
    accepted, decoded with Pillow, capped at 1600 px on the long edge and
    re-encoded to WebP — which also strips EXIF, so a photo never ships its
    camera location to students. The bytes land in the same content-addressed
    store imported decks use, so the existing teacher/student image routes
    serve them with no second code path. A picture-only slide needs no title or
    text; `alt_text` (300 chars) describes it for screen readers and defaults to
    the slide title. Replacing or removing a picture, deleting the slide or
    deleting the lesson prunes the blob only after the change is committed and
    only when no other slide or attachment still references it.
  - `portal/grading/redaction.py` builds a new allowlisted rubric view for
    students. It never emits `correct_answer`, `expected_output`,
    `corrected_code` (or other answer keys), and scrubs answer values embedded
    in question prose (for example `"התשובה: first name"`). The broken code of
    debugging questions is kept so the task remains solvable; code identical
    to the answer is omitted. Titles, topics, sections, weights, questions and
    criteria remain visible.
  - Student assignment pages verify the rubric SHA-256, render the redacted
    view server-side, and fail closed with a neutral 503 if the file changed
    or disappeared. Rubric paths, hashes and answers never reach the browser.
  - `portal/tests/test_phase2_redaction.py` runs over **every** JSON file in
    `assignments/` and `configs/`; `portal/tests/test_phase2_lessons.py`
    covers CRUD, visibility, slide order, XSS escaping, CSRF and ownership.
- **Phase 3 — submissions, queue and grades**
  - One `portal_submissions` row per (assignment, student) with a revision
    counter. Resubmitting changes the text and bumps the revision, but
    `first_submitted_at` stays frozen for the late policy.
  - `POST /student/assignments/{id}/submit` validates, stores the text
    verbatim, queues a durable `portal_grading_jobs` row and returns
    `202 Accepted` (JSON) or a flash + `303` (plain form). An identical
    resubmission is idempotent and costs no model call.
  - One daemon `GradingWorker` processes jobs sequentially through
    `portal/grading/adapter.py` and the engine's own pacer. Groq 429s and an
    exhausted daily quota become `deferred` jobs with a retry time and a
    worker-wide cooldown; genuine engine errors become `failed`. A job whose
    revision was superseded is skipped.
  - `portal_grades` stores the complete `GradingResult` JSON plus score,
    Hebrew feedback, deduction breakdown, audit verdict, rubric hash/title,
    late points and model. A regrade clears any previous review.
  - Students see the score, feedback, per-task status and deductions at
    `/student/submissions/{id}` (no rubric paths, hashes, answers or model).
  - Teachers get `/teacher/grading` (per-class counts), a full-roster grading
    page per assignment, a submission review page with the complete audit,
    review notes and a manual regrade action.
  - The teacher class screen splits into two tabs: `תוכן` (lessons and
    assignments) and `תלמידים` (the roster with add/remove). Both tabs show
    counts, and add/remove actions return to the students tab
    (`?tab=students`).
  - See `portal/docs/phase-3-grading.md` for the schema, state machine and
    failure semantics.
- **Phase 4 — secure execution playground**
  - `portal/sandbox/executor.py` runs one program per fresh
    `python -X utf8 -I -S` interpreter in a throwaway temp directory and
    returns `stdout`, `stderr`, `exit_code`, `execution_time_ms`, `timed_out`
    and `truncated`. It never touches the grading engine or the database.
  - Hard four-second wall clock (exit `124`), CPU/memory caps (Windows Job
    Object; POSIX `RLIMIT_CPU/AS/FSIZE/NOFILE/CORE`), 64 KiB per stream,
    two concurrent runs and a stripped environment.
  - The child-side audit hook blocks sockets/DNS, subprocess/process
    creation, `ctypes`/native loading, dangerous imports, reads outside the
    working directory and stdlib, writes outside the working directory and
    `chdir` escapes. Errors render student frames only.
  - Student-only session endpoints with CSRF (JSON errors) and a per-student
    20-runs-per-minute limit. The assignment page gains an English IDE-style
    editor with a "Run code" button and a docked console driven by `app.js`;
    output streams while the program runs and `input()` prompts are answered
    from the console line — no page reload and no saved code. The one-shot
    `POST /student/sandbox/run` remains for simple runs and tests.
  - `portal/tests/test_phase4_sandbox.py` covers timeout, syntax/runtime
    errors, stdout/stderr separation, interactive input, output deltas,
    output cap, temp cleanup, memory exhaustion, outside reads/writes,
    network/subprocess/ctypes blocking, env stripping, rate limiting, CSRF,
    session ownership, size caps and access rules.
  - See `portal/docs/phase-4-sandbox.md` for the threat model.
- **Phase 5 — gradebook and exports**
  - `/teacher/classes/{id}/gradebook` maps every enrolled student against
    every published assignment. A cell uses the **current revision only**, so
    a resubmission hides the old score until the new text is graded. Cells are
    `graded`, `queued`, `failed` or `missing`; lateness comes from the locked
    first hand-in or a stored late penalty.
  - Class, per-assignment and per-student averages sit above and below a
    responsive matrix with a sticky student column. Counts cover submitted,
    missing, queued, failed, late and needs-review.
  - `/teacher/classes/{id}/export` downloads a UTF-8-with-BOM CSV (CRLF,
    Hebrew headers, numeric scores, per-assignment columns, per-student review
    notes) with formula-injection protection for Excel.
  - `/teacher/students/{id}/profile` shows one student's work across every
    class the teacher owns, including feedback and review notes.
  - All three routes are teacher-only and ownership-checked (foreign ids return
    404). The class page links to the gradebook and CSV; student names open the
    profile.
  - See `portal/docs/phase-5-gradebook.md` for the matrix and CSV contract.
- **Phase 6 — class expansion and content replication**
  - `python -m portal.scripts.sync_classes` plans the initialization of
    **ז4, ח3, ח4, ח6, ט2, ט3, ט4** from the content already imported into
    the portal (currently ז3; ח8 is excluded). It matches each name to its
    Google Classroom course, creates or reuses the portal class, links
    `portal_classroom_links`, and copies every lesson, slide deck and
    assignment through the existing reuse helpers.
  - Publication is per class: `classroom` mirrors that course's real
    `PUBLISHED`/`DRAFT` state, `draft`/`published` are fixed, and `source`
    copies the base item's state. `--class-mode CLASS=MODE`, `--publish TEXT`
    and `--draft TEXT` override individual classes or items; unreached items
    stay drafts.
  - `/teacher/sync-classes` is the same plan in the teacher UI (cached catalog,
    no Google calls), with a checkbox, mode and item list per class.
  - The planner is read-only until `--apply`; applying is idempotent — items
    are found by provenance or normalized title, so reruns only correct
    publication flags and never duplicate content.
  - See `portal/docs/phase-6-class-sync.md` for matching, modes and failure
    semantics.
- **Phase 7 — student directories and historical grades**
  - `python -m portal.scripts.import_students` reads every class folder
    (`.xlsx` alephon + Classroom CSV), creates student accounts keyed by
    national id, attaches contact details, enrolls each student once and
    writes the review mapping. Passwords for new accounts are random and land
    only in the git-ignored `portal/data/student_credentials.csv`.
  - Automatic name matching between Hebrew alephon names and Classroom
    Latin/Cyrillic/handle names is conservative: only complete-name matches
    are pre-confirmed. Everything else is reviewed by the teacher in
    `portal/data/student_mapping.csv` (`student_id`, `confirmed`).
  - `python -m portal.scripts.import_grades` imports a Classroom column only
    when its title matches a portal assignment in the same class. Scores are
    scaled by the `נקודות` row, duplicate `X`/`X [Auto]` columns are merged or
    reported as conflicts, and unmatched columns are listed as skipped.
  - Imported work becomes a normal submission + grade with
    `configured_model = "classroom-import"`. The gradebook, CSV export,
    student profile and result pages mark it `היסטורי`; the engine never
    regrades it and the UI hides the regrade button. Real engine grades and
    non-empty submissions are never overwritten, and reruns are idempotent.
  - Both commands preview by default; `--apply` writes. A class with no
    published assignments keeps its imported grades until the assignment is
    published.
  - See `portal/docs/phase-7-imports.md` for the folder contract, mapping
    columns and conflict rules.
- **Mixed x.3 worksheets**
  - 2.3 and 3.3 ("תרגול לפיצוח") mix free code, fill-in-the-blank programs,
    output/trace tables, true/false claims and multiple-choice blanks, so each
    assignment renders a curated worksheet instead of one code box.
  - Specs are validated JSON files named after the rubric
    (`portal/worksheets/lesson_2_worksheet.json`, `lesson_3_worksheet.json`).
    A missing or invalid spec falls back to the previous single-text page, so
    the Docs are never parsed at request time.
  - Every widget answer still becomes one composed Hebrew document for the
    existing engine; structured values are stored per submission revision in
    `portal_worksheet_responses` so reloads restore exact answers.
  - `portal/tests/test_worksheets.py` covers both live sheets, validation,
    compose/collect, storage, reload, resubmission and teacher preview.
  - Typed answers are also saved before hand-in: `portal_answer_drafts` holds
    one in-progress payload per student and assignment, restored only when it
    is newer than the last submission and deleted on submit. The page autosaves
    1.5 s after typing stops and on tab close. See "Drafts and autosave" in
    `portal/docs/worksheets.md`; `portal/tests/test_answer_drafts.py` covers it.
  - The assignment title in the class page and the grading page's "צפייה
    במטלה" button open the read-only preview; drafts are previewable by their
    owner with a draft notice.
  - See `portal/docs/worksheets.md` for the schema, field names and the steps
    to author a new sheet.
- **Exam sittings**
  - An assignment with `exam_config` is served as an exam: one random version
    and one shuffled item order per student, a single hand-in, and marking from
    `portal/exams/checks/units_1_3.json` by the local checker (`exam-checker`),
    with no model call and no Run button. The window is enforced by the engine:
    the paper is not served before `opens_at` and nothing is accepted after
    `closes_at`.
  - The key is a graded rubric, not a bare list of answers: every traced line
    carries its own points, a Hebrew or English explanation of the error earns
    the full explanation points on its own (the corrected code is marked
    separately), and a corrected program is marked on independent criteria (it
    runs, it prints the value, and the source really converts or joins). Hebrew
    keywords match inside a word (`במרכאות`, `המחרוזת`); Latin tokens match at
    a word start, so `int` inside `print` is not a conversion. A
    program-level question checks two different inputs, so a hardcoded `print`
    loses the second one. `python -m portal.scripts.exam_tools verify` proves
    the points add up, that every tracing snippet prints what the key expects,
    and that the key's own reference solution scores 100;
    `... exam_tools rubrics` regenerates the engine view. The teacher-facing
    breakdown is in `portal/docs/exam-units-1-3-rubric.md`.
  - The window is editable from the class page ("חלון הגשה" next to the exam)
    without unpublishing it mid-lesson. Both ends are `datetime-local` values in
    the school's zone, converted to UTC on the way in; a missing or reversed
    window is rejected and nothing is half-written. Homework keeps its
    date-only deadline instead.
  - A make-up sitting is granted per student in the same panel: pick one
    enrolled student and give them their own two hours ("מועד אישי להשלמה").
    The grant lives in `portal_exam_windows` and replaces both ends for that
    student only, so the class window stays closed for everyone else; the
    student's gate, countdown, autosave and hand-in all read the personal
    window. Clearing the grant restores the class window. A new table is
    created by `create_all` on startup, so no migration is needed.
  - While the exam is open and unsent, the student page shows a sticky
    countdown to the server's `closes_at` (`data-exam-clock` in the template,
    driven by `[data-exam-countdown]` in `app.js`). It warns during the last ten
    minutes and sends the paper automatically about five seconds before the
    deadline, while the server-side window is still open — the submit button is
    never disabled. An entirely empty paper is not sent; it shows "הזמן תם" and
    the student can still submit manually until the server closes. The
    countdown measures time against the server's own clock (`data-server-now`),
    so a wrong device clock cannot send early or late, and the server
    independently refuses a late hand-in.
  - The checker is exact by design, so the teacher can override one question
    on the submission review page: enter the points (0 to the question's
    maximum) with an optional note, or press "ניקוד מלא" to accept the answer.
    The automatic value is stored alongside the override, "ביטול התאמה"
    restores it, and every change is appended to the review note. The score,
    feedback, deductions and audit are recomputed from the adjusted questions,
    so the student page, gradebook and CSV export all agree. Only exam hand-ins
    (`configured_model = exam-checker`) expose these controls.
  - The result is the teacher's to publish. A hand-in shows the student a
    confirmation popup ("המבחן הוגש") and nothing else: no score, no feedback
    and no paper. The class page then offers three states through
    `exam_release` on the assignment (migration 0002, default `held`):
    `held` keeps the confirmation, `scores` shows the number alone, and
    `full` adds the feedback, the per-question detail and the read-only paper.
    The status labels in the student feed, the submission page and the exam
    page all follow the same state, so a class sitting days later cannot read
    a paper another class already sat. The teacher can return to `held` at any
    time. Only an exam accepts a release; an ordinary assignment still shows
    its grade as soon as it is graded.
  - `portal/tests/test_exam_engine.py` covers allocation, shuffling, window
    enforcement, the teacher's window editor, personal make-up windows, the
    countdown markup, release states and their hidden surfaces, the hand-in
    popup, manual point overrides and marking.

## Run it

From the project root, in the existing virtual environment:

```powershell
.venv\Scripts\python -m pip install -r portal/requirements-portal.txt

# Create the first teacher (password prompt; never in shell history)
.venv\Scripts\python -m portal.scripts.bootstrap_teacher --username mori --name "מורי כהן"

# Start the portal on its own port; Streamlit keeps running separately
.venv\Scripts\python -m uvicorn portal.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Health check: `GET /healthz`.

### A public address for phones and lab computers

`run_public.ps1` starts the portal if it is not running, opens a Cloudflare
Quick Tunnel to it and prints the temporary HTTPS address:

```powershell
powershell -File portal\scripts\run_public.ps1
```

The same address is written to `portal\data\public_url.txt`. Give it to
students; they sign in with the national ID and password they received.

- The tunnel only forwards traffic: the database, grading worker and sandbox
  stay on this computer.
- This computer must stay **on and online**; the portal and tunnel processes
  run in the background. If the computer restarts, run the script again.
- A Quick Tunnel has no account and no SLA. The address changes whenever
  `cloudflared` restarts, so run the script again and hand out the new address.
- Students on shared lab computers should sign out when they finish. Saved
  answers survive either way, because drafts live on the server.

### Move it off this computer

A small Linux server gives the portal a stable HTTPS address and keeps it
running when this laptop is off. `portal/docs/deploy-linux.md` has the full
sequence (server, free DuckDNS name, upload, install, verification,
backups, updates, rollback). Two scripts do the mechanical work:

```powershell
# first install: code + a consistent database snapshot, with the Windows
# rubric paths rewritten for the server (the laptop database is not touched)
.venv\Scripts\python -m portal.scripts.build_deploy_bundle

# later updates: code only, never the server's database
.venv\Scripts\python -m portal.scripts.build_deploy_bundle --code-only
```

`python -m portal.scripts.check_engine` runs one real grade through the
existing engine without writing a submission; use it after the move to prove
the rubrics, API keys and audit all work on the server.

The Cloudflare tunnel above stays the fallback until the server has been
verified.

### See Phase 3 with demo data

No Groq call is needed to walk the grading screens:

```powershell
.venv\Scripts\python -m portal.scripts.seed_phase3_demo --teacher <your-username>
```

This adds the class **הדגמת Phase 3 (אפשר למחוק)** with four demo students
(`demo.noa`, `demo.itai`, `demo.maya`, `demo.dan`; throwaway password
`DemoPass123`) and every grading state. The same data feeds the Phase 5
gradebook, CSV export and student profiles. Remove it with
`python -m portal.scripts.seed_phase3_demo --cleanup`; the script touches only
its own demo rows.

### Expand the real classes (Phase 6)

Preview first; nothing is written without `--apply`:

```powershell
.venv\Scripts\python -m portal.scripts.sync_classes --teacher <your-username>
.venv\Scripts\python -m portal.scripts.sync_classes --teacher <your-username> --apply
```

The first run creates the seven target classes, links each to its Google
Classroom course and copies the existing lessons and assignments with
publication
derived from that course's `PUBLISHED`/`DRAFT` state. The same plan is at
`/teacher/sync-classes`. Re-running is safe: existing copies are matched by
provenance or title and only their `published` flag is corrected. Pass
`--refresh` to re-scan Classroom first, and `--class-mode CLASS=MODE`
(`classroom`, `draft`, `published`, `source`) to override one class.

### Import the real rosters and grades (Phase 7)

Keep one folder per class with the alephon `.xlsx` and the Classroom CSV:

```powershell
# 1. Preview + write the name-matching review file.
.venv\Scripts\python -m portal.scripts.import_students --source "C:\Users\...\אלפונים"

# 2. Create accounts/enrollments (random passwords -> portal/data/student_credentials.csv).
.venv\Scripts\python -m portal.scripts.import_students --source "C:\Users\...\אלפונים" --apply

# 3. Correct student_id / confirmed in portal/data/student_mapping.csv, then:
.venv\Scripts\python -m portal.scripts.import_grades --source "C:\Users\...\אלפונים"
.venv\Scripts\python -m portal.scripts.import_grades --source "C:\Users\...\אלפונים" --apply

# 4. Exam/quiz grades: one "הזנת ציונים לאירוע ציון" workbook per class folder.
.venv\Scripts\python -m portal.scripts.import_exam_grades --source "C:\Users\...\אלפונים"
.venv\Scripts\python -m portal.scripts.import_exam_grades --source "C:\Users\...\אלפונים" --apply
```

Both commands are read-only without `--apply`, skip unmatched columns instead
of guessing, never overwrite a real portal grade, and rerun without
duplicates. Exam events become grade-only assignments per class (no rubric,
no submission form); students see their score, and a missing score is shown
as "לא הוזן ציון". Details: `portal/docs/phase-7-imports.md`.

To replace the random initial passwords with a rule students can remember —
the given name (second word of the school name) plus the national id, e.g.
`אביה220019152` for `"אבורמד אביה"` — run:

```powershell
.venv\Scripts\python -m portal.scripts.set_student_passwords
.venv\Scripts\python -m portal.scripts.set_student_passwords --apply
```

The first command prints the plan only (251 students, the demo account is
skipped because it has no ת.ז.). `--apply` hashes every password before
opening the write transaction, updates all rows in one short transaction, and
never prints a password. `student_credentials.csv` keeps the old random
passwords and no longer matches the accounts after this run.

### Hand the students their passwords

```powershell
.venv\Scripts\python -m portal.scripts.export_student_credentials
```

Writes `portal/data/credentials/` (private, git-ignored): a CSV, an index page
and one printable RTL page per class. Each class page has a search box — type a
name or ת.ז. to show one card — and a print button for cutting into slips. The
export refuses to write anything unless **every** password verifies against its
stored hash, so a student who was reset separately cannot receive a wrong slip.
Run it on the server after a roster or password change, then download the
folder; do not publish it or leave it open on a shared screen.

Configuration is optional — see `portal/.env.example`. The database defaults
to `portal/data/portal.db`; the signing key is generated once in
`portal/data/secret_key`. Both are git-ignored.

### Changing the schema of a database that already exists

`create_all` creates missing tables but never alters one that is already on
disk. A change to an existing table — a new column, a type change, a backfill —
is therefore an ordered migration in `portal/migrations/__init__.py`:

```python
Migration("0001", "הוספת תקציר לשיעור", lambda conn: conn.execute(
    text("ALTER TABLE portal_lessons ADD COLUMN summary TEXT DEFAULT ''")))
```

Append it to `MIGRATIONS`, add the matching field to `portal/models.py`, and
restart. The server applies pending migrations on startup right after
`create_all`, so a deployed bundle upgrades itself. New *tables* still belong
in `models.py` alone.

```powershell
.venv\Scripts\python -m portal.scripts.migrate            # status, read-only
.venv\Scripts\python -m portal.scripts.migrate --dry-run  # what would run
.venv\Scripts\python -m portal.scripts.migrate --apply    # server stopped
```

Status and `--dry-run` never write — they do not even create the tracking table
(`portal_schema_migrations`). Each migration runs in its own transaction on a
private engine with manual transaction control, because Python's sqlite3 driver
would otherwise autocommit DDL and make a rollback impossible. The schema change
and its tracking row commit together; a failure rolls back, raises, and stops
startup rather than serving a half-migrated schema. Foreign keys are off on that
connection so a table rebuild is possible, and `PRAGMA foreign_key_check` runs
afterwards — a violation also stops startup. Never edit a migration that has
already been applied anywhere; add a new one.

## Tests

```powershell
.venv\Scripts\python -m pytest portal/tests -q
```

The parity tests use the same fake-LLM seams as `tests/test_json_repair.py`;
no network or API key is needed. `portal/tests/test_phase3_grading.py`
replaces the live grading worker with a recorder, so the suite never calls
Groq either. Phase 4 executor tests launch real child interpreters; route
tests use fake executors, so nothing depends on HTTP or Groq. Phase 5 tests
insert grades directly and never call the engine. Phase 6 tests drive the
planner, applier, CLI and teacher screen against a fake Classroom catalog, so
they need no Google credentials. Phase 7 tests build a minimal XLSX and
Classroom CSV, then drive both planners, the appliers, the mapping file, the
gradebook/profile and the web guards without touching the real database.
Current state: 380 passed, 5 skipped.

`portal/tests/test_teacher_actions.py` covers the two teacher actions added
after Phase 7: the one-shot password reset (reveal, hash-only storage, lockout
clearing, CSRF, ownership, teacher-only) and editing a live deadline (set,
clear, invalid value, guards, published-only editor).

`portal/tests/test_slide_images.py` covers uploaded slide pictures: WebP
re-encoding with EXIF stripped, downscaling, transparency, rejection of empty /
oversize / non-image / truncated files, teacher upload and student serving,
picture-only slides, plain forms without a file, blob pruning on replace /
remove / slide delete / lesson delete, a blob shared by two slides surviving one
removal, alt-text-only edits, and the CSRF / role / ownership guards. Its
autouse `store` fixture keeps every test blob in `tmp_path`, so the suite never
writes into the real `portal/content`.

`portal/tests/test_migrations.py` covers the migration runner: ordering,
run-once behaviour, a real rollback of half-applied DDL, malformed and duplicate
versions, a recorded version missing from the codebase, the foreign-key check,
in-memory databases, startup wiring, and the CLI's read-only status, `--dry-run`,
`--apply` and failure exit code. Every test uses its own temporary database and
passes `--database-url` explicitly, so the live database is never a CLI default.

The pre-existing suite is run separately and is unchanged:

```powershell
.venv\Scripts\python -m pytest tests -q
```

Root-level `pytest` still stops on `src/test_script.py` (null bytes); scope to
`tests/` and `portal/tests/`. The 13 pre-existing failures recorded in
`baseline.json` are environmental and were not touched.

## Fidelity rules (do not break)

1. Pass the student's text to the engine **verbatim** — no trimming or
   escaping in the portal.
2. Do not edit the prompt, including its "Google Doc" wording; changing it
   changes grades.
3. Never expose a rubric file, its path, its hash or its answers to a student
   route or template. Only `portal.grading.redaction.redact_rubric()` output
   may be rendered.
4. Re-verify `rubric_sha256` when publishing **and** when serving an assignment
   to a student; if the file changed, refuse instead of showing or grading
   against different answers.
5. Pop `last_audit_for` exactly once, immediately, in the same thread.
6. `first_submitted_at` is written once, on the first hand-in, and never
   updated by a resubmission.
7. A lesson is visible to a student only when it is published and either
   global or linked to a class the student is enrolled in.
8. A grading job grades exactly the revision it was queued for. If the student
   resubmitted meanwhile, mark the job `superseded`; never grade stale text.
9. A deferred job (quota/rate limit) is not a failure and must not delete the
   submission or an older grade.

## Phase 1–7 limitations (intentional)

- One web process and one grading worker per database. The queue itself is
  durable; do not run a second worker against the same SQLite file.
- `create_all` plus a small ordered migration runner (`portal/migrations`).
  New tables are still created from the models; changes to existing tables go
  through a versioned migration applied on startup. No third-party migrator and
  no downgrade path: migrations are forward-only, and the registry is empty
  today because every live table was created by `create_all`.
- Lesson slides accept text, code and one uploaded picture each. Arbitrary file
  attachments on a manually authored slide are still a later addition (imported
  assignments already keep their attachments).
- Login throttling and the sandbox run limit are in-process; move them to the
  database before running more than one web worker.
- No self-service or email password reset: a teacher resets a student from the
  roster or profile page, and the bootstrap script can update a teacher.
  Resetting does not revoke an already-signed-in session cookie; it stays valid
  until it expires or the student logs out.
- The sandbox is a Python-level sandbox with OS caps, not a hardened
  container: run it under a dedicated OS account and, for untrusted network
  users, behind an outbound firewall or in a container/VM. Its audit hook
  blocks Python networking, not raw OS traffic from a compromised
  interpreter.
- The playground supports the standard library only, has no debugger and
  keeps nothing between runs. See `portal/docs/phase-4-sandbox.md`.
- Phase 6 creates classes and copies content; it does not enroll students, and
  it never writes to Google Classroom. Publication is chosen locally or
  mirrored from the cached catalog — syncing a changed Classroom course needs
  `--refresh`.
- Phase 7 imports only into assignments that already exist. Historical grades
  for a class with no published assignments are stored but invisible in the
  gradebook until publication. Imported grades have no code, rubric or audit,
  cannot be regraded by the engine, and the Classroom export is a snapshot
  that must be refreshed to pick up later work.
