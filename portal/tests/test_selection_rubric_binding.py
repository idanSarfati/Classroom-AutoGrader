"""An x.2 hand-in must always bind lesson x, even from a stale catalog.

Regression for the 4.2 import into a second class: the catalog was scanned
before ``assignments/lesson_4.json`` existed, so the fuzzy matcher cached
``lesson_2.json`` ("matching number '2' in title") for "4.2 | הגשת משימות".
The picker pre-filled that guess and the selection job trusted the submitted
value, creating an assignment with 2.2's questions. The hard curriculum rule
is now re-applied live both when the picker renders and when the selection is
bound, so no stale suggestion can win and no re-scan is needed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from portal.ingestion import catalog, importer
from portal.ingestion.classroom_source import CourseInfo
from portal.ingestion.types import CourseSnapshot, CourseworkItem, ImportManifest
from portal.models import (
    ClassGroup,
    ClassroomCatalog,
    ClassroomLink,
    ImportJob,
)
from portal.tests.helpers import create_user, csrf, login
from portal.worksheets import for_rubric

# The exact suggestion the live catalog cached on Nov 8 for both 4.2 items.
STALE = ("assignments/lesson_2.json", 0.85, "matching number '2' in title")
HAND_IN_TITLE = "4.2 | הגשת משימות"
COURSE_ID = "c-t4"
COURSE_NAME = "פייתון א' - ט4"
TEACHER_PASSWORD = "TeacherPass1!"


def _hand_in(item_id: str = "cw-42", title: str = HAND_IN_TITLE) -> CourseworkItem:
    return CourseworkItem(
        id=item_id, title=title, state="PUBLISHED", source_version="v1"
    )


def _snapshot(*items: CourseworkItem) -> CourseSnapshot:
    return CourseSnapshot(
        course_id=COURSE_ID, course_name=COURSE_NAME, coursework=items
    )


class StaleSource:
    """A source whose fuzzy matcher still guesses lesson_2 for everything."""

    def __init__(self, snapshot: CourseSnapshot):
        self.snapshot = snapshot
        self.calls: list[str] = []

    def suggest_rubric(self, title):
        self.calls.append(title)
        return STALE


@pytest.fixture
def setup(app):
    teacher_id = create_user(
        app, "mor42bind", TEACHER_PASSWORD, display_name="מורה"
    )
    with app.state.session_factory() as db:
        group = ClassGroup(name="ט'4", code="TET4RB", teacher_id=teacher_id)
        db.add(group)
        db.commit()
        return teacher_id, group.id


def _bind(app, setup, source, *, rubrics, item=None):
    teacher_id, class_id = setup
    item = item if item is not None else _hand_in()
    with app.state.session_factory() as db:
        group = db.get(ClassGroup, class_id)
        job = importer.build_selection_preview(
            db,
            source,
            teacher_id=teacher_id,
            group=group,
            snapshot=source.snapshot,
            coursework_ids=[item.id],
            actions={f"coursework:{item.id}": "assignment"},
            rubrics=rubrics,
        )
        manifest = ImportManifest.from_json(json.loads(job.manifest_json))
        assert len(manifest.items) == 1
        return manifest.items[0]


# ------------------------------------------------- selection bind (the bug)


def test_stale_lesson_2_guess_cannot_bind_a_4_2_hand_in(app, setup):
    source = StaleSource(_snapshot(_hand_in()))
    # The picker submits the exact stale pre-fill the live catalog produced.
    entry = _bind(
        app,
        setup,
        source,
        rubrics={"coursework:cw-42": "assignments/lesson_2.json"},
    )
    assert entry.suggested_action == "assignment"
    assert entry.suggested_rubric == "assignments/lesson_4.json"
    assert entry.rubric_score == 1.0
    assert entry.rubric_reason == "lesson number match"
    # The hard rule needs no fuzzy call at all.
    assert source.calls == []
    # And the bound rubric really renders the 4.2 exercises, not 2.2's.
    worksheet = for_rubric(entry.suggested_rubric)
    assert worksheet is not None and worksheet.slug == "lesson_4"
    assert len(worksheet.items) == 4


def test_hard_rule_fills_an_empty_rubric_field(app, setup):
    source = StaleSource(_snapshot(_hand_in()))
    entry = _bind(app, setup, source, rubrics={})
    assert entry.suggested_rubric == "assignments/lesson_4.json"
    assert source.calls == []


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("1.2 | הגשת משימות", "assignments/lesson_1.json"),
        ("2.2 | הגשת משימות", "assignments/lesson_2.json"),
        ("3.2 | הגשת משימות", "assignments/lesson_3.json"),
        ("4.2 | הגשת משימות", "assignments/lesson_4.json"),
        (" 4.2 assignment", "assignments/lesson_4.json"),
    ],
)
def test_rule_covers_every_existing_lesson(app, setup, title, expected):
    item = _hand_in(title=title)
    source = StaleSource(_snapshot(item))
    entry = _bind(
        app,
        setup,
        source,
        rubrics={f"coursework:{item.id}": "assignments/lesson_2.json"},
        item=item,
    )
    assert entry.suggested_rubric == expected


def test_non_hand_in_titles_keep_the_teachers_choice(app, setup):
    item = _hand_in(title="3.3 | דף עבודה")
    source = StaleSource(_snapshot(item))
    entry = _bind(
        app,
        setup,
        source,
        rubrics={f"coursework:{item.id}": "assignments/lesson_3_worksheet.json"},
        item=item,
    )
    assert entry.suggested_rubric == "assignments/lesson_3_worksheet.json"
    assert source.calls == []


def test_unknown_lesson_falls_back_to_the_fuzzy_suggestion(app, setup):
    # No lesson_9.json exists: the hard rule stays silent and the old
    # fuzzy path still proposes something the teacher can edit.
    item = _hand_in(title="9.2 | הגשת משימות")
    source = StaleSource(_snapshot(item))
    entry = _bind(app, setup, source, rubrics={}, item=item)
    assert source.calls == [item.title]
    assert entry.suggested_rubric == STALE[0]


# ------------------------------------------------- picker render + POST


def _seed_picker(app, teacher_id, class_id, *, suggestions):
    snapshot = _snapshot(_hand_in())
    payload = catalog.serialize(
        [CourseInfo(id=COURSE_ID, name=COURSE_NAME)],
        {COURSE_ID: snapshot},
        suggestions,
        [],
        [],
    )
    with app.state.session_factory() as db:
        db.add(
            ClassroomLink(
                teacher_id=teacher_id,
                class_id=class_id,
                course_id=COURSE_ID,
                course_name=COURSE_NAME,
            )
        )
        row = ClassroomCatalog(teacher_id=teacher_id)
        row.status = "ready"
        row.error = ""
        row.catalog_json = payload
        row.new_ids_json = "[]"
        # The live scan date that froze the wrong suggestion.
        row.synced_at = datetime(2026, 11, 8, 15, 48)
        db.add(row)
        db.commit()


class _ExplodingSource:
    def suggest_rubric(self, title):
        raise AssertionError("the picker must not need Google or fuzzy guesses")


def _rubric_field_value(page_text: str, item_id: str) -> str:
    match = re.search(
        rf'name="rubric_coursework_{item_id}"[^>]*value="([^"]*)"', page_text
    )
    assert match is not None, "rubric input missing from the picker"
    return match.group(1)


def test_picker_prefills_the_hard_rule_over_the_cached_guess(app, setup, client):
    teacher_id, class_id = setup
    _seed_picker(
        app, teacher_id, class_id, suggestions={COURSE_ID: {"cw-42": STALE}}
    )
    app.state.classroom_source = _ExplodingSource()
    login(client, "mor42bind", TEACHER_PASSWORD)
    page = client.get(f"/teacher/classes/{class_id}/new")
    assert page.status_code == 200
    assert _rubric_field_value(page.text, "cw-42") == "assignments/lesson_4.json"
    assert "הצעה: lesson number match" in page.text


def test_submitting_the_stale_prefill_still_binds_lesson_4(app, setup, client):
    teacher_id, class_id = setup
    _seed_picker(
        app, teacher_id, class_id, suggestions={COURSE_ID: {"cw-42": STALE}}
    )
    app.state.classroom_source = _ExplodingSource()

    class _RecordingWorker:
        def __init__(self):
            self.job_ids: list[int] = []

        def enqueue(self, job_id: int) -> None:
            self.job_ids.append(job_id)

    worker = _RecordingWorker()
    app.state.import_worker = worker

    login(client, "mor42bind", TEACHER_PASSWORD)
    token = csrf(client, f"/teacher/classes/{class_id}/new")
    body = [
        ("csrf_token", token),
        ("course_id", COURSE_ID),
        ("pick", "coursework:cw-42"),
        ("target_coursework_cw-42", "assignment"),
        # The stale value a browser would submit from an old cached page.
        ("rubric_coursework_cw-42", "assignments/lesson_2.json"),
    ]
    response = client.post(
        f"/teacher/classes/{class_id}/new",
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
        assert job is not None and job.kind == "selection"
        manifest = ImportManifest.from_json(json.loads(job.manifest_json))
        assert len(manifest.items) == 1
        entry = manifest.items[0]
        assert entry.suggested_action == "assignment"
        assert entry.suggested_rubric == "assignments/lesson_4.json"