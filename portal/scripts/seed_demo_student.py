"""Create a removable demo student in a real class and seed past grades.

The account exists so the teacher can walk the whole student pipeline on a
phone: open lessons, submit an assignment, wait for the grading worker, then
see the score and past grades. It is deliberately separate from the Phase 3
demo class: it enrolls in a real class (default: ז'4) so lessons and published
assignments are the real ones.

* The login name is ``demo.student`` and the password is random. The password
  is written to ``portal/data/demo_student_credentials.csv`` (git-ignored) and
  is never printed.
* Past grades are fabricated for assignments that are already due. They never
  call the model and their jobs are terminal, so the background worker leaves
  them alone.
* The newest assignment ("3.2 | הגשת משימות") is intentionally left unsubmitted
  so the teacher can test a live hand-in end to end.

Run from the project root::

    python -m portal.scripts.seed_demo_student
    python -m portal.scripts.seed_demo_student --apply
    python -m portal.scripts.seed_demo_student --cleanup --apply

``--db`` points the run at a copy (dry-run first, as with every live-DB
script). ``graded_at`` values below are **naive UTC**, because that is what
SQLite stores and what the pages convert back to Asia/Jerusalem for display.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from portal.auth.security import hash_password
from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.grading import adapter
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    SUBMISSION_GRADED,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    GradingJob,
    StudentRecord,
    Submission,
    User,
)
from portal.paths import DATA_DIR

DEMO_USERNAME = "demo.student"
DEMO_DISPLAY_NAME = "תלמיד דמו"
DEMO_CLASS_NAME = "ז'4"
CREDENTIALS_PATH = DATA_DIR / "demo_student_credentials.csv"
PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
PASSWORD_LENGTH = 10


@dataclass(frozen=True)
class SeedSpec:
    """One past assignment plus the fabricated grade for it."""

    title: str
    score: int
    feedback: str
    code: str
    graded_at: datetime
    partial_task: int = 0


SEEDS: tuple[SeedSpec, ...] = (
    SeedSpec(
        title="1.2 | הגשת משימות",
        score=88,
        feedback=(
            "עבודה יפה! רוב הפתרונות נכונים והקוד ברור. שימו לב לסדר "
            "הפעולות ולבדיקת הפלט לפני ההגשה."
        ),
        code=(
            'first_name = "דניאל"\n'
            "age = 13\n"
            'print("שלום", first_name)\n'
            'print("גיל:", age)\n'
            "future_age = age + 5\n"
            'print("בעוד חמש שנים אהיה בן", future_age)\n'
        ),
        graded_at=datetime(2026, 9, 21, 15, 30),
    ),
    SeedSpec(
        title="2.2 | הגשת משימות",
        score=92,
        feedback=(
            "מצוין! השמה מרובה ושילוב משתנים בפלט נעשו נכון. כדאי "
            "להוסיף הדפסה ברורה יותר בין השאלות."
        ),
        code=(
            "x, y = 7, 3\n"
            "total = x + y\n"
            "difference = x - y\n"
            'print("סכום:", total)\n'
            'print("הפרש:", difference)\n'
            'name, grade = "נועה", 95\n'
            'print(name, "קיבלה", grade)\n'
        ),
        graded_at=datetime(2026, 10, 19, 14, 10),
    ),
    SeedSpec(
        title="2.3 | תרגול לפיצוח - דף עבודה",
        score=74,
        feedback=(
            "התחלה טובה. חלק מהתרגילים הושלמו, אבל יש כמה טעויות בהמרת "
            "טיפוסים ובסדר הפעולות. עברו שוב על השאלות שסומנו ונסו שוב."
        ),
        code=(
            'color = "כחול"\n'
            "count = 3\n"
            'print("הצבע", color, "מופיע", count, "פעמים")\n'
            "price = 12.5\n"
            "quantity = 4\n"
            "total = price * quantity\n"
            'print("לתשלום:", total)\n'
        ),
        graded_at=datetime(2026, 10, 26, 14, 45),
        partial_task=2,
    ),
    SeedSpec(
        title="תרגול יחידות 1-2",
        score=81,
        feedback=(
            "הבנה טובה של יחידות 1–2. יש לשפר את מעקב הריצה והדיבאגינג – "
            "שווה לכתוב את הערכים בכל שלב על דף לפני שמריצים."
        ),
        code=(
            "a = 5\n"
            "b = a * 2\n"
            "a = a + 1\n"
            'print("a =", a)\n'
            'print("b =", b)\n'
            "if b > a:\n"
            '    print("b גדול מ-a")\n'
        ),
        graded_at=datetime(2026, 10, 26, 17, 5),
        partial_task=1,
    ),
)


def generate_password() -> str:
    import secrets

    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def task_names(rubric) -> list[str]:
    """Rubric task names, for both lesson rubrics and exam specs."""
    names: list[str] = []
    for task in getattr(rubric, "tasks", None) or []:
        name = getattr(task, "name", "")
        if name:
            names.append(str(name))
    for section in getattr(rubric, "sections", None) or []:
        section_name = str(getattr(section, "section_name", "") or "")
        for question in getattr(section, "questions", None) or []:
            question_id = str(getattr(question, "id", "") or "")
            label = " · ".join(part for part in (section_name, question_id) if part)
            if label:
                names.append(label)
    return names or ["המשימה"]


def build_tasks(names: list[str], score: int, partial_index: int) -> tuple[list[dict], list[dict]]:
    """Fabricate task verdicts and deductions that add up to 100 - score."""
    missing = max(0, 100 - int(score))
    evaluations = [
        {
            "task_name": name,
            "student_answer_found": "",
            "status": "CORRECT",
            "notes": "הפתרון מלא ותקין.",
        }
        for name in names
    ]
    if missing == 0:
        return evaluations, []

    index = min(max(partial_index, 0), len(names) - 1)
    evaluations[index]["status"] = "PARTIALLY_CORRECT"
    evaluations[index]["notes"] = "הפתרון חלקי או שחסר שלב."
    return evaluations, [
        {
            "task_name": names[index],
            "points_deducted": missing,
            "reason": "חלק מהשלב חסר או לא מדויק.",
        }
    ]


def _record(
    student_name: str,
    spec: SeedSpec,
    *,
    binding: adapter.RubricBinding,
    names: list[str],
) -> dict:
    tasks, deductions = build_tasks(names, spec.score, spec.partial_task)
    return {
        "student_name": student_name,
        "score": spec.score,
        "feedback_hebrew": spec.feedback,
        "deduction_breakdown": deductions,
        "task_evaluations": tasks,
        "audit": {
            "ok": True,
            "autofixed": False,
            "flags": [],
            "findings": [],
            "original_score": spec.score,
            "final_score": spec.score,
            "deductions_total": 100 - spec.score,
            "needs_review": False,
            "badge": "✅ clean",
            "summary": "audit clean",
        },
        "rubric_sha256": binding.sha256,
        "rubric_title": binding.title,
        "late_penalty_points": 0,
        "configured_model": "demo/mock-model",
        "graded_at": spec.graded_at.isoformat(),
    }


def _ensure_user(db, *, apply: bool) -> tuple[User | None, str | None]:
    existing = db.scalar(select(User).where(User.username == DEMO_USERNAME))
    if existing is not None:
        return existing, None
    password = generate_password()
    user = User(
        username=DEMO_USERNAME,
        display_name=DEMO_DISPLAY_NAME,
        password_hash=hash_password(password),
        role=ROLE_STUDENT,
    )
    if apply:
        db.add(user)
        db.flush()
    return (user if apply else None), password


def _write_credentials(password: str) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CREDENTIALS_PATH.exists()
    with CREDENTIALS_PATH.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(
                ("class", "display_name", "username", "password")
            )
        writer.writerow((DEMO_CLASS_NAME, DEMO_DISPLAY_NAME, DEMO_USERNAME, password))


def _find_assignment(db, class_id: int, title: str) -> Assignment | None:
    """The class's copy of one assignment; published copies win on a tie."""
    rows = db.scalars(
        select(Assignment)
        .where(Assignment.class_id == class_id, Assignment.title == title)
        .order_by(Assignment.published.desc(), Assignment.id)
    ).all()
    return rows[0] if rows else None


def _seed(db, *, apply: bool) -> tuple[dict[str, int], str | None]:
    group = db.scalar(select(ClassGroup).where(ClassGroup.name == DEMO_CLASS_NAME))
    if group is None:
        raise SystemExit(f"error: class {DEMO_CLASS_NAME!r} not found.")
    teacher = db.get(User, group.teacher_id)
    if teacher is None or teacher.role != ROLE_TEACHER:
        raise SystemExit("error: class teacher account not found.")

    user, password = _ensure_user(db, apply=apply)
    counts = {"user": 0, "enrollment": 0, "record": 0, "grades": 0}
    if user is not None and password is not None:
        counts["user"] = 1

    # Bind every rubric before any write: a stale hash or a moved file must
    # abort the whole run, exactly as the grading worker would refuse it.
    plans = []
    for spec in SEEDS:
        assignment = _find_assignment(db, group.id, spec.title)
        if assignment is None:
            print(f"  MISSING assignment: {spec.title}")
            continue
        binding = adapter.load_rubric(
            assignment.rubric_path,
            expected_sha256=assignment.rubric_sha256,
        )
        names = task_names(binding.rubric)
        plans.append((spec, assignment, binding, names))
        print(
            f"  {spec.title:<40} -> assignment {assignment.id}, "
            f"score {spec.score}, {len(names)} tasks, "
            f"rubric {binding.title!r}"
        )
    if len(plans) != len(SEEDS):
        raise SystemExit("error: seed assignments are missing from the class.")

    if not apply:
        return counts, password

    assert user is not None
    if db.scalar(
        select(Enrollment).where(
            Enrollment.class_id == group.id,
            Enrollment.student_id == user.id,
        )
    ) is None:
        db.add(Enrollment(class_id=group.id, student_id=user.id))
        counts["enrollment"] = 1

    if db.scalar(
        select(StudentRecord).where(StudentRecord.student_id == user.id)
    ) is None:
        db.add(
            StudentRecord(
                student_id=user.id,
                national_id=None,
                email="demo.student@example.invalid",
                class_label="ז4",
                source_file="seed_demo_student",
            )
        )
        counts["record"] = 1

    for spec, assignment, binding, names in plans:
        existing = db.scalar(
            select(Submission).where(
                Submission.assignment_id == assignment.id,
                Submission.student_id == user.id,
            )
        )
        if existing is not None:
            print(f"  skip (already seeded): {spec.title}")
            continue
        record = _record(user.display_name, spec, binding=binding, names=names)
        submission = Submission(
            assignment_id=assignment.id,
            student_id=user.id,
            text=spec.code,
            revision=1,
            status=SUBMISSION_GRADED,
            first_submitted_at=spec.graded_at,
            submitted_at=spec.graded_at,
        )
        db.add(submission)
        db.flush()
        job = GradingJob(
            submission_id=submission.id,
            revision=1,
            state=SUBMISSION_GRADED,
            finished_at=spec.graded_at,
        )
        db.add(job)
        db.flush()
        db.add(
            Grade(
                submission_id=submission.id,
                revision=1,
                job_id=job.id,
                score=spec.score,
                feedback_hebrew=spec.feedback,
                result_json=json.dumps(
                    {
                        "student_name": record["student_name"],
                        "score": record["score"],
                        "feedback_hebrew": record["feedback_hebrew"],
                        "deduction_breakdown": record["deduction_breakdown"],
                        "task_evaluations": record["task_evaluations"],
                    },
                    ensure_ascii=False,
                ),
                deduction_json=json.dumps(
                    record["deduction_breakdown"], ensure_ascii=False
                ),
                audit_json=json.dumps(record["audit"], ensure_ascii=False),
                audit_ok=True,
                audit_needs_review=False,
                audit_summary="audit clean",
                rubric_sha256=binding.sha256,
                rubric_title=binding.title,
                late_penalty_points=0,
                configured_model="demo/mock-model",
                graded_at=spec.graded_at,
            )
        )
        counts["grades"] += 1
        print(f"  seeded grade {spec.score}: {spec.title}")
    return counts, password


def _cleanup(db) -> tuple[int, int]:
    user = db.scalar(select(User).where(User.username == DEMO_USERNAME))
    if user is None:
        return 0, 0
    submissions = db.scalars(
        select(Submission).where(Submission.student_id == user.id)
    ).all()
    grade_rows = 0
    for submission in submissions:
        for grade in db.scalars(
            select(Grade).where(Grade.submission_id == submission.id)
        ).all():
            db.delete(grade)
            grade_rows += 1
        for job in db.scalars(
            select(GradingJob).where(GradingJob.submission_id == submission.id)
        ).all():
            db.delete(job)
        db.delete(submission)
    for record in db.scalars(
        select(StudentRecord).where(StudentRecord.student_id == user.id)
    ).all():
        db.delete(record)
    for enrollment in db.scalars(
        select(Enrollment).where(Enrollment.student_id == user.id)
    ).all():
        db.delete(enrollment)
    db.delete(user)
    db.commit()
    if CREDENTIALS_PATH.exists():
        CREDENTIALS_PATH.unlink()
    return 1, grade_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite file to use instead of the configured database (use a copy for dry runs).",
    )
    args = parser.parse_args()

    cfg = load_settings()
    database_url = cfg.database_url
    if args.db is not None:
        database_url = f"sqlite:///{args.db.resolve().as_posix()}"
    engine = build_engine(database_url)
    init_db(engine)
    factory = build_session_factory(engine)
    try:
        with factory() as db:
            if args.cleanup:
                if not args.apply:
                    user = db.scalar(
                        select(User).where(User.username == DEMO_USERNAME)
                    )
                    print(
                        "Demo student:",
                        "found" if user is not None else "not found",
                    )
                    return 0
                users, grades = _cleanup(db)
                print(f"Removed demo student: users={users}, grades={grades}")
                return 0

            counts, password = _seed(db, apply=args.apply)
            if not args.apply:
                print(
                    "Dry run only; re-run with --apply to create the account "
                    "and seed grades."
                )
                return 0
            db.commit()
            if counts["user"] and password:
                # Only after the row is committed, so a rolled-back run can
                # never leave a password for an account that does not exist.
                _write_credentials(password)
            print(
                "Created demo student "
                f"{DEMO_USERNAME!r} in {DEMO_CLASS_NAME!r}; "
                f"enrollment={counts['enrollment']}, "
                f"record={counts['record']}, "
                f"new grades={counts['grades']}."
            )
            if counts["user"]:
                print(f"Credentials written to {CREDENTIALS_PATH}")
            else:
                print("Account already existed; credentials file unchanged.")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())