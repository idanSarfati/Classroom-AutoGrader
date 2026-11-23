"""The memorable-password rule and its one-transaction apply.

The school name is ``"<family> <given> ..."``, so the password is the second
word plus the national id. These tests pin that rule and prove the apply step
really replaces the stored hash.
"""
from __future__ import annotations

from pathlib import Path

from portal.auth.security import hash_password, verify_password
from portal.db import build_engine, build_session_factory, init_db
from portal.models import ROLE_STUDENT, StudentRecord, User
from portal.scripts.set_student_passwords import (
    apply_hashes,
    build_plan,
    hash_plan,
    student_password,
)


def _factory(tmp_path: Path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'portal.db').as_posix()}")
    init_db(engine)
    return build_session_factory(engine)


def test_the_password_is_the_second_name_word_plus_the_id() -> None:
    assert student_password("אבורמד אביה", "220019152") == "אביה220019152"
    assert student_password("דדון אריאל אליהו", "221015514") == "אריאל221015514"
    assert student_password("  כהן   דנה  ", "300000001") == "דנה300000001"


def test_a_name_without_a_given_word_or_an_id_has_no_password() -> None:
    assert student_password("תלמיד דמו", "") is None
    assert student_password("abc", "220019152") is None
    assert student_password("", "220019152") is None


def test_the_directory_id_wins_over_a_generated_login_name(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(
            User(
                username="s12345678",
                display_name="כהן דנה",
                password_hash="x",
                role=ROLE_STUDENT,
            )
        )
        db.commit()
        user = db.query(User).one()
        db.add(StudentRecord(student_id=user.id, national_id="300000001"))
        db.commit()

    with factory() as db:
        changes, skipped = build_plan(db)

    assert skipped == []
    assert [change.password for change in changes] == ["דנה300000001"]


def test_a_numeric_username_is_used_when_the_record_has_no_id(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="demo.student",
                    display_name="תלמיד דמו",
                    password_hash="x",
                    role=ROLE_STUDENT,
                ),
                User(
                    username="221015514",
                    display_name="דדון אריאל אליהו",
                    password_hash="x",
                    role=ROLE_STUDENT,
                ),
            ]
        )
        db.commit()

    with factory() as db:
        changes, skipped = build_plan(db)

    assert [change.password for change in changes] == ["אריאל221015514"]
    assert [item.display_name for item in skipped] == ["תלמיד דמו"]
    assert "ת.ז" in skipped[0].reason


def test_apply_replaces_the_hash_and_the_new_password_logs_in(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(
            User(
                username="220019152",
                display_name="אבורמד אביה",
                password_hash=hash_password("old-password"),
                role=ROLE_STUDENT,
            )
        )
        db.commit()
        user_id = db.query(User).one().id

    with factory() as db:
        changes, skipped = build_plan(db)
    assert skipped == []
    apply_hashes(factory, changes, hash_plan(changes))

    with factory() as db:
        user = db.get(User, user_id)
        assert verify_password(user.password_hash, "אביה220019152")
        assert not verify_password(user.password_hash, "old-password")
