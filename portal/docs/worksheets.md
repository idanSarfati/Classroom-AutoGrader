# Mixed worksheets for the x.3 "תרגול לפיצוח" assignments

The 2.3 and 3.3 assignments are Google Docs with several kinds of exercises:
free code, fill-in-the-blank programs, output/trace tables, true/false claims
and multiple-choice blanks. A single code box would force students to describe
answers in prose, so the portal renders a curated **worksheet spec** instead:
each exercise keeps its own shape and every answer is still submitted as one
plain Hebrew document for the existing grader.

## Design decision: specs, not Doc parsing

The portal never parses a Google Doc at request time. A Doc is only the authoring
source; the portal owns a validated JSON spec per rubric:

- `portal/worksheets/<rubric stem>.json` — for example
  `assignments/lesson_2_worksheet.json` selects
  `portal/worksheets/lesson_2_worksheet.json`.
- `portal/worksheets/schema.py` validates the file and composes answers.
- If the spec is missing, invalid or names another rubric, the page silently
  falls back to the previous single-text behavior. A broken spec can never make
  an assignment unopenable.
- Adding a future sheet means adding one JSON file; no routes or templates
  change.

The original Doc stays reachable in the collapsed "הוראות והסבר כלליים" card,
and the exercise card mirrors it with one widget per part.

There are deliberately **no link/file widgets**: a pasted Drive link cannot be
graded offline, and the portal is the code workspace. Doc exercises that asked
for a link became direct code editors.

## Spec format (version 1)

```json
{
  "version": 1,
  "rubric": "lesson_2_worksheet",
  "title": "...",
  "note": "...",
  "items": [
    {
      "key": "ex1",
      "number": 1,
      "title": "...",
      "intro": "...",
      "parts": [
        { "key": "a", "kind": "code_line", "label": "א.", "prompt": "...",
          "placeholder": "name = ..." },
        { "key": "c", "kind": "choice", "label": "ג.", "prompt": "...",
          "code": "my_birth_year = 2006\nprint(type(my_birth_year))",
          "options": ["נכון", "לא נכון"],
          "statements": [{ "prompt": "..." }, { "prompt": "..." }] }
      ]
    }
  ]
}
```

Rules enforced by `parse_worksheet`:

- `version` must be `1` and `rubric` must equal the file stem.
- Item keys, item numbers and part keys are unique; labels/prompts are optional.
- `kind` is one of:

  | kind        | Rendering                                             | Limit |
  |-------------|-------------------------------------------------------|-------|
  | `code`      | full IDE editor (syntax colours, Run code, console)   | 200,000 chars |
  | `code_line` | one LTR monospace input                               | 500 chars |
  | `short`     | small answer textarea                                 | 20,000 chars |
  | `table`     | rows of editable cells; `readonly`/`code` columns show given data | 500 chars per cell |
  | `choice`    | radio pills per statement; each statement may override `options` | options ≥ 2 |

- A `table` part lists `columns` (`key`, `label`, `readonly`, `code`) and
  `rows` as arrays of strings, one value per column.
- A `code` part may carry a starter `code` with blank lines; students edit a
  copy. Empty `code` renders an empty IDE.
- Parts that need `input()` work normally: the console answers prompts from the
  student, and the spec note says the input is presumed valid.

## Answers, names and the composed document

Field names are derived from keys, so the spec is the single source of truth:

- `answer_<item>__<part>` — `code`, `code_line`, `short`
- `answer_<item>__<part>__r<row>__<column>` — one table cell
- `answer_<item>__<part>__s<statement>` — one choice

`Worksheet.collect(form)` reads these into a JSON-able dict;
`Worksheet.compose(values)` renders the graded text:

- heading `## תרגיל N: title` per item (the same heading shape the engine
  already expects from the assignment form), with `(א)` / `(ב)` part labels and
  the part prompt before multi-part answers;
- tables as readable lines such as `78 / 95 / 90: average: 87.6; פלט: ...`;
- choices as `1. statement — chosen option`;
- empty answers are omitted, and an entirely empty sheet is rejected with a
  Hebrew error instead of queueing a blank submission.

The composed text is what `Submission.text` stores and what the grading engine
reads; it needs no rubric change and the rubric stays bound by SHA-256.

## Persistence and revisions

Structured widget values live in `portal_worksheet_responses`, keyed by
`(submission_id, revision)` with a JSON `payload`. This keeps reload/restore
exact without parsing Hebrew text, and a resubmission writes a new revision
row while the old revision and its grade remain intact. `init_db` creates the
table on the next start; existing databases need a restart (back up the SQLite
file first, as with any schema change).

On first load after deployment, a submission created before the split is shown
in a collapsed "ההגשה הקודמת שלכם (טקסט אחד)" block; the next submission
replaces it. Widget values prefill from the stored payload, so a failed or
queued grade never loses the student's answers.

## Drafts and autosave

Typed work is saved before the student hands it in, so a refresh, a closed tab,
a shared lab computer or a lost connection does not throw the sheet away.

- Browser: `portal/static/app.js` watches every form with `data-draft-url`.
  After 1.5 s without typing it posts the same fields to
  `POST /student/assignments/{id}/draft`; on `pagehide` it sends the last
  snapshot with `navigator.sendBeacon`. The status line next to the submit
  button shows "שומר…" then "נשמר אוטומטית HH:MM", and a failed save keeps
  retrying on the next keystroke.
- Server: the payload lives in `portal_answer_drafts`, keyed by
  `(assignment_id, student_id)` and tagged `worksheet`, `answers` or `code`.
  It creates **no** submission, job, revision or grade. The endpoint applies
  the same checks as submit (published, enrolled, student role, CSRF) and
  refuses payloads over 300 KB.
- Restore rule: a draft is shown only when it is newer than the current
  submission, together with a "שחזרנו את הטיוטה ששמרת" notice. An older draft
  is ignored, so it can never replace graded work. A successful submit deletes
  it; an empty autosave also clears it.
- SQLite runs in WAL mode (`PRAGMA journal_mode=WAL`), so many small autosaves
  overlap with the single grading worker instead of serialising on one writer
  lock. Because WAL keeps recent pages outside the `.db` file, stop the server
  before copying the database for a backup.

## Teacher preview and grading flow

- `/teacher/preview/assignments/{id}` renders the same worksheet read-only for
  any assignment the teacher owns (published or draft): inputs are disabled
  and there is no submit form. A draft shows a "טיוטה" banner explaining that
  students will see it only after publishing.
- Clicking an assignment's title in the class page opens that preview, and the
  grading page has a "צפייה במטלה" button, so the teacher never has to guess
  the id.
- Submission goes through the existing route and worker unchanged: text saved,
  job queued, `202` for the fetch form, reload into the pending state, and the
  grade appears automatically.
- The answered counter ("נענו N מתוך M") counts an item as answered when any of
  its fields has content, checked radio included.

## Adding or changing a sheet

1. Copy the exercise text from the Doc into a new spec next to the rubric stem.
2. Validate locally:
   `.venv\Scripts\python -m pytest portal/tests/test_worksheets.py -q`
   The suite checks both live specs, control kinds, unique field names, invalid
   spec rejection, compose/collect round-trips, submit + payload storage,
   reload restore, revision replacement, preview read-only mode and unchanged
   x.2 behavior.
3. Restart uvicorn so templates and specs reload.

The live sheets are `portal/worksheets/lesson_2_worksheet.json` (7 items;
exercise 5 is absent because the rubric marks it exempt) and
`lesson_3_worksheet.json` (8 items). Sixteen x.3 assignments across eight
classes share these two rubrics. Publication flags are untouched by the
worksheet feature; 3.3 remains a draft where the teacher left it as a draft.
