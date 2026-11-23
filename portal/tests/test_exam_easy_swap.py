"""The in-place paper swap: safe only while nobody has opened the exam.

``replace_assignment_exam`` is the tool that moves ז'3 / ז'4 from the hard
``unit3_input`` paper to ``unit3_input_easy``. These tests prove the two things
that make it safe to run on the live database:

* a dry run writes nothing, and ``--apply`` changes only the exam binding
  (name, inherited instructions, pinned rubric) while the class, window and
  published flag stay as they were;
* an assignment that already has a submission (or a drawn version, or a
  personal window) is refused, because the students' answers would otherwise
  move under a different paper.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from portal.auth.security import hash_password
from portal.db import build_engine, build_session_factory, init_db
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    ExamAllocation,
    ExamStudentWindow,
    Submission,
    User,
)
from portal.scripts import replace_assignment_exam

OLD = "unit3_input"
NEW = "unit3_input_easy"


def _seed(settings):
    """One class with two exam assignments; only the second has a hand-in."""
    engine = build_engine(settings.database_url)
    try:
        init_db(engine)
        factory = build_session_factory(engine)
        with factory() as db:
            teacher = User(
                username="t1",
                display_name="Teacher",
                password_hash=hash_password("Pass12345"),
                role=ROLE_TEACHER,
            )
            student = User(
                username="s1",
                display_name="Student",
                password_hash=hash_password("Pass12345"),
                role=ROLE_STUDENT,
            )
            db.add_all([teacher, student])
            db.flush()
            group = ClassGroup(name="ז'3", code="Z3", teacher_id=teacher.id)
            db.add(group)
            db.flush()
            clean = Assignment(
                class_id=group.id,
                title="3.4 | בוחן",
                instructions="old",
                rubric_path="assignments/exam_unit3_input_a.json",
                rubric_sha256="0" * 64,
                exam_config=OLD,
                published=True,
            )
            used = Assignment(
                class_id=group.id,
                title="3.4 | בוחן (ט'2)",
                instructions="old",
                rubric_path="assignments/exam_unit3_input_a.json",
                rubric_sha256="0" * 64,
                exam_config=OLD,
                published=True,
            )
            db.add_all([clean, used])
            db.flush()
            db.add(
                Submission(
                    assignment_id=used.id, student_id=student.id, text="answer"
                )
            )
            db.commit()
            return clean.id, used.id, student.id
    finally:
        engine.dispose()


def _reload(settings, assignment_id: int) -> Assignment:
    engine = build_engine(settings.database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as db:
            return db.get(Assignment, assignment_id)
    finally:
        engine.dispose()


def _run(settings, *extra: str) -> int:
    return replace_assignment_exam.main(
        [
            "--exam", NEW,
            "--database-url", settings.database_url,
            *extra,
        ]
    )


def test_dry_run_changes_nothing(portal_settings, capsys) -> None:
    clean_id, _used_id, _student_id = _seed(portal_settings)
    code = _run(portal_settings, "--assignments", str(clean_id))
    assert code == 0
    assert "dry-run" in capsys.readouterr().out
    assert _reload(portal_settings, clean_id).exam_config == OLD


def test_apply_rebinds_only_the_exam_fields(portal_settings) -> None:
    clean_id, _used_id, _student_id = _seed(portal_settings)
    before = _reload(portal_settings, clean_id)
    code = _run(portal_settings, "--assignments", str(clean_id), "--apply")
    assert code == 0
    after = _reload(portal_settings, clean_id)
    assert after.exam_config == NEW
    assert after.rubric_path == "assignments/exam_unit3_input_easy_a.json"
    assert after.rubric_sha256 != "0" * 64
    assert "13 שאלות" in after.instructions
    # Identity is untouched: class, window and published state.
    assert after.class_id == before.class_id
    assert after.opens_at == before.opens_at
    assert after.closes_at == before.closes_at
    assert after.published is True
    assert after.title == before.title


def test_an_assignment_with_a_submission_is_refused(portal_settings, capsys) -> None:
    _clean_id, used_id, _student_id = _seed(portal_settings)
    code = _run(portal_settings, "--assignments", str(used_id), "--apply")
    assert code == 1
    assert "REFUSED" in capsys.readouterr().out
    assert _reload(portal_settings, used_id).exam_config == OLD


def test_a_drawn_version_also_blocks_the_swap(portal_settings) -> None:
    clean_id, _used_id, student_id = _seed(portal_settings)
    engine = build_engine(portal_settings.database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as db:
            db.add(
                ExamAllocation(
                    assignment_id=clean_id,
                    student_id=student_id,
                    version="a",
                    seed=1,
                )
            )
            db.commit()
    finally:
        engine.dispose()

    code = _run(portal_settings, "--assignments", str(clean_id), "--apply")
    assert code == 1
    assert _reload(portal_settings, clean_id).exam_config == OLD


def test_a_personal_window_also_blocks_the_swap(portal_settings) -> None:
    clean_id, _used_id, student_id = _seed(portal_settings)
    engine = build_engine(portal_settings.database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as db:
            db.add(
                ExamStudentWindow(
                    assignment_id=clean_id,
                    student_id=student_id,
                    opens_at=datetime(2026, 1, 1, 8, 0),
                    closes_at=datetime(2026, 1, 1, 10, 0),
                )
            )
            db.commit()
    finally:
        engine.dispose()

    code = _run(portal_settings, "--assignments", str(clean_id), "--apply")
    assert code == 1
    assert _reload(portal_settings, clean_id).exam_config == OLD


def test_an_ordinary_homework_assignment_is_refused(portal_settings) -> None:
    engine = build_engine(portal_settings.database_url)
    try:
        init_db(engine)
        factory = build_session_factory(engine)
        with factory() as db:
            teacher = User(
                username="t1",
                display_name="Teacher",
                password_hash=hash_password("Pass12345"),
                role=ROLE_TEACHER,
            )
            db.add(teacher)
            db.flush()
            group = ClassGroup(name="ז'3", code="Z3", teacher_id=teacher.id)
            db.add(group)
            db.flush()
            homework = Assignment(
                class_id=group.id,
                title="3.3 | תרגיל",
                instructions="",
                rubric_path="assignments/lesson_3.json",
                rubric_sha256="0" * 64,
                exam_config="",
                published=True,
            )
            db.add(homework)
            db.commit()
            homework_id = homework.id
    finally:
        engine.dispose()

    code = _run(portal_settings, "--assignments", str(homework_id), "--apply")
    assert code == 1
