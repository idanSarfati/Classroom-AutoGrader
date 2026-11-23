"""The printable password sheets mirror the stored hashes exactly.

The sheet is what a student types from, so a single wrong password is worse
than no sheet at all: the export verifies every password against its hash and
writes nothing when any of them differs.
"""
from __future__ import annotations

import csv
from pathlib import Path

from portal.auth.security import hash_password
from portal.db import build_engine, build_session_factory, init_db
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    ClassGroup,
    Enrollment,
    StudentRecord,
    User,
)
from portal.scripts.export_student_credentials import (
    build_credentials,
    verify_credentials,
    write_sheets,
)


def _factory(tmp_path: Path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'portal.db').as_posix()}")
    init_db(engine)
    return build_session_factory(engine)


def _seed(factory) -> dict[str, int]:
    with factory() as db:
        teacher = User(
            username="teacher",
            display_name="מורה",
            password_hash="x",
            role=ROLE_TEACHER,
        )
        db.add(teacher)
        db.commit()
        group = ClassGroup(name="ז'3", code="z3", teacher_id=teacher.id)
        db.add(group)
        db.commit()

        students = [
            ("220019152", "אבורמד אביה", "220019152"),
            ("221015514", "דדון אריאל אליהו", "221015514"),
        ]
        for username, display_name, national_id in students:
            user = User(
                username=username,
                display_name=display_name,
                password_hash=hash_password(display_name.split()[1] + national_id),
                role=ROLE_STUDENT,
            )
            db.add(user)
            db.commit()
            db.add(StudentRecord(student_id=user.id, national_id=national_id))
            db.add(Enrollment(class_id=group.id, student_id=user.id))
            db.commit()

        demo = User(
            username="demo.student",
            display_name="תלמיד דמו",
            password_hash="x",
            role=ROLE_STUDENT,
        )
        db.add(demo)
        db.commit()
        return {"group": group.id, "demo": demo.id}


def test_sheets_use_the_given_name_and_the_id(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    _seed(factory)

    with factory() as db:
        entries, skipped = build_credentials(db)

    assert [item.display_name for item in skipped] == ["תלמיד דמו"]
    assert [(entry.display_name, entry.password) for entry in entries] == [
        ("אבורמד אביה", "אביה220019152"),
        ("דדון אריאל אליהו", "אריאל221015514"),
    ]
    assert {entry.class_name for entry in entries} == {"ז'3"}


def test_a_student_without_an_id_is_reported_not_exported(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    _seed(factory)

    with factory() as db:
        entries, skipped = build_credentials(db)

    assert len(entries) == 2
    assert [item.display_name for item in skipped] == ["תלמיד דמו"]
    assert "ת.ז" in skipped[0].reason


def test_one_skipped_student_is_counted_once_across_classes(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory() as db:
        teacher = User(
            username="teacher",
            display_name="מורה",
            password_hash="x",
            role=ROLE_TEACHER,
        )
        db.add(teacher)
        db.commit()
        for code, name in (("z3", "ז'3"), ("z4", "ז'4")):
            db.add(ClassGroup(name=name, code=code, teacher_id=teacher.id))
        db.commit()
        demo = User(
            username="demo.student",
            display_name="תלמיד דמו",
            password_hash="x",
            role=ROLE_STUDENT,
        )
        db.add(demo)
        db.commit()
        for group in db.query(ClassGroup).all():
            db.add(Enrollment(class_id=group.id, student_id=demo.id))
        db.commit()

    with factory() as db:
        entries, skipped = build_credentials(db)

    assert entries == []
    assert [item.display_name for item in skipped] == ["תלמיד דמו"]


def test_every_password_must_match_its_stored_hash(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    _seed(factory)
    with factory() as db:
        entries, _ = build_credentials(db)
        # The teacher reset one student to a different password.
        user = db.query(User).filter_by(username="221015514").one()
        user.password_hash = hash_password("סיסמה-אחרת")
        db.commit()
        entries, _ = build_credentials(db)

    checked, mismatched = verify_credentials(entries)

    assert mismatched == [entries[1].user_id]
    assert [entry.verified for entry in checked] == [True, False]


def test_the_written_sheets_are_readable_and_private(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    _seed(factory)
    with factory() as db:
        entries, _ = build_credentials(db)
    checked, mismatched = verify_credentials(entries)
    assert mismatched == []

    out = tmp_path / "sheets"
    written = write_sheets(out, checked, "10.11.2026")

    assert {path.name for path in written} == {
        "index.html",
        "student_credentials.csv",
        "class-1.html",
    }
    rows = list(
        csv.reader((out / "student_credentials.csv").read_text(encoding="utf-8-sig").splitlines())
    )
    assert rows[0] == ["כיתה", "שם מלא", "שם משתמש (ת.ז.)", "סיסמה"]
    assert rows[1] == ["ז'3", "אבורמד אביה", "220019152", "אביה220019152"]

    sheet = (out / "class-1.html").read_text(encoding="utf-8")
    assert 'dir="rtl"' in sheet
    assert "אביה220019152" in sheet
    assert "data-search" in sheet
    assert "window.print" in sheet
    assert not (out / "class-none.html").exists()

    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="class-1.html"' in index
    assert 'href="student_credentials.csv"' in index


def test_names_are_html_escaped(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory() as db:
        teacher = User(
            username="teacher",
            display_name="מורה",
            password_hash="x",
            role=ROLE_TEACHER,
        )
        db.add(teacher)
        db.commit()
        group = ClassGroup(name="ז'3", code="z3", teacher_id=teacher.id)
        db.add(group)
        db.commit()
        password = "דנה300000001"
        user = User(
            username="300000001",
            display_name="<b>כהן</b> דנה",
            password_hash=hash_password(password),
            role=ROLE_STUDENT,
        )
        db.add(user)
        db.commit()
        db.add(StudentRecord(student_id=user.id, national_id="300000001"))
        db.add(Enrollment(class_id=group.id, student_id=user.id))
        db.commit()

    with factory() as db:
        entries, _ = build_credentials(db)
    checked, _ = verify_credentials(entries)
    out = tmp_path / "sheets"
    write_sheets(out, checked, "10.11.2026")

    sheet = (out / "class-1.html").read_text(encoding="utf-8")
    assert "&lt;b&gt;כהן&lt;/b&gt; דנה" in sheet
    assert "<b>כהן</b>" not in sheet
