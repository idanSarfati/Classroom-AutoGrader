# Phase 3 — Student submissions and the grading engine

Status: implemented. All code lives under `portal/`; `src/`, `app.py`,
`config/` and `tests/` are untouched. The grading engine is used read-only
through `portal/grading/adapter.py`.

## 1. Data model (three new tables, `create_all` upgrades an existing DB)

| Table | Purpose | Key rules |
|---|---|---|
| `portal_submissions` | One row per (assignment, student) | `revision` starts at 1; `first_submitted_at` is written once and never touched by a resubmission |
| `portal_grading_jobs` | Durable work queue | `state` ∈ `queued`, `grading`, `graded`, `failed`, `deferred`, `superseded`; each row pins the submission `revision` it will grade |
| `portal_grades` | One immutable result per (submission, revision) | Full engine JSON plus split score/feedback/deduction/audit columns, and review fields |

`Submission.status` mirrors the latest job for fast lists: `queued`,
`grading`, `graded`, `failed`, `deferred`.

`Grade` stores `result_json` (the complete `GradingResult` model dump),
`deduction_json`, `audit_json`, and denormalised `score`, `feedback_hebrew`,
`audit_ok`, `audit_needs_review`, `audit_summary`, `rubric_sha256`,
`rubric_title`, `late_penalty_points`, `configured_model`, `graded_at`.
Review fields are `reviewed_at`, `reviewed_by`, `review_note`; a regrade clears
them so an old approval cannot silently cover a new score.

## 2. Submission flow

`POST /student/assignments/{assignment_id}/submit` (CSRF protected):

1. Validate: student role, published assignment, enrolment, non-empty text,
   at most `MAX_SUBMISSION_CHARS` (200,000).
2. `service.submit_work()`:
   - First hand-in creates the submission (`revision=1`,
     `first_submitted_at = submitted_at = now`) and a job.
   - Identical text is idempotent: an active job is reused; an already graded
     revision is returned without a new job; a failed/deferred one is requeued.
   - Different text bumps `revision`, refreshes `submitted_at`, leaves
     `first_submitted_at` frozen for the late policy, and creates a new job.
3. The job is committed and the worker is woken.
4. `Accept: application/json` (the JS editor) receives `202 Accepted` with
   `submission_id`, `revision`, `status` and `job_id`. A plain browser form
   post gets a flash message and `303` back to the assignment page.

The editor is a plain `<textarea>` with `dir="ltr"` and a live character
counter; submitting works without JavaScript too.

### Deadlines and late work

A deadline never closes the submission form. The template always renders the
form, and the route only checks that the assignment is published, that the
student is enrolled, and that a non-empty answer arrived. `first_submitted_at`
is compared with the assignment's `due_at` by `src.late_policy.penalty_for`,
so a hand-in after the deadline is graded normally with the configured
`LATE_PENALTY_POINTS` deduction (10 points by default) instead of being
rejected.

A draft gets its deadline in the same step that publishes it: the **פרסום**
action opens a panel with an optional date field (pre-filled when the draft
already has a date), and `POST /teacher/assignments/{id}/toggle` stores it with
the publish. The field is a calendar date only; the stored deadline is
**23:59** in the school's timezone on that day, converted to naive UTC. The
class page and the student assignment page both state that late work is still
accepted and how many points it costs, read from the same `config.settings`
value the engine uses.

## 3. Single background worker

`portal/main.py` starts one daemon thread (`GradingWorker`) in the FastAPI
lifespan, mirroring the ingestion worker. The in-memory queue is only a wake
signal; jobs live in SQLite, so a restart loses no work.

Startup calls `reset_stale()`, which returns any job stuck in `grading` (and
its submission) to `queued`. If two processes ever run, the database claim is
still the source of truth; running one worker per database is the supported
configuration.

Each pass claims one job, executes it, and calls the engine's own pacer
(`src.llm_evaluator.paced_pause`, wrapped as
`adapter.between_grades_pause`) only when another job is waiting. Claiming
re-checks the submission revision: if the student resubmitted after the job
was queued, the old job becomes `superseded` without spending a model call.

### Grading a job

The worker loads the rubric through `adapter.load_rubric(path,
expected_sha256=...)` and calls `adapter.grade_submission(...)` with the text
verbatim, the assignment due date, and the frozen `first_submitted_at`. The
engine is the same `evaluate_student_submission` path used by the Streamlit
app; the audit verdict is captured once, immediately.

### Failure handling

| Engine signal | Job state | UI message |
|---|---|---|
| `adapter.DailyQuotaExhausted` | `deferred`, `next_attempt_at = now + 1 h`, worker-wide cooldown | "הבדיקה תתחדש אוטומטית…" |
| `adapter.LLMAPIError` with HTTP 429 | `deferred`, `next_attempt_at = now + 15 min` (or `Retry-After`) | same, with the retry time |
| Other `LLMAPIError`, `LLMEvaluationError`, `RubricBindingError`, unexpected exception | `failed` with a short Hebrew explanation plus the exception type | "הבדיקה נכשלה…" |

A deferral is not a failure: the student keeps the submitted work, the
submission shows "ממתין לבדיקה", and due deferred jobs are promoted back to
`queued` automatically. The worker cooldown prevents every queued job from
burning its own 429 when the daily Groq quota is exhausted.

Grades are only written after a complete engine result. A failed or deferred
job never overwrites an existing grade for an older revision.

## 4. Student-facing results

`GET /student/submissions/{id}` renders `student_result.html`:
score ring, Hebrew feedback, late-penalty points, per-task evaluations, the
deduction list, submitted code, and a link back to the assignment to resubmit.
The page deliberately omits the rubric file, its hash, the configured model
and the audit internals. Status badges also appear beside assignments on the
student dashboard and class page.

## 5. Teacher grading overview and review

- `GET /teacher/grading` — all classes, one section per class, with counts
  (submitted, pending, graded, failed, needs review) and links to each
  assignment.
- `GET /teacher/classes/{id}/grading` — the same view scoped to one class.
- `GET /teacher/assignments/{id}/grading` — the full enrolled roster,
  including students who have not submitted ("לא הוגש").
- `GET /teacher/submissions/{id}` — score, feedback, model, rubric title and
  hash, late points, the complete audit (summary, original vs. final score,
  findings with severity), task evaluations, deductions and the verbatim
  submission.
- `POST .../review` — saves a note and marks the result reviewed.
- `POST .../regrade` — forces a fresh job for the current revision; the page
  auto-refreshes while a job is queued or running.

Every teacher route verifies ownership through the assignment and returns 404
for foreign ids. Students receive 403 on teacher routes.

## 6. Tests

`portal/tests/test_phase3_grading.py` (21 tests) covers:

- 202 JSON submission, verbatim persistence, invalid/oversized/CSRF input;
- resubmission revision bump with a frozen `first_submitted_at`, idempotent
  identical resubmission;
- student result rendering with answers, rubric hashes and model names
  redacted; cross-student access denied;
- worker persistence of the full result and audit, pacing only between queued
  jobs, quota deferral plus cooldown and retry, persistent 429, evaluation
  failure, restart reset, superseded revisions;
- teacher overview counts, roster, review page/note, regrade, ownership.

The route tests replace the real worker with a recorder, so no test can reach
Groq. Full portal suite: `pytest portal/tests -q` → 136 passed, 5 skipped.

## 7. Trying it with demo data

The site can be populated with a removable demo class without calling Groq:

```powershell
.venv\Scripts\python -m portal.scripts.seed_phase3_demo --teacher idan.sarfati
```

It creates the class **הדגמת Phase 3 (אפשר למחוק)** with four demo students and
two assignments, showing every UI state: graded with an audit review, graded
and already reviewed, failed, deferred (quota), late with a penalty, a clean
100, and a student who did not submit.

- Student logins: `demo.noa`, `demo.itai`, `demo.maya`, `demo.dan`, shared
  throwaway password `DemoPass123`.
- Teacher view: the class appears on the dashboard and under **בדיקות**.
- The seeded jobs are terminal or deferred 30 days ahead, so the background
  worker never sends them to the model.
- Real submissions through the UI still call the engine normally.

Remove every demo row (class, students, submissions, jobs, grades) with:

```powershell
.venv\Scripts\python -m portal.scripts.seed_phase3_demo --cleanup
```

The script only touches its own `demo.*` users and the demo class; real data
is never modified.

## 8. Remaining limits

- One grading worker per database; the engine's pacing is tuned for sequential
  grading.
- No code sandbox yet: students submit text/code, and execution is a later
  phase.
- `create_all` only; Phase 3 adds tables, never columns to old tables.
- The queue has no priorities or per-class limits yet.
