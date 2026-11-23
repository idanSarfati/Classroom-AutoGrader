# Phase 6 — class expansion and content replication

Phase 6 initializes the rest of the school's classes and copies the content
already in the portal (currently ז3) into them. It is a **planner plus an
applier**:

1. `build_plan` reads the database and the cached Classroom catalog and
   returns exactly what would change — no writes.
2. `apply_plan` writes that plan: classes, Classroom links, lessons,
   assignments and their publication flags.

The same plan feeds the CLI and the teacher screen, so both do the same
thing. Running it again is safe: nothing is duplicated and only publication
flags are corrected.

## Files

| File | Purpose |
| --- | --- |
| `portal/sync/matching.py` | Target/excluded class lists, Hebrew class-name folding, Classroom course matching |
| `portal/sync/service.py` | `build_plan`, `apply_plan`, plan/outcome dataclasses, publication modes |
| `portal/scripts/sync_classes.py` | `python -m portal.scripts.sync_classes` CLI |
| `portal/web/sync_routes.py` | `GET/POST /teacher/sync-classes` |
| `portal/templates/teacher_class_sync.html` | Teacher screen: per-class checkbox, mode and item list |
| `portal/tests/test_phase6_class_sync.py` | Matching, planning, publication, idempotency, CLI, web and access tests |

## Targets

```python
TARGET_CLASSES = ("ז3", "ז4", "ח3", "ח4", "ח6", "ט2", "ט3", "ט4")
EXCLUDED_CLASSES = ("ח8",)
```

ז3 supplies the base content. The other seven are created if missing. **ח8 is
excluded on purpose**: passing it as a target raises `SyncError` instead of
silently skipping it.

Class names are compared after folding: Unicode bidi controls (`Cf`),
case, geresh/gershayim and punctuation are removed and whitespace collapsed.
`"ז'3"`, `"ז3"` and `"ז 3"` are the same class.

## Classroom course matching

For each target class the planner looks for a course in the local catalog
whose name matches. A course name is split into its whole key plus
dash/colon/pipe segments and whitespace tokens, so
`"פייתון א' - ז4"` yields the keys `פייתוןאז4`, `פייתונא`, `ז4`.

- Exactly one match → the class is linked/updated in
  `portal_classroom_links` (`course_id`, name, section, `last_synced_at`).
- More than one match → the class is an **error**; nothing is guessed.
- No match and no existing link → an **error** for a target class.
- No match but an existing link → the link is kept and a warning is shown.

The base content never blocks a run: if it has no course match it only
warns, because its portal content is still valid to copy.

## Publication modes

Publication is decided per class, never globally by accident:

| Mode | Meaning |
| --- | --- |
| `classroom` (default) | Mirror the state of the matching item in **that class's** course: `PUBLISHED` → published, `DRAFT` → draft |
| `draft` | Everything copied as a draft |
| `published` | Everything copied as published |
| `source` | Copy the flag from the same base item |

`--class-mode ז4=draft` (CLI) or the select on each card (UI) overrides the
mode for one class. `--publish TEXT` / `--draft TEXT` force items whose title
contains `TEXT`; a forced draft wins over a forced publish.

Classroom matching uses `portal.ingestion.normalize.title_key`, which strips
`[Auto]`, copy prefixes and punctuation:

- **Assignments** match Classroom `ASSIGNMENT` coursework by title only.
- **Lessons** match any non-assignment coursework title **or attachment
  title**, because a presentation is often an attached deck rather than a
  coursework row.
- Any `PUBLISHED` copy wins over a `DRAFT` copy with the same title.
- No match in that course → the item stays a draft in `classroom` mode; an
  unreached item is never published by accident.

## Idempotency

An item counts as already present in a class when either:

1. its provenance matches — `reused_from_lesson_id` /
   `reused_from_assignment_id` points at the source item, or
2. its normalized title matches an existing item in that class.

For an existing item the applier only updates `published`; it never creates a
second copy. New copies are recorded with provenance, so a later run finds
them by rule 1 even if the title was edited.

If no target class exists, the planner reports it as “new”; after `--apply`
the class is created with a unique six-character join code and the same
teacher. Classes are matched by id, then exact name, then folded name, so a
pre-existing `ז'4` is reused instead of duplicated.

## Copy semantics

New lessons are cloned through the existing importer helpers
(`portal.ingestion.importer._clone_lesson` / `_clone_assignment`), which
already copy slides and `SlideContent`, assignment content and attachments,
and set the provenance fields. The clone helpers always create a **draft**;
Phase 6 then sets the planned publication flag. Source items are only read —
the content being copied is only read.

## CLI

```powershell
# Preview only (default): prints what each class would get
.venv\Scripts\python -m portal.scripts.sync_classes --teacher idan.sarfati

# Apply, with Classroom-derived publication
.venv\Scripts\python -m portal.scripts.sync_classes --teacher idan.sarfati --apply

# One class as published, one as drafts, everything else from Classroom
.venv\Scripts\python -m portal.scripts.sync_classes --apply `
  --class-mode "ט3=published" --class-mode "ח3=draft"

# Force one unit to stay hidden everywhere
.venv\Scripts\python -m portal.scripts.sync_classes --apply --draft "מבחן"

# Re-scan Google Classroom first (slow; needs Google credentials)
.venv\Scripts\python -m portal.scripts.sync_classes --refresh --apply
```

Options: `--teacher`, `--source` (the class whose content is copied; default
`ז3`), `--classes`
(comma-separated, default all targets), `--mode`, `--class-mode`,
`--publish`, `--draft`, `--refresh`, `--apply`, `--verbose`.

Exit codes: `0` success (or a clean preview), `1` the plan/outcome has errors,
`2` the plan itself could not be built. Without `--apply` nothing is written.

## Teacher screen

`/teacher` → **הרחבת כיתות** (`/teacher/sync-classes`).

- Shows the catalog scan time and the per-item plan for every target.
- Each card has a checkbox, the matched course name, the class/link status,
  a publication mode and warnings.
- **בצע הרחבה** applies only the selected classes and flashes a Hebrew
  summary. It is CSRF-checked like every other form.
- The page is teacher-only; foreign classes are not reachable by id because
  the screen never takes a class id — it works from the signed-in teacher's
  own catalog.

The screen uses the cached catalog, so opening it performs **no** Google
calls. Use `--refresh` when course content changed in Classroom.

## Failure semantics

- Planning is read-only. A `SyncError` (bad target, missing base content,
  ambiguous course) is shown in Hebrew by both entry points.
- Apply runs each class in a savepoint. One class failing does not roll back
  the others; its error is collected in `SyncOutcome.errors` and the CLI
  exits `1`.
- A missing Classroom snapshot is a warning, not a failure: the class is still
  created, and its items follow the chosen mode.
