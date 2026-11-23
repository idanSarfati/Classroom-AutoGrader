# Phase 2.5 — Classroom content ingestion plan

Status: analysis + implementation plan. No production code changed yet.
Scope: all new code stays under `portal/`; `src/`, `app.py`, `config/` and
`tests/` remain read-only.

## 1. Executive summary

Import Classroom content **once**, store it **locally**, and serve it from the
portal under portal authentication. Students never receive a Google login, a
Drive id, or a Drive/Docs/Slides URL.

Recommended route, in this order:

1. **2.5a — Docs + assignments (works with today's token).**
   Read courses, topics, coursework and attachments; export Google Docs as
   HTML; sanitize it; store it in the portal DB; bind coursework to local
   rubrics in a teacher-reviewed preview.
2. **2.5b — Slides as local images (works with today's token).**
   Export each deck as PDF through the Drive API (verified working) and render
   pages to WebP with `pypdfium2`; extend the existing slide viewer to display
   local images. This is pixel-faithful and mobile-friendly, at the cost of
   slide text/searchability.
3. **2.5c — optional structured import (needs a one-time portal re-consent).**
   Add `presentations.readonly` and
   `classroom.courseworkmaterials.readonly` through a portal-owned OAuth
   script, enabling per-slide text, the Classroom "Materials" tab, and better
   DOCX/Docs fidelity. This never modifies `src/auth.py` or the root token.

Do **not** wait for 2.5c: 2.5a and 2.5b cover Docs, coursework, and deck
visuals with the existing credentials.

## 2. What the existing integration actually provides (verified read-only)

The probe used the real `token.json` and made read-only calls only. No course
names, titles, file ids or student data are reproduced here.

| Capability | Result |
|---|---|
| Active courses | 11 |
| Coursework (PUBLISHED + DRAFT) | 1,148 |
| Attachments | 977 references to 495 unique files |
| Attachment types | 930 Google Doc refs, 47 DOCX; 2 links, 5 forms |
| Attached Slides decks | 0 |
| Token scopes | 9, including full `drive`, `documents.readonly`, Classroom read/write |
| `presentations.readonly` | **not granted** — Slides API returns HTTP 403 |
| `classroom.courseworkmaterials.readonly` | **not in the root token** — Materials tab returns HTTP 403. The portal token is granted it after one one-time consent (see below) |
| Drive `files.export` of a deck to PDF | **works** (1.7 MB test deck) |
| Drive `files.export` of a deck to HTML | unsupported (HTTP 400) |
| Docs API text extraction | works (2.3k-char test doc) |
| Drive Docs → HTML export | works; preserves `h1`, `h3`, `p`, `ul`, `ol`, `li` |
| Docs HTML styling | 183 `<span>` and 339 inline `style=` attributes in the sample; must be normalized/stripped |

### 2.1 The existing attachment helper is wrong for this account

Classroom returns Drive attachments as a **nested** `SharedDriveFile`:

```json
{"driveFile": {"driveFile": {"id": "...", "title": "...", "alternateLink": "..."},
               "shareMode": "..."}}
```

`src/list_coursework_materials.py::describe_materials` reads
`material["driveFile"]["id"]`, so it silently reports **zero** Drive files for
all 977 attachments. The ingestion boundary must normalize both the nested
shape and the flat `DriveFile` shape. Do not patch `src/`; normalize in
`portal/ingestion/`.

### 2.2 There is no course → deck mapping today

No coursework in the scanned courses attaches a Slides deck, and the Materials
tab is inaccessible. Therefore the plan must support:

- decks attached to coursework or the Materials tab (once scoped), **and**
- a teacher-selected **Drive folder or deck** import for decks that are not
  attached anywhere. This is the only reliable path for the existing decks.

## 3. Gaps and constraints

- `get_google_credentials()` may start an interactive OAuth flow. Calling it
  from a FastAPI request would block or try to open a browser. The web process
  must use a **non-interactive** loader: load the token, refresh if possible,
  and raise an actionable error if not. Only a CLI may run consent.
- The root token is shared with the existing Streamlit app. Treat it as
  read-only: refresh in memory/atomically, never modify its scopes, never
  print it.
- `ClassroomService` is built for one process and serializes Google I/O with a
  lock. The importer must be a **single worker**, not a pool.
- A full import can touch hundreds of files. Preview must be per course, and
  apply must be resumable; do not loop all 1,148 items in one HTTP request.
- The portal has `create_all`, not migrations. New tables are safe; do not
  alter existing tables in this phase.
- `portal/content/` is not yet git-ignored. It must be added before any
  imported file is written there.

## 4. Target architecture

```text
Google Classroom/Drive/Docs
        |
        |  read-only, one boundary module
        v
portal/ingestion/classroom_source.py   (imports src.* here and nowhere else)
        |  normalized snapshots (no raw Drive payloads)
        v
portal/ingestion/converter.py          Docs HTML -> sanitized HTML
portal/ingestion/deck.py               PDF -> WebP pages
        |
        v
portal/ingestion/importer.py           plan -> DB + blobs, idempotent
        |
        +--> portal.db  (text, metadata, source versions, job state)
        +--> portal/content/blobs/<sha256>.<ext>  (images, PDFs, DOCX)
        |
        v
teacher preview/apply routes  +  CLI
        |
        v
student routes (auth + class visibility)  ->  local lesson viewer / assignment page
```

Two read-only boundaries now exist, both documented in `portal/__init__.py`:

- `portal/grading/adapter.py` → grading engine (unchanged)
- `portal/ingestion/classroom_source.py` → Classroom/Drive/Docs reads

Nothing else in `portal/` may import `src.*`.

## 5. Data model (new tables only)

All tables are new, so `Base.metadata.create_all` is sufficient; no existing
table is altered.

```text
portal_classroom_links
  id, teacher_id, class_id UNIQUE, course_id, course_name, section,
  last_synced_at
  -- one portal class is linked to one Classroom course

portal_import_jobs
  id, teacher_id, class_id, course_id, state, counts_json, error,
  created_at, started_at, finished_at
  -- states: preview, queued, running, completed, partial, failed, cancelled

portal_import_items
  id, job_id, source_kind, source_id, parent_id, title, decision,
  status, message, portal_type, portal_id, source_version, content_hash
  UNIQUE(teacher_id, class_id, source_kind, source_id)
  -- source_kind: coursework | material | drive_file
  -- status: pending | imported | updated | skipped | failed | needs_review

portal_content_blobs
  sha256 PK, rel_path, mime_type, byte_size, created_at
  -- content-addressed; identical exports are stored once

portal_lesson_sources
  lesson_id PK/FK, source_kind, source_id, source_version, topic_name,
  render_mode (images|text|mixed), page_count, imported_at, content_hash

portal_slide_content
  slide_id PK/FK, image_blob_sha, alt_text, width, height,
  text_snapshot, body_html (sanitized)

portal_assignment_content
  assignment_id PK/FK, body_html (sanitized), body_text, topic_name,
  source_kind, source_id, source_version, attachments_json, imported_at
```

`attachments_json` and all source ids are **teacher-only**; no student route
reads or serializes them.

Assignment import policy:

- Only coursework the teacher explicitly binds to a rubric becomes an
  `Assignment`. This preserves the existing invariant that every published
  assignment has a verified rubric hash.
- The preview **suggests** a rubric with
  `src.config_match.suggest_config_for_assignment`, limited to
  `portal.paths.RUBRIC_ROOTS` (`assignments/`, `configs/`). The teacher
  confirms; a low score or ambiguity is shown, never silently accepted.
- Coursework imported without a rubric can become a **lesson/reading
  resource** instead, or be skipped and left in a "needs rubric" list.

## 6. Storage and serving

```text
portal/content/
  blobs/<sha[:2]>/<sha>.<ext>     immutable, content-addressed
  imports/<job_id>/manifest.json  teacher-only audit/preview
```

- Text lives in SQLite; binary media lives in `content/blobs/`.
- DB stores relative paths and SHA-256, never absolute paths.
- Add `content/` and `data/google_token.json` to `portal/.gitignore`.
- Serve media through authenticated routes, **not** `StaticFiles`:
  - `/student/lessons/{lesson_id}/slides/{position}/image`
  - `/student/assignments/{assignment_id}/media/{name}`
  - `/teacher/imports/{job_id}/preview` (teacher-only)
- Each media route re-checks:
  - the lesson/assignment is published and visible to this student/class, and
  - the blob belongs to that object.
- Send `Cache-Control: private, max-age=3600`, an `ETag` from the SHA-256,
  `X-Content-Type-Options: nosniff`, and an allowlisted `Content-Type`
  (WebP/PNG/PDF only; never SVG).
- Never expose `/content/blobs/...` as a public URL and never write the Drive
  id into student HTML.

## 7. Conversion pipeline

### 7.1 Google Docs → local HTML

Use the Drive HTML export (`files.export(mimeType="text/html")`) because it
preserves headings, lists and tables. The Docs API text extraction remains the
fallback when export fails.

1. Strip `<html>`, `<head>`, `<style>`, scripts and comments; keep `<body>`.
2. Convert bold/italic `<span style="...">` runs to `<strong>`/`<em>`;
   discard all other inline styles (prevents CSS injection and mobile layout
   breakage).
3. Sanitize with `nh3` (preferred) or `bleach`:
   - tags: `p h1 h2 h3 h4 h5 h6 ul ol li table thead tbody tr td th strong em
     code pre blockquote br`
   - attributes: table cell colspan/rowspan only; no `style`, `class`, `id`,
     event handlers, `iframe`, `object`, `script`
   - links: drop Drive/Docs URLs and keep the link text; other links may be
     removed for a strictly offline reader
4. Images:
   - Docs HTML may reference `lh3.googleusercontent.com` URLs that require
     auth. Download them server-side with the same credentials, store the
     bytes as blobs, and rewrite `src` to the portal media URL.
   - If an image cannot be fetched, replace it with an alt-text placeholder and
     mark the item `needs_review`; never leave the external URL.
5. Store the sanitized HTML in `portal_assignment_content.body_html` or
   `portal_slide_content.body_html`; store a plain-text fallback for search.
6. Sanitize **at import time**, and add a test that the stored HTML contains no
   `script`, `style`, `iframe`, `on*` or `googleusercontent` strings.

The doc body is teacher-owned, but it can contain links/images from arbitrary
sources. Treat it as untrusted input.

### 7.2 Google Slides → local slide images

With today's token, the Slides API is unavailable; the Drive PDF export works.

1. `files().export(fileId=deck_id, mimeType="application/pdf")` through the
   service's retry choke point.
2. Render each page with `pypdfium2` (Apache/BSD, lighter and not AGPL):
   ```python
   import pypdfium2 as pdfium
   from io import BytesIO

   def render_pages(pdf_bytes: bytes, max_width: int = 1600) -> list[bytes]:
       pdf = pdfium.PdfDocument(pdf_bytes)
       try:
           pages = []
           for index in range(len(pdf)):
               page = pdf[index]
               width_pt, _ = page.get_size()
               scale = min(max_width / width_pt, 2.0)
               image = page.render(scale=scale).to_pil()
               if image.mode not in ("RGB", "RGBA"):
                   image = image.convert("RGB")
               buffer = BytesIO()
               image.save(buffer, format="WEBP", quality=82, method=4)
               pages.append(buffer.getvalue())
           return pages
       finally:
           pdf.close()
   ```
3. Store each page as a blob; create one `LessonSlide` per page with
   `title="שקופית N"`, plus a `PortalSlideContent` row with the image blob and
   optional text extracted from the PDF.
4. Extend `student_lesson.html`:
   ```jinja
   {% if current.media %}
     <img class="slide-image" loading="lazy"
          src="/student/lessons/{{ lesson.id }}/slides/{{ index }}/image"
          alt="{{ current.media.alt_text or 'שקופית ' ~ (index + 1) }}">
   {% endif %}
   ```
   Keep the existing text/code rendering as a fallback for manually authored
   slides.
5. Use the first page as the lesson cover/thumbnail (optional).

Trade-off: image slides are not copyable/searchable. If the teacher needs code
or text from a deck, 2.5c adds structured text via the Slides API; the image
remains the visual source of truth.

### 7.3 DOCX and other attachments

47 attachments are DOCX. The Docs API cannot read them.

- 2.5a/b: download via `files.get_media`, store the blob, and show it as a
  local downloadable attachment (teacher-only until published). No external
  link.
- 2.5c: convert DOCX to sanitized HTML with `mammoth` (new dependency) or
  extract text with `python-docx`; then treat it like a Doc.
- PDFs: store and serve locally; no conversion needed.
- Forms/YouTube/external links: keep as teacher-only metadata; do not expose
  external URLs to students. A future phase can embed approved resources.

### 7.4 Classroom "Materials" tab

Blocked by a missing scope. In 2.5c, after re-consent:

- `courses.courseWorkMaterials.list` returns the Materials tab items; map them
  to lessons/resources exactly like coursework attachments.
- The preview shows a clear "requires additional permission" badge until the
  portal token has the scope.

## 8. Import workflow

### 8.1 Preview → apply

1. **Choose course + destination class.**
   `list_courses()`; remember the link in `portal_classroom_links`.
2. **Build a preview manifest** (server-side snapshot):
   - topics (`list_topics`) and coursework (`list_course_work_items`);
   - normalized attachments (nested and flat Drive shapes);
   - Drive mime types and modification times;
   - suggested rubric per coursework, with score/reason;
   - duplicate/update status against `portal_import_items`.
3. **Teacher decisions** per item:
   - `assignment` + rubric (required), or
   - `lesson` (deck/doc/reading), or
   - `skip`.
   - Default: import as **draft**, `published=False`. Nothing reaches students
     until the teacher reviews and publishes.
4. **Apply** creates a `PortalImportJob` and enqueues it.
5. **Worker** processes items one by one, commits per item, updates the job,
   and records failures without aborting the run.
6. **Review screen** shows imported/updated/skipped/failed/needs_review and
   links to the created portal objects.

### 8.2 Idempotency and updates

- Unique `(teacher_id, class_id, source_kind, source_id)` prevents duplicates.
- Compare `source_version` (Drive `modifiedTime`/`version`, Docs `revisionId`,
  or a SHA-256 of the exported bytes).
- Unchanged source → `skipped`.
- Changed source → update generated fields and mark `updated`.
- Teacher-edited portal object (`updated_at > imported_at`) → `needs_review`
  by default; `--force` overwrites. Never silently discard a teacher edit.
- Deleting an imported object in the portal never deletes the Google source.

### 8.3 Where it runs

- **CLI (authoritative, resumable):**
  ```powershell
  .venv\Scripts\python -m portal.scripts.classroom_import list-courses
  .venv\Scripts\python -m portal.scripts.classroom_import preview `
      --course-id <id> --class-id <id> --out plan.json
  .venv\Scripts\python -m portal.scripts.classroom_import apply --plan plan.json
  .venv\Scripts\python -m portal.scripts.classroom_import resume --job-id <id>
  ```
  `preview` is the default; `apply` is explicit. `--include-drafts`,
  `--limit`, `--kinds docs,slides`, `--publish` are opt-in.
- **Web UI (teacher-only):** preview + apply endpoints enqueue the same job.
  A single daemon worker thread started by the app lifespan processes it. The
  job page polls with HTMX or a meta refresh. If the process dies, `resume`
  picks up `pending` items.
- Do not run a thread pool: the Google client and its lock are single-threaded
  by design, and SQLite is happiest with one writer.

## 9. Mapping rules

| Classroom | Portal |
|---|---|
| Course | Existing `ClassGroup`, linked through `portal_classroom_links` |
| Topic | `topic_name` on lesson/assignment content; grouping label in preview |
| Coursework title | `Assignment.title` / `Lesson.title` (max 160 chars; truncate + report) |
| Coursework description HTML | `portal_assignment_content.body_html` (sanitized) |
| `dueDate` + `dueTime` | `Assignment.due_at`, naive UTC (existing convention) |
| `maxPoints` | Reference only; the portal rubric's `total_points` governs grading |
| Drive Doc attachment | Assignment instructions (if description empty) and/or local resource |
| Drive Slides attachment | Lesson + local slide images |
| Drive DOCX/PDF | Local downloadable resource (2.5a) or converted HTML (2.5c) |
| Forms/links/videos | Teacher-only metadata; not exposed |
| Student submissions/grades | **Never imported** |

An assignment is created only when the teacher selects a rubric. The rubric
path is resolved under `assignments/`/`configs/`, hashed at publish time using
the existing Phase 1/2 flow, and never sent to students.

## 10. Security and privacy

- Teacher-only routes; every job/object is ownership-checked; CSRF on all
  state-changing forms (`Depends(verify_csrf)`).
- No Drive ids, file paths, tokens, or rubric answers in student HTML, JSON or
  media URLs.
- Imported HTML sanitized; no scripts/styles/iframes/event handlers; no
  external image or link fetches in the student browser.
- Media serving checks publication and class membership on every request.
- Filenames are generated from content hashes; never trust Drive names for
  paths.
- Tokens stay server-side; `portal/data/` and `portal/content/` are git-ignored;
  logs never include tokens or document content.
- The importer is read-only against Google: no Classroom writes, no grade
  changes, no comments, no student data.
- If a scope is missing or revoked, fail with an actionable message and do not
  silently fall back to exposing a Drive link.

## 11. Reliability and performance

- Reuse `src.classroom_service.ClassroomService` (retry/backoff and the Google
  I/O lock) but build it in the ingestion boundary only.
- One service instance per worker; caches for Drive mime types and profiles.
- Per-course preview; per-item transactions; resumable job; `--limit`.
- Content-addressed dedupe: 930 Doc references collapse to 495 unique files,
  and repeated exports of the same bytes store one blob.
- Bound exports/pages: skip decks above a configured page/size limit and mark
  `needs_review`; never let one bad file abort a course.
- Add an orphan-blob cleanup command after a successful import.
- Never call `get_google_credentials()` from a request handler. Use
  `portal/ingestion/google_auth.py::load_credentials(interactive=False)`;
  the CLI may call it with `interactive=True` for the one-time consent.

## 12. Testing plan (no network)

- Fake `ClassroomSource` fixtures:
  - nested `SharedDriveFile` and flat `DriveFile`;
  - Docs HTML with headings/lists/tables/spans and one external image;
  - a tiny valid PDF for the renderer;
  - 403 missing-scope and 404 unreadable-file cases.
- Unit tests: attachment normalization, sanitizer (XSS payloads, styles,
  external images), PDF page rendering (stub or fixture), blob hashing,
  idempotency, conflict detection, path safety.
- Integration tests with the existing portal fixtures:
  - preview → apply creates draft lessons/assignments and blobs;
  - re-apply creates no duplicates;
  - changed source updates, teacher edit becomes `needs_review`;
  - unpublished imported content is invisible to students;
  - student media route requires login and class visibility;
  - no Drive id/path/hash/answer appears in student HTML;
  - deleting a lesson/assignment removes its link rows (blobs cleaned by the
    orphan command, not by cascade).
- Existing suites remain unchanged: `pytest portal/tests -q` and
  `pytest tests -q` must stay at their recorded baselines.
- Manual live smoke: preview one course read-only, verify counts and that the
  generated HTML contains no external URLs.

## 13. Milestones and files

### 2.5a — Docs, coursework, assignment drafts
- `portal/ingestion/classroom_source.py` — boundary, snapshots, normalization
- `portal/ingestion/google_auth.py` — non-interactive token loader
- `portal/ingestion/converter.py` — Docs HTML → sanitized HTML
- `portal/ingestion/importer.py` — preview/apply, idempotency, DB writes
- `portal/ingestion/worker.py` — single background worker (web)
- `portal/models.py` — new extension/job tables
- `portal/scripts/classroom_import.py` — CLI
- `portal/web/teacher_routes.py`, templates — import preview/apply/status
- `portal/web/student_routes.py`, templates — rich instructions rendering
- `portal/requirements-portal.txt` — add `nh3`
- tests + README + `.gitignore`

### 2.5b — Slides as local images
- `portal/ingestion/deck.py` — PDF export → WebP pages
- `portal/requirements-portal.txt` — add `pypdfium2`
- student slide viewer + media route
- tests

### 2.5c — optional, after re-consent
- `portal/scripts/google_consent.py` — request the superset of scopes, write
  `portal/data/google_token.json`; never touch `src/auth.py`
- Materials tab ingestion, structured slide text, DOCX conversion, Docs image
  localization, sync/diff UI

## 14. Decisions to confirm before implementation

1. **Slides:** accept image-based slides now (recommended), or wait for a
   one-time re-consent to add structured text? Images can ship immediately.
2. **Materials tab:** defer until re-consent (recommended), or include the
   consent flow in the first implementation?
3. **Default publish state:** import as drafts and require review
   (recommended), or auto-publish? Drafts protect students from wrong rubric
   bindings.
4. **Deck source:** rely on attachments only, or also add the Drive
   folder/deck picker (recommended, because no decks are attached today)?
5. **Import entry point:** CLI + web preview/apply (recommended), or web only?

## 15. Ready-to-use code sketches

### 15.1 Normalize the two attachment shapes

```python
def drive_file_from_material(material: dict) -> dict | None:
    node = material.get("driveFile")
    if not isinstance(node, dict):
        return None
    # SharedDriveFile: {"driveFile": {...}, "shareMode": "..."}
    inner = node.get("driveFile")
    file = inner if isinstance(inner, dict) else node
    file_id = str(file.get("id") or "").strip()
    if not file_id:
        return None
    return {
        "id": file_id,
        "title": str(file.get("title") or "").strip(),
        "link": str(file.get("alternateLink") or "").strip(),
        "share_mode": node.get("shareMode"),
    }
```

### 15.2 Course snapshot boundary

```python
@dataclass(frozen=True)
class Attachment:
    file_id: str
    title: str
    mime_type: str | None

@dataclass(frozen=True)
class Coursework:
    id: str
    title: str
    description_html: str
    state: str
    work_type: str
    topic_id: str | None
    topic_name: str | None
    due_at: datetime | None
    max_points: int | None
    attachments: tuple[Attachment, ...]

@dataclass(frozen=True)
class CourseSnapshot:
    course_id: str
    course_name: str
    section: str | None
    topics: Mapping[str, str]
    coursework: tuple[Coursework, ...]
```

`classroom_source.py` is the only module that imports `src.classroom_service`,
`src.auth`, `src.api_retry` or `src.config_match`.

### 15.3 Non-interactive credentials

```python
def load_credentials(*, interactive: bool = False) -> Credentials:
    # Load token.json; refresh if possible; never start a browser here.
    # interactive=True is reserved for the CLI consent script.
    ...
    raise IngestionAuthError(
        "Google authorization is missing or expired. Run: "
        "python -m portal.scripts.google_consent"
    )
```

The web process must call this with `interactive=False` only.

## 16. Acceptance criteria

- A teacher can preview one course and see every coursework item, its topic,
  due date, attachments and a suggested rubric.
- Applying the plan creates **draft** lessons/assignments locally; students
  see nothing until publish.
- A Doc renders with headings/lists and no external Google URLs.
- A deck renders in the existing slide viewer as locally served images, with
  previous/next navigation and no Drive/PDF link.
- Re-running an import creates no duplicates; changed sources update; teacher
  edits are preserved or flagged.
- An assignment is never created or graded without a teacher-confirmed rubric.
- Student pages contain no Drive ids, paths, hashes, tokens or answer fields.
- `pytest portal/tests -q` and `pytest tests -q` stay at their baselines.

## 17. Implementation status (Phase 2.5a + 2.5b)

Implemented under `portal/` only. `src/`, `app.py`, `config/` and the existing
`tests/` tree are untouched.

| Area | Module |
| --- | --- |
| Google boundary (only `src.*` importer) | `portal/ingestion/classroom_source.py` |
| Non-interactive credentials | `portal/ingestion/google_auth.py` |
| Docs HTML → sanitized local HTML | `portal/ingestion/converter.py` |
| PDF → WebP slide images | `portal/ingestion/deck.py` |
| Title/tag + content dedupe | `portal/ingestion/normalize.py` |
| Preview/apply, updates, reuse | `portal/ingestion/importer.py` |
| Local course/deck catalog (scan once) | `portal/ingestion/catalog.py` |
| Background queue (one thread) | `portal/ingestion/worker.py` |
| Content-addressed blobs | `portal/ingestion/storage.py` |
| CLI | `portal/scripts/classroom_import.py` |
| Teacher preview/apply/status + reuse UI | `portal/web/teacher_routes.py`, templates |
| One-screen class picker (course items + Drive decks) | `/teacher/classes/{id}/new`, `teacher_class_new.html` |
| Student HTML/attachments/slide images | `portal/web/student_routes.py`, templates |

### Add from the course (one-screen picker)

A class page has two entry points - **שיעור חדש** and **הוספה מהקורס** - that
both open `/teacher/classes/{id}/new`. That page lists the linked Classroom
course's coursework (grouped by topic, with a quick filter), the teacher's
Drive teaching decks, and the standalone Google Docs that hold lesson
material, all in the same window:

1. Tick the specific items to add.
2. For each coursework row choose **שיעור / חומר לימוד**, **מטלה עם מחוון** or
   **לא להוסיף**. Decks and documents always become lessons.
3. Assignments must have a rubric; the field appears only for that choice and
   is pre-filled from the title suggestion when one is confident.
4. **הוספה לכיתה כטיוטה** validates rubrics, records the choice in a
   `selection` import job and queues it. The redirect shows per-item progress.

Already-imported items show a badge and default to their existing type.
The badge survives duplicate groups and course copies: importing any copy
marks the visible row (**כבר בכיתה**) and the same title imported from another
course copy is recognized too. Files listed under an assignment carry their
own **כבר בכיתה** badge once that assignment (or the file itself) is in class.
Exact duplicates are listed once (with a "N עותקים זהים אוחדו" badge) instead
of as disabled dead rows. Each assignment appears **once**: when a Classroom
draft mirrors a published copy, only the published copy is offered (the draft
stays hidden), and a title that exists only as a draft keeps a **טיוטה בקורס**
badge. Everything lands as a draft.

Coursework and Drive are two views of the same material: the deck or worksheet
attached to a Classroom item is also found by the Drive folder scan. The picker
hides the Drive row when its normalized title matches an attachment of a
visible coursework row. The comparison ignores `[auto]`/`[Template]` tags,
copy prefixes and the `פייתון א'` course marker, so "3.3 | פייתון א | תרגול
לפיצוח" and "[Template] 3.3 | תרגול לפיצוח" are one material. The coursework
row wins because it carries the rubric, due date and type selector, and its
attachment holds the content. A Drive deck or document with no coursework
counterpart still appears.

The **כבר בכיתה** badge applies to published coursework and to Drive decks. A
Classroom draft is never marked as "already in class", even if the same id was
imported earlier - it was not published to the class. The duplicate-group and
cross-course title checks described above only run for published rows.

The teacher's unused **פייתון ב'** course and its Drive decks are excluded from
the picker. Decks are recognized by their parent folder name (the folder
carries the marker even when the title does not, e.g. "2.1 | פעולות ותנאים |
חזרה"), with a title marker as fallback for older catalogs. The filter runs on
read as well as on scan, and the course is not offered in the selector.

Coursework and Drive decks share one section and one quick filter, and every
topic is a collapsed `<details>` by default - opening a topic reveals its
items. A Drive deck joins the topic matching the number in its title
("2.1 | ..." goes to topic 2); 0.x decks join an unnumbered topic whose own
rows are 0.x (for example **היכרות**), and unmatched rows are collected under
**חומרים ללא נושא** at the end. Searching opens the matching groups and
collapses them again when the field is cleared; the summary shows how many
rows are selected so a closed topic with a checked item is visible.

Standalone **Google Docs** are scanned from the folders that hold the decks
(and any other teaching folder with a lesson number). A doc enters the picker
only when its title starts with a lesson number ("4.5 | ..."), so shared
notebooks and student hand-ins stay out; a doc with no number is ignored.
Drive's "[Template]" and "[תבנית]" markers are stripped before the number is
read and from the display title, so worksheets that exist only as
"[Template] 11.3 | ..." (and their "עותק של" copies) still appear as lesson
11.3. Numbers in the folder name count for the **פייתון ב'** filter, just
like decks, so a doc in a ב' folder is hidden.

### The Classroom Materials tab

Classroom keeps **Coursework** (assignments and questions) and **Materials**
(a separate tab: worksheets, links, videos) in two different resources.
Reading Materials needs its own scope, ``classroom.courseworkmaterials.readonly``;
the coursework scopes the grading engine already holds do not cover it and the
API answers **403 insufficient authentication scopes**. The portal therefore
requests the extra scope in addition to ``src.auth.SCOPES`` (``src/`` itself is
not modified) and needs one new consent:

```powershell
.venv\Scripts\python -m portal.scripts.classroom_import auth
```

The auth command is allowed to run the browser consent even when the existing
token lacks the new scope; a normal web request never opens a browser. After
consent the scan reads every page of ``courses.courseWorkMaterials.list`` and
merges the items into the same snapshot with work type ``COURSE_MATERIAL`` and
an id prefixed ``material:`` (the resource id alone could collide with a
coursework id). Materials have no due date or points, show a **חומר לימוד
בקורס** badge, default to **שיעור / חומר לימוד**, and import as lessons. Titles
do not need a lesson number: the Materials tab is hand-curated teaching
content. If the scope is missing, the scan fails with a message that names the
auth command instead of silently returning fewer rows.

The list call passes ``courseWorkMaterialStates=["PUBLISHED", "DRAFT"]``:
without it the API returns only published items, which is exactly how the
teacher's draft worksheets (5.6, 6.6, 9.5, 10.6, 13.7, 13.8, 16.4, 18.4 and
the two unnumbered topic reviews) stayed invisible. Materials are often not
Drive files at all - H5P and lomdot are plain external links, and a few items
carry no content in Classroom. A lesson built from a link material gets a
**קישורים** slide with clickable, escaped links (the picker labels the
attachment **קישור**); an item whose Classroom body is genuinely empty still
imports as a title-only draft, because a readable draft is easier for the
teacher to fix than a silent skip.

The list reads like the syllabus, not like Classroom's newest-first order:
topics are sorted by their leading number (**1 | מבוא לאלגוריתמיקה והדפסה**
before **2 | משתנים** ... before **20 |**, with unnumbered topics last), and
materials inside a topic use a natural key so 1.1 → 1.2 → 2.1 → 2.10 and a
title without a number ("תרגול למבחן יחידות 1-2") comes after the numbered
items. Drive decks and documents use the same natural key.

### Scan once, then read locally

Opening the picker does **not** call Google. The teacher's catalog of courses,
coursework, Drive decks and lesson documents is stored in
`portal_classroom_catalog` by an explicit scan (the button at the top of the
picker):

- **סריקת חומרים חדשים** queues a `catalog_sync` job on the existing single
  import worker and returns immediately; the page shows "סורק עכשיו…" and
  reloads itself every 4 seconds until the scan finishes.
- The scan refreshes the course list and all Drive deck and document names,
  plus the coursework and Materials of the course currently shown (the linked
  course by default). A full scan was measured at ~2m45s for 11 courses /
  1,830 items (1,148 coursework + 682 Materials, drafts included); a single
  course takes ~6s.
- Drive listing follows `nextPageToken` to the last page. Google caps a page
  at 1,000 files, and the teaching library is larger than one page (425
  decks), so a single call silently hides the rest - the scan walks every
  page before it saves the catalog.
- Decks whose parent folder name already starts with a lesson number are
  submission folders, not teaching material, and are skipped during a whole-
  Drive scan. "עותק של" is stripped from display titles and from the dedupe
  key, so Drive copies collapse to one row with an "N עותקים" badge instead
  of flooding the leftovers group.
- The first scan of a course is a baseline. Later scans compare source ids and
  `updateTime` versions, store the new ids, and the picker marks those rows
  with a **חדש** badge and a count banner.
- Selecting items to add reads the same cached snapshot, so submitting is
  instant too; only the worker downloads Docs/decks when it creates drafts.
- A failed scan keeps the previous catalog and shows the error. Scans are not
  resumable: on startup any `running` sync left by a restart is closed so the
  picker never stays stuck on "סורק עכשיו…".

### Runbook

Web: a class → **שיעור חדש** / **הוספה מהקורס** → the one-screen picker above.
The general **כיתות → ייבוא תוכן** page still offers the full course preview
(or Drive decks by folder/id) with an explicit review step before applying.
Jobs run in a single background thread with per-item commits and can be
resumed.

CLI, from the project root:

```powershell
.venv\Scripts\python -m portal.scripts.classroom_import list-courses
.venv\Scripts\python -m portal.scripts.classroom_import preview --course-id 123 --class-id 2 --out plan.json
.venv\Scripts\python -m portal.scripts.classroom_import apply --job-id 7
.venv\Scripts\python -m portal.scripts.classroom_import decks --class-id 2 --folder-id DRIVE_FOLDER --apply
.venv\Scripts\python -m portal.scripts.classroom_import reuse --class-id 3 --lesson-id 12
```

`auth` is only needed when no readable token exists; web requests never open a
browser, and refreshed tokens are written to `portal/data/google_token.json`
only. The root `token.json` stays read-only.

### Behavior notes

- Every import creates drafts (`published = false`); publishing is a teacher
  action. Assignments require a confirmed rubric.
- `[auto]`, `(auto)` and `[אוטומטי]` are stripped from display titles, and a
  leading "עותק של" is stripped too. Items with the same title, due date,
  attachments and description collapse; a canonical copy is kept. Same title
  with different content is preserved. The Drive deck and document picker
  groups identically titled files into one row and shows the copy count, so a
  library with 12 copies of a lesson is one choice.
- Decks selected directly from Drive render through the PDF → WebP path even
  though they have no attachment rows; a failed render falls back to a text
  slide instead of aborting the lesson.
- A selected Google Doc imports as a lesson with `source_kind = "drive_doc"`:
  the HTML export is converted to a local text slide and the original doc is
  recorded as a local attachment. The same doc imported from another class is
  recognized by source id, and the **כבר בכיתה** badge follows it.
- Re-applying an unchanged source skips it. A changed source updates the
  assignment's generated body/attachments while preserving publish state.
- Cross-class reuse clones lesson slides or assignment content and shares the
  same content-addressed blobs; the clone is a draft, and the same source is
  not reused twice into one class.
- Student pages never contain Drive ids/links, blob paths/hashes, rubric paths
  or answer fields. Media is served only through authenticated routes that
  re-check publication, class membership and object ownership.

### Verification

- `pytest portal/tests -q` → **115 passed, 5 skipped** (includes the
  no-network ingestion tests with fake Classroom/Docs/Drive fixtures, the
  picker tests - selected items become draft lessons/assignments, a document
  becomes a lesson with the doc text, an assignment without a rubric is
  rejected before any job is created, topics/materials render in curriculum
  order, duplicates are collapsed into one row and every topic starts closed -
  the Materials-tab tests (both states requested, every page read, the
  missing-scope error names the auth command, a material renders the **חומר
  לימוד בקורס** badge and defaults to a lesson, a link material becomes a
  safe **קישורים** slide), the one-time re-consent test, and scan tests: the
  picker never calls Google, a scan fills the cache in the background, Drive
  listing walks every `nextPageToken` page, and a second scan marks only
  genuinely new materials).
- `pytest tests -q` → **530 passed, 13 failed** — the same 13 pre-existing
  LLM/network-environment failures as the recorded baseline, unrelated to the
  portal.
