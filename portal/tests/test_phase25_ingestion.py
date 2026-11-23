"""Phase 2.5 ingestion tests: no network, no Google account.

A fake source replaces the Classroom/Drive boundary, so these tests exercise
the real converter, dedupe, blob storage, importer, reuse and web routes.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from io import BytesIO
from urllib.parse import urlencode

import pytest
from PIL import Image
from sqlalchemy import func, select

from portal.auth.security import hash_password
from portal.ingestion import catalog, converter, importer
from portal.ingestion.deck import render_pdf_pages
from portal.ingestion.normalize import (
    clean_title,
    dedupe_coursework,
    dedupe_drive_items,
    material_key,
    natural_key,
    strip_auto_tags,
    topic_sort_key,
)
from portal.ingestion.classroom_source import CourseInfo
from portal.ingestion.storage import ContentStore, ContentStoreError
from portal.ingestion.worker import ImportWorker
from portal.ingestion.types import (
    Attachment,
    CourseSnapshot,
    CourseworkItem,
    DeckPayload,
    DocPayload,
    DriveItem,
    ImportManifest,
)
from portal.models import (
    Assignment,
    AssignmentAttachment,
    AssignmentContent,
    ClassGroup,
    ContentBlob,
    Enrollment,
    ImportJob,
    Lesson,
    LessonSource,
    ROLE_STUDENT,
    ROLE_TEACHER,
    SlideContent,
    User,
)
from portal.tests.helpers import create_user, csrf, login


# --------------------------------------------------------------------- fakes


def _png(colour: str = "navy") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (40, 24), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (320, 180), "white").save(buffer, format="PDF")
    return buffer.getvalue()


DOC_HTML = """
<html><head><style>p{color:red}</style><script>alert(1)</script></head>
<body>
  <h1>הוראות המטלה</h1>
  <p>כתבו פונקציה.</p>
  <span style="font-weight:700">חשוב מאוד</span>
  <img src="https://lh3.googleusercontent.com/secret-image" alt="תרשים">
  <table><tr><td>א</td><td>ב</td></tr></table>
  <a href="https://drive.google.com/file/d/DRIVE_ID_123/view">קישור לכלי עזר</a>
  <script>alert(2)</script>
</body></html>
"""


class FakeSource:
    def __init__(self, snapshot: CourseSnapshot, *, rubrics=None, docs=None, decks=None):
        self.snapshot = snapshot
        self.rubrics = rubrics or {}
        self.docs = docs or {}
        self.decks = decks or {}

    def load_course(self, course_id, *, include_drafts=False):
        return self.snapshot

    def suggest_rubric(self, title):
        return self.rubrics.get(title, ("", 0.0, ""))

    def doc_html(self, file_id):
        return self.docs.get(file_id, DocPayload(html="", text=""))

    def deck_pdf(self, file_id):
        return self.decks.get(file_id, DeckPayload(pdf=_pdf()))

    def download(self, file_id):
        return b"%PDF-1.4 fake"

    def fetch_image(self, url):
        assert "googleusercontent" in url
        return _png()

    def list_courses(self):
        return [
            CourseInfo(
                id=self.snapshot.course_id,
                name=self.snapshot.course_name,
                section=self.snapshot.section,
            )
        ]

    def deck_info(self, file_id):
        return DriveItem(file_id=file_id, title="מצגת", mime_type=(
            "application/vnd.google-apps.presentation"
        ))

    def list_drive_decks(self, folder_id=""):
        return [
            DriveItem(
                file_id=file_id,
                title=f"מצגת {file_id}",
                mime_type="application/vnd.google-apps.presentation",
            )
            for file_id in self.decks
        ]


class ExplodingSource:
    """Fails the test if the picker touches Google on a normal page view."""

    def __getattr__(self, name):
        raise AssertionError(f"picker must not call Google: {name}")


def _prime_catalog(app, source, teacher_id):
    with app.state.session_factory() as db:
        return catalog.refresh(db, source, teacher_id)


def _snapshot(version: str = "v1", body: str = "") -> CourseSnapshot:
    """A course snapshot; empty ``body`` means the attached Doc is the source."""
    due = datetime(2026, 3, 1, 12, 0)
    description = f"<p>{body}</p>" if body else ""
    attachment = Attachment(
        file_id="doc-1",
        title="דף הוראות",
        mime_type="application/vnd.google-apps.document",
        url="https://docs.google.com/document/d/DRIVE_ID_123",
    )
    assignment = CourseworkItem(
        id="cw-1",
        title="מטלה 1 [auto]",
        description_html=description,
        due_at=due,
        source_version=version,
        attachments=(attachment,),
    )
    duplicate = CourseworkItem(
        id="cw-1-dup",
        title="מטלה 1 [auto] [auto]",
        description_html=description,
        due_at=due,
        source_version=version,
        attachments=(attachment,),
    )
    deck = CourseworkItem(
        id="cw-deck",
        title="שיעור מצגת",
        state="PUBLISHED",
        source_version="deck-v1",
        attachments=(
            Attachment(
                file_id="deck-1",
                title="מצגת",
                mime_type="application/vnd.google-apps.presentation",
            ),
        ),
    )
    reading = CourseworkItem(
        id="cw-read",
        title="דף קריאה",
        state="PUBLISHED",
        source_version="read-v1",
        attachments=(
            Attachment(
                file_id="doc-2",
                title="דף קריאה",
                mime_type="application/vnd.google-apps.document",
            ),
        ),
    )
    return CourseSnapshot(
        course_id="course-1",
        course_name="קורס בדיקה",
        coursework=(assignment, duplicate, deck, reading),
    )


# ----------------------------------------------------------------- utilities


@pytest.fixture
def store(app, tmp_path):
    value = ContentStore(tmp_path / "content")
    app.state.content_store = value
    return value


def _setup_classes(app):
    teacher_id = create_user(
        app, "mor", "TeacherPass1", role=ROLE_TEACHER, display_name="מורה"
    )
    student_id = create_user(
        app, "talmid", "StudentPass1", role=ROLE_STUDENT, display_name="תלמיד"
    )
    with app.state.session_factory() as db:
        first = ClassGroup(name="כיתה א", code="AAAAAA", teacher_id=teacher_id)
        second = ClassGroup(name="כיתה ב", code="BBBBBB", teacher_id=teacher_id)
        db.add_all([first, second])
        db.flush()
        db.add(Enrollment(class_id=first.id, student_id=student_id))
        db.commit()
        return teacher_id, student_id, first.id, second.id


def _source(version: str = "v1", body: str = "") -> FakeSource:
    return FakeSource(
        _snapshot(version, body),
        rubrics={"מטלה 1 [auto]": ("assignments/lesson_1.json", 0.92, "exact id")},
        docs={
            "doc-1": DocPayload(html=DOC_HTML, text="הוראות"),
            "doc-2": DocPayload(html="<h2>קריאה</h2><p>טקסט</p>", text="קריאה טקסט"),
        },
        decks={"deck-1": DeckPayload(pdf=_pdf())},
    )


# --------------------------------------------------------------- unit tests


def test_auto_tags_are_removed_from_display_titles():
    assert strip_auto_tags("מטלה 3 [auto] [auto]") == "מטלה 3"
    assert strip_auto_tags("Lesson (Auto)") == "Lesson"
    assert clean_title("  [auto]  ") == "ללא כותרת"


def test_drive_copy_prefix_is_removed_from_display_titles():
    assert clean_title("עותק של 4.5 | סיכום") == "4.5 | סיכום"
    assert clean_title("עותק של עותק של 4.5 | סיכום") == "4.5 | סיכום"


def test_template_marker_does_not_hide_a_numbered_lesson():
    assert (
        strip_auto_tags("[Template] 11.3 | פייתון א | מסעדת מדבורגר - חלק ב")
        == "11.3 | פייתון א | מסעדת מדבורגר - חלק ב"
    )
    assert (
        clean_title("עותק של [Template] [Template] 11.3 | מסעדת מדבורגר")
        == "11.3 | מסעדת מדבורגר"
    )


def test_link_slide_keeps_external_links_but_escapes_them():
    from portal.ingestion.types import PreviewItem

    item = PreviewItem(
        source_kind="coursework",
        source_id="material:m-1",
        title="תרגול",
        attachments=[
            {
                "kind": "link",
                "url": "https://h5p.org/node/1?a=1&b=2",
                "title": "<b>תרגול</b>",
            },
            {"kind": "link", "url": "javascript:alert(1)", "title": "רע"},
        ],
    )

    body_html, text = importer._links_slide(item)

    assert 'href="https://h5p.org/node/1?a=1&amp;b=2"' in body_html
    assert "&lt;b&gt;תרגול&lt;/b&gt;" in body_html
    assert "javascript:" not in body_html
    assert text == "<b>תרגול</b>"


def test_materials_sort_by_lesson_number_not_by_string():
    titles = ["2.10 תרגול", "2.2 משתנים", "2.1 קלט", "תרגול למבחן יחידות 1-2"]
    assert sorted(titles, key=natural_key) == [
        "2.1 קלט",
        "2.2 משתנים",
        "2.10 תרגול",
        "תרגול למבחן יחידות 1-2",
    ]


def test_topics_sort_by_their_leading_number():
    topics = ["סביבת עבודה", "2 | משתנים", "20 | פרויקט", "1 | מבוא לאלגוריתמיקה והדפסה", ""]
    assert sorted(topics, key=topic_sort_key) == [
        "1 | מבוא לאלגוריתמיקה והדפסה",
        "2 | משתנים",
        "20 | פרויקט",
        "סביבת עבודה",
        "",
    ]


def test_dedupe_keeps_different_content_with_the_same_title():
    base = dict(
        id="a",
        title="מטלה [auto]",
        source_version="1",
    )
    keep, info = dedupe_coursework(
        [
            CourseworkItem(description_html="<p>גרסה ארוכה</p>", **base),
            CourseworkItem(id="b", title="מטלה", description_html="<p>אחרת</p>", source_version="2"),
            CourseworkItem(id="c", title="מטלה [auto]", description_html="<p>גרסה ארוכה</p>", source_version="1"),
        ]
    )
    assert len(keep) == 2
    assert info["c"].duplicate_of == "a"
    assert info["a"].duplicate_count == 1


def test_dedupe_groups_identically_titled_drive_decks():
    items = [
        DriveItem(file_id="a", title="שיעור 1", mime_type="x"),
        DriveItem(file_id="b", title="שיעור 1", mime_type="x"),
        DriveItem(file_id="c", title="שיעור 1 [auto]", mime_type="x"),
        DriveItem(file_id="d", title="שיעור 2", mime_type="x"),
    ]
    keep, info = dedupe_drive_items(items)
    assert len(keep) == 2
    first = next(deck for deck in keep if deck.title.startswith("שיעור 1"))
    assert info[first.file_id].duplicate_count == 2
    for file_id in ("a", "b", "c"):
        if file_id != first.file_id:
            assert info[file_id].duplicate_of == first.file_id
    assert info["d"].duplicate_count == 0


def test_material_key_ignores_course_marker_and_drive_tags():
    # The same worksheet is attached to coursework without the course marker
    # and found in Drive with it; the picker must treat both as one material.
    assert material_key("3.3 | פייתון א | תרגול לפיצוח - דף עבודה") == material_key(
        "3.3 | תרגול לפיצוח - דף עבודה"
    )
    assert material_key("3.2 | פייתון א' | הגשת משימות") == material_key(
        "[Template] 3.2 | הגשת משימות [Auto]"
    )
    assert material_key("3.1 | מצגת השיעור") != material_key("3.2 | הגשת משימות")


def test_converter_removes_scripts_styles_and_external_urls():
    converted = converter.convert_doc_html(
        DOC_HTML,
        resolve_image=lambda _src, _alt: "/student/assignments/9/media/1",
    )
    html = converted.html
    assert "<strong>חשוב מאוד</strong>" in html
    assert "<table>" in html
    assert "script" not in html
    assert "style" not in html
    assert "googleusercontent" not in html
    assert "drive.google.com" not in html
    assert 'src="/student/assignments/9/media/1"' in html
    assert "קישור לכלי עזר" in html  # link text kept, href dropped
    assert converted.images_kept == 1


def test_converter_replaces_an_unreachable_image_with_a_placeholder():
    converted = converter.convert_doc_html(DOC_HTML, resolve_image=lambda _s, _a: None)
    assert "googleusercontent" not in converted.html
    assert "תרשים" in converted.html
    assert converted.images_missing == 1


def test_tiny_pdf_renders_to_webp_pages():
    pages = render_pdf_pages(_pdf())
    assert len(pages) == 1
    assert pages[0].webp[:4] == b"RIFF"
    image = Image.open(BytesIO(pages[0].webp))
    assert image.format == "WEBP"
    assert pages[0].width > 0 and pages[0].height > 0


def test_content_store_rejects_traversal_and_empty_files(tmp_path):
    value = ContentStore(tmp_path)
    stored = value.save(_png(), "image/png")
    assert value.path_for(stored.rel_path).is_file()
    with pytest.raises(ContentStoreError):
        value.path_for("../outside.txt")
    with pytest.raises(ContentStoreError):
        value.save(b"", "image/png")


def test_auth_loader_never_opens_a_browser(monkeypatch, tmp_path):
    from portal.ingestion import google_auth

    monkeypatch.setattr(
        google_auth, "_candidate_tokens", lambda: [tmp_path / "missing.json"]
    )
    with pytest.raises(google_auth.IngestionAuthError):
        google_auth.load_credentials(interactive=False, required_scopes=())


def test_auth_cli_reconsents_when_the_token_lacks_a_scope(monkeypatch, tmp_path):
    """Adding the Materials scope must be grantable, not a dead end."""
    from portal.ingestion import google_auth

    token = tmp_path / "google_token.json"
    token.write_text(
        json.dumps(
            {
                "token": "access",
                "refresh_token": "refresh",
                "client_id": "client",
                "client_secret": "secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": ["https://www.googleapis.com/auth/drive"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(google_auth, "_candidate_tokens", lambda: [token])
    seen: list[tuple[str, ...]] = []

    def _consent(required):
        seen.append(tuple(required))
        return "fresh-credentials"

    monkeypatch.setattr(google_auth, "_run_consent", _consent)

    with pytest.raises(google_auth.IngestionAuthError) as caught:
        google_auth.load_credentials(interactive=False)
    assert "classroom_import auth" in str(caught.value)
    assert seen == []

    assert google_auth.load_credentials(interactive=True) == "fresh-credentials"
    assert google_auth.MATERIALS_SCOPE in seen[0]


def test_auth_cli_reconsents_when_the_refresh_token_is_dead(monkeypatch, tmp_path):
    """An expired/revoked grant must be renewable, not a dead end.

    Regression: the refresh-failure branch raised even in interactive mode, so
    ``classroom_import auth`` - the very command the error told the teacher to
    run - could never replace a dead Testing-mode token.
    """
    from google.auth.exceptions import RefreshError

    from portal.ingestion import google_auth

    token = tmp_path / "google_token.json"
    token.write_text(
        json.dumps(
            {
                "token": "access",
                "refresh_token": "refresh",
                "client_id": "client",
                "client_secret": "secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "expiry": "2020-01-01T00:00:00Z",
                "scopes": list(google_auth._required_scopes()),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(google_auth, "_candidate_tokens", lambda: [token])

    def _dead_refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(google_auth.Credentials, "refresh", _dead_refresh)
    seen: list[tuple[str, ...]] = []

    def _consent(required):
        seen.append(tuple(required))
        return "fresh-credentials"

    monkeypatch.setattr(google_auth, "_run_consent", _consent)

    with pytest.raises(google_auth.IngestionAuthError) as caught:
        google_auth.load_credentials(interactive=False)
    assert "classroom_import auth" in str(caught.value)
    assert seen == []

    assert google_auth.load_credentials(interactive=True) == "fresh-credentials"
    assert len(seen) == 1


# ------------------------------------------------------------ integration


def test_preview_apply_reuse_and_student_visibility(app, store):
    teacher_id, student_id, first_id, second_id = _setup_classes(app)
    source = _source()

    with app.state.session_factory() as db:
        group = db.get(ClassGroup, first_id)
        job = importer.build_course_preview(
            db,
            source,
            teacher_id=teacher_id,
            group=group,
            course_id="course-1",
            include_drafts=True,
        )
        manifest = ImportManifest.from_json(json.loads(job.manifest_json))
        by_id = {item.source_id: item for item in manifest.items}

        assert by_id["cw-1"].suggested_action == "assignment"
        assert by_id["cw-1"].suggested_rubric == "assignments/lesson_1.json"
        assert by_id["cw-1"].title == "מטלה 1"
        assert by_id["cw-1-dup"].duplicate_of == "cw-1"
        assert by_id["cw-deck"].suggested_action == "lesson"
        assert by_id["cw-read"].suggested_action == "lesson"

        counts = importer.apply_job(db, store, source, job)
        assert counts["imported"] == 3
        assert counts["skipped"] == 1

        assignment = db.scalar(
            select(Assignment).where(Assignment.class_id == first_id)
        )
        assert assignment is not None and assignment.published is False
        content = db.get(AssignmentContent, assignment.id)
        assert content is not None
        assert "<strong>חשוב מאוד</strong>" in content.body_html
        assert "googleusercontent" not in content.body_html
        assert "drive.google.com" not in content.body_html
        assert "DRIVE_ID_123" not in content.body_html
        attachment = db.scalar(
            select(AssignmentAttachment).where(
                AssignmentAttachment.assignment_id == assignment.id
            )
        )
        assert attachment is not None and attachment.blob_sha
        assert store.path_for_sha(db, attachment.blob_sha)[0].is_file()

        deck_lesson = db.scalar(
            select(Lesson).where(Lesson.title == "שיעור מצגת")
        )
        assert deck_lesson is not None and deck_lesson.published is False
        slide = db.scalar(
            select(SlideContent).where(SlideContent.slide_id.in_(
                select(importer.LessonSlide.id).where(
                    importer.LessonSlide.lesson_id == deck_lesson.id
                )
            ))
        )
        assert slide is not None and slide.image_blob_sha
        assert store.path_for_sha(db, slide.image_blob_sha)[0].is_file()
        deck_lesson_id = deck_lesson.id
        assignment_id = assignment.id
        attachment_id = attachment.id

        # Re-apply: idempotent, no second copy of anything.
        job.state = "queued"
        again = importer.apply_job(db, store, source, job)
        assert again["imported"] == 0 and again["updated"] == 0
        assert again["skipped"] == 4
        assert db.scalar(
            select(func.count(Assignment.id)).where(
                Assignment.class_id == first_id
            )
        ) == 1

        # Cross-class reuse shares the same blob bytes.
        blob_count = db.scalar(select(func.count(ContentBlob.sha256)))
        group_two = db.get(ClassGroup, second_id)
        clone = importer.reuse_lesson(db, deck_lesson, group_two)
        db.commit()
        assert clone.class_id == second_id
        assert clone.published is False
        assert db.scalar(select(func.count(ContentBlob.sha256))) == blob_count
        clone_content = db.scalar(
            select(SlideContent)
            .join(importer.LessonSlide, importer.LessonSlide.id == SlideContent.slide_id)
            .where(importer.LessonSlide.lesson_id == clone.id)
        )
        assert clone_content.image_blob_sha == slide.image_blob_sha
        with pytest.raises(importer.ReuseError):
            importer.reuse_lesson(db, deck_lesson, group_two)
        db.rollback()

    _assert_student_flow(app, student_id, deck_lesson_id, assignment_id, attachment_id)


def _assert_student_flow(app, student_id, lesson_id, assignment_id, attachment_id):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "talmid", "StudentPass1")

        # Drafts are invisible.
        assert client.get(f"/student/lessons/{lesson_id}").status_code == 404
        assert client.get(f"/student/assignments/{assignment_id}").status_code == 404
        assert (
            client.get(f"/student/lessons/{lesson_id}/slides/0/image").status_code
            == 404
        )

        with app.state.session_factory() as db:
            db.get(Lesson, lesson_id).published = True
            db.get(Assignment, assignment_id).published = True
            db.commit()

        page = client.get(f"/student/lessons/{lesson_id}")
        assert page.status_code == 200
        assert f"/student/lessons/{lesson_id}/slides/0/image" in page.text
        assert "googleusercontent" not in page.text

        image = client.get(f"/student/lessons/{lesson_id}/slides/0/image")
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/webp")
        assert image.headers.get("etag")

        assignment = client.get(f"/student/assignments/{assignment_id}")
        assert assignment.status_code == 200
        assert "<strong>חשוב מאוד</strong>" in assignment.text
        for forbidden in ("googleusercontent", "drive.google.com", "DRIVE_ID_123"):
            assert forbidden not in assignment.text
        assert f"/student/assignments/{assignment_id}/media/{attachment_id}" in assignment.text

        media = client.get(
            f"/student/assignments/{assignment_id}/media/{attachment_id}"
        )
        assert media.status_code == 200
        assert media.headers["content-type"].startswith("image/png")

        # An enrolled-class check protects the media route too.
        outsider_id = create_user(
            app, "outsider", "OutsiderPass1", role=ROLE_STUDENT
        )
        assert outsider_id
        with app.state.session_factory() as db:
            db.get(User, student_id).is_active = True
        client.post("/logout", data={"csrf_token": csrf(client, "/student")})
        login(client, "outsider", "OutsiderPass1")
        assert (
            client.get(f"/student/assignments/{assignment_id}").status_code == 404
        )
        assert (
            client.get(
                f"/student/assignments/{assignment_id}/media/{attachment_id}"
            ).status_code
            == 404
        )
        assert (
            client.get(f"/student/lessons/{lesson_id}/slides/0/image").status_code
            == 404
        )
    finally:
        client.close()


def test_changed_source_updates_an_assignment(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    source = _source()

    with app.state.session_factory() as db:
        group = db.get(ClassGroup, first_id)
        job = importer.build_course_preview(
            db, source, teacher_id=teacher_id, group=group, course_id="course-1"
        )
        importer.apply_job(db, store, source, job)

        changed = _source(version="v2", body="הוראות חדשות")
        job_two = importer.build_course_preview(
            db, changed, teacher_id=teacher_id, group=group, course_id="course-1"
        )
        manifest = ImportManifest.from_json(json.loads(job_two.manifest_json))
        first = next(item for item in manifest.items if item.source_id == "cw-1")
        assert first.already_imported is True
        first.suggested_action = "assignment"
        job_two.manifest_json = json.dumps(manifest.as_json(), ensure_ascii=False)
        db.commit()

        counts = importer.apply_job(db, store, changed, job_two)
        assert counts["updated"] == 1
        assignment = db.scalar(
            select(Assignment).where(Assignment.class_id == first_id)
        )
        content = db.get(AssignmentContent, assignment.id)
        assert "הוראות חדשות" in content.body_html
        assert assignment.published is False


def test_teacher_import_job_page_and_reuse_form(app, store):
    teacher_id, _student_id, first_id, second_id = _setup_classes(app)
    source = _source()
    with app.state.session_factory() as db:
        group = db.get(ClassGroup, first_id)
        job = importer.build_course_preview(
            db, source, teacher_id=teacher_id, group=group, course_id="course-1"
        )
        importer.apply_job(db, store, source, job)
        job_id = job.id
        lesson = db.scalar(select(Lesson).where(Lesson.title == "שיעור מצגת"))
        lesson_id = lesson.id

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/import/jobs/{job_id}")
        assert page.status_code == 200
        assert "מטלה 1" in page.text
        assert "כפילות שזוהתה" in page.text
        assert "googleusercontent" not in page.text

        class_page = client.get(f"/teacher/classes/{second_id}")
        assert class_page.status_code == 200
        # The cross-class reuse card was removed from the class screen, so a
        # lesson that lives in another class is no longer listed here. The
        # reuse POST route below still works for callers that use it directly.
        assert "שימוש חוזר מכיתה אחרת" not in class_page.text
        assert "שיעור מצגת" not in class_page.text

        token = csrf(client, f"/teacher/classes/{second_id}")
        response = client.post(
            f"/teacher/classes/{second_id}/reuse/lesson",
            data={"csrf_token": token, "lesson_id": lesson_id},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with app.state.session_factory() as db:
            clones = db.scalars(
                select(Lesson).where(
                    Lesson.class_id == second_id,
                    Lesson.title == "שיעור מצגת",
                )
            ).all()
            assert len(clones) == 1 and clones[0].published is False
            provenance = db.get(LessonSource, clones[0].id)
            assert provenance.reused_from_lesson_id == lesson_id
    finally:
        client.close()


def test_deck_preview_from_drive(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    source = _source()
    with app.state.session_factory() as db:
        group = db.get(ClassGroup, first_id)
        job = importer.build_deck_preview(
            db,
            source,
            teacher_id=teacher_id,
            group=group,
            file_ids=["deck-9"],
        )
        manifest = ImportManifest.from_json(json.loads(job.manifest_json))
        assert len(manifest.items) == 1
        assert manifest.items[0].source_kind == "drive_deck"
        assert manifest.items[0].suggested_action == "lesson"
        counts = importer.apply_job(db, store, source, job)
        assert counts["imported"] == 1
        lesson = db.scalar(
            select(Lesson).where(Lesson.class_id == first_id)
        )
        assert lesson is not None and lesson.published is False
        provenance = db.get(LessonSource, lesson.id)
        assert provenance is not None and provenance.render_mode == "images"
        slide = db.scalar(
            select(SlideContent)
            .join(importer.LessonSlide, importer.LessonSlide.id == SlideContent.slide_id)
            .where(importer.LessonSlide.lesson_id == lesson.id)
        )
        assert slide is not None and slide.image_blob_sha
        assert store.path_for_sha(db, slide.image_blob_sha)[0].is_file()


# ------------------------------------------- classroom picker (one screen)


class _RecordingWorker:
    def __init__(self):
        self.job_ids: list[int] = []

    def enqueue(self, job_id: int) -> None:
        self.job_ids.append(job_id)


def test_class_picker_adds_selected_items_as_lesson_or_assignment(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    source = _source()
    _prime_catalog(app, source, teacher_id)
    # From here on, opening or submitting the picker must use the local cache.
    app.state.classroom_source = ExplodingSource()
    worker = _RecordingWorker()
    app.state.import_worker = worker

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        assert "הוספת שיעור או מטלה" in page.text
        assert "מטלה 1" in page.text
        assert "שיעור מצגת" in page.text
        assert "מצגת deck-1" in page.text
        assert "שיעור / חומר לימוד" in page.text
        assert "מטלה עם מחוון" in page.text
        assert "googleusercontent" not in page.text
        # Duplicates are grouped away instead of shown as dead rows, and the
        # Drive decks share the same selection list (and filter) as the course.
        assert 'value="coursework:cw-1-dup"' not in page.text
        assert "כפילות — לא ניתן לבחור" not in page.text
        assert 'id="materials"' in page.text
        assert 'id="decks"' not in page.text
        assert '<details class="picker-group" data-picker-group open' not in page.text
        assert page.text.index('id="materials"') < page.text.index("מצגת deck-1")

        token = csrf(client, f"/teacher/classes/{first_id}/new")
        body = [
            ("csrf_token", token),
            ("course_id", "course-1"),
            ("pick", "coursework:cw-1"),
            ("target_coursework_cw-1", "assignment"),
            ("rubric_coursework_cw-1", "assignments/lesson_1.json"),
            ("pick", "coursework:cw-read"),
            ("target_coursework_cw-read", "lesson"),
            ("pick", "deck:deck-1"),
        ]
        response = client.post(
            f"/teacher/classes/{first_id}/new",
            content=urlencode(body),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/teacher/import/jobs/")
        job_id = int(location.rsplit("/", 1)[-1])
        assert worker.job_ids == [job_id]

        with app.state.session_factory() as db:
            job = db.get(ImportJob, job_id)
            assert job is not None
            assert job.kind == "selection" and job.state == "queued"
            manifest = ImportManifest.from_json(json.loads(job.manifest_json))
            actions = {
                item.source_id: item.suggested_action
                for item in manifest.items
            }
            assert actions == {
                "cw-1": "assignment",
                "cw-read": "lesson",
                "deck-1": "lesson",
            }

            counts = importer.apply_job(db, store, source, job)
            assert counts["imported"] == 3

            assignments = db.scalars(
                select(Assignment).where(Assignment.class_id == first_id)
            ).all()
            assert len(assignments) == 1 and assignments[0].published is False
            assert assignments[0].rubric_path.endswith("lesson_1.json")

            lessons = db.scalars(
                select(Lesson).where(Lesson.class_id == first_id)
            ).all()
            assert len(lessons) == 2
            assert all(lesson.published is False for lesson in lessons)
            deck_lesson = next(
                lesson for lesson in lessons if lesson.title == "מצגת deck-1"
            )
            provenance = db.get(LessonSource, deck_lesson.id)
            assert provenance is not None and provenance.render_mode == "images"

        after = client.get(f"/teacher/classes/{first_id}/new")
        assert after.status_code == 200
        assert "כבר בכיתה" in after.text
        # Existing assignments pre-fill the rubric as a project-relative name
        # so it matches one of the picker's options instead of a Windows path.
        assert 'value="assignments/lesson_1.json"' in after.text
        assert "C:\\" not in after.text.split('id="materials"', 1)[1]
    finally:
        client.close()


def test_class_picker_rejects_an_assignment_without_a_rubric(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    source = _source()
    _prime_catalog(app, source, teacher_id)
    app.state.classroom_source = ExplodingSource()
    app.state.import_worker = _RecordingWorker()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        token = csrf(client, f"/teacher/classes/{first_id}/new")
        body = [
            ("csrf_token", token),
            ("course_id", "course-1"),
            ("pick", "coursework:cw-1"),
            ("target_coursework_cw-1", "assignment"),
            ("rubric_coursework_cw-1", ""),
        ]
        response = client.post(
            f"/teacher/classes/{first_id}/new",
            content=urlencode(body),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "לא נבחר קובץ מחוון" in response.text
        with app.state.session_factory() as db:
            assert db.scalar(select(func.count(ImportJob.id))) == 0
    finally:
        client.close()


def test_picker_orders_materials_like_a_syllabus(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)

    def item(cid, title, topic, **extra):
        values = dict(
            id=cid, title=title, state="PUBLISHED", source_version="v1",
            topic_name=topic,
        )
        values.update(extra)
        return CourseworkItem(**values)

    snapshot = CourseSnapshot(
        course_id="course-1",
        course_name="קורס בדיקה",
        coursework=(
            item("cw-22", "2.2 משתנים", "2 | משתנים"),
            item("cw-210", "2.10 תרגול", "2 | משתנים"),
            item("cw-21", "2.1 קלט", "2 | משתנים"),
            # A distinct Classroom draft stays selectable, sorted after the
            # published copy and labelled so the repeated title is not noise.
            item(
                "cw-21d", "2.1 קלט", "2 | משתנים",
                state="DRAFT", due_at=datetime(2026, 3, 2, 12, 0),
            ),
            item("cw-prac", "תרגול למבחן יחידות 1-2", "2 | משתנים"),
            item("cw-12", "1.2 הדפסה", "1 | מבוא לאלגוריתמיקה והדפסה"),
            item("cw-11", "1.1 אלגוריתם", "1 | מבוא לאלגוריתמיקה והדפסה"),
            item("cw-32", "3.2 | הגשת משימות", "3 | פקודת קלט"),
            item("cw-03", "0.3 | משימת התנסות", "היכרות"),
            item("cw-04", "0.4 | צילום מסך", "היכרות"),
        ),
    )

    class NumberedDecks(FakeSource):
        """Drive decks carry their lesson number and must join that topic."""

        def list_drive_decks(self, folder_id=""):
            mime = "application/vnd.google-apps.presentation"
            return [
                DriveItem(file_id="deck-2", title="2.1 | מצגת משתנים", mime_type=mime),
                DriveItem(file_id="deck-0", title="0.1 | מצגת היכרות", mime_type=mime),
                DriveItem(file_id="deck-9", title="9.1 | מצגת בלי נושא", mime_type=mime),
            ]

    _prime_catalog(app, NumberedDecks(snapshot), teacher_id)
    app.state.classroom_source = ExplodingSource()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        expected = [
            "1 | מבוא לאלגוריתמיקה והדפסה",
            "1.1 אלגוריתם",
            "1.2 הדפסה",
            "2 | משתנים",
            "2.1 קלט",
            "2.2 משתנים",
            "2.10 תרגול",
            "תרגול למבחן יחידות 1-2",
        ]
        positions = [page.text.index(text) for text in expected]
        assert positions == sorted(positions)
        # Drive decks join the topic that matches the number in their name;
        # 0.x decks join the unnumbered "היכרות" topic (its rows are 0.3/0.4),
        # and a deck whose number matches nothing gets its own group.
        assert page.text.count('<details class="picker-group" data-picker-group>') == 5
        assert '<details class="picker-group" data-picker-group open' not in page.text
        assert page.text.index("2 | משתנים") < page.text.index('value="deck:deck-2"')
        assert page.text.index('value="deck:deck-2"') < page.text.index("3 | פקודת קלט")
        assert page.text.index("היכרות") < page.text.index('value="deck:deck-0"')
        assert page.text.index('value="deck:deck-0"') < page.text.index("חומרים ללא נושא")
        assert page.text.index("חומרים ללא נושא") < page.text.index('value="deck:deck-9"')
        # The Classroom DRAFT copy of "2.1 קלט" mirrors the published one and
        # is not offered as a second choice; the published id wins.
        assert page.text.count("2.1 קלט") == 1
        assert 'value="coursework:cw-21"' in page.text
        assert 'value="coursework:cw-21d"' not in page.text
        assert "טיוטה בקורס" not in page.text
    finally:
        client.close()


def test_picker_shows_one_row_per_material_when_coursework_and_drive_overlap(
    app, store
):
    """A worksheet attached to coursework is not offered again as a Drive doc.

    Classroom lists the same content once as coursework and once as the Drive
    copy found in the class folder. The coursework row already carries the
    attachment, the rubric and the type selector, so the Drive duplicate is
    hidden. A Drive document with no coursework counterpart stays.
    """
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    doc_mime = "application/vnd.google-apps.document"
    deck_mime = "application/vnd.google-apps.presentation"

    worksheet = CourseworkItem(
        id="cw-33",
        title="3.3 | תרגול לפיצוח - דף עבודה [Auto]",
        state="DRAFT",
        topic_name="3 | פקודת קלט",
        source_version="v1",
        attachments=(
            Attachment(
                file_id="doc-33",
                title="[Template] 3.3 | פייתון א | תרגול לפיצוח - דף עבודה",
                mime_type=doc_mime,
            ),
        ),
    )
    deck_material = CourseworkItem(
        id="material:31",
        title="3.1 | פייתון א | פקודת קלט | מצגת השיעור",
        state="PUBLISHED",
        work_type="COURSE_MATERIAL",
        topic_name="3 | פקודת קלט",
        source_version="v1",
        attachments=(
            Attachment(
                file_id="deck-31",
                title="3.1 | פייתון א | פקודת קלט | מצגת השיעור",
                mime_type=deck_mime,
            ),
        ),
    )
    snapshot = CourseSnapshot(
        course_id="course-1",
        course_name="קורס בדיקה",
        coursework=(worksheet, deck_material),
    )

    class OverlappingDrive(FakeSource):
        def list_drive_decks(self, folder_id=""):
            return [
                DriveItem(
                    file_id="deck-31-copy",
                    title="3.1 | פייתון א | פקודת קלט | מצגת השיעור",
                    mime_type=deck_mime,
                )
            ]

        def list_drive_docs(self, folder_ids):
            return [
                DriveItem(
                    file_id="doc-33-copy",
                    title="3.3 | פייתון א | תרגול לפיצוח - דף עבודה",
                    mime_type=doc_mime,
                ),
                # No coursework counterpart for this document: it stays.
                DriveItem(
                    file_id="doc-35",
                    title="3.5 | סיכום בקטנה",
                    mime_type=doc_mime,
                ),
            ]

    _prime_catalog(app, OverlappingDrive(snapshot), teacher_id)
    app.state.classroom_source = ExplodingSource()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        assert 'value="coursework:cw-33"' in page.text
        assert 'value="coursework:material:31"' in page.text
        assert 'value="doc:doc-33-copy"' not in page.text
        assert 'value="deck:deck-31-copy"' not in page.text
        assert 'value="doc:doc-35"' in page.text
    finally:
        client.close()


def test_python_b_decks_are_hidden_from_the_picker(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)

    class MixedDecks(FakeSource):
        def list_drive_decks(self, folder_id=""):
            mime = "application/vnd.google-apps.presentation"
            return [
                DriveItem(
                    file_id="deck-a",
                    title="1.1 | מצגת פייתון א",
                    mime_type=mime,
                ),
                # Plain title, recognised only through its parent folder.
                DriveItem(
                    file_id="deck-b",
                    title="2.1 | פעולות ותנאים | חזרה",
                    mime_type=mime,
                    folder_id="folder-b",
                    folder_name="פייתון ב' מורים ללא מייל ארגוני",
                ),
                # Title marker still works for a catalog cached before the
                # folder provenance was stored.
                DriveItem(
                    file_id="deck-c",
                    title="3.1 | פייתון ב' | ביצוע חוזר",
                    mime_type=mime,
                ),
            ]

    _prime_catalog(app, MixedDecks(_snapshot()), teacher_id)
    with app.state.session_factory() as db:
        cached = catalog.load(db, teacher_id)
        assert [deck.file_id for deck in cached.decks] == ["deck-a"]

    app.state.classroom_source = ExplodingSource()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        assert 'value="deck:deck-a"' in page.text
        assert 'value="deck:deck-b"' not in page.text
        assert 'value="deck:deck-c"' not in page.text
    finally:
        client.close()


def test_picker_badges_a_row_whose_duplicate_copy_is_in_class(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    _prime_catalog(app, _source(), teacher_id)

    # The teacher imported the id of the duplicate copy, not the canonical
    # row the picker lists.
    with app.state.session_factory() as db:
        assignment = Assignment(
            class_id=first_id,
            title="מטלה 1",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="0" * 64,
            published=False,
        )
        db.add(assignment)
        db.flush()
        db.add(
            AssignmentContent(
                assignment_id=assignment.id,
                source_kind="coursework",
                source_id="cw-1-dup",
                source_version="v1",
                original_title="מטלה 1 [auto] [auto]",
            )
        )
        db.commit()

    app.state.classroom_source = ExplodingSource()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        row = page.text.split('value="coursework:cw-1"', 1)[1].split("</li>", 1)[0]
        assert "כבר בכיתה" in row
    finally:
        client.close()


def test_picker_badges_material_imported_from_another_course_copy(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    _prime_catalog(app, _source(), teacher_id)

    # Every course has its own Classroom id for the same material; the badge
    # must survive switching between course copies.
    with app.state.session_factory() as db:
        assignment = Assignment(
            class_id=first_id,
            title="מטלה 1",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="0" * 64,
            published=False,
        )
        db.add(assignment)
        db.flush()
        db.add(
            AssignmentContent(
                assignment_id=assignment.id,
                source_kind="coursework",
                source_id="copy-from-another-course",
                source_version="v1",
                original_title="מטלה 1 [auto]",
            )
        )
        db.commit()

    app.state.classroom_source = ExplodingSource()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        row = page.text.split('value="coursework:cw-1"', 1)[1].split("</li>", 1)[0]
        assert "כבר בכיתה" in row
    finally:
        client.close()


def test_picker_does_not_badge_a_classroom_draft(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    snapshot = CourseSnapshot(
        course_id="course-1",
        course_name="קורס בדיקה",
        coursework=(
            CourseworkItem(
                id="cw-draft",
                title="3.3 | תרגול לפיצוח - דף עבודה",
                state="DRAFT",
                source_version="v1",
            ),
        ),
    )
    _prime_catalog(app, FakeSource(snapshot), teacher_id)

    # The same id was imported earlier, but in Classroom it is only a draft,
    # so the picker must not claim it is already published to the class.
    with app.state.session_factory() as db:
        assignment = Assignment(
            class_id=first_id,
            title="3.3 | תרגול לפיצוח - דף עבודה",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="0" * 64,
            published=False,
        )
        db.add(assignment)
        db.flush()
        db.add(
            AssignmentContent(
                assignment_id=assignment.id,
                source_kind="coursework",
                source_id="cw-draft",
                source_version="v1",
                original_title="3.3 | תרגול לפיצוח - דף עבודה",
            )
        )
        db.commit()

    app.state.classroom_source = ExplodingSource()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        row = page.text.split('value="coursework:cw-draft"', 1)[1].split("</li>", 1)[0]
        assert "טיוטה בקורס" in row
        assert "כבר בכיתה" not in row
    finally:
        client.close()


def test_picker_without_a_catalog_opens_without_calling_google(app, store):
    _teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    app.state.classroom_source = ExplodingSource()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        assert "טעינת החומרים עכשיו" in page.text
        assert "עדיין לא נסרקו החומרים" in page.text
    finally:
        client.close()


def test_scan_route_runs_in_background_and_fills_the_catalog(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    source = _source()
    app.state.classroom_source = source
    worker = _RecordingWorker()
    app.state.import_worker = worker

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        token = csrf(client, f"/teacher/classes/{first_id}")
        response = client.post(
            f"/teacher/classes/{first_id}/scan",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert worker.job_ids, "scan must be queued on the import worker"
        job_id = worker.job_ids[0]

        with app.state.session_factory() as db:
            job = db.get(ImportJob, job_id)
            assert job is not None and job.kind == "catalog_sync"
            assert job.state == "queued"
            pending = catalog.load(db, teacher_id)
            assert pending.is_refreshing and not pending.has_data

        # The page now says a scan is running instead of blocking on Google.
        running = client.get(f"/teacher/classes/{first_id}/new")
        assert running.status_code == 200
        assert "סורק עכשיו" in running.text
        assert "data-catalog-pending" in running.text

        real = ImportWorker(
            app.state.session_factory,
            store.root,
            source_factory=lambda: source,
        )
        counts = real.process(job_id)
        assert counts is not None and counts["coursework"] == 4

        with app.state.session_factory() as db:
            job = db.get(ImportJob, job_id)
            assert job is not None and job.state == "completed"
            cached = catalog.load(db, teacher_id)
            assert cached.status == "ready" and cached.has_data
            assert "course-1" in cached.snapshots
            assert cached.snapshots["course-1"].coursework
            assert cached.suggestions["course-1"]["cw-1"][0] == (
                "assignments/lesson_1.json"
            )
            assert "deck-1" in {deck.file_id for deck in cached.decks}

        ready = client.get(f"/teacher/classes/{first_id}/new")
        assert ready.status_code == 200
        assert "מעודכן" in ready.text
        assert "מטלה 1" in ready.text
    finally:
        client.close()


def test_a_restart_resets_a_stale_scan(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    with app.state.session_factory() as db:
        catalog.mark_refreshing(db, teacher_id)
        job = ImportJob(
            teacher_id=teacher_id,
            class_id=first_id,
            kind="catalog_sync",
            state="running",
        )
        db.add(job)
        db.commit()
        job_id = job.id

        assert catalog.reset_stale(db) == 1
        job = db.get(ImportJob, job_id)
        cached = catalog.load(db, teacher_id)
    assert job.state == "failed"
    assert not cached.is_refreshing
    assert cached.status == "failed"
    assert "נקטעה" in cached.error


def test_second_scan_marks_only_new_materials(app, store):
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    first = _source()
    _prime_catalog(app, first, teacher_id)


    extra = CourseworkItem(
        id="cw-new",
        title="חומר חדש",
        state="PUBLISHED",
        source_version="new-v1",
    )
    newer_snapshot = replace(
        first.snapshot,
        coursework=first.snapshot.coursework + (extra,),
    )
    newer = FakeSource(
        newer_snapshot,
        rubrics=first.rubrics,
        docs=first.docs,
        decks=first.decks,
    )
    with app.state.session_factory() as db:
        counts = catalog.refresh(db, newer, teacher_id)
        cached = catalog.load(db, teacher_id)
    assert counts["new"] == 1
    assert cached.new_ids == {"coursework:cw-new"}

    app.state.classroom_source = ExplodingSource()
    app.state.import_worker = _RecordingWorker()

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        assert "נמצאו 1 פריטים חדשים" in page.text
        assert "חדש" in page.text
    finally:
        client.close()


class _Page:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeDriveFiles:
    def __init__(self, pages):
        self.pages = list(pages)
        self.list_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Page(self.pages.pop(0))

    def get(self, **kwargs):
        return _Page({"name": "פייתון א' - ז3"})


class _FakeDrive:
    def __init__(self, pages):
        self._files = _FakeDriveFiles(pages)

    def files(self):
        return self._files


class _FakeGoogleService:
    def __init__(self, pages):
        self.drive = _FakeDrive(pages)

    def _execute(self, request, action):
        return request.execute()


def test_drive_deck_listing_walks_every_page():
    """The account has hundreds of decks; a lost page hides lessons."""
    from portal.ingestion.classroom_source import ClassroomSource

    slides = "application/vnd.google-apps.presentation"
    source = ClassroomSource(
        service=_FakeGoogleService(
            [
                {
                    "files": [
                        {
                            "id": "deck-1",
                            "name": "1.1 | מצגת",
                            "mimeType": slides,
                            "parents": ["folder-a"],
                        }
                    ],
                    "nextPageToken": "page-2",
                },
                {
                    "files": [
                        {
                            "id": "deck-2",
                            "name": "12.1 | מצגת",
                            "mimeType": slides,
                            "parents": ["folder-a"],
                        }
                    ]
                },
            ]
        )
    )

    decks = source.list_drive_decks("")

    assert [deck.file_id for deck in decks] == ["deck-1", "deck-2"]
    assert len(source._service.drive._files.list_calls) == 2
    assert "pageToken" not in source._service.drive._files.list_calls[0]
    assert source._service.drive._files.list_calls[1]["pageToken"] == "page-2"


def test_drive_doc_listing_accepts_template_numbered_docs():
    """Worksheets exist only as "[Template] 11.3 | ..." docs; keep them."""
    from portal.ingestion.classroom_source import ClassroomSource

    docs_mime = "application/vnd.google-apps.document"
    source = ClassroomSource(
        service=_FakeGoogleService(
            [
                {
                    "files": [
                        {
                            "id": "doc-113",
                            "name": (
                                "עותק של [Template]  11.3 | פייתון א | "
                                "מסעדת מדבורגר - חלק ב"
                            ),
                            "mimeType": docs_mime,
                            "parents": ["folder-a"],
                        },
                        {
                            "id": "doc-scratch",
                            "name": "מצגת ללא שם",
                            "mimeType": docs_mime,
                            "parents": ["folder-a"],
                        },
                    ]
                }
            ]
        )
    )

    docs = source.list_drive_docs(["folder-a"])

    assert [doc.file_id for doc in docs] == ["doc-113"]
    assert docs[0].title == "11.3 | פייתון א | מסעדת מדבורגר - חלק ב"


class _FakeMaterialsResource:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[dict] = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Page(self.pages.pop(0))


class _FakeClassroomResource:
    def __init__(self, pages):
        self._materials = _FakeMaterialsResource(pages)

    def courses(self):
        return self

    def courseWorkMaterials(self):
        return self._materials


class _MaterialsService:
    """Only the reads ``load_course`` needs, plus the Materials resource."""

    def __init__(self, pages):
        self.classroom = _FakeClassroomResource(pages)

    def list_course_work_items(self, course_id, *, states):
        return []

    def list_topics(self, course_id):
        return {}

    def list_courses(self):
        return [CourseInfo(id="course-1", name="פייתון א' - ז3", section=None)]

    def _execute(self, request, action):
        return request.execute()


def test_load_course_merges_the_materials_tab():
    """The Materials tab is a separate resource; its items are lessons."""
    from portal.ingestion.classroom_source import ClassroomSource

    source = ClassroomSource(
        service=_MaterialsService(
            [
                {
                    "courseWorkMaterial": [
                        {
                            "id": "m-1",
                            "title": "שיבוץ נקודות על מערכת הצירים",
                            "state": "PUBLISHED",
                            "materials": [
                                {
                                    "link": {
                                        "url": "https://example.com/axes",
                                        "title": "מערכת הצירים",
                                    }
                                }
                            ],
                        }
                    ],
                    "nextPageToken": "page-2",
                },
                {
                    "courseWorkMaterial": [
                        {
                            "id": "m-2",
                            "title": "מבחן תנאי פשוט, מורכב וחלוקת שלמים",
                            "state": "PUBLISHED",
                        }
                    ]
                },
            ]
        )
    )

    snapshot = source.load_course("course-1", include_drafts=True)

    assert [item.work_type for item in snapshot.coursework] == [
        "COURSE_MATERIAL",
        "COURSE_MATERIAL",
    ]
    assert [item.id for item in snapshot.coursework] == [
        "material:m-1",
        "material:m-2",
    ]
    calls = source._service.classroom._materials.calls
    assert len(calls) == 2
    assert calls[0]["courseWorkMaterialStates"] == ["PUBLISHED", "DRAFT"]
    assert "pageToken" not in calls[0]
    assert calls[1]["pageToken"] == "page-2"


def test_materials_scope_error_tells_the_teacher_to_reauthorize():
    from portal.ingestion.classroom_source import ClassroomSource, SourceError
    from grading_engine.classroom_service import ClassroomServiceError

    class _DeniedService(_MaterialsService):
        def __init__(self):
            # The request object is built first; the 403 happens on execute.
            self.classroom = _FakeClassroomResource([{}])

        def _execute(self, request, action):
            raise ClassroomServiceError(
                "Failed to list course materials (HTTP 403): "
                "Request had insufficient authentication scopes."
            )

    source = ClassroomSource(service=_DeniedService())

    with pytest.raises(SourceError) as caught:
        source.list_course_materials("course-1")

    assert "classroom_import auth" in str(caught.value)


def test_class_picker_lists_and_imports_lesson_documents(app, store):
    """Worksheets and summaries are Docs; they belong in the same picker."""
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    slides = "application/vnd.google-apps.presentation"
    docs_mime = "application/vnd.google-apps.document"

    class DocSource(FakeSource):
        def list_drive_decks(self, folder_id=""):
            return [
                DriveItem(
                    file_id="deck-4",
                    title="4.1 | מצגת פעולות",
                    mime_type=slides,
                    folder_id="folder-4",
                    folder_name="פייתון א' - ז3",
                )
            ]

        def list_drive_docs(self, folder_ids):
            assert folder_ids == ["folder-4"]
            return [
                DriveItem(
                    file_id="doc-45",
                    title="4.5 | סיכום בקטנה",
                    mime_type=docs_mime,
                    folder_id="folder-4",
                    folder_name="פייתון א' - ז3",
                )
            ]

        def doc_html(self, file_id):
            return DocPayload(html="<h1>סיכום</h1><p>תוכן המסמך</p>", text="")

    source = DocSource(_snapshot())
    _prime_catalog(app, source, teacher_id)
    app.state.classroom_source = ExplodingSource()
    worker = _RecordingWorker()
    app.state.import_worker = worker

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        assert "4.5 | סיכום בקטנה" in page.text
        assert 'value="doc:doc-45"' in page.text

        token = csrf(client, f"/teacher/classes/{first_id}/new")
        response = client.post(
            f"/teacher/classes/{first_id}/new",
            content=urlencode(
                [
                    ("csrf_token", token),
                    ("course_id", "course-1"),
                    ("pick", "doc:doc-45"),
                ]
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        job_id = int(response.headers["location"].rsplit("/", 1)[-1])
        assert worker.job_ids == [job_id]

        with app.state.session_factory() as db:
            job = db.get(ImportJob, job_id)
            manifest = ImportManifest.from_json(json.loads(job.manifest_json))
            assert [item.source_kind for item in manifest.items] == ["drive_doc"]
            counts = importer.apply_job(db, store, source, job)
            assert counts["imported"] == 1
            lesson = db.scalar(
                select(Lesson).where(Lesson.class_id == first_id)
            )
            assert lesson is not None
            assert lesson.title == "4.5 | סיכום בקטנה"
            provenance = db.get(LessonSource, lesson.id)
            assert provenance is not None and provenance.source_kind == "drive_doc"
            slide = db.scalar(
                select(SlideContent)
                .join(
                    importer.LessonSlide,
                    importer.LessonSlide.id == SlideContent.slide_id,
                )
                .where(importer.LessonSlide.lesson_id == lesson.id)
            )
            assert slide is not None
            assert "תוכן המסמך" in (slide.text_snapshot or "")

        after = client.get(f"/teacher/classes/{first_id}/new")
        assert "4.5 | סיכום בקטנה" in after.text
        assert "כבר בכיתה" in after.text
    finally:
        client.close()


def test_class_picker_offers_materials_tab_items_as_lessons(app, store):
    """The Materials tab is not coursework; its rows must still be pickable."""
    teacher_id, _student_id, first_id, _second_id = _setup_classes(app)
    material = CourseworkItem(
        id="material:m-9",
        title="שיבוץ נקודות על מערכת הצירים",
        state="PUBLISHED",
        work_type="COURSE_MATERIAL",
        source_version="m-v1",
        attachments=(
            Attachment(
                file_id="doc-9",
                title="דף שיבוץ",
                mime_type="application/vnd.google-apps.document",
            ),
            Attachment(
                file_id="",
                title="תרגול H5P",
                url="https://h5p.org/node/1",
                kind="link",
            ),
        ),
    )
    snapshot = CourseSnapshot(
        course_id="course-1",
        course_name="קורס בדיקה",
        coursework=(material,),
    )
    source = FakeSource(
        snapshot,
        docs={"doc-9": DocPayload(html="<p>שיבוץ נקודות</p>", text="")},
    )
    _prime_catalog(app, source, teacher_id)
    app.state.classroom_source = ExplodingSource()
    worker = _RecordingWorker()
    app.state.import_worker = worker

    from fastapi.testclient import TestClient

    client = TestClient(app)
    try:
        login(client, "mor", "TeacherPass1")
        page = client.get(f"/teacher/classes/{first_id}/new")
        assert page.status_code == 200
        assert "שיבוץ נקודות על מערכת הצירים" in page.text
        assert "חומר לימוד בקורס" in page.text
        assert 'value="coursework:material:m-9"' in page.text
        select_start = page.text.index('name="target_coursework_material:m-9"')
        select_block = page.text[select_start : select_start + 800]
        assert '<option value="lesson" selected>' in select_block

        token = csrf(client, f"/teacher/classes/{first_id}/new")
        response = client.post(
            f"/teacher/classes/{first_id}/new",
            content=urlencode(
                [
                    ("csrf_token", token),
                    ("course_id", "course-1"),
                    ("pick", "coursework:material:m-9"),
                ]
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        job_id = int(response.headers["location"].rsplit("/", 1)[-1])

        with app.state.session_factory() as db:
            job = db.get(ImportJob, job_id)
            manifest = ImportManifest.from_json(json.loads(job.manifest_json))
            assert [item.source_id for item in manifest.items] == [
                "material:m-9"
            ]
            assert manifest.items[0].suggested_action == "lesson"
            counts = importer.apply_job(db, store, source, job)
            assert counts["imported"] == 1
            lesson = db.scalar(
                select(Lesson).where(Lesson.class_id == first_id)
            )
            assert lesson is not None
            assert lesson.title == "שיבוץ נקודות על מערכת הצירים"
            provenance = db.get(LessonSource, lesson.id)
            assert provenance is not None
            assert provenance.source_kind == "coursework"
            bodies = [
                row.body_html
                for row in db.scalars(
                    select(SlideContent)
                    .join(
                        importer.LessonSlide,
                        importer.LessonSlide.id == SlideContent.slide_id,
                    )
                    .where(importer.LessonSlide.lesson_id == lesson.id)
                )
            ]
            assert any(
                'href="https://h5p.org/node/1"' in body for body in bodies
            )
            assert any("תרגול H5P" in body for body in bodies)
    finally:
        client.close()
