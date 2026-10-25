# Classroom AutoGrader

Automated Python code checker for Google Classroom submissions.

> **Status:** Foundation only — project scaffold and Google OAuth are set up.
> Classroom/Drive data retrieval is intentionally not implemented yet.

## Project layout

```
Clasroom AutoGrader/
├── config/
│   ├── __init__.py
│   └── settings.py        # .env + pydantic-backed runtime settings
├── src/
│   ├── __init__.py
│   ├── auth.py            # Google OAuth helper (get_google_credentials)
│   ├── api_retry.py       # retry policy for transient Google API failures
│   ├── classroom_service.py # Classroom / Drive / Docs API wrapper
│   └── llm_evaluator.py   # Groq grading + prompt
├── .env.example           # template for .env
├── .gitignore
├── requirements.txt       # pinned production dependencies
└── README.md
```

Secrets that must never be committed (all ignored via `.gitignore`):

- `.venv/` — local virtual environment
- `.env` — environment variables
- `credentials.json` — OAuth client secrets (Google Cloud Console)
- `token.json` — issued user tokens (created automatically)

## Setup

### 1. Create and activate the virtual environment

PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

bash/zsh:

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
# source .venv/bin/activate     # macOS/Linux
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Configure Google OAuth

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the **Google Classroom API** (and Drive API for later
   stages).
2. Create an **OAuth client ID** of type **Desktop app**.
3. Download the JSON and save it as `credentials.json` in the project root.
4. Optionally copy `.env.example` to `.env` and adjust values.

### 4. Verify authentication

```bash
python src/auth.py
```

On first run a browser window opens for the Google consent flow; the resulting
token is stored in `token.json`. Subsequent runs refresh it automatically.
Success output:

```
Authentication successful!
```

### 5. Preview submissions (interactive test CLI)

```bash
python -m src.fetch_submissions
```

Lists active courses -> pick an assignment -> prints a
`Student Name | Submission Status | First 3 lines of code` preview for every
TURNED_IN submission whose work is a Google Doc (other attachment types are
flagged as `Unsupported attachment type`).

### 6. Evaluate a submission with Groq (single-pass LLM grading)

1. Create an API key at [Groq Console](https://console.groq.com/keys) and set
   `GROQ_API_KEY` in `.env` (see `.env.example`).
2. Run the example evaluation:

```bash
python -m src.test_llm_grading
```

Loads the teacher context from `assignments/lesson_1.json`, evaluates a mock
student Google Doc with `qwen/qwen3.8-27b` (configurable via `GROQ_MODEL`;
OpenAI SDK against `GROQ_BASE_URL`, temperature 0.2, strict JSON-Schema output
validated against the Pydantic `GradingResult` model), and prints the result
JSON plus formatted Hebrew feedback.

### 7. Full grading pipeline (primary CLI)

```bash
python -m src.main
```

Menu-driven **Review & Release** workflow (nothing reaches students without
an explicit confirmation):

1. **[1] Evaluate new submissions** - pick course -> assignment ->
   `assignments/*.json` rubric config -> grade all TURNED_IN Google Docs with
   Groq (429/500/502/503/504 retries included) -> summary table -> Mashov CSV
   (UTF-8-SIG) **and** a JSON evaluation report. Both land in
   `exports/<Course Name>/<Assignment Title>/` (folders are created
   automatically), so classes, assignments, and re-submissions never mix in a
   flat listing: the CSV keeps the stable name `<Assignment Title>.csv` (one
   per assignment, overwritten each run) and every run adds a timestamped
   `<YYYYMMDD-HHMMSS>.json` report. No Google Classroom state is changed.
   The rubric choice is remembered per assignment in
   `config/assignment_mapping.json` (`course_work_id` -> `assignments/*.json`),
   so later runs of the same assignment use it automatically instead of
   prompting; you are only asked again if the mapped file was deleted,
   renamed, or cannot be parsed (the mapping then self-heals on re-selection).
2. **[2] Push grades & feedback from a saved report** - lists saved reports
   recursively (newest first, shown as
   `exports/<Course>/<Assignment>/<timestamp>.json`; legacy flat
   `exports/*.json` files are still listed) -> shows the student/score summary
   -> asks
   `Publish grades & comments and return submissions to students? (yes/no)`
   -> asks which delivery channels to use -> for each student: set the grade,
   **write `feedback_hebrew` into their Google Doc** (Docs API), optionally post
   it as a comment (Drive Comments API), and return the submission.
   Per-student failures never abort the batch.
   **Cleanup:** when every student was released with no remaining errors, the
   report JSON and its CSV are deleted automatically (empty
   class/assignment folders are pruned too). If any grade, doc write, comment,
   or return failed, the report is kept in `exports/` and the pending students
   are listed, so the issue can be fixed and option [2] re-run.

### Feedback delivery options

Feedback is delivered as a **Google Drive comment** on the student's own
document, anchored to the end of their work so it appears as a bubble in the
margin. The student's code and text are never modified.

Google Classroom only allows a Developer Console project to modify coursework
that the *same* OAuth client created. Assignments made in the Classroom UI
therefore return **HTTP 403** on the grade patch - the score never lands. The
comment still reaches the student:

| Option | Default | What it does |
| --- | --- | --- |
| Publish grades to Classroom | on | `studentSubmissions.patch` (fails with 403 for foreign coursework) |
| Leave a comment on the Google Doc 💬 | on | `comments.create` / `comments.update` (Drive Comments API) |
| Return submissions to students | on | releases the submission; **always skipped** when the grade did not publish |

A grade failure never blocks the comment, so a restricted assignment still
delivers feedback.

**Publishing is idempotent.** Every comment this app leaves carries an invisible
ownership tag (zero-width characters - the student sees only the feedback
text), so a re-run:

* **updates** that comment with the current text instead of adding a second
  bubble - running the release ten times still leaves exactly one comment;
* **deletes** any extra marked comments left by an earlier run or a
  double-click, and reports how many it cleaned up;
* **replaces** (delete + create) a comment Google refuses to edit, e.g. one
  the teacher already resolved.

Comments the teacher wrote themselves are never matched, rewritten or deleted.
The anchor is placed on the last non-empty line using Google's own per-element
`startIndex` values; if Google rejects the range (empty document, table-only
body) the comment is posted unanchored rather than failing. A 403/404 is
*not* retried unanchored, since that would only hide the real error.

**Late submissions are penalised, deterministically.** A submission handed in
after the deadline loses **10 points** (`LATE_PENALTY_POINTS`, set it to `0` to
switch the rule off). The rule lives in `src/late_policy.py` and is applied in
code *after* grading - never delegated to the model, so it is applied
identically to every late student rather than whenever the model remembers.

The configured value is read in `config/settings.py` and is deliberately hard
to lose: unset, blank, or unparseable all fall back to **10**; a value outside
`0-100` is clamped rather than rejected, because a `LATE_PENALTY_POINTS=150`
typo used to raise a `ValidationError` at import time and take the whole app
down on startup. A `0` is the only way to turn the rule off, and it has to be a
real `0` - so both the quoted (`"0"`) and bare (`0`) forms are accepted in
`secrets.toml`, and reading only strings used to turn a bare `0` back into a
10-point charge.

Lateness is resolved from the strongest available signal, in this order:

1. The submission's original hand-in time - from submission history, or the
   submission resource's creation time that Classroom reports with the
   submission. A resubmission moves the submission's last-update timestamp but
   not this instant, so work first handed in on time stays on time no matter
   how late the resubmission is. If the original submission was on time, no
   late penalty is charged for the resubmission.
2. Classroom's own `lateState` verdict, then its `late` flag.
3. Only if neither is reported - a comparison of the submission timestamp
   against the assignment's exact `dueDate` + `dueTime`. Because that timestamp
   is the last update (not the first hand-in), a late resubmission can make
   on-time work look late: when history is unavailable and this is the only
   evidence of lateness, the previous grading comment on the student's
   document is checked - if it never opened with the late-penalty notice, the
   original hand-in is treated as on time and no penalty is charged.

If none of them is available the work is graded as on time and a warning is
logged: a missing signal must never invent a penalty.
(In practice Classroom often reports no verdict at all unless a late-submission
policy is configured, which is why the timestamp fallback exists and logs when
it decides.)

The charge is visible in three places, because a penalty nobody can see is
indistinguishable from no penalty: the score drops, a `Late submission` row is
added to the deduction breakdown (keeping `sum(points) == 100 - score` true even
when a low score is clamped at zero), and the Hebrew feedback opens with
*"העבודה הוגשה לאחר מועד ההגשה, ולכן הופחתו 10 נקודות בגין איחור."* so the
student knows why points were lost. The figure rendered is the number actually
charged, and the sentence takes the singular for a one-point penalty. Re-running
is safe - a previous charge is refunded and the notice is replaced rather than
stacked, including one written by an earlier (misspelled) version of the text.

**"Success" means verified.** After the Comments API answers, the comment is
re-read and its content compared with the generated feedback. If the text is
not actually there, the run reports a failure instead of a false
`Comment: Success`.

**Every comment call carries a valid `fields` selector.** Drive v3 will not
guess a partial response: `comments.list`, `comments.get`, `comments.create`
and `comments.update` **all** answer
`HTTP 400: The 'fields' parameter is required for this method` when it is
missing. A selector is also a liability, though - Drive validates every name in
it against the resource schema and answers `HTTP 400: Invalid field selection
<name>` for anything it does not recognise, and one wrong name fails the entire
call. The `Comment` resource exposes `createdTime`, **not** `created`, and a
Classroom-API habit carried over here once broke listing outright.

Each call therefore sends one explicit selector, kept in
`src/classroom_service.py`:

| Call | `fields` |
| --- | --- |
| `comments.list` | `nextPageToken,comments(id,content,author/displayName,quotedFileContent/value,createdTime)` |
| `comments.get` (verification) | `id,content,resolved` |
| `comments.create` / `comments.update` | `id` |

`id` and `content` are what the app actually needs (the comment id is reused to
update our own bubble, the content is what the feedback marker is matched on);
`author/displayName` and `quotedFileContent/value` make a listed thread readable
without a second round-trip; `createdTime` is the v3 spelling of the creation
time. `nextPageToken` is named explicitly because it sits *beside* the comment
array rather than inside it - name `fields` and pagination has to be asked for.
The verification re-read asks for `resolved` as well, so a thread the teacher
resolved first is visible when a check looks odd.

Leaving `fields` off the verification `comments.get` was the nastier of the
two: the comment is written successfully, and only *then* does the run fail -
so the student has the feedback, the app reports an error, and a re-run tries
to update a comment that was never broken.

The test suite mirrors the Drive v3 `Comment` schema, validates any selector
against it in both nesting spellings Drive accepts (`a(b)` and `a/b`),
*requires* `fields` on all four methods, and answers a call with only the
selected fields - so every one of these failure modes fails in CI instead of in
front of a teacher.

### Streamlit: the cached connection

Streamlit caches the `ClassroomService` - and the OAuth credentials captured
when it was built - for the life of the server process. A server started before
a scope was added would keep using the old token and every comment would fail
while the CLI (which builds a fresh service each run) worked. The grant is
therefore re-validated on every script run and the cache is dropped
automatically when a scope is missing. **🔄 Reconnect Google** in the sidebar
forces a rebuild at any time - use it after running `python src/auth.py` so the
app picks up the new permissions without restarting the server.

**Restart the server after editing the code, not just the token.** Beyond the
cached service, a running server keeps every already-imported module in
`sys.modules`, so a rerun picks up edits to `app.py` but *not* to anything it
imported at start-up. Newly added modules are imported fresh while the old
versions of existing ones are still in memory, which mixes old and new code in
one process - e.g. a server started before the late-submission policy existed
keeps the pre-policy `StudentSubmission` class and fails with
`'StudentSubmission' object has no attribute 'late_state'`. That specific case
now raises a message saying so; in general, **stop and start the server after
changing `src/`**.

### Network resilience

Every Classroom / Drive / Docs request runs through a shared retry policy
(`src/api_retry.py`): a dropped socket, DNS failure, SSL error, or a
`429/5xx` from Google is paused and retried (4 attempts, exponential backoff
with jitter). This is what keeps Windows `WinError 10053`
(`ConnectionAbortedError`) from killing a long grading run with a raw
traceback - Streamlit shows a `🔄 Network hiccup... retrying` notice instead.
Non-transient errors (403, 404, 400) still fail fast with an actionable
message.

Note: leaving comments requires the `drive` OAuth scope. If your `token.json`
predates it, the next run opens the browser once for re-authorization
(equivalently: run `python src/auth.py`).

## OAuth scopes used

- `https://www.googleapis.com/auth/classroom.courses.readonly`
- `https://www.googleapis.com/auth/classroom.coursework.students`
- `https://www.googleapis.com/auth/classroom.rosters.readonly`
- `https://www.googleapis.com/auth/documents.readonly` (read the submission)
- `https://www.googleapis.com/auth/drive` (create/update/delete feedback comments; not covered by `drive.readonly`)

## Running the dashboard

Three scripts manage the Streamlit dashboard. They are scoped to this project's
folder, so other Streamlit apps on the machine are never affected.

| Script | What it does |
| --- | --- |
| `Run-Dashboard.ps1` | Stop any stale instance, then start the dashboard on port 8501 and wait for a health check. |
| `Stop-Streamlit.ps1` | Stop this project's dashboard processes and free port 8501. `-List` for a dry run. |
| `Register-DashboardService.ps1` | Run the dashboard **silently in the background**, starting automatically at logon. |
| `Unregister-DashboardService.ps1` | Stop and remove that background service. |

`Launch-DashboardService.vbs` is the windowless launcher the scheduled task runs;
you never need to call it yourself.

### One-off launch (manual)

```powershell
.\Run-Dashboard.ps1                 # starts on http://localhost:8501
.\Run-Dashboard.ps1 -Port 8502      # different port
.\Stop-Streamlit.ps1 -List          # see what is running, change nothing
```

`.streamlit/config.toml` pins the port to 8501 so a plain `streamlit run app.py`
lands on the same URL, and binds to `localhost` only (the dashboard holds Google
credentials and must not be reachable from the network).

### Always-on background service (recommended)

```powershell
.\Register-DashboardService.ps1              # register; starts at your next logon
.\Register-DashboardService.ps1 -StartNow    # also start it immediately
```

The task `ClassroomAutoGrader-Dashboard`:

- launches **completely hidden** — the action is `wscript.exe` running
  `Launch-DashboardService.vbs`, which starts PowerShell with `SW_HIDE`, and
  `Run-Dashboard.ps1 -Service` starts Streamlit hidden too. **No console, no
  terminal and no taskbar entry is ever created.** (`powershell.exe
  -WindowStyle Hidden` alone is not enough here: the console host can realise a
  visible window first, which is how stray windows ended up on the desktop.)
- triggers **30 s after each logon**, with `StartWhenAvailable` so a missed
  trigger catches up,
- has **no execution time limit** (`PT0S`), so a server that has been up for
  weeks is never killed by Task Scheduler's default 72 h limit,
- **restarts the dashboard itself** if the Streamlit process dies — recovery
  takes a few seconds and does not depend on Task Scheduler's restart policy
  (which is configured as 999 × 1 min as a backstop, but was found not to fire
  reliably on its own),
- ignores overlapping instances (`IgnoreNew`), runs on battery and is never
  stopped for being idle,
- runs in **your** interactive session, which is what makes `localhost:8501`
  reachable from the browser and lets the dashboard use your Google sign-in.

Because it runs as you, the dashboard is only available while you are logged on
— it is not a SYSTEM service, and that is intentional.

```powershell
Get-ScheduledTask     -TaskName "ClassroomAutoGrader-Dashboard"   # status
Stop-ScheduledTask    -TaskName "ClassroomAutoGrader-Dashboard"   # pause it
Start-ScheduledTask   -TaskName "ClassroomAutoGrader-Dashboard"   # start/resume it
Get-Content logs\dashboard-service.log -Tail 40                   # service log
.\Unregister-DashboardService.ps1                                # remove it
```

If a restart is ever needed after editing `app.py`, stop and start the task (or
just run `.\Stop-Streamlit.ps1` — the supervisor notices and relaunches the
current code within seconds).

No Administrator rights are needed for either script. The service clears any
stale instance before it starts (unless you pass `-KeepOld`), so
`http://localhost:8501` is always served by the current code.

## Roadmap

- [x] Project scaffold, pinned dependencies, virtual environment
- [x] Google OAuth authentication helper
- [x] Classroom course / coursework retrieval
- [x] Student submission download (Docs + Drive)
- [x] Retry policy for transient Google API failures (socket drops, `WinError 10053`)
- [x] Feedback delivery: Drive comment and/or text written into the student's Google Doc
- [x] Streamlit UI and CLI release workflows with per-channel delivery options
- [x] Automated grading pipeline and reporting
