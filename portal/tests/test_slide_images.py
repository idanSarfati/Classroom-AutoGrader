"""Teacher-uploaded slide pictures: validation, storage, serving and removal.

Uploads are re-encoded to WebP (metadata stripped, dimensions capped) and land
in the same content store imported decks use, so the existing student and
teacher image routes serve them without a second code path.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from portal.ingestion.images import (
    MAX_EDGE,
    MAX_UPLOAD_BYTES,
    ImageUploadError,
    prepare_slide_image,
)
from portal.ingestion.storage import ContentStore
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    ContentBlob,
    LessonSlide,
    SlideContent,
)
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"


@pytest.fixture(autouse=True)
def store(app, tmp_path):
    """Keep every test blob in a temporary directory, never portal/content."""
    value = ContentStore(tmp_path / "content")
    app.state.content_store = value
    return value


def _db(app):
    return app.state.session_factory()


def _png(size=(64, 48), colour=(200, 30, 90), mode="RGB") -> bytes:
    buffer = BytesIO()
    Image.new(mode, size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_metadata() -> bytes:
    image = Image.new("RGB", (32, 32), (10, 120, 200))
    exif = Image.Exif()
    exif[270] = "מצלמה של המורה — מיקום בית הספר"
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _login_teacher(client, username="mori"):
    response = login(client, username, TEACHER_PASSWORD)
    assert response.status_code == 303, response.text


def _create_lesson(client, title="שיעור 4 – לולאות") -> int:
    token = csrf(client, "/teacher/lessons")
    response = client.post(
        "/teacher/lessons",
        data={
            "csrf_token": token,
            "title": title,
            "summary": "",
            "class_id": "",
            "published": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])


def _add_slide(client, lesson_id, *, image=None, fields=None):
    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    data = {"csrf_token": token}
    data.update(fields or {})
    files = {"image": image} if image is not None else None
    return client.post(
        f"/teacher/lessons/{lesson_id}/slides",
        data=data,
        files=files,
        follow_redirects=False,
    )


def _slide_id(response) -> int:
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("#slide-", 1)[1])


def _content(app, slide_id) -> SlideContent | None:
    with _db(app) as db:
        return db.get(SlideContent, slide_id)


# ------------------------------------------------------------- re-encoding


def test_upload_is_reencoded_to_webp_without_metadata():
    prepared = prepare_slide_image(_jpeg_with_metadata())
    assert prepared.mime_type == "image/webp"
    assert prepared.width == 32 and prepared.height == 32
    with Image.open(BytesIO(prepared.webp)) as image:
        assert image.format == "WEBP"
        assert not dict(image.getexif())
        assert "exif" not in image.info


def test_large_picture_is_downscaled_for_phones():
    prepared = prepare_slide_image(_png(size=(4000, 3000)))
    assert max(prepared.width, prepared.height) == MAX_EDGE
    assert (prepared.width, prepared.height) == (1600, 1200)


def test_transparency_survives_reencoding():
    prepared = prepare_slide_image(_png(size=(20, 20), colour=(1, 2, 3, 0), mode="RGBA"))
    with Image.open(BytesIO(prepared.webp)) as image:
        assert image.format == "WEBP"
        assert image.mode in {"RGBA", "RGB"}


def test_empty_oversize_and_non_picture_uploads_are_rejected():
    for data in (
        b"",
        b"x" * (MAX_UPLOAD_BYTES + 1),
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"%PDF-1.4 not a picture",
        b"\x89PNG\r\n\x1a\ntruncated",
    ):
        try:
            prepare_slide_image(data)
        except ImageUploadError as exc:
            assert str(exc)
        else:  # pragma: no cover - the point of the test
            raise AssertionError(f"accepted {data[:12]!r}")


# ------------------------------------------------------------------- web


def test_teacher_uploads_picture_and_student_sees_it(app, client, new_client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)

    response = _add_slide(
        client,
        lesson_id,
        image=("צילום מסך.png", _png(), "image/png"),
        fields={"title": "תרשים זרימה", "alt_text": "תרשים של לולאת while"},
    )
    slide_id = _slide_id(response)

    content = _content(app, slide_id)
    assert content is not None
    assert content.image_blob_sha and len(content.image_blob_sha) == 64
    assert (content.width, content.height) == (64, 48)
    assert content.alt_text == "תרשים של לולאת while"

    served = client.get(f"/teacher/lessons/{lesson_id}/slides/{slide_id}/image")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/webp")

    create_user(app, "noa", STUDENT_PASSWORD, role=ROLE_STUDENT)
    login(new_client, "noa", STUDENT_PASSWORD)
    page = new_client.get(f"/student/lessons/{lesson_id}?slide=0")
    assert page.status_code == 200
    assert f"/student/lessons/{lesson_id}/slides/0/image" in page.text
    assert "תרשים של לולאת while" in page.text
    picture = new_client.get(f"/student/lessons/{lesson_id}/slides/0/image")
    assert picture.status_code == 200
    assert picture.headers["content-type"].startswith("image/webp")


def test_picture_only_slide_needs_no_title_or_text(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)

    slide_id = _slide_id(
        _add_slide(client, lesson_id, image=("board.png", _png(), "image/png"))
    )
    with _db(app) as db:
        slide = db.get(LessonSlide, slide_id)
        assert slide.title == "" and slide.body == "" and slide.code == ""
    assert _content(app, slide_id).image_blob_sha


def test_plain_form_without_a_file_still_creates_a_slide(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)

    response = _add_slide(
        client, lesson_id, fields={"title": "הסבר", "body": "לולאה חוזרת על קוד."}
    )
    slide_id = _slide_id(response)
    assert _content(app, slide_id) is None
    page = client.get(f"/teacher/lessons/{lesson_id}")
    assert "לולאה חוזרת על קוד." in page.text


def test_replacing_a_picture_drops_the_unreferenced_blob(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)
    slide_id = _slide_id(
        _add_slide(
            client,
            lesson_id,
            image=("first.png", _png(colour=(255, 0, 0)), "image/png"),
            fields={"title": "שקופית"},
        )
    )
    first = _content(app, slide_id).image_blob_sha

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    response = client.post(
        f"/teacher/lessons/{lesson_id}/slides/{slide_id}/update",
        data={"csrf_token": token, "title": "שקופית", "alt_text": "תיאור חדש"},
        files={"image": ("second.png", _png(colour=(0, 0, 255)), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    second = _content(app, slide_id).image_blob_sha
    assert second != first
    assert _content(app, slide_id).alt_text == "תיאור חדש"

    store = client.app.state.content_store
    with _db(app) as db:
        assert db.get(ContentBlob, first) is None
        assert db.get(ContentBlob, second) is not None
        assert not store.has_sha(db, first)


def test_removing_a_picture_keeps_the_slide_and_its_text(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)
    slide_id = _slide_id(
        _add_slide(
            client,
            lesson_id,
            image=("board.png", _png(), "image/png"),
            fields={"title": "כותרת נשמרת", "body": "גוף נשמר"},
        )
    )
    sha = _content(app, slide_id).image_blob_sha

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    response = client.post(
        f"/teacher/lessons/{lesson_id}/slides/{slide_id}/image/remove",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    content = _content(app, slide_id)
    assert content.image_blob_sha is None
    assert content.width == 0 and content.height == 0
    with _db(app) as db:
        slide = db.get(LessonSlide, slide_id)
        assert slide.title == "כותרת נשמרת"
        assert slide.body == "גוף נשמר"
        assert db.get(ContentBlob, sha) is None
    assert client.get(
        f"/teacher/lessons/{lesson_id}/slides/{slide_id}/image"
    ).status_code == 404


def test_alt_text_can_be_edited_without_uploading_again(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)
    slide_id = _slide_id(
        _add_slide(
            client,
            lesson_id,
            image=("board.png", _png(), "image/png"),
            fields={"title": "שקופית", "alt_text": "תיאור ישן"},
        )
    )
    sha = _content(app, slide_id).image_blob_sha

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    response = client.post(
        f"/teacher/lessons/{lesson_id}/slides/{slide_id}/update",
        data={"csrf_token": token, "title": "שקופית", "alt_text": "תיאור חדש"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    content = _content(app, slide_id)
    assert content.alt_text == "תיאור חדש"
    assert content.image_blob_sha == sha


def test_bad_upload_is_rejected_and_no_slide_is_created(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)

    response = _add_slide(
        client,
        lesson_id,
        image=("notes.txt", b"just text, not a picture", "text/plain"),
        fields={"title": "לא נשמר"},
    )
    assert response.status_code == 303
    with _db(app) as db:
        assert db.scalars(
            select(LessonSlide).where(LessonSlide.lesson_id == lesson_id)
        ).all() == []
    page = client.get(f"/teacher/lessons/{lesson_id}")
    assert "רק תמונות" in page.text or "אינו תמונה" in page.text


def test_deleting_a_slide_or_lesson_drops_its_pictures(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)
    store = client.app.state.content_store

    slide_id = _slide_id(
        _add_slide(
            client, lesson_id, image=("board.png", _png(), "image/png"),
            fields={"title": "שקופית"},
        )
    )
    sha = _content(app, slide_id).image_blob_sha
    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    assert client.post(
        f"/teacher/lessons/{lesson_id}/slides/{slide_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    ).status_code == 303
    with _db(app) as db:
        assert db.get(ContentBlob, sha) is None
        assert not store.has_sha(db, sha)

    other_id = _slide_id(
        _add_slide(
            client, lesson_id, image=("board2.png", _png(colour=(0, 200, 0)), "image/png"),
            fields={"title": "שקופית אחרת"},
        )
    )
    other_sha = _content(app, other_id).image_blob_sha
    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    assert client.post(
        f"/teacher/lessons/{lesson_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    ).status_code == 303
    with _db(app) as db:
        assert db.get(ContentBlob, other_sha) is None
        assert not store.has_sha(db, other_sha)


def test_a_picture_shared_by_two_slides_survives_one_removal(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)
    picture = _png(colour=(9, 9, 9))
    first = _slide_id(
        _add_slide(client, lesson_id, image=("same.png", picture, "image/png"),
                   fields={"title": "ראשונה"})
    )
    second = _slide_id(
        _add_slide(client, lesson_id, image=("same.png", picture, "image/png"),
                   fields={"title": "שנייה"})
    )
    sha = _content(app, first).image_blob_sha
    assert _content(app, second).image_blob_sha == sha

    token = csrf(client, f"/teacher/lessons/{lesson_id}")
    assert client.post(
        f"/teacher/lessons/{lesson_id}/slides/{first}/image/remove",
        data={"csrf_token": token},
        follow_redirects=False,
    ).status_code == 303
    store = client.app.state.content_store
    with _db(app) as db:
        assert db.get(ContentBlob, sha) is not None
        assert store.has_sha(db, sha)
    assert client.get(
        f"/teacher/lessons/{lesson_id}/slides/{second}/image"
    ).status_code == 200


def test_oversize_upload_is_refused_without_creating_a_slide(app, client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)

    response = _add_slide(
        client,
        lesson_id,
        image=("huge.png", b"x" * (MAX_UPLOAD_BYTES + 1), "image/png"),
        fields={"title": "לא נשמר"},
    )
    assert response.status_code == 303
    with _db(app) as db:
        assert db.scalars(
            select(LessonSlide).where(LessonSlide.lesson_id == lesson_id)
        ).all() == []
        assert db.scalars(select(ContentBlob)).all() == []
    page = client.get(f"/teacher/lessons/{lesson_id}")
    assert "8MB" in page.text


def test_upload_guards(app, client, new_client):
    create_user(app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER)
    _login_teacher(client)
    lesson_id = _create_lesson(client)

    missing_csrf = client.post(
        f"/teacher/lessons/{lesson_id}/slides",
        data={"title": "בלי אסימון"},
        files={"image": ("x.png", _png(), "image/png")},
        follow_redirects=False,
    )
    assert missing_csrf.status_code == 400

    create_user(app, "noa", STUDENT_PASSWORD, role=ROLE_STUDENT)
    login(new_client, "noa", STUDENT_PASSWORD)
    token = csrf(new_client, "/student")
    as_student = new_client.post(
        f"/teacher/lessons/{lesson_id}/slides",
        data={"csrf_token": token, "title": "לא שלי"},
        files={"image": ("x.png", _png(), "image/png")},
        follow_redirects=False,
    )
    assert as_student.status_code == 403

    create_user(app, "mori2", TEACHER_PASSWORD, role=ROLE_TEACHER)
    other = TestClient(app)
    try:
        _login_teacher(other, "mori2")
        token = csrf(other, "/teacher/lessons")
        response = other.post(
            f"/teacher/lessons/{lesson_id}/slides",
            data={"csrf_token": token, "title": "לא שלי"},
            files={"image": ("x.png", _png(), "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 404
    finally:
        other.close()

    with _db(app) as db:
        assert db.scalars(
            select(LessonSlide).where(LessonSlide.lesson_id == lesson_id)
        ).all() == []