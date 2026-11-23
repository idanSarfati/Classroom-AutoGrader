"""Phase 6 tests: class expansion and content replication.

The tests never touch Google. A fake catalog (the same JSON the real scanner
stores) provides eleven courses with mixed published/draft items, and the
reference class is seeded directly in the temporary portal database.
"""
from __future__ import annotations

import html
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from portal.ingestion import catalog
from portal.ingestion.classroom_source import CourseInfo
from portal.ingestion.types import Attachment, CourseSnapshot, CourseworkItem
from portal.models import (
    Assignment,
    AssignmentAttachment,
    AssignmentContent,
    ClassGroup,
    ClassroomCatalog,
    ClassroomLink,
    Lesson,
    LessonSlide,
    LessonSource,
    SlideContent,
    User,
)
from portal.sync import (
    EXCLUDED_CLASSES,
    MODE_CLASSROOM,
    SyncError,
    apply_plan,
    build_plan,
    class_key,
    find_course_matches,
)
from portal.tests.helpers import create_user, csrf_from, login

# RTL controls are exactly what Google returns; matching must ignore them.
Z3_COURSE = "\u202bפייתון א' - ז3\u202c"
Z4_COURSE = "\u202bפייתון א' - ז4\u202c"
H3_COURSE = "\u202bפייתון א' - ח3\u202c"
H4_COURSE = "\u202bפייתון א' - ח4\u202c"
H6_COURSE = "\u202bפייתון א' - ח6\u202c"
T2_COURSE = "\u202bפייתון א' - ט2\u202c"
T3_COURSE = "\u202bפייתון א' - ט3\u202c"
T4_COURSE = "\u202bפייתון א' - ט4\u202c"
H8_COURSE = "\u202bפייתון א' - ח8\u202c"
TEST_COURSE = "\u202bפייתון א' - טסטים\u202c"
PYTHON_B_COURSE = "פייתון ב'"

COURSE_IDS = {
    "ז3": "c-z3",
    "ז4": "c-z4",
    "ח3": "c-h3",
    "ח4": "c-h4",
    "ח6": "c-h6",
    "ט2": "c-t2",
    "ט3": "c-t3",
    "ט4": "c-t4",
    "ח8": "c-h8",
    "טסטים": "c-test",
    "פייתון ב'": "c-py2",
}

LESSON_ONE = "0.1 | פייתון א | מצגת השיעור | היכרות"
LESSON_TWO = "1.1 | היכרות"
ASSIGNMENT_32 = "3.2 | הגשת משימות"
ASSIGNMENT_22 = "2.2 | הגשת משימות"
ASSIGNMENT_12 = "1.2 | הגשת משימות"
ASSIGNMENT_REVIEW = "תרגול יחידות 1-2"


# ---------------------------------------------------------------------------
# Fixture world
# ---------------------------------------------------------------------------


def _assignment(
    item_id: str, title: str, state: str, topic: str = ""
) -> CourseworkItem:
    return CourseworkItem(
        id=item_id,
        title=title,
        state=state,
        work_type="ASSIGNMENT",
        topic_name=topic,
    )


def _material(
    item_id: str,
    title: str,
    state: str,
    attachment_title: str = "",
    topic: str = "",
) -> CourseworkItem:
    attachments = (
        (
            Attachment(
                file_id=f"file-{item_id}",
                title=attachment_title,
                mime_type="application/vnd.google-apps.presentation",
            ),
        )
        if attachment_title
        else ()
    )
    return CourseworkItem(
        id=item_id,
        title=title,
        state=state,
        work_type="COURSE_MATERIAL",
        topic_name=topic,
        attachments=attachments,
    )


def _snapshot(course_id: str, name: str, items) -> CourseSnapshot:
    return CourseSnapshot(
        course_id=course_id,
        course_name=name,
        coursework=tuple(items),
    )


def _catalog_world():
    courses = [
        CourseInfo(id="c-z3", name=Z3_COURSE),
        CourseInfo(id="c-z4", name=Z4_COURSE),
        CourseInfo(id="c-h3", name=H3_COURSE),
        CourseInfo(id="c-h4", name=H4_COURSE),
        CourseInfo(id="c-h6", name=H6_COURSE),
        CourseInfo(id="c-t2", name=T2_COURSE),
        CourseInfo(id="c-t3", name=T3_COURSE),
        CourseInfo(id="c-t4", name=T4_COURSE),
        CourseInfo(id="c-h8", name=H8_COURSE),
        CourseInfo(id="c-test", name=TEST_COURSE),
        CourseInfo(id="c-py2", name=PYTHON_B_COURSE),
    ]
    snapshots = {
        "c-z3": _snapshot(
            "c-z3",
            Z3_COURSE,
            [
                _assignment("z3-a1", "3.2 | הגשת משימות [Auto]", "DRAFT"),
            ],
        ),
        # ז4 has reached 3.2 and 1.2, not 2.2 or the review worksheet. The
        # lesson deck is a draft; the 1.1 material exists but is a different
        # deck, so the reference lesson must stay a draft.
        "c-z4": _snapshot(
            "c-z4",
            Z4_COURSE,
            [
                _assignment("z4-a1", "3.2 | הגשת משימות [Auto]", "PUBLISHED"),
                _assignment("z4-a2", "2.2 | הגשת משימות [Auto]", "DRAFT"),
                _assignment("z4-a3", "1.2 | הגשת משימות", "PUBLISHED"),
                _material(
                    "z4-m1",
                    "0.1 | היכרות",
                    "DRAFT",
                    attachment_title=LESSON_ONE,
                ),
                _material(
                    "z4-m2",
                    "1.1 | למידה תכל'ס | מבוא",
                    "PUBLISHED",
                    attachment_title="1.1 מצגת השיעור _ מבוא",
                ),
            ],
        ),
        "c-h3": _snapshot(
            "c-h3",
            H3_COURSE,
            [
                _assignment("h3-a1", "3.2 | הגשת משימות", "DRAFT"),
                _assignment("h3-a2", "2.2 | הגשת משימות", "DRAFT"),
                _assignment("h3-a3", "1.2 | הגשת משימות", "DRAFT"),
                _material(
                    "h3-m1",
                    "0.1 | היכרות",
                    "DRAFT",
                    attachment_title=LESSON_ONE,
                ),
            ],
        ),
        "c-h4": _snapshot("c-h4", H4_COURSE, []),
        # ח6 is ahead: every matching item is already published.
        "c-h6": _snapshot(
            "c-h6",
            H6_COURSE,
            [
                _assignment("h6-a1", "3.2 | הגשת משימות [Auto]", "PUBLISHED"),
                _assignment("h6-a2", "2.2 | הגשת משימות [Auto]", "PUBLISHED"),
                _assignment("h6-a3", "1.2 | הגשת משימות", "PUBLISHED"),
                _assignment("h6-a4", "תרגול יחידות 1-2", "PUBLISHED"),
                _material(
                    "h6-m1",
                    "0.1 | היכרות",
                    "PUBLISHED",
                    attachment_title=LESSON_ONE,
                ),
                _material(
                    "h6-m2",
                    "1.1 | היכרות",
                    "PUBLISHED",
                    attachment_title=LESSON_TWO,
                ),
            ],
        ),
        "c-t2": _snapshot("c-t2", T2_COURSE, []),
        "c-t3": _snapshot("c-t3", T3_COURSE, []),
        "c-t4": _snapshot("c-t4", T4_COURSE, []),
        "c-h8": _snapshot("c-h8", H8_COURSE, []),
        "c-test": _snapshot("c-test", TEST_COURSE, []),
        "c-py2": _snapshot("c-py2", PYTHON_B_COURSE, []),
    }
    return courses, snapshots


def _write_catalog(app, teacher_id: int, courses, snapshots) -> None:
    payload = catalog.serialize(courses, snapshots, {}, [], [])
    with app.state.session_factory() as db:
        row = db.scalar(
            select(ClassroomCatalog).where(
                ClassroomCatalog.teacher_id == teacher_id
            )
        )
        if row is None:
            row = ClassroomCatalog(teacher_id=teacher_id)
            db.add(row)
        row.status = "ready"
        row.error = ""
        row.catalog_json = payload
        row.new_ids_json = "[]"
        row.synced_at = datetime(2026, 11, 8, 15, 48)
        db.commit()


def _seed_reference(app, teacher_id: int) -> int:
    with app.state.session_factory() as db:
        group = ClassGroup(
            name="ז'3", code="SRC123", teacher_id=teacher_id
        )
        db.add(group)
        db.flush()
        db.add(
            ClassroomLink(
                teacher_id=teacher_id,
                class_id=group.id,
                course_id="c-z3",
                course_name=Z3_COURSE,
            )
        )

        lesson_one = Lesson(
            teacher_id=teacher_id,
            class_id=group.id,
            title=LESSON_ONE,
            summary="פתיחה",
            published=False,
            position=1,
        )
        lesson_two = Lesson(
            teacher_id=teacher_id,
            class_id=group.id,
            title=LESSON_TWO,
            summary="",
            published=True,
            position=2,
        )
        db.add_all([lesson_one, lesson_two])
        db.flush()
        for lesson in (lesson_one, lesson_two):
            db.add(
                LessonSource(
                    lesson_id=lesson.id,
                    source_kind="drive_deck",
                    source_id=f"deck-{lesson.id}",
                    original_title=lesson.title,
                    content_hash="a" * 64,
                )
            )
        slide = LessonSlide(
            lesson_id=lesson_one.id,
            position=0,
            title="ברוכים הבאים",
            body="שלום",
        )
        db.add(slide)
        db.flush()
        db.add(
            SlideContent(
                slide_id=slide.id,
                body_html="<p>שלום</p>",
                text_snapshot="שלום",
            )
        )

        assignments = [
            (
                ASSIGNMENT_32,
                "3.2 | הגשת משימות [Auto]",
                "3 | פקודת קלט",
            ),
            (
                ASSIGNMENT_22,
                "2.2 | הגשת משימות [Auto]",
                "2 | משתנים",
            ),
            (
                ASSIGNMENT_12,
                "1.2 | הגשת משימות",
                "1 | מבוא",
            ),
            (ASSIGNMENT_REVIEW, ASSIGNMENT_REVIEW, "2 | משתנים"),
        ]
        for index, (title, original, topic) in enumerate(assignments, start=1):
            assignment = Assignment(
                class_id=group.id,
                title=title,
                instructions="",
                rubric_path="rubrics/test.xlsx",
                rubric_sha256="b" * 64,
                published=False,
            )
            db.add(assignment)
            db.flush()
            db.add(
                AssignmentContent(
                    assignment_id=assignment.id,
                    body_html="<p>מטלה</p>",
                    body_text="מטלה",
                    topic_name=topic,
                    source_kind="coursework",
                    source_id=f"a-{index}",
                    original_title=original,
                    content_hash="c" * 64,
                )
            )
            db.add(
                AssignmentAttachment(
                    assignment_id=assignment.id,
                    title=f"נספח {index}",
                    mime_type="application/pdf",
                    position=0,
                )
            )
        db.commit()
        return group.id


@pytest.fixture
def world(app):
    teacher_id = create_user(
        app, "teacher", "TeacherPass1!", display_name="מורה"
    )
    source_id = _seed_reference(app, teacher_id)
    courses, snapshots = _catalog_world()
    _write_catalog(app, teacher_id, courses, snapshots)
    return app, teacher_id, source_id


# ---------------------------------------------------------------------------
# Unit-level matching
# ---------------------------------------------------------------------------


def test_class_key_normalizes_geresh_and_bidi_controls():
    assert class_key("ז'3") == class_key("ז3") == "ז3"
    assert class_key("\u202bפייתון א' - ז4\u202c") != "ז4"
    assert class_key("ח'4") == "ח4"
    assert class_key("ט״2") == "ט2"


def test_course_matching_uses_whole_class_token(app):
    courses, _ = _catalog_world()
    matches = find_course_matches(courses, "ז4")
    assert [course.id for course in matches] == ["c-z4"]
    assert find_course_matches(courses, "ח8")[0].id == "c-h8"
    assert find_course_matches(courses, "טסטים")[0].id == "c-test"
    assert find_course_matches(courses, "פייתון ב'")[0].id == "c-py2"
    assert find_course_matches(courses, "יא1") == []


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_plan_maps_every_target_and_differentiates_publication(world):
    app, teacher_id, _ = world
    with app.state.session_factory() as db:
        plan = build_plan(db, teacher_id)

        assert plan.source_name == "ז'3"
        assert plan.source_lessons == 2
        assert plan.source_assignments == 4
        tokens = [item.token for item in plan.targets]
        assert tokens == ["ז4", "ח3", "ח4", "ח6", "ט2", "ט3", "ט4"]
        assert all(item.error == "" for item in plan.targets)
        assert all(item.course_id for item in plan.targets)
        assert plan.classes_to_create == 7
        assert plan.links_to_create == 7
        assert plan.items_to_create == 42

        by_token = {item.token: item for item in plan.classes}
        z4 = by_token["ז4"]
        assert z4.course_name == "פייתון א' - ז4"
        states = {item.title: item.desired_published for item in z4.items}
        assert states[ASSIGNMENT_32] is True
        assert states[ASSIGNMENT_12] is True
        assert states[ASSIGNMENT_22] is False
        assert states[ASSIGNMENT_REVIEW] is False
        # The 0.1 deck is a draft in the target course, and the 1.1 material
        # is a different deck - both lessons stay drafts.
        assert states[LESSON_ONE] is False
        assert states[LESSON_TWO] is False

        h6 = by_token["ח6"]
        assert all(item.desired_published for item in h6.items)
        h3 = by_token["ח3"]
        assert not any(item.desired_published for item in h3.items)
        # No snapshot means "unknown", never "publish everything".
        assert not any(
            item.desired_published for item in by_token["ט2"].items
        )
        assert by_token["ט2"].snapshot_available is True


def test_plan_excludes_h8_and_rejects_it_as_a_target(world):
    app, teacher_id, _ = world
    with app.state.session_factory() as db:
        plan = build_plan(db, teacher_id)
        assert "ח8" not in [item.token for item in plan.classes]
        with pytest.raises(SyncError, match="מוחרגת"):
            build_plan(db, teacher_id, targets=["ח8"])
        assert EXCLUDED_CLASSES == ("ח8",)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_apply_creates_classes_links_and_copies_content(world):
    app, teacher_id, source_id = world
    with app.state.session_factory() as db:
        plan = build_plan(db, teacher_id)
        outcome = apply_plan(db, plan)
        db.commit()

        assert outcome.classes_created == 7
        assert outcome.links_created == 7
        assert outcome.lessons_created == 14
        assert outcome.assignments_created == 28
        assert outcome.errors == []

        groups = {
            class_key(group.name): group
            for group in db.scalars(
                select(ClassGroup).where(
                    ClassGroup.teacher_id == teacher_id
                )
            ).all()
        }
        assert set(groups) == {
            "ז3", "ז4", "ח3", "ח4", "ח6", "ט2", "ט3", "ט4"
        }
        assert "ח8" not in groups
        codes = [group.code for group in groups.values()]
        assert len(set(codes)) == len(codes)

        z4 = groups["ז4"]
        assert z4.name == "ז'4"
        link = db.scalar(
            select(ClassroomLink).where(ClassroomLink.class_id == z4.id)
        )
        assert link is not None
        assert link.course_id == "c-z4"
        assert link.course_name == "פייתון א' - ז4"
        assert link.last_synced_at is not None

        z4_assignments = db.scalars(
            select(Assignment).where(Assignment.class_id == z4.id)
        ).all()
        assert {item.title for item in z4_assignments} == {
            ASSIGNMENT_32,
            ASSIGNMENT_22,
            ASSIGNMENT_12,
            ASSIGNMENT_REVIEW,
        }
        published = {
            item.title: item.published for item in z4_assignments
        }
        assert published == {
            ASSIGNMENT_32: True,
            ASSIGNMENT_22: False,
            ASSIGNMENT_12: True,
            ASSIGNMENT_REVIEW: False,
        }
        for assignment in z4_assignments:
            content = db.get(AssignmentContent, assignment.id)
            assert content.reused_from_assignment_id is not None
            attachments = db.scalars(
                select(AssignmentAttachment).where(
                    AssignmentAttachment.assignment_id == assignment.id
                )
            ).all()
            assert len(attachments) == 1

        z4_lessons = db.scalars(
            select(Lesson).where(Lesson.class_id == z4.id)
        ).all()
        assert {lesson.title for lesson in z4_lessons} == {
            LESSON_ONE,
            LESSON_TWO,
        }
        assert not any(lesson.published for lesson in z4_lessons)
        copied = next(
            lesson for lesson in z4_lessons if lesson.title == LESSON_ONE
        )
        slides = db.scalars(
            select(LessonSlide).where(LessonSlide.lesson_id == copied.id)
        ).all()
        assert len(slides) == 1
        assert db.get(SlideContent, slides[0].id) is not None

        h6 = groups["ח6"]
        assert all(
            assignment.published
            for assignment in db.scalars(
                select(Assignment).where(Assignment.class_id == h6.id)
            ).all()
        )
        assert all(
            lesson.published
            for lesson in db.scalars(
                select(Lesson).where(Lesson.class_id == h6.id)
            ).all()
        )

        # The reference class never receives its own clones.
        assert (
            len(
                db.scalars(
                    select(Assignment).where(Assignment.class_id == source_id)
                ).all()
            )
            == 4
        )
        assert (
            len(
                db.scalars(
                    select(Lesson).where(Lesson.class_id == source_id)
                ).all()
            )
            == 2
        )


def test_rerun_is_idempotent_and_updates_publication(world):
    app, teacher_id, _ = world
    with app.state.session_factory() as db:
        apply_plan(db, build_plan(db, teacher_id))
        db.commit()

        plan = build_plan(db, teacher_id)
        assert plan.items_to_create == 0
        outcome = apply_plan(db, plan)
        db.commit()
        assert outcome.classes_created == 0
        assert outcome.links_created == 0
        assert outcome.created_total == 0
        assert outcome.unchanged == 42

        # The class moves forward: 2.2 is now published, 1.2 goes back.
        courses, snapshots = _catalog_world()
        z4_snapshot = snapshots["c-z4"]
        changed = tuple(
            CourseworkItem(
                id=item.id,
                title=item.title,
                state=(
                    "PUBLISHED"
                    if item.title == "2.2 | הגשת משימות [Auto]"
                    else "DRAFT"
                    if item.title == "1.2 | הגשת משימות"
                    else item.state
                ),
                work_type=item.work_type,
                topic_name=item.topic_name,
                attachments=item.attachments,
            )
            for item in z4_snapshot.coursework
        )
        snapshots["c-z4"] = CourseSnapshot(
            course_id="c-z4",
            course_name=Z4_COURSE,
            coursework=changed,
        )
        _write_catalog(app, teacher_id, courses, snapshots)

        db.expire_all()
        plan = build_plan(db, teacher_id)
        actions = {
            item.title: item.action
            for item in next(
                item for item in plan.classes if item.token == "ז4"
            ).items
        }
        assert actions[ASSIGNMENT_22] == "publish"
        assert actions[ASSIGNMENT_12] == "unpublish"
        outcome = apply_plan(db, plan)
        db.commit()
        assert outcome.created_total == 0

        z4 = db.scalar(
            select(ClassGroup).where(
                ClassGroup.teacher_id == teacher_id,
                ClassGroup.name == "ז'4",
            )
        )
        published = {
            item.title: item.published
            for item in db.scalars(
                select(Assignment).where(Assignment.class_id == z4.id)
            ).all()
        }
        assert published[ASSIGNMENT_22] is True
        assert published[ASSIGNMENT_12] is False
        # Still exactly one copy of each.
        assert len(published) == 4


def test_publication_modes_and_forced_titles(world):
    app, teacher_id, _ = world
    with app.state.session_factory() as db:
        draft_plan = build_plan(
            db, teacher_id, targets=["ז4"], mode="draft"
        )
        apply_plan(db, draft_plan)
        db.commit()

        z4 = db.scalar(
            select(ClassGroup).where(
                ClassGroup.teacher_id == teacher_id,
                ClassGroup.name == "ז'4",
            )
        )

        def flags():
            return {
                item.title: item.published
                for item in db.scalars(
                    select(Assignment).where(Assignment.class_id == z4.id)
                ).all()
            }

        assert not any(flags().values())

        published_plan = build_plan(
            db, teacher_id, targets=["ז4"], mode="published"
        )
        apply_plan(db, published_plan)
        db.commit()
        assert all(flags().values())

        # Forced draft wins over the mode; forced publish overrides a draft.
        forced = build_plan(
            db,
            teacher_id,
            targets=["ז4"],
            mode="published",
            force_draft=("1.2",),
            force_publish=("2.2",),
        )
        apply_plan(db, forced)
        db.commit()
        flags_now = flags()
        assert flags_now[ASSIGNMENT_12] is False
        assert flags_now[ASSIGNMENT_22] is True

        source_plan = build_plan(
            db, teacher_id, targets=["ז4"], mode="source"
        )
        apply_plan(db, source_plan)
        db.commit()
        # Every source assignment is a draft in the reference class.
        assert not any(flags().values())


def test_per_class_modes_in_one_run(world):
    app, teacher_id, _ = world
    with app.state.session_factory() as db:
        plan = build_plan(
            db,
            teacher_id,
            targets=["ז4", "ח6"],
            class_modes={"ז4": "draft", "ח6": "published"},
        )
        apply_plan(db, plan)
        db.commit()
        for token, expected in (("ז4", False), ("ח6", True)):
            group = db.scalar(
                select(ClassGroup).where(
                    ClassGroup.teacher_id == teacher_id,
                    ClassGroup.name == ("ז'4" if token == "ז4" else "ח'6"),
                )
            )
            assignments = db.scalars(
                select(Assignment).where(Assignment.class_id == group.id)
            ).all()
            assert all(item.published is expected for item in assignments)


def test_existing_link_is_updated_and_existing_title_is_not_duplicated(world):
    app, teacher_id, _ = world
    with app.state.session_factory() as db:
        group = ClassGroup(name="ז'4", code="OLD123", teacher_id=teacher_id)
        db.add(group)
        db.flush()
        db.add(
            ClassroomLink(
                teacher_id=teacher_id,
                class_id=group.id,
                course_id="wrong-course",
                course_name="קורס ישן",
            )
        )
        existing = Assignment(
            class_id=group.id,
            title=ASSIGNMENT_32,
            instructions="",
            rubric_path="rubrics/test.xlsx",
            rubric_sha256="d" * 64,
            published=False,
        )
        db.add(existing)
        db.flush()
        db.add(
            AssignmentContent(
                assignment_id=existing.id,
                source_kind="coursework",
                source_id="manual-copy",
                original_title=ASSIGNMENT_32,
            )
        )
        db.commit()

        plan = build_plan(db, teacher_id, targets=["ז4"])
        z4 = plan.classes[0]
        assert z4.class_exists is True
        assert z4.link_will_update is True
        item = next(i for i in z4.items if i.title == ASSIGNMENT_32)
        assert item.portal_id == existing.id
        assert item.action == "publish"

        apply_plan(db, plan)
        db.commit()
        link = db.scalar(
            select(ClassroomLink).where(ClassroomLink.class_id == group.id)
        )
        assert link.course_id == "c-z4"
        count = len(
            db.scalars(
                select(Assignment).where(Assignment.class_id == group.id)
            ).all()
        )
        assert count == 4


def test_missing_catalog_reports_errors_instead_of_publishing(app):
    teacher_id = create_user(app, "teacher", "TeacherPass1!")
    _seed_reference(app, teacher_id)
    with app.state.session_factory() as db:
        plan = build_plan(db, teacher_id)
        assert plan.classes_to_create == 0
        assert len(plan.errors) == 7
        assert all("Google Classroom" in error for error in plan.errors)
        outcome = apply_plan(db, plan)
        assert outcome.created_total == 0
        assert len(outcome.errors) == 7


# ---------------------------------------------------------------------------
# CLI and web entry points
# ---------------------------------------------------------------------------


def test_cli_preview_and_apply(world, portal_settings, monkeypatch, capsys):
    app, teacher_id, _ = world
    from portal.scripts import sync_classes

    monkeypatch.setattr(
        sync_classes, "load_settings", lambda: portal_settings
    )

    assert sync_classes.main(["--teacher", "teacher", "--classes", "ז4"]) == 0
    output = capsys.readouterr().out
    assert "תצוגה בלבד" in output
    with app.state.session_factory() as db:
        assert db.scalar(select(ClassGroup).where(ClassGroup.name == "ז'4")) is None

    assert (
        sync_classes.main(
            ["--teacher", "teacher", "--classes", "ז4", "--apply"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "בוצע" in output
    with app.state.session_factory() as db:
        group = db.scalar(
            select(ClassGroup).where(
                ClassGroup.teacher_id == teacher_id,
                ClassGroup.name == "ז'4",
            )
        )
        assert group is not None
        assert (
            len(
                db.scalars(
                    select(Assignment).where(Assignment.class_id == group.id)
                ).all()
            )
            == 4
        )


def test_web_screen_previews_and_applies(world, client):
    app, teacher_id, _ = world
    login(client, "teacher", "TeacherPass1!")
    page = client.get("/teacher/sync-classes")
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert "הרחבת כיתות" in text
    assert "ז'4" in text
    assert "פייתון א' - ז4" in text
    assert "לפי מצב הפרסום בקורס Classroom" in text
    assert "ח'8" not in text
    assert "ח8" not in text

    token = csrf_from(page.text)
    response = client.post(
        "/teacher/sync-classes",
        data={
            "csrf_token": token,
            "target": ["ז4", "ח3"],
            "mode_ז4": MODE_CLASSROOM,
            "mode_ח3": "draft",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "נוצרו" in response.text

    with app.state.session_factory() as db:
        groups = {
            class_key(group.name): group
            for group in db.scalars(
                select(ClassGroup).where(
                    ClassGroup.teacher_id == teacher_id
                )
            ).all()
        }
        assert set(groups) == {"ז3", "ז4", "ח3"}
        z4 = groups["ז4"]
        flags = {
            item.title: item.published
            for item in db.scalars(
                select(Assignment).where(Assignment.class_id == z4.id)
            ).all()
        }
        assert flags[ASSIGNMENT_32] is True
        assert flags[ASSIGNMENT_22] is False
        h3 = groups["ח3"]
        assert not any(
            item.published
            for item in db.scalars(
                select(Assignment).where(Assignment.class_id == h3.id)
            ).all()
        )


def test_web_screen_is_teacher_only(world, new_client):
    app, _, _ = world
    create_user(app, "student", "StudentPass1!", role="student")
    login(new_client, "student", "StudentPass1!")
    assert new_client.get("/teacher/sync-classes").status_code == 403
