"""Seed a removable Phase 3 demo class into the real portal database.

The demo exists so the teacher can walk every grading screen without spending
Groq quota. It never calls the grading engine: grades and audit verdicts are
fabricated, and every seeded job is already terminal (``graded``/``failed``)
or deferred far in the future, so the background worker leaves it alone.

Run from the project root::

    python -m portal.scripts.seed_phase3_demo
    python -m portal.scripts.seed_phase3_demo --cleanup

The script only touches its own demo class (``הדגמת Phase 3``), its demo
students (``demo.*`` usernames) and their submissions/grades. Real classes,
students and grades are never modified.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from portal.auth.security import hash_password
from portal.config import load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.grading import adapter
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    SUBMISSION_DEFERRED,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    Assignment,
    ClassGroup,
    Enrollment,
    Grade,
    GradingJob,
    Submission,
    User,
    utcnow,
)

DEMO_CLASS_NAME = "הדגמת Phase 3 (אפשר למחוק)"
DEMO_CLASS_CODE = "DEMO3"
DEMO_PASSWORD = "DemoPass123"  # throwaway demo credential, never a real account
DEMO_USERNAMES = {
    "demo.noa": "נועה הדגמה",
    "demo.itai": "איתי הדגמה",
    "demo.maya": "מאיה הדגמה",
    "demo.dan": "דן הדגמה",
}


def _code(suffix: str) -> str:
    return (
        "students = [\n"
        '    {"name": "נועה", "grade": 95},\n'
        '    {"name": "איתי", "grade": 88},\n'
        "]\n"
        "\n"
        "for student in students:\n"
        '    print(student["name"], student["grade"])\n'
        "\n"
        f"# {suffix}\n"
    )


def _record(
    student_name: str,
    score: int,
    feedback: str,
    *,
    binding: adapter.RubricBinding,
    graded_at: datetime,
    late_points: int = 0,
    tasks: list[dict] | None = None,
    deductions: list[dict] | None = None,
    findings: list[dict] | None = None,
) -> dict:
    """Build the same flat record ``GradeArtifact.to_record()`` would produce."""
    findings = findings or []
    flags = [item["flag"] for item in findings]
    if findings:
        summary = f"audit needs review: {', '.join(flags)}"
        badge = f"⚠️ {summary}"
    else:
        summary = "audit clean"
        badge = "✅ clean"
    return {
        "student_name": student_name,
        "score": score,
        "feedback_hebrew": feedback,
        "deduction_breakdown": deductions or [],
        "task_evaluations": tasks or [],
        "audit": {
            "ok": not findings,
            "autofixed": False,
            "flags": flags,
            "findings": findings,
            "original_score": score,
            "final_score": score,
            "deductions_total": 100 - score,
            "needs_review": bool(findings),
            "badge": badge,
            "summary": summary,
        },
        "rubric_sha256": binding.sha256,
        "rubric_title": "מטלת הדגמה – רשימות ולולאות",
        "late_penalty_points": late_points,
        "configured_model": "demo/mock-model",
        "graded_at": graded_at.isoformat(),
    }


def _persist_grade(
    db,
    submission: Submission,
    job: GradingJob,
    record: dict,
    *,
    reviewed_by: int | None = None,
    review_note: str = "",
) -> Grade:
    audit = record.get("audit") or {}
    result_json = json.dumps(
        {
            "student_name": record.get("student_name") or "",
            "score": int(record["score"]),
            "feedback_hebrew": record.get("feedback_hebrew") or "",
            "deduction_breakdown": record.get("deduction_breakdown") or [],
            "task_evaluations": record.get("task_evaluations") or [],
        },
        ensure_ascii=False,
    )
    grade = Grade(
        submission_id=submission.id,
        revision=submission.revision,
        job_id=job.id,
        score=int(record["score"]),
        feedback_hebrew=record.get("feedback_hebrew") or "",
        result_json=result_json,
        deduction_json=json.dumps(
            record.get("deduction_breakdown") or [], ensure_ascii=False
        ),
        audit_json=json.dumps(audit, ensure_ascii=False) if audit else "",
        audit_ok=bool(audit.get("ok", True)),
        audit_needs_review=bool(audit.get("needs_review", False)),
        audit_summary=str(audit.get("summary") or "")[:500],
        rubric_sha256=record.get("rubric_sha256") or "",
        rubric_title=record.get("rubric_title") or "",
        late_penalty_points=int(record.get("late_penalty_points") or 0),
        configured_model=record.get("configured_model") or "",
        graded_at=datetime.fromisoformat(record["graded_at"]).replace(tzinfo=None),
        reviewed_at=utcnow() if reviewed_by else None,
        reviewed_by=reviewed_by,
        review_note=review_note,
    )
    db.add(grade)
    return grade


def _new_submission(
    db,
    assignment: Assignment,
    student: User,
    text: str,
    status: str,
    first: datetime,
    submitted: datetime,
) -> Submission:
    submission = Submission(
        assignment_id=assignment.id,
        student_id=student.id,
        text=text,
        revision=1,
        status=status,
        first_submitted_at=first,
        submitted_at=submitted,
    )
    db.add(submission)
    db.flush()
    return submission


def _new_job(db, submission: Submission, state: str, **kwargs) -> GradingJob:
    job = GradingJob(
        submission_id=submission.id,
        revision=submission.revision,
        state=state,
        **kwargs,
    )
    db.add(job)
    db.flush()
    return job


def _delete_demo(factory, usernames: set[str]) -> tuple[int, int]:
    classes = users = 0
    with factory() as db:
        for group in db.scalars(
            select(ClassGroup).where(ClassGroup.name.in_([DEMO_CLASS_NAME]))
        ).all():
            db.delete(group)
            classes += 1
        db.flush()
        for user in db.scalars(
            select(User).where(User.username.in_(usernames))
        ).all():
            db.delete(user)
            users += 1
        db.commit()
    return classes, users


def _seed(factory, teacher_id: int) -> dict:
    binding = adapter.load_rubric("assignments/lesson_1.json")
    now = utcnow()

    students: dict[str, User] = {}
    for username, display_name in DEMO_USERNAMES.items():
        user = User(
            username=username,
            display_name=display_name,
            password_hash=hash_password(DEMO_PASSWORD),
            role=ROLE_STUDENT,
        )
        students[username] = user

    with factory() as db:
        db.add_all(students.values())
        db.flush()
        group = ClassGroup(
            name=DEMO_CLASS_NAME, code=DEMO_CLASS_CODE, teacher_id=teacher_id
        )
        db.add(group)
        db.flush()
        for user in students.values():
            db.add(Enrollment(class_id=group.id, student_id=user.id))

        review_assignment = Assignment(
            class_id=group.id,
            title="הדגמה · מטלה שנבדקה (כולל סקירה)",
            instructions=(
                "כתבו תוכנית שמחזיקה רשימת תלמידים ומדפיסה את שמם וציון. "
                "השתמשו בלולאת for."
            ),
            rubric_path=str(binding.path),
            rubric_sha256=binding.sha256,
            due_at=datetime(2026, 11, 1, 12, 0),
            published=True,
        )
        waiting_assignment = Assignment(
            class_id=group.id,
            title="הדגמה · מטלה שממתינה לבדיקה",
            instructions=(
                "כתבו פונקציה שמקבלת רשימת ציונים ומחזירה את הממוצע. "
                "הדפיסו את התוצאה."
            ),
            rubric_path=str(binding.path),
            rubric_sha256=binding.sha256,
            due_at=datetime(2026, 11, 5, 12, 0),
            published=True,
        )
        db.add_all([review_assignment, waiting_assignment])
        db.flush()

        tasks_ok = [
            {
                "task_name": "איסוף נתונים",
                "student_answer_found": 'students = [{"name": ...}]',
                "status": "CORRECT",
                "notes": "The list of dictionaries is built correctly.",
            },
            {
                "task_name": "הדפסה בלולאה",
                "student_answer_found": "for student in students:",
                "status": "CORRECT",
                "notes": "Every student is printed once.",
            },
        ]
        tasks_partial = [
            {
                "task_name": "איסוף נתונים",
                "student_answer_found": "students = []",
                "status": "PARTIALLY_CORRECT",
                "notes": "The list exists but the dictionaries are incomplete.",
            },
            {
                "task_name": "הדפסה בלולאה",
                "student_answer_found": "for s in students:",
                "status": "CORRECT",
                "notes": "The loop works.",
            },
        ]

        # --- Assignment 1: Noa graded + needs review, Itai graded + reviewed,
        #     Maya failed, Dan did not submit.
        noa = students["demo.noa"]
        noa_sub = _new_submission(
            db,
            review_assignment,
            noa,
            _code("noa"),
            SUBMISSION_GRADED,
            datetime(2026, 10, 30, 9, 15),
            datetime(2026, 10, 30, 9, 15),
        )
        noa_job = _new_job(db, noa_sub, SUBMISSION_GRADED)
        noa_record = _record(
            noa.display_name,
            88,
            "עבודה טובה! הלולאה עובדת והפלט ברור. שם המשתנה students קצת כללי – "
            "כדאי לבחור שם שמתאר את התוכן. שימו לב גם ליישור ההדפסה.",
            binding=binding,
            graded_at=now,
            tasks=tasks_partial,
            deductions=[
                {
                    "task_name": "איסוף נתונים",
                    "points_deducted": 12,
                    "reason": "The grade key is missing from one dictionary.",
                }
            ],
            findings=[
                {
                    "flag": "score_math",
                    "message": "deductions sum to 12, score says 88",
                    "severity": "warning",
                }
            ],
        )
        _persist_grade(db, noa_sub, noa_job, noa_record)

        itai = students["demo.itai"]
        itai_sub = _new_submission(
            db,
            review_assignment,
            itai,
            _code("itai"),
            SUBMISSION_GRADED,
            datetime(2026, 10, 29, 18, 40),
            datetime(2026, 10, 29, 18, 40),
        )
        itai_job = _new_job(db, itai_sub, SUBMISSION_GRADED)
        itai_record = _record(
            itai.display_name,
            95,
            "מצוין! הקוד מסודר, הלולאה נכונה והפלט קריא. המשיכו כך.",
            binding=binding,
            graded_at=now,
            tasks=tasks_ok,
            deductions=[
                {
                    "task_name": "General",
                    "points_deducted": 5,
                    "reason": "One output line lacks a space between name and grade.",
                }
            ],
        )
        _persist_grade(
            db,
            itai_sub,
            itai_job,
            itai_record,
            reviewed_by=teacher_id,
            review_note="נבדק ידנית – הציון תקין.",
        )

        maya = students["demo.maya"]
        maya_sub = _new_submission(
            db,
            review_assignment,
            maya,
            _code("maya") + "\n# intentionally incomplete\n",
            SUBMISSION_FAILED,
            datetime(2026, 10, 31, 21, 5),
            datetime(2026, 10, 31, 21, 5),
        )
        _new_job(
            db,
            maya_sub,
            SUBMISSION_FAILED,
            attempts=3,
            error=(
                "הבדיקה נכשלה עקב תקלה זמנית במנוע הבדיקה. "
                "אפשר לנסות שוב מהעמוד. (Type: LLMEvaluationError)"
            ),
            finished_at=now,
        )

        # --- Assignment 2: Noa deferred (far future), Itai graded late,
        #     Dan graded clean, Maya did not submit.
        deferred_at = now + timedelta(days=30)
        noa_b_sub = _new_submission(
            db,
            waiting_assignment,
            noa,
            _code("noa-b"),
            SUBMISSION_DEFERRED,
            datetime(2026, 11, 4, 10, 0),
            datetime(2026, 11, 4, 10, 0),
        )
        _new_job(
            db,
            noa_b_sub,
            SUBMISSION_DEFERRED,
            attempts=1,
            error=(
                "הבדיקה נדחתה כי מכסת המודל היומית נוצלה. "
                "היא תתחדש אוטומטית."
            ),
            next_attempt_at=deferred_at,
        )

        itai_b_sub = _new_submission(
            db,
            waiting_assignment,
            itai,
            _code("itai-b"),
            SUBMISSION_GRADED,
            datetime(2026, 11, 7, 23, 30),
            datetime(2026, 11, 7, 23, 30),
        )
        itai_b_job = _new_job(db, itai_b_sub, SUBMISSION_GRADED)
        itai_b_record = _record(
            itai.display_name,
            72,
            "הפונקציה עובדת, אבל חסר טיפול ברשימה ריקה. "
            "בגלל ההגשה המאוחרת הורדו 8 נקודות נוספות. כדאי לבדוק את המקרה הזה.",
            binding=binding,
            graded_at=now,
            late_points=8,
            tasks=[
                {
                    "task_name": "חישוב ממוצע",
                    "student_answer_found": "sum(grades) / len(grades)",
                    "status": "PARTIALLY_CORRECT",
                    "notes": "Divides by zero on an empty list.",
                }
            ],
            deductions=[
                {
                    "task_name": "חישוב ממוצע",
                    "points_deducted": 20,
                    "reason": "No guard for an empty list.",
                }
            ],
        )
        _persist_grade(db, itai_b_sub, itai_b_job, itai_b_record)

        dan = students["demo.dan"]
        dan_b_sub = _new_submission(
            db,
            waiting_assignment,
            dan,
            _code("dan-b"),
            SUBMISSION_GRADED,
            datetime(2026, 11, 3, 16, 20),
            datetime(2026, 11, 3, 16, 20),
        )
        dan_b_job = _new_job(db, dan_b_sub, SUBMISSION_GRADED)
        dan_b_record = _record(
            dan.display_name,
            100,
            "מושלם! הפונקציה נכונה, מטפלת גם ברשימה ריקה, והפלט ברור.",
            binding=binding,
            graded_at=now,
            tasks=[
                {
                    "task_name": "חישוב ממוצע",
                    "student_answer_found": "if not grades: return 0",
                    "status": "CORRECT",
                    "notes": "Handles the empty list and returns a float.",
                }
            ],
        )
        _persist_grade(db, dan_b_sub, dan_b_job, dan_b_record)

        db.commit()
        return {
            "class": group.id,
            "review_assignment": review_assignment.id,
            "waiting_assignment": waiting_assignment.id,
            "noa_submission": noa_sub.id,
            "itai_submission": itai_sub.id,
            "maya_submission": maya_sub.id,
            "noa_b_submission": noa_b_sub.id,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove the demo class, demo students and their work, then exit.",
    )
    parser.add_argument(
        "--teacher",
        default="",
        help="Teacher username that owns the demo class (default: first teacher).",
    )
    args = parser.parse_args(argv)

    cfg = load_settings()
    engine = build_engine(cfg.database_url)
    init_db(engine)
    factory = build_session_factory(engine)
    try:
        with factory() as db:
            if args.teacher:
                teacher = db.scalar(
                    select(User).where(User.username == args.teacher.lower())
                )
            else:
                teacher = db.scalar(
                    select(User)
                    .where(User.role == ROLE_TEACHER)
                    .order_by(User.id)
                )
            if teacher is None:
                print(
                    "error: no teacher found. Run bootstrap_teacher first.",
                    file=sys.stderr,
                )
                return 2
            teacher_id, teacher_name = teacher.id, teacher.username

        if args.cleanup:
            classes, users = _delete_demo(factory, set(DEMO_USERNAMES))
            print(
                f"Removed {classes} demo class(es) and {users} demo student(s). "
                f"Database: {cfg.database_url}"
            )
            return 0

        classes, users = _delete_demo(factory, set(DEMO_USERNAMES))
        if classes or users:
            print(
                f"Replaced the previous demo ({classes} class(es), "
                f"{users} student(s))."
            )

        ids = _seed(factory, teacher_id)

        print("Phase 3 demo seeded.")
        print(f"  Teacher:   {teacher_name}")
        print(f"  Class:     {DEMO_CLASS_NAME}")
        print(f"  Overview:  /teacher/grading")
        print(f"  Class:     /teacher/classes/{ids['class']}")
        print(
            "  Roster A:  "
            f"/teacher/assignments/{ids['review_assignment']}/grading"
        )
        print(
            "  Roster B:  "
            f"/teacher/assignments/{ids['waiting_assignment']}/grading"
        )
        print(
            f"  Review:    /teacher/submissions/{ids['noa_submission']} "
            "(needs review)"
        )
        print(
            f"             /teacher/submissions/{ids['itai_submission']} "
            "(already reviewed)"
        )
        print(
            f"  Waiting:   /teacher/submissions/{ids['noa_b_submission']}"
        )
        print(
            f"  Failed:    /teacher/submissions/{ids['maya_submission']}"
        )
        print("  Student logins (shared throwaway password: DemoPass123):")
        for username, display_name in DEMO_USERNAMES.items():
            print(f"    {username:12s} {display_name}")
        print(
            "  Student view: log in as demo.noa and open the demo class, "
            "then any assignment."
        )
        print(
            "  Remove everything: "
            "python -m portal.scripts.seed_phase3_demo --cleanup"
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
