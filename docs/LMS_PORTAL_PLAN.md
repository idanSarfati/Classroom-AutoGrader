# Independent Learning Portal — Architecture & Migration Plan

**Status:** Analysis only. No existing code, configuration, data, or deployment was modified.
**Date:** 2026-11-08
**Scope:** Replace the student-facing dependency on Google Classroom with an independent, mobile-first learning portal that reuses the existing AI grading and audit engine with full fidelity.

> **Working-tree note:** the repository already contained uncommitted modifications and deletions before this analysis (visible in `git status`). Nothing in those changes was made by this work. This document is the only artifact added.

---

## 0. Executive summary

### Recommendation

Build the portal as a **separate `portal/` Python service** that imports the existing evaluator **in-process through one small adapter module**. Do not reimplement grading, scoring, late policy, or auditing. Do not modify `src/`, `app.py`, `config/settings.py`, or `requirements.txt`.

Concretely:

| Decision | Recommendation | Why |
|---|---|---|
| Backend | **FastAPI (Python)** | Same runtime as the evaluator. Importing `evaluate_student_submission` is a function call, not an API translation layer. No risk of reimplementing or drifting from the grading contract. |
| Frontend (v1) | **FastAPI + Jinja2 + HTMX/Alpine**, mobile-first, RTL Hebrew, CodeMirror 6 for code | Fastest path to a testable portal with one deployable. Less moving parts than a SPA. A JSON API is still exposed so a React/Vite UI can be added later without touching the backend. |
| Frontend (optional later) | React/Vite PWA consuming the same JSON API | Worth it only if the portal grows into a rich interactive product. Not needed to validate the idea. |
| Database | **SQLite for pilot → PostgreSQL when concurrency/backup needs justify it** | SQLite is enough for a school pilot and keeps operations simple. Use SQLAlchemy 2.x + Alembic so the switch is a connection-string change. |
| Grading execution | **DB-backed job queue + one dedicated grading worker process** | LLM calls take 30–60 s and the free-tier quota is fragile. A student HTTP request must return immediately (`202 Accepted`), not block on Groq. One worker also matches the evaluator's process-local router/pacer state. |
| Python execution | **Separate containerized sandbox service** (Docker, no network, CPU/memory/time limits) | Never `exec()` student code in the web process. The sandbox is the security boundary. |
| Student auth | **Local accounts, teacher-provisioned, username + password + class code. No email, no Google OAuth** | Directly addresses the access problems that motivated the move. |
| Migration strategy | **Parallel build + fixture parity + 2-week shadow pilot, then cut over** | Existing Classroom system keeps running untouched until the portal is proven. |

### The single most important finding

The grading engine is already **Classroom-agnostic**. The core entry point is:

```python
# src/evaluator/core.py:84
def evaluate_student_submission(
    student_name: str,
    raw_doc_text: str,
    rubric: Rubric,                  # AssignmentConfig | AssignmentSpec
    late_penalty_points: int = 0,
) -> GradingResult:
```

It receives **plain text**, a **rubric model**, and a **penalty number**. It has no Google imports, no Drive calls, and no Classroom IDs. The portal only has to produce those three inputs correctly and persist the result. That is the entire integration surface.

Everything else in the current system — OAuth, Drive document extraction, submission fetching, grade release — is Classroom plumbing that the portal replaces. The evaluation, audit, prompt, model routing, and late policy stay exactly as they are.

---

## 1. Current state analysis

### 1.1 Layered map

| Layer | Key modules | Responsibility | Portal relationship |
|---|---|---|---|
| Entry points | `app.py` (Streamlit), `src/main.py` (CLI), `src/daily_check.py` (headless) | Drive the workflows | Replace student/teacher UI; keep CLI for teacher fallback |
| Orchestration | `src/ui/evaluate.py`, `src/ui/dashboard.py`, `src/dashboard.py`, `src/daily_check.py` | Fetch → extract → grade → export | **Replace** with portal flows; reuse the grading calls |
| Classroom integration | `src/classroom_service.py`, `src/fetch_submissions.py`, `src/auth.py`, `src/ui/auth.py` | OAuth, courses, coursework, submissions, Drive/Docs | **Not used by the portal** |
| Domain rules | `src/late_policy.py`, `src/config_match.py`, `src/code_checks.py` | Lateness, rubric matching, static code scan | **Reuse** late policy and code checks; rubric matching becomes teacher selection |
| Evaluation core | `src/evaluator/*` + facade `src/llm_evaluator.py` | Prompt, LLM stages, parsing, retries, pacing, audit, batch | **Reuse verbatim** |
| LLM infrastructure | `src/llm_router.py` | Key pools, fallback/tertiary providers, 429 handling, adaptive pacing | **Reuse**; quota must be coordinated across services |
| Data models | `src/models.py` | Pydantic schemas for courses, submissions, rubrics, grades, reports | **Reuse** `GradingResult`, `AssignmentConfig`, `AssignmentSpec`, `TaskEvaluation`, `DeductionItem`; add portal tables separately |
| Settings | `config/settings.py` | `.env` + `st.secrets` loading, model and audit flags | **Reuse** by importing; portal adds its own prefixed settings |
| Persistence | `src/main.py` (`EvaluationReport`, `write_mashov_csv`, `write_evaluation_report`) | JSON reports + Mashov CSV in `exports/` | **Optional reuse** for export with synthetic models; primary store is portal DB |

### 1.2 The grading contract (what must not change)

`evaluate_student_submission` performs, in order:

1. Builds the prompt with `_build_prompt(student_name, raw_doc_text, rubric)` (`src/evaluator/prompts.py:272`).
   - Injects the rubric tasks/questions as JSON.
   - Injects exempt-task instructions, deterministic code scans (lesson_3 only), and spec evidence scans.
   - Trims the document to `MAX_DOC_CHARS = 6000` characters, keeping head and tail, with an explicit omission marker.
2. Calls the LLM through the stage ladder (`src/evaluator/schema.py:142`):
   - `json_schema` (strict structured output) → `json_object` → free text.
   - Parses/repairs with `_parse_result` and validates against `GradingResult`.
3. Overrides `result.student_name` with the caller's name (authoritative).
4. Applies the late penalty (`src/late_policy.py:301`): reduces score, adds a `Late submission` deduction row, prepends a Hebrew notice. Idempotent if called twice.
5. Runs the deterministic audit (`src/grade_audit.py:250`) — zero tokens — and stores the verdict in a sidecar map keyed by `id(result)` (`src/evaluator/audit.py:52`).
6. Returns the `GradingResult`. **The audit verdict is not attached to the result**; the caller must call `last_audit_for(result)` immediately (`src/evaluator/audit.py:55`), which **pops** it from the sidecar.

Relevant constants that define behavior:

| Constant | Value | Location |
|---|---|---|
| `TEMPERATURE` | `0.2` | `src/evaluator/config.py:61` |
| `MAX_TOKENS` | `800` | `src/evaluator/config.py:70` |
| `MAX_DOC_CHARS` | `6000` | `src/evaluator/config.py:87` |
| `REASONING_EFFORT` | `"none"` | `src/evaluator/config.py:111` |
| `EVAL_PAUSE_SECONDS` | `2.0` | `src/evaluator/config.py:104` |
| `_REQUEST_TIMEOUT_SECONDS` | `180.0` | `src/evaluator/config.py:193` |

**Fidelity rule:** the portal must not pre-trim, pre-summarize, HTML-escape, or annotate the submission text in a way that changes what the model sees. The evaluator owns trimming and formatting.

### 1.3 Rubric models and files

Two schemas are supported (`src/main.py:187`):

- **Legacy `AssignmentConfig`** (`assignments/lesson_*.json`): flat list of tasks with `id`, `name`, `description`, optional `exempt`.
- **Rich `AssignmentSpec`** (`configs/*_spec.json`): sections with weighted questions. Question types: `multiple_choice`, `expected_output`, `debugging`, `code_writing`.

Rubric discovery is `config_match.list_config_files()` over `assignments/`, `config/`, `configs/`, with fuzzy title matching for Classroom coursework. The portal does not need fuzzy matching: a teacher binds a portal assignment to a rubric file explicitly. The assignment mapping file (`config/assignment_mapping.json`) is Classroom-specific and should not be reused.

### 1.4 Audit layer

`AuditVerdict` (`src/grade_audit.py:66`) checks:

- score is an integer in 0–100;
- deduction items are non-negative and sum to `100 - score`;
- deductions do not exceed 100;
- `CORRECT` tasks have no deduction; `INCORRECT`/`MISSING` tasks do not silently cost zero;
- every rubric task has an evaluation, and no unknown task names appear;
- exempt tasks are not graded (info-level);
- feedback is non-empty and does not claim a typo deduction.

It can **auto-fix** a provably-wrong score when the only problem is a deduction-sum mismatch and no other blocking finding exists. Info-level findings (missing task rows, unknown tasks, exempt tasks graded) are batch-wide tuning advice, not per-student work.

The verdict is designed to be persisted next to the grade (`AuditVerdict.to_dict()`), which is exactly what the portal must do. The existing report schema already stores it under `ReportEntry.audit`.

### 1.5 LLM routing, pacing, and quota reality

`LLMRouter` (`src/llm_router.py:283`) is a thread-safe, process-local pool:

- primary Groq key pool (rotation on 429, per-key cooldown);
- fallback provider pool (Gemini/OpenRouter/Cloudflare presets);
- tertiary pool;
- per-minute 429s are waited out; **daily** quota exhaustion parks keys for ~6 hours and fails over immediately;
- `AdaptivePacer` (`src/llm_router.py:919`) spaces calls and backs off on 429s.

Critical operational facts:

- The router, Groq client, and pacer are **module-level singletons** (`_ROUTER`, `_client`, `_PACER`). They are **per process**.
- Free-tier limits are tight: ~30 RPM, ~1K output TPM on the configured Groq model, ~200K tokens/day (see the module docstring). A class batch can consume a day's quota.
- `grade_students_batch(..., pause=paced_pause)` forces **one worker** because back-to-back long completions burst the output-token bucket. The existing UI and daily check pass `pause` for this reason.
- A second service (the portal) running at the same time **shares the provider's external quota** with the existing system even though it has separate in-process router state.

**Implication:** the portal needs one centralized grading worker, and during the pilot it should use a **separate Groq key pool** (or a deliberate schedule) so it cannot starve the 06:00 daily check or the existing Streamlit app.

### 1.6 Persistence and exports

- `write_evaluation_report` (`src/main.py:903`) writes `exports/<Course>/<Assignment>/<timestamp>.json` as an `EvaluationReport` (`src/models.py:310`). It requires `Course` and `CourseWork` objects and Google-style IDs.
- `write_mashov_csv` (`src/main.py:406`) writes a UTF-8-SIG CSV with columns `שם התלמיד`, `ציון`, `משוב מילולי`, `סטטוס הגשה`, using `GradingRow` + `StudentSubmission`.
- These writers are reusable with **synthetic** `Course`/`CourseWork`/`StudentSubmission` objects. That is useful for export, but the portal should own its primary grade record in its own database.

### 1.7 Authentication and Classroom coupling

- `src/auth.py` + `src/ui/auth.py` implement Google OAuth for the **teacher**. There is no student account concept.
- The Streamlit dashboard is pinned to `localhost:8501` (`.streamlit/config.toml`), which is appropriate for a teacher-only tool but unusable for students on phones.
- The portal must be a separate service on its own port, behind HTTPS, with its own session/auth system.

### 1.8 Constraints and gotchas found during review

These are the details that most often break a "reuse the engine" integration:

1. **The rubric files contain answers.** `correct_answer`, `corrected_code`, and often `expected_output` are in `configs/*_spec.json`. The portal must serve a **redacted student view** and never expose the raw rubric JSON to the browser.
2. **The prompt says "Google Doc".** The system/user prompt contains "Raw text extracted from the student's Google Doc" (`src/evaluator/prompts.py:336`). Leave it unchanged for fidelity. It is cosmetic, but changing it changes the prompt and invalidates parity.
3. **`last_audit_for` is destructive.** It pops the verdict. Call it exactly once, immediately after evaluation, in the same thread, and persist the result. Do not call it twice or in a different thread.
4. **`StudentSubmission` and `CourseWork` are required for the late policy.** `penalty_for` (`src/late_policy.py:234`) expects a `StudentSubmission` with `late_state`, `is_late`, `submitted_at`, `first_submitted_at`, plus required fields. The portal must construct **synthetic** instances from portal data.
5. **Late policy signal order matters.** For portal submissions, leave `late_state=None` and `is_late=None`; set `first_submitted_at` to the first attempt and `submitted_at` to the latest attempt. The policy then compares the original hand-in to the due instant — the same semantics as the existing timestamp fallback, without inventing a Classroom verdict. If no due date is set, no penalty is charged.
6. **The spec evidence scan is literal substring matching.** `_spec_scan_block` (`src/evaluator/prompts.py:175`) checks whether `correct_answer` / `corrected_code` / `expected_output` appears in the raw text. If the portal serializes a multiple-choice answer as `"B"` instead of the option's text, or reformats code, the scan reports a false "not found" (supporting evidence only, but still noise). Serialize **verbatim student text**.
7. **`config.settings` loads the root `.env` at import.** `load_dotenv(PROJECT_ROOT / ".env")` runs at import time. If the portal needs different keys/settings, set `os.environ` **before** importing `src.llm_evaluator`/`config.settings`; existing environment values win over `.env`.
8. **Importing `src.main` pulls Google libraries.** `load_rubric` lives there and `src/main.py` imports `src.classroom_service`/`src.fetch_submissions`. That is fine if the portal runs in the same environment/venv as the existing app; it adds import weight but no network calls.
9. **Long-running calls must not be synchronous HTTP.** A 30–60 s LLM call (up to 180 s timeout) will time out on mobile networks and tie up web workers. Use a job queue.
10. **There is no existing code execution.** `src/code_checks.py` is static string analysis only. The Python runner is entirely new and is not part of the grading fidelity contract.
11. **The existing tests pin a lot of behavior.** Run the existing `pytest` suite before and after portal integration; portal tests live under `portal/tests/` and must not modify `tests/`.

### 1.9 Measured test baseline (2026-11-08)

This working tree is **already dirty** (pre-existing modifications/deletions unrelated to this plan), so the baseline is not green:

- `python -m pytest -q` at the repo root: **collection stops** on `src/test_script.py` with `SyntaxError: source code string cannot contain null bytes`.
- `python -m pytest tests -q`: **530 passed, 13 failed** in ~37 s.
- The 13 failures are in `tests/test_fallback_request_body.py`, `tests/test_json_repair.py`, and `tests/test_llm_resilience.py`; at least one is an environment/package issue (`httpx2.InvalidURL: Invalid port: ':1]'` inside the test client), not an evaluator logic failure.

Consequences for the plan:

- Always scope test commands to `tests/` until the null-byte file is dealt with.
- Treat "existing suite green" as a **Phase 0 precondition**, not an assumption. Fixing these failures is a separate, explicitly approved change; the portal work must not silently absorb them.
- Record the exact baseline command and result in `portal/grading/baseline.json` so a later "did we break it?" question has an answer.

---

## 2. Reuse vs. build

### Reuse unchanged (read-only imports)

| Capability | Module | How the portal uses it |
|---|---|---|
| Single-submission grading | `src/evaluator/core.py` | Call `evaluate_student_submission` from the grading worker |
| Batch grading | `src/evaluator/batch.py` | Optional for teacher-triggered bulk re-grades |
| Prompt construction | `src/evaluator/prompts.py` | Do not call directly; it runs inside the evaluator. Use for parity tests. |
| LLM routing / fallback | `src/llm_router.py` | Automatic through the evaluator; inspect status for teacher notices |
| Audit | `src/grade_audit.py` + `src/evaluator/audit.py` | Runs inside the evaluator; persist `last_audit_for(result)` |
| Late policy | `src/late_policy.py` | `penalty_for` with synthetic submission/coursework |
| Rubric models + loader | `src/models.py`, `src/main.py:load_rubric` | Load the exact same rubric file and validate with the same models |
| Static code scan | `src/code_checks.py` | Runs inside the prompt builder; no portal change |
| Settings | `config/settings.py` | Reuse model/audit/penalty flags; portal adds prefixed settings |
| CSV/report formats | `src/main.py` writers | Optional: reuse for Mashov-format export with synthetic models |

### Build new (all inside `portal/`)

| Capability | Notes |
|---|---|
| Web server, routing, templates/API | FastAPI |
| Local user management | Users, roles, classes, enrollments, teacher reset |
| Portal database | Assignments, submissions, attempts, grades, jobs, content, audit log |
| Submission UI | Code editor, worksheet answers, file/text submission, mobile layout, RTL |
| Grading job queue + worker | DB-backed, quota-aware, idempotent |
| Student-safe rubric view | Redacts answers; renders instructions/questions |
| Python execution sandbox | Containerized runner, hidden tests, resource limits |
| Study presentations | Content storage + mobile viewer |
| Teacher gradebook | Review, override, re-grade, export, audit rollup |
| Notifications / status | Quota warnings, grading failures, resubmission notices |
| Observability | Structured logs, job metrics, health checks |

---

## 3. Target architecture

### 3.1 System diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│ Students (phones / laptops, Hebrew RTL)     Teacher (desktop)       │
│        │ HTTPS                                      │               │
│        ▼                                            ▼               │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ Reverse proxy (Caddy / Nginx) — TLS, static files, uploads  │    │
│  └──────────────────────────┬──────────────────────────────────┘    │
│                             │                                       │
│  ┌──────────────────────────▼──────────────────────────────────┐    │
│  │ portal/ — FastAPI web app (single uvicorn worker)           │    │
│  │  auth/session · classes · assignments · submissions         │    │
│  │  student-safe rubric view · teacher gradebook · exports     │    │
│  └───────┬───────────────────────┬─────────────────────┬───────┘    │
│          │                       │                     │            │
│          ▼                       ▼                     ▼            │
│   Portal DB               Job queue (table)      Static content     │
│   SQLite → Postgres       grading_jobs           lessons/, uploads/ │
│          │                       │                                  │
│          │                       ▼                                  │
│          │        ┌──────────────────────────────────────┐           │
│          │        │ Grading worker (separate process)    │           │
│          │        │  imports src.evaluator (read-only)   │           │
│          │        │  evaluate_student_submission(...)    │           │
│          │        │  last_audit_for(...) → DB            │           │
│          │        └──────────────┬───────────────────────┘           │
│          │                       │ HTTPS                           │
│          │                       ▼                                 │
│          │             Groq / Gemini / OpenRouter                  │
│          │             (existing router + key pools)                │
│          │                                                         │
│          │        ┌──────────────────────────────────────┐           │
│          └───────▶│ Code runner sandbox (Docker, :9001)  │           │
│                   │ no network · CPU/mem/time limits     │           │
│                   └──────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────────┘

Existing (untouched, running in parallel):
Streamlit app.py :8501 · src/main.py CLI · src/daily_check.py · exports/
```

### 3.2 Directory layout

Everything new lives under `portal/`. No existing file is edited.

```
portal/
├── __init__.py
├── main.py                     # FastAPI app factory, lifespan, health
├── config.py                   # Portal settings (PORTAL_* env, fallbacks)
├── db.py                       # Engine, session factory
├── models.py                   # SQLAlchemy tables (portal-owned)
├── migrations/                 # Alembic (or a minimal schema.sql for the pilot)
├── auth/
│   ├── passwords.py            # Argon2id hashing
│   ├── sessions.py             # Signed cookie / server-side sessions
│   ├── deps.py                 # current_user, require_teacher, require_student
│   └── reset.py                # Teacher-issued reset codes
├── api/
│   ├── routes_auth.py
│   ├── routes_classes.py
│   ├── routes_assignments.py
│   ├── routes_submissions.py
│   ├── routes_grading.py
│   ├── routes_runner.py
│   └── routes_admin.py
├── services/
│   ├── assignments.py          # Publish, redact, rubric binding
│   ├── submissions.py          # Attempts, serialization, validation
│   ├── gradebook.py            # Review, override, export
│   └── content.py              # Presentations/lessons
├── grading/
│   ├── adapter.py              # THE ONLY module that imports src.* grading code
│   ├── contracts.py            # Serialization + fidelity invariants
│   ├── worker.py               # Queue consumer, pacing, quota handling
│   └── baseline.json           # File hashes / versions for drift detection
├── runner/
│   ├── client.py               # HTTP client to sandbox
│   ├── Dockerfile              # Sandbox image (python:slim, non-root)
│   └── server.py               # Sandbox service (optional if using a container-per-run model)
├── content/
│   ├── lessons/                # HTML/PDF presentations (served statically)
│   └── uploads/                # Student uploads (size-limited, content-type-checked)
├── templates/                  # Jinja2 (if server-rendered v1)
├── static/                     # CSS, JS, CodeMirror, fonts
├── tests/
│   ├── test_parity.py
│   ├── test_grading_adapter.py
│   ├── test_redaction.py
│   ├── test_runner_limits.py
│   └── test_auth.py
├── requirements-portal.txt     # Adds FastAPI, SQLAlchemy, etc.; includes existing requirements
├── .env.example                # Portal-specific settings
├── README.md                   # How to run, test, deploy
└── scripts/
    ├── run-portal.ps1          # Windows service/launcher analogue
    └── run-worker.ps1
```

**Rule:** only `portal/grading/adapter.py` imports from `src/`. Every other portal module depends on portal-owned interfaces. This keeps the integration surface auditable and makes a future extraction to an HTTP grading service straightforward.

### 3.3 Portal data model

Use portal-owned tables; do **not** reuse `EvaluationReport` as the primary store.

| Table | Key fields | Notes |
|---|---|---|
| `users` | `id`, `username` (unique), `display_name`, `password_hash`, `role` (`student`/`teacher`/`admin`), `is_active`, `created_at` | No email required. Display name is what the evaluator sees. |
| `classes` | `id`, `name`, `join_code` (unique), `teacher_id`, `created_at` | Join code for enrollment. |
| `enrollments` | `class_id`, `user_id`, `role_in_class`, `enrolled_at` | Unique `(class_id, user_id)`. |
| `lessons` | `id`, `title`, `slug`, `class_id` (nullable = global), `content_path`, `order`, `published` | Presentations. |
| `assignments` | `id`, `class_id`, `title`, `instructions_md`, `rubric_path`, `rubric_sha256`, `rubric_kind`, `due_at`, `published`, `created_by`, `created_at` | `rubric_path` points to the existing file (e.g. `assignments/lesson_3.json`). |
| `assignment_questions` | `id`, `assignment_id`, `question_key`, `student_view_json` | Redacted view for rendering. Never contains answers. |
| `submissions` | `id`, `assignment_id`, `student_id`, `attempt_no`, `status`, `raw_text`, `structured_answers_json`, `first_submitted_at`, `submitted_at`, `late_penalty_points` | One row per attempt. `first_submitted_at` fixed at the first attempt. |
| `grades` | `id`, `submission_id`, `score`, `feedback_hebrew`, `task_evaluations_json`, `deduction_breakdown_json`, `audit_json`, `model`, `rubric_sha256`, `settings_fingerprint`, `graded_at`, `source` (`system`/`teacher_override`), `superseded_by` | Persist the full `GradingResult` as JSON plus queryable columns. |
| `grading_jobs` | `id`, `submission_id` (unique), `status` (`queued`/`running`/`done`/`failed`/`deferred`), `attempts`, `last_error`, `next_attempt_at`, `started_at`, `finished_at`, `provider` | Unique per submission attempt to avoid duplicate charges. |
| `code_runs` | `id`, `submission_id`/`user_id`, `source_hash`, `stdin`, `stdout`, `stderr`, `exit_code`, `duration_ms`, `status`, `created_at` | Optional retention; consider not storing source for privacy. |
| `sessions` | `id`, `user_id`, `token_hash`, `csrf_token`, `expires_at`, `created_at` | If using server-side sessions. |
| `audit_log` | `id`, `actor_id`, `action`, `entity`, `entity_id`, `details_json`, `created_at` | Teacher actions, overrides, exports, account resets. |

Storing both the structured result and the raw JSON is deliberate: the UI uses the structured fields; the raw JSON is the audit/debug record and preserves schema evolution.

### 3.4 Student-safe rubric rendering (critical)

The rubric files are teacher material and contain answers. Define a redaction function at publish time:

- **Remove:** `correct_answer`, `corrected_code`, and any `expected_output` that is the answer to a "what does this print?" question.
- **Keep:** question text, section names, weights/points, instructions, `requirements` (these are completion criteria, not answers), and `expected_output` only when it is a required program output that the student must produce (mark it explicitly as "required output").
- **Never serve** the raw JSON file path or its contents.
- **Test it:** `portal/tests/test_redaction.py` asserts that no known answer string from every rubric in `assignments/` and `configs/` appears in the published student view.

This is both a security issue (students could read the answers from the network tab) and a grading-integrity issue.

### 3.5 Submission serialization contract

The evaluator receives `raw_doc_text`. The portal's job is to produce a faithful text representation of what the student submitted.

**Legacy `AssignmentConfig` (e.g. `lesson_3`):**

- v1: a single code/answer editor, exactly mirroring the Google Doc. This is the lowest-risk parity path.
- Later, per-task editors may be concatenated with short headers:

```
# --- ex3_1 ---
name = input()
print(name)

# --- ex3_2 ---
...
```

Headers are comments and do not break `code_checks`; they do change the exact text versus a Google Doc, so treat that as a new serialization version and re-run parity.

**Rich `AssignmentSpec`:**

- `multiple_choice`: serialize the **text of the selected option**, not the letter. The spec scan searches for `correct_answer` as a substring.
- `expected_output`: serialize the student's output verbatim.
- `debugging`: serialize the edited code verbatim.
- `code_writing`: serialize the full code verbatim.
- Prefix each answer with a short label containing the question id and section, e.g. `q1 (סולם): 10 apples True`.
- Do not HTML-escape before sending; escape only when rendering.
- Do not pre-trim; the evaluator trims at 6000 characters.

**Reject or flag link-only submissions** before grading (the prompt also handles this, but the portal should show a clear message and ask for pasted code).

### 3.6 Grading adapter (sketch)

This is the only module that touches the existing evaluator.

```python
# portal/grading/adapter.py
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from config.settings import settings
from src.llm_evaluator import evaluate_student_submission, last_audit_for
from src.late_policy import penalty_for
from src.main import load_rubric
from src.models import CourseWork, StudentSubmission


def rubric_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def grade_submission(
    *,
    student_id: str,
    student_name: str,
    submission_id: str,
    raw_text: str,
    rubric_path: Path,
    expected_rubric_sha256: str,
    due_at: datetime | None,
    first_submitted_at: datetime | None,
    submitted_at: datetime | None,
):
    actual_hash = rubric_hash(rubric_path)
    if actual_hash != expected_rubric_sha256:
        raise RuntimeError(
            "Rubric changed after the assignment was published; "
            "the teacher must republish before grading."
        )

    rubric = load_rubric(rubric_path)

    # Synthetic Classroom-shaped objects: the late policy is unchanged,
    # but its inputs come from the portal instead of Google.
    submission = StudentSubmission(
        student_id=student_id,
        student_name=student_name,
        submission_id=submission_id,
        state="TURNED_IN",
        submitted_at=submitted_at,
        first_submitted_at=first_submitted_at,
        # late_state / is_late intentionally None: no Classroom verdict exists.
    )
    coursework = CourseWork(
        id="portal-assignment",
        title="portal-assignment",
        due_datetime=due_at,
    )
    late_points = penalty_for(
        submission,
        coursework,
        settings.late_penalty_points,
    )

    result = evaluate_student_submission(
        student_name,
        raw_text,
        rubric,
        late_penalty_points=late_points,
    )
    # Destructive pop: do this once, immediately, in this thread.
    audit = last_audit_for(result)
    return result, audit, rubric
```

Notes:

- If `due_at` is `None`, `penalty_for` returns 0 (no deadline to be late for).
- `first_submitted_at` must remain the **first attempt** across resubmissions, matching the existing resubmission-aware policy.
- Persist `result.model_dump(mode="json")`, `audit.to_dict()`, `settings.groq_model`, `actual_hash`, and a settings fingerprint.
- If the portal later needs a slim image without Google libraries, replace `load_rubric` with a local 6-line loader and add a parity test over every rubric file. Until then, importing `src.main` is the lowest-drift option.

### 3.7 Grading worker and quota handling

The worker is a separate process (not a FastAPI background task) that consumes `grading_jobs`.

```
loop:
  pick oldest queued job whose next_attempt_at <= now
  mark running
  paced_pause()                       # adaptive; one LLM call in flight
  try:
      result, audit = adapter.grade_submission(...)
      persist grade + audit
      mark done
  except DailyQuotaExhausted:
      mark deferred, next_attempt_at = now + backoff (15m → 30m → 60m)
      set global quota_blocked; notify teacher
  except LLMEvaluationError as exc:
      mark failed with the message; student sees "grading failed, teacher notified"
```

Rules:

- **One grading worker** for the pilot. `grade_students_batch(..., pause=paced_pause)` already serializes when a pause is supplied; for single submissions, call `paced_pause()` before each evaluation.
- Do **not** run multiple uvicorn workers. The router/pacer/client are per-process singletons; multiple web workers would multiply requests and defeat key rotation.
- Use a **separate Groq key pool** for the portal during the pilot (`GROQ_API_KEY` / `GROQ_API_KEYS` in the portal's environment). If that is not possible, schedule portal grading after the 06:00 daily check and expect contention.
- `DailyQuotaExhausted` is a **deferral**, not a student failure. The job is retried when the quota resets; the UI says "queued — provider quota reached".
- Record which provider served the grade (Groq vs fallback). The existing `router.fallback_notice()` is UI-oriented but gives the wording; the worker can read `router.used_fallback` / `fallback_uses`.
- Idempotency: `grading_jobs.submission_id` is unique. A retry after a crash may re-charge tokens; that is acceptable, but do not create two jobs for the same attempt.

### 3.8 Code execution sandbox

This is a new capability, independent of the LLM grading contract.

**Recommended design:** a separate Linux container service exposing `POST /run` on localhost.

- Image: `python:3.12-slim` (or the same minor version as the app), non-root user.
- Per run:
  - create a temp directory, write the student's source to `main.py`;
  - `docker run --rm --network none --memory 256m --cpus 0.5 --pids-limit 64 --read-only --tmpfs /tmp:rw,size=16m -u 10001 ...`
  - `timeout 5s` (wall clock), optional CPU-time limit;
  - capture stdout/stderr separately, cap combined output at 64 KB;
  - UTF-8 input/output;
  - no environment secrets, no access to the host, no network.
- **Never** use `exec()`/`eval()` in the FastAPI process. AST filtering is not a security boundary.
- Windows development: Docker Desktop is the practical path. A plain `subprocess.run([sys.executable, "-I", "-S", "-B", ...])` is acceptable for local development only, not for untrusted production code.
- Hidden tests:
  - store test inputs/expected outputs server-side;
  - run each test in a fresh process (or a fresh container) to avoid state leakage;
  - show pass/fail; optionally hide expected outputs for exam-style questions;
  - compare normalized stdout (strip trailing whitespace, normalize line endings) with a documented policy.
- Rate-limit runs per student (e.g. 1 concurrent, N per minute) and queue when the sandbox is busy.
- **Do not feed execution results into the LLM prompt in phase 1.** That would change the prompt and break fidelity. If desired later, introduce a new prompt version and re-run parity.

### 3.9 Study presentations

There are no existing presentation assets in the repository (only `docs/AUDIT_LAYER.md` and the README). This is net-new content.

- Store each lesson as a static, mobile-first HTML page (preferred) or PDF under `portal/content/lessons/<slug>/`.
- Serve via the reverse proxy or FastAPI static files; no authentication bypass.
- If the source material is PowerPoint, convert to PDF/HTML offline and serve the artifact. Do not store editable PPTX in the runtime path unless needed.
- Sanitize any teacher-provided HTML (e.g. `bleach`) or restrict uploads to PDF/images to avoid stored XSS.
- Add optional per-student `viewed_at` progress later; do not block the MVP on it.

### 3.10 User management without Google OAuth

**Recommended for a school with minors: teacher-provisioned accounts.**

- Teacher creates a class, imports/creates student accounts (username, display name, initial password), and hands out credentials.
- Students log in with username + password. No email required.
- A class join code can support self-enrollment if the teacher enables it, with teacher approval of pending students.
- Teacher can reset a password and generate a one-time code.
- Roles: `admin` (technical), `teacher`, `student`. Teachers only see their classes; students only see enrolled classes.
- Password storage: Argon2id (`argon2-cffi`) or bcrypt via `passlib`. Minimum length; rate-limit login attempts; lockout after repeated failures.
- Sessions: server-side session cookie (`HttpOnly`, `Secure`, `SameSite=Lax`) + CSRF token for state-changing requests. If using a SPA later, use short-lived access tokens + refresh or the same cookie scheme.
- Privacy: collect the minimum (display name, username, class). Avoid storing national IDs, emails, or phone numbers. Define retention for submissions and code runs; back up the DB securely.
- No Google OAuth is required for students or teachers. The existing Google code remains available for the old system and can be used by an admin for migration/export only.

### 3.11 Deployment topology

- Existing Streamlit stays on `localhost:8501`; the portal runs on `127.0.0.1:8001` behind Caddy/Nginx with TLS.
- Separate OS service/process for: web app, grading worker, sandbox. Separate logs.
- Separate venv (or a venv that includes both `requirements.txt` and `portal/requirements-portal.txt`) so the portal can import the evaluator without disturbing the existing environment.
- Environment: set `PORTAL_*` for portal settings. For LLM keys, either reuse the root `.env` (simplest during pilot) or provide a portal-specific key pool. Because `config.settings` loads the root `.env` at import, set portal-specific environment variables **before** importing the evaluator if they must differ.
- Hosting options:
  - school Linux server / VPS with a public hostname and TLS;
  - Cloudflare Tunnel to a school machine if there is no public IP;
  - a cloud VM (Render/Fly/Railway/Hetzner) if the sandbox can run there. Streamlit Community Cloud cannot host FastAPI.
- Backups: nightly DB copy + uploads; test a restore before the pilot.
- Health endpoints: `/healthz` (process), `/readyz` (DB + rubric dir + runner reachability). Do not make readiness depend on the LLM provider.

---

## 4. The 100% fidelity plan

"100% fidelity" is achieved by **reusing the same functions and the same inputs**, not by copying code. Define and test these invariants:

### 4.1 Fidelity invariants

1. **Same rubric bytes.** The portal grades the exact file a teacher published, verified by SHA-256 at grade time. If the file changes, grading stops and the teacher republishes.
2. **Same evaluator code.** The portal imports `src.evaluator.core.evaluate_student_submission`, `src.grade_audit`, `src.late_policy`, `src.code_checks`, and `src.llm_router` from the repository. No forks, no copies.
3. **Same prompt inputs.** The portal sends `student_name`, `raw_doc_text` (verbatim, untrimmed), and the loaded rubric model. It does not add preamble text, markdown wrappers, or instructions.
4. **Same model and settings.** `GROQ_MODEL`, `GROQ_BASE_URL`, fallback settings, `LATE_PENALTY_POINTS`, `LLM_AUDIT_ENABLED`, `LLM_AUDIT_AUTOFIX` come from the same settings source. Record them in the grade row.
5. **Same late policy.** Use `penalty_for` with synthetic `StudentSubmission`/`CourseWork`; `first_submitted_at` is the first attempt; no invented `late_state`/`is_late`.
6. **Same audit handling.** Call `last_audit_for(result)` exactly once immediately after grading; persist `AuditVerdict.to_dict()`. If audit is disabled, store `None` rather than fabricating a verdict.
7. **Same result persistence.** Store the full `GradingResult` JSON, including `deduction_breakdown` and `task_evaluations`, so a re-render or export is lossless.

### 4.2 Parity harness (build in Phase 0)

All tests live in `portal/tests/`; the existing `tests/` directory is not modified.

- **Prompt parity:** call `_build_prompt(student_name, raw_text, rubric)` for each fixture and assert it is byte-identical to a stored golden prompt. This catches accidental text transformation before it reaches the model.
- **Plumbing parity:** monkeypatch the LLM transport (as the existing tests do) to return a fixed JSON payload; run `evaluate_student_submission`; assert the parsed `GradingResult`, late penalty, and audit verdict match golden fixtures.
- **Rubric loader parity:** for every file in `assignments/` and `configs/`, load it through `src.main.load_rubric` and through any portal-local loader (if used) and assert the models are equal.
- **Serialization fixtures:** include every answer shape:
  - legacy code (lesson_3),
  - multiple choice with option text,
  - expected output,
  - debugging with edited code,
  - code writing,
  - empty submission,
  - link-only submission,
  - very long submission (>6000 chars, to verify evaluator trimming),
  - Hebrew/RTL text with invisible characters.
- **Real-LLM smoke:** a small, manually run set (5–10 fixtures) to confirm end-to-end behavior and feedback. LLM nondeterminism means compare score bands and presence of feedback, not exact strings.

### 4.3 Shadow pilot

1. Pilot one class on the portal.
2. For the first two weeks, the teacher spot-checks 10–20 portal submissions by running the same text through the existing Evaluate tab (or a `portal/tools/parity_check.py` CLI that prints the exact prompt and result).
3. Track:
   - score agreement within the same band;
   - audit verdicts;
   - late-penalty correctness;
   - grading failures and quota deferrals;
   - student completion rates on mobile.
4. Cut over only after the teacher signs off. Keep the existing system running until then.

### 4.4 Drift detection

- Store a `portal/grading/baseline.json` with the git commit and SHA-256 of the critical files: `src/evaluator/*`, `src/llm_router.py`, `src/grade_audit.py`, `src/late_policy.py`, `src/code_checks.py`, `src/models.py`.
- At worker startup, warn (do not block) if the hashes differ from the baseline. This tells you that a change to the existing engine happened and parity should be re-tested.
- Record `settings_fingerprint` (model, temperature, max tokens, audit flags, penalty) with each grade.
- Run the existing `pytest` suite plus portal tests in CI.

---

## 5. Phased work plan

Estimates assume one experienced developer working part-time; adjust to the actual team. Each phase has a hard gate: do not start the next phase until the exit criteria are met.

### Phase 0 — Baseline and parity harness (2–3 days)

**Goal:** freeze the grading contract and prove the portal can reproduce it.

- Write an ADR for the architecture decisions above.
- Create `portal/grading/baseline.json` (file hashes + git commit + measured test baseline).
- Build the parity fixtures and the fake-LLM test harness.
- Run `pytest tests` (not the repo root, which currently stops on a null-byte file); record the result. Triage the 13 pre-existing failures as a separate approved change.
- Define the submission serialization contract and redaction rules.

**Exit:** fixture tests pass; the pre-existing test state is documented and triaged; the existing suite is green (or the remaining failures are explicitly accepted in writing); the team agrees on the contract.

### Phase 1 — Portal skeleton, auth, and classes (week 1)

**Goal:** a running portal where a teacher can create a class and a student can log in.

- FastAPI app, settings, DB schema, migrations.
- Password hashing, sessions, CSRF, login rate limiting.
- Teacher-provisioned accounts; class creation; join code; enrollment.
- Mobile-first layout shell, Hebrew RTL, health endpoints.
- Portal test suite + CI.

**Exit:** teacher creates a class and student accounts; a student logs in on a phone; no existing code changed.

### Phase 2 — Content and assignment catalog (week 2)

**Goal:** students see lessons and assignments; no answers leak.

- Lesson/presentation storage and mobile viewer.
- Teacher binds a portal assignment to an existing rubric file; publish flow stores path + SHA-256.
- Redacted student view for both `AssignmentConfig` and `AssignmentSpec`.
- Due dates/instructions.
- Redaction tests over every rubric file.

**Exit:** a student can open a lesson and an assignment; the network payload contains no answers; the teacher can publish/unpublish.

### Phase 3 — Submissions and grading integration (weeks 2–3)

**Goal:** end-to-end grading with the existing engine.

- Submission UI (single text/code editor first; structured per-question later).
- Attempts and serialization; link-only validation.
- Grading adapter + DB queue + worker + pacing.
- Persist `GradingResult` + audit + model + rubric hash.
- Student feedback view; teacher review view.
- Quota deferral and failure handling.
- Parity fixtures run in CI.

**Exit:** a portal submission is graded by the same evaluator, the audit verdict is persisted, a quota exhaustion defers rather than fails, and fixture parity passes.

### Phase 4 — Python execution sandbox (weeks 3–4)

**Goal:** safe, useful code execution on mobile.

- Containerized runner with resource/network limits.
- Run button + stdout/stderr/exit code UI.
- Optional per-question tests; hidden test storage; pass/fail display.
- Rate limiting and queueing.
- Security review: escape attempts, fork bombs, huge output, infinite loops, file access.

**Exit:** untrusted code cannot access the network/host or exceed limits; a student can run code from a phone; results are displayed clearly.

### Phase 5 — Teacher gradebook and operations (weeks 4–5)

**Goal:** the teacher can run the class from the portal.

- Gradebook: per assignment/student, audit badges, needs-review list, manual override.
- Re-grade and supersede; audit log.
- Mashov CSV export (reuse `write_mashov_csv` with synthetic models if the format must match) or a portal-native CSV.
- Class/assignment analytics (completion, score distribution, quota status).
- Backups, restore drill, logs, health checks.
- Accessibility and RTL polish; device testing.

**Exit:** teacher can review, override, export, and audit a full class; backup/restore is verified.

### Phase 6 — Pilot, shadow, and cutover decision (weeks 5–6)

**Goal:** prove the portal in a real class without touching the old system.

- Run one class on the portal in parallel with the existing system.
- Shadow/spot-check grading parity; collect metrics and feedback.
- Fix P0/P1 issues; document known limitations.
- Teacher sign-off; rollback plan.
- Decide whether to scale to all classes. Decommissioning the old system is a **separate, later decision**.

**Exit:** pilot report, parity evidence, security review, teacher approval.

### What is explicitly out of scope for the first release

- Automatic grade sync back to Google Classroom (manual export is enough during transition).
- A full rich-text presentation authoring studio.
- Complex quiz engines beyond the existing rubric types.
- Multi-school tenancy.
- Offline grading (the PWA can cache the shell, but grading requires the server).

---

## 6. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Shared LLM quota between old and new systems | Grading stalls or fails during the parallel period | Separate Groq key pool for the portal; schedule heavy use after the 06:00 daily check; queue + defer on daily exhaustion; show a teacher-facing quota notice |
| Multi-process router state | Request bursts, exhausted keys, inconsistent rotation | One web worker; one grading worker; DB queue. Do not scale uvicorn workers for grading. |
| Answer leakage from rubric files | Cheating, grading integrity | Redaction at publish time; never serve raw rubric JSON; automated tests over all rubric files |
| Sandbox escape / resource abuse | Server compromise | Container per run, non-root, no network, read-only FS, CPU/memory/PID/time limits, output cap, security review |
| Serialization changes the prompt | Grade drift vs. existing system | Explicit serialization contract; fixture parity; no prompt changes; record a serialization version |
| `last_audit_for` misuse | Audit verdict lost or attached to the wrong result | Call once, immediately, same thread; persist; test it |
| Rubric file changes mid-term | Students graded against a different rubric | Store SHA-256 at publish; verify at grade time; block and ask the teacher to republish |
| Mobile UX / Hebrew RTL | Low adoption | Mobile-first design, early device testing, large touch targets, CodeMirror mobile fallback, RTL from day one |
| Hosting/HTTPS complexity | Students cannot connect | Reverse proxy + TLS, or Cloudflare Tunnel; test from a real phone on the school network and off-network |
| Data privacy | Regulatory/ethical risk | Minimal PII, role-based access, encrypted transport, backups, retention policy, audit log |
| Operational burden | Service goes down unnoticed | Health checks, logs, alerts, simple restart scripts, documented runbook |
| Overbuilding | Missed timeline | Phase gates; ship the thin path (single editor, one class, one rubric type) first |

---

## 7. Decisions to confirm

These materially affect the design; they are listed as recommendations, not blockers.

1. **Hosting:** school LAN only, or reachable from home? (Determines TLS/tunnel/cloud and whether the sandbox can run locally.)
2. **Scale:** how many classes/students, and how many simultaneous submissions? (SQLite vs PostgreSQL; worker count.)
3. **Account provisioning:** teacher-created accounts (recommended) vs. self-registration with a class code and approval.
4. **Code execution scope:** "run and see output" only, or hidden tests per question? Which modules are allowed? Is `input()`/stdin needed?
5. **Presentation source:** are there existing PPTX/PDF/HTML files, and who authors new lessons? (No presentation assets exist in the repo today.)
6. **Mashov integration:** is a CSV export sufficient, or is an automated import expected?
7. **Resubmission policy:** latest attempt only, best attempt, or teacher choice? (The evaluator sees one text per grade.)
8. **Transition:** should the portal ever write grades back to Classroom, or is the old system read-only during the overlap? (Recommendation: no automatic writes.)

---

## 8. Immediate next steps (this week)

1. **Approve the architecture and the decisions in §7.** The rest can proceed without all answers, but hosting and account provisioning affect Phase 1.
2. **Create the `portal/` skeleton** (FastAPI, DB, auth, health) and run `pytest tests` to confirm the current baseline is unchanged.
3. **Build the grading adapter and parity fixtures.** This is the highest-risk integration; do it before building UI.
4. **Stand up a sandbox proof-of-concept** (Docker, no network, limits) and test escape/fork-bomb/infinite-loop cases.
5. **Draft the student and teacher wireframes** for phone and desktop; validate the submission flow with 2–3 real students.
6. **Schedule the pilot** with one class and define the parity acceptance criteria.

---

## Appendix A — Integration evidence map

| Concern | File / symbol |
|---|---|
| Single grading entry point | `src/evaluator/core.py:84` `evaluate_student_submission` |
| Batch grading | `src/evaluator/batch.py:105` `grade_students_batch` |
| Prompt assembly / trimming | `src/evaluator/prompts.py:272` `_build_prompt`; `MAX_DOC_CHARS` at `src/evaluator/config.py:87` |
| Structured-output ladder | `src/evaluator/schema.py:142` `_STAGES` |
| Late policy decision | `src/late_policy.py:140` `is_late`; `:234` `penalty_for` |
| Late penalty application | `src/late_policy.py:301` `apply_late_penalty` |
| Audit checks | `src/grade_audit.py:250` `audit_grading_result` |
| Audit verdict model | `src/grade_audit.py:66` `AuditVerdict` |
| Audit sidecar | `src/evaluator/audit.py:52` `_AUDIT_SIDECAR`; `:55` `last_audit_for` |
| LLM router | `src/llm_router.py:283` `LLMRouter`; `:801` `build_router_from_settings` |
| Adaptive pacing | `src/llm_router.py:919` `AdaptivePacer`; `src/evaluator/pacer.py:69` `paced_pause` |
| Rubric models | `src/models.py:126` `AssignmentConfig`; `:191` `AssignmentSpec`; `:260` `GradingResult` |
| Rubric loader | `src/main.py:187` `load_rubric` |
| Report/CSV writers | `src/main.py:903` `write_evaluation_report`; `:406` `write_mashov_csv` |
| Static code scan | `src/code_checks.py` |
| Settings | `config/settings.py:327` `settings` |
| Existing UI | `app.py`, `src/ui/*` (Streamlit, port 8501) |
| Existing headless path | `src/daily_check.py` |

## Appendix B — Fidelity checklist (print and tick)

- [ ] Rubric file loaded through the same `load_rubric`; SHA-256 matches the published hash.
- [ ] `student_name` is the portal display name, not the username.
- [ ] `raw_doc_text` is verbatim, untrimmed, not HTML-escaped, no added preamble.
- [ ] Multiple-choice serialization contains the selected option's **text**.
- [ ] Debugging/code-writing serialization contains the student's code verbatim.
- [ ] No pre-trim at 6000 characters; the evaluator performs the trim.
- [ ] `late_penalty_points` comes from `penalty_for` with `first_submitted_at` = first attempt.
- [ ] `late_state` and `is_late` are `None` for portal submissions.
- [ ] `evaluate_student_submission` is called with the same rubric model type.
- [ ] `last_audit_for(result)` is called once, immediately, same thread.
- [ ] Full `GradingResult` JSON + `AuditVerdict.to_dict()` are persisted.
- [ ] Model name, rubric hash, settings fingerprint, and serialization version are recorded.
- [ ] `DailyQuotaExhausted` defers the job; it does not fail the student.
- [ ] Parity fixtures pass against the golden prompts/results.
- [ ] The existing `pytest` suite is green.

## Appendix C — Recommended portal environment variables

```
# Portal
PORTAL_ENV=production
PORTAL_SECRET_KEY=<random 32+ bytes>
PORTAL_BASE_URL=https://portal.example.school
PORTAL_DATABASE_URL=sqlite:///portal/data/portal.sqlite3
PORTAL_SESSION_COOKIE_SECURE=true
PORTAL_MAX_UPLOAD_MB=5
PORTAL_RUNNER_URL=http://127.0.0.1:9001
PORTAL_RUNNER_TIMEOUT_SECONDS=5

# Reused by the existing evaluator (set before importing config.settings)
GROQ_API_KEY=...
GROQ_API_KEYS=...
GROQ_MODEL=qwen/qwen3.8-27b
GROQ_BASE_URL=https://api.groq.com/openai/v1
LLM_FALLBACK_PROVIDER=gemini
LLM_FALLBACK_API_KEY=...
LATE_PENALTY_POINTS=10
LLM_AUDIT_ENABLED=true
LLM_AUDIT_AUTOFIX=true
```

Do not commit real secrets. Keep the portal `.env` out of version control, and do not overwrite the existing root `.env` while the old system is running.
