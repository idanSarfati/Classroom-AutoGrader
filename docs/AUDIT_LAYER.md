# The Audit Layer

How the Classroom AutoGrader reviews, validates, and verifies LLM grading output
before any grade or feedback is finalized or released.

---

## 1. Purpose & Goal

The LLM is non-deterministic: the same submission can come back with a different
score, a missing task row, or an arithmetic slip in the deduction breakdown on
any given run. The **Audit Layer** is the deterministic counterweight — pure
local math and rule checks, **zero extra LLM tokens**, **zero network calls** —
that runs over every grading result before a teacher ever sees, edits, or
publishes it.

Its contract:

1. **Nothing unvalidated reaches a grade.** Every completion must parse into a
   valid Pydantic `GradingResult` or the request is repaired/retried/degraded
   (§3.1).
2. **Every score that survives is provably self-consistent** or explicitly
   flagged/auto-fixed (§3.2).
3. **Every flag that cannot be resolved deterministically is surfaced to a
   human**, never silently swallowed (§3.3–3.4).
4. **The audit itself can never break grading.** Every audit entry point
   degrades to a warning/log instead of an exception (§4).
5. **The verdict travels with the grade** — stored inside the report JSON so a
   batch graded days ago still carries its own confidence into the Release tab
   (§5).

The end state is one of three verdicts per grade, produced by
`src.grade_audit.AuditVerdict`:

| Verdict | Meaning |
|---|---|
| **clean** | All checks passed; `verdict.ok is True`. |
| **auto-fixed** | A provably-wrong score (deductions don't sum to the score) was repaired locally; `verdict.autofixed is True`, flag recorded. |
| **needs review** | Blocking findings remain that only a human can adjudicate; `verdict.needs_review is True`. |

---

## 2. Architecture Overview

```
        LLM completion (Groq, strict json_schema)
                 │
                 ▼
 ┌───────────────────────────────────────────────────────────┐
 │ GATE 1 — Schema & Pydantic validation   src/llm_evaluator │
 │   _STAGES ladder → _parse_result → _parse_payload         │
 │   repair_json / _repair_candidates → GradingResult        │
 │   .model_validate[_json] → _audit_deductions (warn-only)  │
 └───────────────────────────────────────────────────────────┘
                 │ valid GradingResult
                 ▼
   apply_late_penalty(result, late_penalty_points)   ← policy, not LLM
                 │
                 ▼
 ┌───────────────────────────────────────────────────────────┐
 │ GATE 2 — Inline deterministic audit       src/grade_audit │
 │   _run_audit → audit_grading_result → AuditVerdict        │
 │   score math · bounds · status coherence · rubric cover   │
 │   autofix: score := 100 − sum(deductions) when provable   │
 └───────────────────────────────────────────────────────────┘
                 │ verdict on sidecar (last_audit_for)
                 ▼
 write_evaluation_report → ReportEntry.audit = verdict.to_dict()
                 │ saved under exports/<Course>/<Assignment>/<ts>.json
                 ▼
 ┌───────────────────────────────────────────────────────────┐
 │ GATE 3 — Saved-report re-audit        src/report_audit    │
 │   audit_saved_reports → per-entry audit_grading_result    │
 │   re-resolves rubric, catches edits/new rules (read-only) │
 └───────────────────────────────────────────────────────────┘
                 │ stats + findings
                 ▼
 ┌───────────────────────────────────────────────────────────┐
 │ GATE 4 — Release gate                         app.py      │
 │   render_release_tab → _render_report_audit_warning       │
 │   Audit column + warning before any grade is published    │
 └───────────────────────────────────────────────────────────┘
```

Gates 1–2 run **inline after every single evaluation**. Gate 3 re-runs on
demand (daily check, CLI, dashboard) because a rubric can be edited, a teacher
can hand-edit a score in the Release tab, and an old report may predate a newly
added audit rule. Gate 4 is the last stop before students see a number.

## 3. Core Audit Checks & Pipeline Steps

### 3.1 Schema & Pydantic Validation (Gate 1)

**Where:** `src/llm_evaluator.py` — `_build_response_format`, `_STAGES`,
`_parse_result`, `_parse_payload`, `repair_json`, `_repair_candidates`,
`_coerce_payload`, `_audit_deductions`.

The prompt's JSON contract is enforced twice: once by asking the provider for a
**strict `json_schema` response format** projected from the `GradingResult`
Pydantic model (`_RESPONSE_FORMAT`), and once locally by validating whatever
comes back:

1. **Stage ladder.** `_STAGES` tries, strongest guarantee first:
   `json_schema` → `json_object` (JSON-only instruction appended) → `text`
   (free text, JSON-only + concise reminders). If a stage fails with a
   *degradable* error (`_is_degradable`: an `LLMJSONParsingError`, or HTTP 400
   "failed to generate JSON"), the loop re-asks with the next, looser stage
   rather than losing the student. The first working stage is remembered in
   `_first_working_stage` so the rest of the batch skips doomed round-trips.
2. **Local repair before re-asking.** `_parse_payload` tries, in order: the raw
   payload as-is, then every candidate from `_repair_candidates`
   (`repair_json` strips markdown code fences, BOM/zero-width junk, extracts the
   JSON span around prose, removes trailing commas, and attempts to close a
   truncated container). For each syntactically-valid candidate it first tries
   `GradingResult.model_validate_json(candidate)`, then a
   `GradingResult.model_validate(_coerce_payload(data))` key/value coercion
   pass for drifted key names/shapes. **Validation always runs against the
   untouched model** — only the *shape* of the payload is ever relaxed, never
   the field constraints.
3. **Total failure is a typed error, not a crash.** If no candidate validates,
   `_parse_payload` raises `LLMJSONParsingError` (subclass of
   `LLMEvaluationError`) carrying every failure reason plus a snippet of the raw
   answer — which is exactly the condition `_is_degradable` treats as worth a
   second request.
4. **Warn-only math pre-check.** After a successful parse, `_audit_deductions`
   compares `sum(deduction_breakdown.points_deducted)` against `100 - score`
   and **logs a warning without failing** — a small arithmetic slip must not
   throw away an otherwise valid grade. The real repair happens in Gate 2.
5. **Report-level validation.** Saved reports must re-validate as
   `EvaluationReport` when loaded anywhere: the Release loader
   (`app.py`, `EvaluationReport.model_validate`), the CLI review flow
   (`src/main.py`), and the re-audit (`src/report_audit.audit_report_file`,
   which records `audited.error = "not a valid evaluation report: …"` instead
   of raising).

### 3.2 Score Consistency & Mathematical Audit (Gate 2)

**Where:** `src/grade_audit.audit_grading_result(result, config, autofix=True)`.

Runs on the finished `GradingResult` (after `apply_late_penalty`, so the late
rule is already baked in). The math checks:

| Flag constant | Check | Severity |
|---|---|---|
| `FLAG_SCORE_OUT_OF_RANGE` | `score` is not an `int` in `0..100`. | `error` |
| `FLAG_DEDUCTIONS_EXCEED_MAX` | `sum(points_deducted) > 100` — the breakdown is internally impossible, so no correct score can be derived from it. | `error` |
| `FLAG_DEDUCTION_SUM_MISMATCH` | `score != 100 - sum(deductions)` — a slipped addition. | `error` |
| `FLAG_NEGATIVE_DEDUCTION` | Any deduction item has `points_deducted < 0`. | `warning` |

**Autofix rule (the only automatic repair in the system):** when
`deduction_sum_mismatch` fires and `0 <= 100 - total <= 100`, the audit rewrites
`result.score = 100 - total` in place, sets `verdict.autofixed = True`,
`verdict.final_score`, and logs `Audit auto-fixed score X -> Y`. This is
*provably* correct — the itemised breakdown is the model's own evidence. When
`autofix=False` (or `settings.llm_audit_autofix` is off) the same finding is
kept as a blocking error and the score is left untouched.
`deductions_exceed_max` deliberately gets **no** autofix: a fabricated
breakdown has no correct score to derive.

A clean run records `verdict.deductions_total`, `original_score`,
`final_score`, and `verdict.ok = True`.

### 3.3 Sanity & Edge-case Checks (Gate 2)

| Flag constant | Check | Severity |
|---|---|---|
| `FLAG_CORRECT_TASK_WITH_DEDUCTION` | A task evaluation says `CORRECT` but the breakdown deducts points for that task — status and points contradict each other. | `warning` |
| `FLAG_ZERO_DEDUCTION_NON_CORRECT` | A task evaluation says `INCORRECT` or `MISSING` but zero points are deducted for it — a "penalty" with no actual deduction. | `warning` |
| `FLAG_TYPO_DEDUCTION_CLAIM` | Feedback text matches `_TYPO_CLAIM_RE` (`typo` / `שגיאת כתיב` … `deduct` / `הורד` / `קיזז` / `-N`) — the teacher's rule is *typos are free*. | `warning` |
| `FLAG_EMPTY_FEEDBACK` | `feedback_hebrew` is empty/whitespace. | `warning` |

Task-status and deduction matching is done by **normalized task name**
(`_norm`: strip + casefold), so `DeductionItem.task_name` and
`TaskEvaluation.task_name` must name the same task to be compared.

### 3.4 Rubric Match Verification (Gate 2)

**Where:** `src/grade_audit._task_map(config)`.

Every question/task defined by the rubric must have a valid evaluation entry,
and vice versa. `_task_map` accepts **both rubric schemas**:

* **Legacy `AssignmentConfig`** — flat `config.tasks` list (name, exempt).
* **`AssignmentSpec`** (e.g. `configs/units_1_2_spec.json`) — sections are
  flattened into per-question synthetic tasks named
  `"{section.section_name} - {question.id}"`, all non-exempt, so coverage is
  checked per question rather than per legacy task.

| Flag constant | Check | Severity |
|---|---|---|
| `FLAG_MISSING_TASK_EVALUATION` | A rubric task has no corresponding `task_evaluations` entry. | `info` (systemic) |
| `FLAG_EXEMPT_TASK_GRADED` | A task marked `exempt: true` in the rubric was nevertheless graded or deducted. | `info` (systemic) |
| `FLAG_UNKNOWN_TASK_ID` | An evaluation references a task the rubric doesn't define (hallucinated row). | `info` (systemic) |

### 3.5 Severity Tiers & Verdict Semantics

Findings carry `severity ∈ {error, warning, info}` (`SEVERITY_*` constants):

* **`error`** — the number is provably wrong (the math does not close).
* **`warning`** — this student's grade breaks a rule the teacher set.
* **`info`** — *systemic*: identical for every student because the evaluator
  omitted a task row or graded an exempt task. The fix is one rubric/prompt
  change, not N per-grade decisions — so these are batched in
  `SYSTEMIC_FLAGS = {missing_task_evaluation, exempt_task_graded, unknown_task_id}`
  and deliberately **do not** make a grade `needs_review`.

Verdict semantics (`src/grade_audit.AuditVerdict`):

```
blocking  = findings with severity in {error, warning}
systemic  = findings with severity == info
needs_review = bool(blocking) and not autofixed
ok        = no findings at all, or autofixed with only the repaired
            deduction_sum_mismatch finding remaining
```

Helpers used everywhere the verdict is displayed:

* `summary()` — `"audit clean"` / `"audit auto-fixed: deduction_sum_mismatch"` /
  `"audit needs review: zero_deduction_non_correct x3"` (flags de-duplicated
  with counts).
* `badge()` — one-cell emoji badge for tables (CLI / Streamlit / dashboard all
  render the *same* wording via `src.main.audit_summary`).
* `to_dict()` / `from_dict()` — flat JSON round-trip persisted next to the
  grade; `from_dict` never raises.
* `summarize_verdicts(verdicts)` — batch rollup dict with keys `total`,
  `clean`, `autofixed`, `needs_review`, `notes`, `corrected_points` (points of
  score math repaired), `flags` (per-grade, `Counter.most_common` order), and
  `systemic` (info-level, shown separately as rubric/prompt advice).

## 4. Fallback & Recovery Mechanics

What happens when a check fails, in pipeline order:

1. **Transport-level retry (pre-audit).** HTTP 429/500/502/503/504 and network
   drops are retried by tenacity (up to 4 attempts, exponential backoff) with
   key rotation / fallback-provider routing. No audit concept needed here.
2. **Response-format degradation (pre-audit).** A degradable failure
   (`LLMJSONParsingError`, or HTTP 400 structured-output rejection) drops the
   request to the next stage of `_STAGES` (`json_schema` → `json_object` →
   `text`) and asks the LLM again — this is the *only* place the system re-asks
   the model on a content failure. If all stages fail,
   `evaluate_student_submission` raises `LLMEvaluationError` listing every
   attempt, and the caller records an error for that student instead of a
   fabricated grade.
3. **Local repair (no LLM call).** `repair_json` / `_repair_candidates` handle
   fences, prose, trailing commas, and truncation locally before any re-ask.
4. **Audit autofix (deterministic).** A provably-wrong score
   (`deduction_sum_mismatch`) is repaired in place when
   `LLM_AUDIT_AUTOFIX=true` (default). The verdict records both the original
   and final score so the repair is auditable (`autofixed` badge,
   `corrected_points` in the rollup).
5. **Flag, don't fail.** Anything ambiguous (status/points contradictions,
   empty feedback, typo claims, coverage gaps) becomes a finding. The grade
   **still flows** through the pipeline with `needs_review` set — the audit
   never discards a student's result.
6. **The audit itself is best-effort.** `_run_audit` checks
   `settings.llm_audit_enabled` first, imports `grade_audit` defensively, wraps
   the actual audit in `try/except` (`logger.warning` — "audit must never break
   grading"), and only then parks the verdict on the `_AUDIT_SIDECAR` map for
   `last_audit_for(result)` to pop. A crash inside the audit degrades to
   "no verdict attached" (`audit_summary` renders `-`), not a failed grade.
   Same principle in `daily_check.audit_stored_reports` (skip + warn) and
   `report_audit` (unreadable file → `result.errors`, run continues).
7. **Re-audit is read-only by default.**
   `audit_saved_reports(..., apply_fixes=False)` — the default — *reports*
   wrong stored grades without rewriting a single file. `apply_fixes=True`
   corrects scores **in the returned objects only**, never on disk.
8. **Human review is the final backstop.** The Release tab reads each stored
   `ReportEntry.audit` verdict and warns *before anything is published*:
   `_render_report_audit_warning` shows
   `"🔍 Audit: N of M grade(s) … need a human look"` with a per-student findings
   expander (or an `st.info` when only autofixes occurred). Nothing in the
   audit layer can push a grade to students — only the Release flow does that.

There is **no LLM "repair prompt"** after the audit: by design, post-hoc
corrections are either provable local math (autofix) or a human decision. The
only LLM re-ask is Gate 1's format/parse degradation ladder, which happens
*before* a result exists.

## 5. Integration Points in the Codebase

| Stage | File | Key functions / objects |
|---|---|---|
| Gate 1: parse & validate | `src/llm_evaluator.py` | `_RESPONSE_FORMAT`, `_STAGES`, `_parse_result`, `_parse_payload`, `repair_json`, `_repair_candidates`, `_coerce_payload`, `_audit_deductions`, `LLMJSONParsingError`, `_is_degradable` |
| Gate 2: inline audit | `src/grade_audit.py` | `audit_grading_result`, `AuditVerdict`, `AuditFinding`, `_task_map`, `summarize_verdicts`, `FLAG_*`, `SEVERITY_*`, `SYSTEMIC_FLAGS` |
| Audit trigger | `src/llm_evaluator.py` | `evaluate_student_submission` calls `_run_audit(result, rubric, label)` right after `apply_late_penalty`; gated by `settings.llm_audit_enabled` / `settings.llm_audit_autofix` |
| Verdict transport | `src/llm_evaluator.py` → `src/main.py` | `_AUDIT_SIDECAR` (keyed by `id(result)`) → `last_audit_for(result)` pops it → callers build `GradingRow(..., audit=...)` |
| CLI evaluation flow | `src/main.py` | `GradingRow.audit`, `audit_summary`, `_audit_note`, `_audit_payload`, `write_evaluation_report` (persists `ReportEntry.audit`), per-student Audit column + `"Audit: X clean, Y auto-fixed, Z need review"` rollup |
| CLI `--grade` / daily check | `src/daily_check.py` | `grade_pending_submissions` (inline audit reused via `evaluate_student_submission`), `audit_stored_reports()` → `DailyCheckResult.audit`, `format_audit_summary_from` renders it into `summary.txt`, flags `--grade`/`--no-grade` (default on) and `--no-audit` |
| Dashboard dry-run | `src/dashboard.py` | rows built with `audit=last_audit_for(result)` |
| Evaluate tab (Streamlit) | `app.py` | `GradingRow(sub, result=res, audit=last_audit_for(res))`, `"Audit"` column via `audit_summary(r)` |

| Report persistence | `src/main.py` / `src/models.py` | `write_evaluation_report` → `EvaluationReport` / `ReportEntry.audit: Optional[dict]`, `rubric_config_id` (spec rubrics store `assignment_title` as the id) |
| Gate 3: saved-report re-audit | `src/report_audit.py` | `audit_saved_reports`, `audit_report_file`, `load_config_for` (resolves rubric from `assignments/` **and** `configs/`; spec matched by `assignment_title`, legacy by `assignment_id`; missing rubric → weaker coverage check + recorded `config_error`, never silently skipped), `AuditedEntry.newly_found` (catches Release-tab score edits & new rules), `format_audit_summary` |
| Daily Reports tab | `app.py` | `_render_daily_audit_section` — Graded/Clean/Auto-fixed/Needs-review metrics + findings warning |
| **Gate 4: Release tab** | `app.py` | `render_release_tab` → `_render_editable_entries` (Audit column via `_entry_audit_badge`) → `_render_report_audit_warning` (pre-publish warning + findings expander); report loaded via `EvaluationReport.model_validate` |
| CLI summary tool | `src/report_audit.py` | `python -m src.report_audit` — text or `--json` rollup, **exit code 1 when any grade needs review** (CI/scheduler friendly) |
| Configuration | `config/settings.py` | `llm_audit_enabled` (`LLM_AUDIT_ENABLED`, default `true`), `llm_audit_autofix` (`LLM_AUDIT_AUTOFIX`, default `true`) |

### Rubric coverage across both schemas

The audit is schema-agnostic end to end:

* **Inline audit** — `_task_map` flattens `AssignmentSpec.sections[].questions[]`
  or reads `AssignmentConfig.tasks[]`.
* **Re-audit rubric resolution** — `report_audit.load_config_for` scans
  `assignments/` then `configs/`, matching `assignment_id` (legacy) or
  `assignment_title` (spec, per `src.main.rubric_config_id`).
* **Reports** — one `EvaluationReport` shape regardless of rubric type; the
  rubric is referenced only through `assignment_config_id`.

## 6. Running / Inspecting the Audit

```bash
# Re-audit every saved report under exports/ (read-only, zero tokens).
# Text summary; exit 1 if any grade needs a human look.
python -m src.report_audit

# Machine-readable rollup (stats + rows + findings).
python -m src.report_audit --json

# Limit scope / point at other directories.
python -m src.report_audit --root exports --assignments assignments --limit 20
```

Daily check (headless) runs the same re-audit automatically unless `--no-audit`,
stores it in `daily_check.json`, and prints the `GRADE AUDIT` block into
`summary.txt`.

Per-run inline audit knobs:

```bash
LLM_AUDIT_ENABLED=false   # skip the deterministic audit pass entirely
LLM_AUDIT_AUTOFIX=false   # flag score mismatches instead of repairing them
```

Reading a single verdict:

```python
from src.grade_audit import AuditVerdict

verdict = AuditVerdict.from_dict(entry.audit)   # from a saved EvaluationReport
print(verdict.summary())     # "audit clean" / "audit needs review: …"
print(verdict.badge())       # one-cell badge for a table
print(verdict.needs_review)  # does a human still have to look?
```

---

## 7. Flag Reference

| Flag | Severity | Raised when |
|---|---|---|
| `deduction_sum_mismatch` | error | `score ≠ 100 − sum(deductions)`; autofix candidate |
| `score_out_of_range` | error | score not an int in 0–100 |
| `deductions_exceed_max` | error | deductions sum > 100 (impossible breakdown) |
| `negative_deduction` | warning | a deduction item has negative points |
| `correct_task_with_deduction` | warning | task `CORRECT` yet points deducted for it |
| `zero_deduction_non_correct` | warning | task `INCORRECT`/`MISSING` with 0 deducted |
| `typo_deduction_claim` | warning | feedback claims points were deducted for typos |
| `empty_feedback` | warning | feedback is empty |
| `missing_task_evaluation` | info (systemic) | rubric task has no evaluation entry |
| `exempt_task_graded` | info (systemic) | exempt rubric task was graded anyway |
| `unknown_task_id` | info (systemic) | evaluation references a task not in the rubric |

Severity contract: `error`/`warning` findings block (`needs_review`) unless the
grade was auto-fixed; `info` findings are batch-level rubric/prompt advice and
are rolled up separately in `summarize_verdicts()['systemic']`.

---

*Tests: `tests/test_llm_resilience.py` (audit checks, autofix, JSON round-trip,
rollup) and `tests/test_report_audit.py` (saved-report re-audit, severity
separation, read-only guarantee).*






