"""Plan and apply the alephon → portal student import.

The plan is built from read-only queries and contains every decision: which
account is created, which display name or contact detail changes, and which
class enrollment is missing. ``--apply`` executes it in one transaction, so a
failure leaves the database exactly as it was.

Identity rules
--------------

* The national id (``ת.ז``) is the stable key, stored on
  :class:`~portal.models.StudentRecord`, **not** on the login row. A corrected
  display name or a moved class never creates a second account.
* The login name is the national id. It passes :mod:`portal.validation`
  unchanged (digits, 3–32 characters) and is easy for a student to remember.
* Existing passwords are never reset. New accounts receive a random password
  that is written to a dedicated credentials file, never printed.
"""
from __future__ import annotations

import csv
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.auth.security import hash_password
from portal.imports.sources import Alephon, RosterStudent
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    ClassGroup,
    Enrollment,
    StudentRecord,
    User,
)
from portal.sync.matching import class_key, normalize_class_name

PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
PASSWORD_LENGTH = 10

CREDENTIAL_HEADERS = (
    "class",
    "display_name",
    "national_id",
    "username",
    "password",
)

_CONTACT_FIELDS = (
    ("email", "אימייל"),
    ("phone", "טלפון"),
    ("parent_name", "שם הורה"),
    ("parent_phone", "טלפון הורה"),
    ("parent_email", "אימייל הורה"),
)


@dataclass(frozen=True)
class PlannedStudent:
    """One alephon row and what the import will do with it."""

    group: ClassGroup
    roster: RosterStudent
    user: User | None = None
    record: StudentRecord | None = None
    creates_account: bool = False
    enrolls: bool = False
    changes: tuple[str, ...] = ()
    warning: str = ""

    @property
    def display_name(self) -> str:
        return self.roster.display_name

    @property
    def national_id(self) -> str:
        return self.roster.national_id

    @property
    def actionable(self) -> bool:
        return self.creates_account or self.enrolls or bool(self.changes)


@dataclass(frozen=True)
class StudentPlan:
    entries: tuple[PlannedStudent, ...]
    warnings: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()

    @property
    def new_accounts(self) -> int:
        return sum(1 for entry in self.entries if entry.creates_account)

    @property
    def new_enrollments(self) -> int:
        return sum(1 for entry in self.entries if entry.enrolls)

    @property
    def updated(self) -> int:
        return sum(
            1
            for entry in self.entries
            if entry.changes and not entry.creates_account
        )

    @property
    def unchanged(self) -> int:
        return sum(
            1
            for entry in self.entries
            if not entry.actionable and not entry.warning
        )

    @property
    def skipped(self) -> int:
        return sum(1 for entry in self.entries if entry.warning)


@dataclass(frozen=True)
class Credential:
    class_label: str
    display_name: str
    national_id: str
    username: str
    password: str = field(repr=False)


def generate_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def _username_for(national_id: str, taken: set[str]) -> str:
    if national_id and national_id not in taken:
        return national_id
    while True:
        candidate = "s" + "".join(secrets.choice("0123456789") for _ in range(8))
        if candidate not in taken:
            return candidate


def _normalise_name(raw: str) -> str:
    return " ".join((raw or "").split())


def build_student_plan(db: Session, alephons: list[Alephon]) -> StudentPlan:
    """Compare the alephon files with the database without writing anything."""
    groups = list(db.scalars(select(ClassGroup).order_by(ClassGroup.id)).all())
    by_key: dict[str, ClassGroup] = {}
    for group in groups:
        by_key.setdefault(class_key(normalize_class_name(group.name)), group)

    records_by_id = {
        record.national_id: record
        for record in db.scalars(select(StudentRecord)).all()
        if record.national_id
    }
    users = {user.username: user for user in db.scalars(select(User)).all()}
    users_by_id = {user.id: user for user in users.values()}
    taken_usernames = set(users)
    records_without_id = {
        (
            class_key(record.class_label),
            _normalise_name(users_by_id[record.student_id].display_name),
        ): record
        for record in db.scalars(select(StudentRecord)).all()
        if not record.national_id and record.student_id in users_by_id
    }
    enrolled = {
        (row.class_id, row.student_id)
        for row in db.scalars(select(Enrollment)).all()
    }

    warnings: list[str] = []
    seen_ids: dict[str, str] = {}
    entries: list[PlannedStudent] = []
    classes: list[str] = []

    for alephon in alephons:
        label = normalize_class_name(alephon.class_label)
        group = by_key.get(class_key(label))
        if group is None:
            warnings.append(
                f"הכיתה '{label}' לא נמצאה בפורטל; הסטודנטים שלה לא יובאו. "
                "הריצו קודם python -m portal.scripts.sync_classes --apply."
            )
            continue
        classes.append(group.name)
        classes_for_alephon = set()

        for roster in alephon.students:
            national_id = roster.national_id
            if national_id:
                if national_id in seen_ids:
                    warnings.append(
                        f"{group.name}: ת.ז {national_id} מופיעה פעמיים "
                        f"({seen_ids[national_id]} ו-{roster.display_name}); "
                        "השורה השנייה דולגה."
                    )
                    continue
                seen_ids[national_id] = roster.display_name
                if national_id in classes_for_alephon:
                    continue
                classes_for_alephon.add(national_id)

            record = records_by_id.get(national_id) if national_id else None
            if record is None:
                # No id match: fall back to class + exact display name so a
                # row whose ת.ז arrived later updates the same account instead
                # of creating a second one.
                record = records_without_id.get(
                    (class_key(group.name), _normalise_name(roster.display_name))
                )
            user = users_by_id.get(record.student_id) if record is not None else None
            if user is None and national_id:
                candidate = users.get(national_id)
                if candidate is not None:
                    if candidate.role != ROLE_STUDENT:
                        warnings.append(
                            f"{group.name}: שם המשתמש {national_id} שייך לחשבון "
                            f"{candidate.role}; {roster.display_name} לא יובא."
                        )
                        continue
                    user = candidate

            warning = ""
            creates_account = user is None
            changes: list[str] = []
            enrolls = False

            if creates_account:
                username = _username_for(national_id, taken_usernames)
                taken_usernames.add(username)
                enrolls = True
                changes.append("חשבון חדש")
            else:
                if user.role != ROLE_STUDENT:
                    warnings.append(
                        f"{group.name}: החשבון של {roster.display_name} אינו "
                        "חשבון תלמיד; השורה דולגה."
                    )
                    continue
                if _normalise_name(user.display_name) != _normalise_name(
                    roster.display_name
                ):
                    changes.append("שם תצוגה")
                if record is None:
                    changes.append("כרטיס תלמיד")
                    enrolls = (group.id, user.id) not in enrolled
                else:
                    for field_name, label in _CONTACT_FIELDS:
                        old = (getattr(record, field_name) or "").strip()
                        new = (getattr(roster, field_name) or "").strip()
                        if new and new != old:
                            changes.append(label)
                    if record.class_label != roster.class_label:
                        changes.append("כיתה באלפון")
                    enrolls = (group.id, user.id) not in enrolled
                if not user.is_active:
                    warning = "החשבון קיים אך מושבת; לא הופעל מחדש."

            entries.append(
                PlannedStudent(
                    group=group,
                    roster=roster,
                    user=user,
                    record=record,
                    creates_account=creates_account,
                    enrolls=enrolls,
                    changes=tuple(changes),
                    warning=warning,
                )
            )

    if not entries and not warnings:
        warnings.append("לא נמצאו תלמידים לייבוא.")
    return StudentPlan(
        entries=tuple(entries),
        warnings=tuple(warnings),
        classes=tuple(dict.fromkeys(classes)),
    )


def _record_for(user: User, roster: RosterStudent) -> StudentRecord:
    return StudentRecord(
        student_id=user.id,
        national_id=roster.national_id or None,
        email=roster.email,
        phone=roster.phone,
        parent_name=roster.parent_name,
        parent_phone=roster.parent_phone,
        parent_email=roster.parent_email,
        class_label=roster.class_label,
        source_file=roster.source_file,
    )


def apply_student_plan(db: Session, plan: StudentPlan) -> list[Credential]:
    """Execute a plan and return credentials for accounts created just now.

    The caller writes the credentials file **after** a successful commit. The
    function never prints or logs a password.
    """
    # End the planning read transaction; SQLite refuses to upgrade it on a
    # busy database and the plan is fully materialised already.
    db.commit()

    taken_usernames = set(db.scalars(select(User.username)).all())
    credentials: list[Credential] = []
    for entry in plan.entries:
        if entry.warning and not entry.actionable:
            continue
        roster = entry.roster

        if entry.creates_account:
            username = _username_for(roster.national_id, taken_usernames)
            taken_usernames.add(username)
            password = generate_password()
            user = User(
                username=username,
                display_name=_normalise_name(roster.display_name),
                password_hash=hash_password(password),
                role=ROLE_STUDENT,
                is_active=True,
            )
            db.add(user)
            db.flush()
            db.add(_record_for(user, roster))
            credentials.append(
                Credential(
                    class_label=entry.group.name,
                    display_name=user.display_name,
                    national_id=roster.national_id,
                    username=username,
                    password=password,
                )
            )
        else:
            user = entry.user
            if user is None:  # pragma: no cover - guarded by the planner
                continue
            user.display_name = _normalise_name(roster.display_name)
            record = entry.record
            if record is None:
                record = _record_for(user, roster)
                db.add(record)
            else:
                for field_name, _label in _CONTACT_FIELDS:
                    new = (getattr(roster, field_name) or "").strip()
                    if new:
                        setattr(record, field_name, new)
                if roster.national_id:
                    record.national_id = roster.national_id
                record.class_label = roster.class_label
                record.source_file = roster.source_file

        if entry.enrolls:
            db.add(Enrollment(class_id=entry.group.id, student_id=user.id))

    db.commit()
    return credentials


def write_credentials(path: Path, rows: list[Credential]) -> None:
    """Append new credentials to a UTF-8-BOM CSV, keeping earlier batches.

    The file lives under ``portal/data/`` (git-ignored). It is the teacher's
    only copy of the passwords: neither the database nor the CLI output keeps
    a plaintext copy.
    """
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(CREDENTIAL_HEADERS)
        for row in rows:
            writer.writerow(
                [
                    row.class_label,
                    row.display_name,
                    row.national_id,
                    row.username,
                    row.password,
                ]
            )


__all__ = [
    "CREDENTIAL_HEADERS",
    "Credential",
    "PASSWORD_ALPHABET",
    "PASSWORD_LENGTH",
    "PlannedStudent",
    "StudentPlan",
    "apply_student_plan",
    "build_student_plan",
    "generate_password",
    "write_credentials",
]
