"""Unit tests for the assignment-material listing used to build configs.

Run from the project root::

    python -m pytest tests/test_coursework_materials.py -q

Pure unit tests: no network, no credentials, no API key. They pin what the
teacher relies on when choosing which Drive file holds an assignment's
instructions: the two-level ``materials`` nesting is flattened correctly, a
Drive file is recognised from its id *or* its link, non-Drive attachments are
not mistaken for files, an unfamiliar attachment type is skipped rather than
printed as an empty row, and an unreadable file still leaves a usable id and
link behind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.list_coursework_materials as lister  # noqa: E402
from src.classroom_service import ClassroomServiceError  # noqa: E402
from src.models import Course  # noqa: E402

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
def drive_group(title: str = "הוראות", **file_fields: Any) -> dict[str, Any]:
    """A materials group holding a single Drive file."""
    drive_file: dict[str, Any] = {"title": "Lesson deck"}
    drive_file.update(file_fields)
    return {"title": title, "materials": [{"driveFile": drive_file}]}
SLIDES_ID = "ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210"


# --------------------------------------------------------------------------- #
# Material flattening
# --------------------------------------------------------------------------- #


def test_a_drive_file_is_reported_with_its_id_and_link():
    """The two things the teacher needs to open the instructions later."""
    materials = lister.describe_materials(
        {
            "materials": [
                drive_group(
                    id=DOC_ID,
                    alternateLink=f"https://drive.google.com/file/d/{DOC_ID}/view",
                )
            ]
        }
    )

    assert len(materials) == 1
    material = materials[0]
    assert material.kind == lister.MATERIAL_KIND_DRIVE
    assert material.file_id == DOC_ID
    assert DOC_ID in material.url


def test_the_drive_id_is_recovered_from_the_link_when_it_is_absent():
    """Classroom omits ``driveFile.id`` on some attachments - only the link."""
    materials = lister.describe_materials(
        {
            "materials": [
                drive_group(alternateLink=f"https://drive.google.com/open?id={DOC_ID}")
            ]
        }
    )

    assert materials[0].file_id == DOC_ID


def test_a_drive_file_with_neither_id_nor_link_is_still_listed():
    """Unidentifiable, but not lost - the kind and title survive."""
    materials = lister.describe_materials({"materials": [drive_group()]})

    assert len(materials) == 1
    assert materials[0].kind == lister.MATERIAL_KIND_DRIVE
    assert materials[0].file_id is None
    assert materials[0].url is None


def test_the_group_title_is_the_fallback_name_of_the_file():
    """Classroom often leaves ``driveFile.title`` empty."""
    materials = lister.describe_materials(
        {
            "materials": [
                {"title": "דף עבודה", "materials": [{"driveFile": {"id": DOC_ID}}]}
            ]
        }
    )

    assert materials[0].title == "דף עבודה"


def test_a_link_is_not_reported_as_a_drive_file():
    """A web link has no Drive id and must not be sent to the Drive API."""
    materials = lister.describe_materials(
        {
            "materials": [
                {
                    "title": "מאגר תרגילים",
                    "materials": [
                        {"link": {"url": "https://example.com/x", "title": "אתר"}}
                    ],
                }
            ]
        }
    )

    assert materials[0].kind == lister.MATERIAL_KIND_LINK
    assert materials[0].file_id is None
    assert materials[0].url == "https://example.com/x"


def test_video_and_form_attachments_keep_their_urls():
    materials = lister.describe_materials(
        {
            "materials": [
                {
                    "title": "סרטון הסבר",
                    "materials": [{"youtubeVideo": {"id": "abc123"}}],
                },
                {
                    "title": "טופס",
                    "materials": [
                        {"form": {"formUrl": "https://docs.google.com/forms/d/f1"}}
                    ],
                },
            ]
        }
    )

    assert [m.kind for m in materials] == [
        lister.MATERIAL_KIND_YOUTUBE,
        lister.MATERIAL_KIND_FORM,
    ]
    assert materials[0].url.endswith("v=abc123")
    assert materials[1].url == "https://docs.google.com/forms/d/f1"


def test_an_unknown_material_type_is_skipped_not_printed_empty(caplog):
    """A new Classroom material type must not become a mystery empty row."""
    with caplog.at_level("WARNING"):
        materials = lister.describe_materials(
            {"materials": [{"title": "חומר עתידי", "materials": [{"quiz": {}}]}]}
        )

    assert materials == []
    assert "Unsupported material type" in caplog.text


def test_an_assignment_without_materials_is_not_an_error():
    assert lister.describe_materials({}) == []
    assert lister.describe_materials({"materials": []}) == []
# --------------------------------------------------------------------------- #
# Assignment view
# --------------------------------------------------------------------------- #


def test_assignment_fields_are_flattened_for_display():
    info = lister.describe_assignment(
        {
            "id": "cw-1",
            "title": "שיעור 3",
            "state": "PUBLISHED",
            "workType": "ASSIGNMENT",
            "topicId": "t-9",
            "maxPoints": 100,
            "dueDate": {"year": 2026, "month": 3, "day": 1},
            "dueTime": {"hours": 14, "minutes": 30},
            "description": "<p>Read the <b>slides</b> first.</p>",
            "associatedWithDeveloper": True,
            "materials": [drive_group(id=DOC_ID)],
        },
        {"t-9": "יחידה 3"},
    )

    assert info.id == "cw-1"
    assert info.title == "שיעור 3"
    assert info.topic == "יחידה 3"
    assert info.max_points == 100
    assert info.due == "2026-03-01 14:30"
    assert info.developer_owned is True
    # HTML is stripped so the one-line preview stays readable.
    assert "<b>" not in info.description
    assert "Read the slides first." in info.description
    assert info.materials[0].file_id == DOC_ID


def test_a_long_description_is_truncated_with_an_ellipsis():
    info = lister.describe_assignment({"description": "x" * 900})

    assert len(info.description) <= lister.DESCRIPTION_PREVIEW_CHARS + 3
    assert info.description.endswith("...")


def test_a_minimal_payload_still_produces_a_usable_row():
    """No title / due date / materials must not raise while listing a course."""
    info = lister.describe_assignment({"id": "cw-2"})

    assert info.title == "Untitled assignment"
    assert info.due == ""
    assert info.materials == []
    assert info.max_points is None


def test_mime_types_are_labelled_so_the_file_kind_is_readable():
    assert lister.describe_mime("application/vnd.google-apps.document") == "Google Doc"
# --------------------------------------------------------------------------- #
# Drive metadata enrichment
# --------------------------------------------------------------------------- #


class _FakeDriveLookup:
    """Stands in for ``ClassroomService.get_drive_file_info``."""

    def __init__(self, info: dict[str, dict[str, Any]]) -> None:
        self.info = info
        self.calls: list[str] = []

    def get_drive_file_info(self, file_id: str) -> dict[str, Any] | None:
        self.calls.append(file_id)
        return self.info.get(file_id)


def _assignment_with(*file_ids: str) -> lister.AssignmentInfo:
    return lister.describe_assignment(
        {
            "id": "cw",
            "title": "A",
            "materials": [drive_group(id=file_id) for file_id in file_ids],
        }
    )


def test_drive_metadata_marks_the_attachment_kind_and_name():
    """Distinguishing a Slides deck from a Doc is the point of the lookup."""
    service = _FakeDriveLookup(
        {
            SLIDES_ID: {
                "id": SLIDES_ID,
                "name": "שיעור 3 - מצגת",
                "mimeType": "application/vnd.google-apps.presentation",
                "webViewLink": (
                    f"https://docs.google.com/presentation/d/{SLIDES_ID}/edit"
                ),
            }
        }
    )
    assignment = _assignment_with(SLIDES_ID)

    lister.enrich_with_drive_metadata(service, [assignment])

    material = assignment.materials[0]
    assert material.mime_type == "application/vnd.google-apps.presentation"
    assert material.drive_name == "שיעור 3 - מצגת"
    assert material.readable is True
    assert "presentation/d" in material.web_view_link


def test_each_file_is_looked_up_once_even_when_shared_between_assignments():
    """A shared instruction deck must not cost one Drive call per assignment."""
    service = _FakeDriveLookup({DOC_ID: {"id": DOC_ID, "name": "Doc"}})

    lister.enrich_with_drive_metadata(
        service, [_assignment_with(DOC_ID), _assignment_with(DOC_ID)]
    )

    assert service.calls == [DOC_ID]


def test_an_unreadable_file_keeps_its_id_and_link_instead_of_disappearing():
    """A file outside our Drive still has to be openable by hand."""
    service = _FakeDriveLookup({})
    assignment = _assignment_with(DOC_ID)

    lister.enrich_with_drive_metadata(service, [assignment])

    material = assignment.materials[0]
    assert material.readable is False
    assert material.file_id == DOC_ID
    assert material.mime_type is None
    assert material.url  # still clickable


def test_non_drive_materials_are_never_looked_up():
    """A web link has no Drive id; sending it to the API would just 404."""
    service = _FakeDriveLookup({})
    assignment = lister.describe_assignment(
        {
            "id": "cw",
            "materials": [
                {
                    "title": "קישור",
                    "materials": [{"link": {"url": "https://x.test"}}],
                }
],
        }
    )

    lister.enrich_with_drive_metadata(service, [assignment])

    assert service.calls == []


def test_enrichment_tolerates_a_service_that_cannot_read_drive():
    """Enrichment is decoration; the service reports an unreadable file as
    ``None`` and the listing completes with the id and link intact."""

    def _explode(_file_id: str):
        raise AssertionError("should not be reached")

    service = SimpleNamespace(get_drive_file_info=_explode)
    assignment = lister.AssignmentInfo(
        id="cw", title="A", state="PUBLISHED", work_type="ASSIGNMENT"
    )

    lister.enrich_with_drive_metadata(service, [assignment])
# --------------------------------------------------------------------------- #
# Course fetch + CLI plumbing
# --------------------------------------------------------------------------- #


class _FakeService:
    """Minimal ClassroomService stand-in for ``list_course_assignments``."""

    def __init__(self, items, topics=None, topics_error=None):
        self._items = items
        self._topics = topics or {}
        self._topics_error = topics_error
        self.states_seen: list[tuple[str, ...]] = []

    def list_course_work_items(self, course_id, states=("PUBLISHED", "DRAFT")):
        self.states_seen.append(tuple(states))
        return list(self._items)

    def list_topics(self, course_id):
        if self._topics_error:
            raise self._topics_error
        return dict(self._topics)


COURSE = Course(id="c1", name="פייתון א' - ט4", section="ט4")


def test_only_published_assignments_are_fetched_by_default():
    service = _FakeService([{"id": "cw", "title": "A"}])

    assignments = lister.list_course_assignments(service, COURSE, drive_lookup=False)

    assert service.states_seen == [("PUBLISHED",)]
    assert [a.id for a in assignments] == ["cw"]


def test_drafts_are_included_only_when_asked_for():
    service = _FakeService([])

    lister.list_course_assignments(
        service, COURSE, include_drafts=True, drive_lookup=False
    )

    assert service.states_seen == [("PUBLISHED", "DRAFT")]


def test_a_course_whose_topics_cannot_be_listed_still_lists_its_assignments(caplog):
    """Topic names are decoration; losing them must not hide the assignments."""
    service = _FakeService(
        [{"id": "cw", "title": "A", "topicId": "t1", "materials": []}],
        topics_error=ClassroomServiceError("HTTP 403: forbidden"),
    )

    with caplog.at_level("WARNING"):
        assignments = lister.list_course_assignments(
            service, COURSE, drive_lookup=False
        )

    assert [a.id for a in assignments] == ["cw"]
    # The raw topic id is kept so the row still says where the work lives.
    assert assignments[0].topic == "t1"


def test_the_json_dump_is_written_with_every_assignment(tmp_path):
    """The dump is the input to the next step, so it must be complete."""
    course = Course(id="c1", name="Course", section="ט4")
    listing = {
        "c1": [
            lister.describe_assignment(
                {"id": "cw-1", "title": "A", "materials": [drive_group(id=DOC_ID)]}
            )
        ]
    }
    target = tmp_path / "nested" / "dump.json"

    lister._dump_json(target, [course], listing)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assignments = payload["courses"][0]["assignments"]
    assert assignments[0]["materials"][0]["file_id"] == DOC_ID


def test_a_course_can_be_selected_by_exact_id():
    course = Course(id="c1", name="A", section="1")
    other = Course(id="c2", name="B", section="2")

    assert lister._resolve_course([course, other], "c2") is other


def test_a_course_can_be_selected_by_a_partial_name():
    course = Course(id="c1", name="פייתון א' - ט4", section="ט4")

    assert lister._resolve_course([course], "ט4") is course


def test_an_unmatched_course_query_returns_nothing(capsys):
    assert lister._resolve_course([Course(id="c1", name="A")], "zzz") is None
    assert "No active course matches" in capsys.readouterr().err


def test_the_cli_defaults_to_published_without_drive_lookups():
    args = lister._parse_args([])

    assert args.all_courses is False
    assert args.include_drafts is False
    assert args.no_drive_lookup is False
    assert args.json is None


@pytest.mark.parametrize(
    ("flag", "attribute"),
    [
        ("--all-courses", "all_courses"),
        ("--include-drafts", "include_drafts"),
        ("--no-drive-lookup", "no_drive_lookup"),
    ],
)
def test_every_flag_turns_on(flag, attribute):
    assert getattr(lister._parse_args([flag]), attribute) is True


def test_the_json_flag_keeps_its_path():
    args = lister._parse_args(["--json", "out/dump.json"])

    assert args.json == Path("out/dump.json")