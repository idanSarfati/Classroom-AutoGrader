"""The flat last-24h report on the automatic-check screen.

The teacher asked for a small table: which class, which assignment/exam/quiz,
which student, and whether the model check succeeded — with the reason when it
did not. :func:`portal.exams.auto_status.recent_report` builds it from the same
append-only run log as the history below, and the screen renders it as
``.recent-table`` inside the shared ``.table-scroll`` frame.

Everything here seeds run rows directly (no exam machinery, no model, no
network): the report is a read-only view over the log, so the log *is* the
fixture. ``requeued`` rows are resolved against real grading jobs, because the
pass only records the hand-over — the job decides the final fate.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from portal.exams import auto_status
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    SUBMISSION_DEFERRED,
    SUBMISSION_FAILED,
    SUBMISSION_GRADED,
    SUBMISSION_QUEUED,
    Assignment,
    ClassGroup,
    ExamAutoCheckRun,
    Grade,
    GradingJob,
    Submission,
)
from portal.tests.helpers import create_user, login
from portal.timeutil import utcnow

PASSWORD = "RecentReport123"
SCREEN = "/teacher/exam-auto-check"
TZ = "Asia/Jerusalem"


# ---------------------------------------------------------------------- seeds
_TAG = 0


def _next_tag() -> int:
    global _TAG
    _TAG += 1
    return _TAG


def _seed(app) -> dict:
    """One teacher with an exam, a quiz and a regular assignment in one class.

    Accounts are created before the write session opens (SQLite locking).
    A second teacher owns one more assignment, to prove the report never
    leaks another teacher's papers.
    """
    tag = _next_tag()
    teacher_id = create_user(app, f"recent-mori-{tag}", PASSWORD, role=ROLE_TEACHER)
    other_id = create_user(app, f"recent-other-{tag}", PASSWORD, role=ROLE_TEACHER)
    students = {}
    for index in range(6):
        name = f"תלמיד {tag}-{index}"
        students[index] = (
            create_user(
                app,
                f"recent-talmid-{tag}-{index}",
                PASSWORD,
                role=ROLE_STUDENT,
                display_name=name,
            ),
            name,
        )
    info: dict = {
        "teacher_id": teacher_id,
        "teacher_username": f"recent-mori-{tag}",
        "students": students,
    }
    with app.state.session_factory() as db:
        group = ClassGroup(
            name=f"כיתת דוח {tag}", code=f"RPT{tag:02d}", teacher_id=teacher_id
        )
        db.add(group)
        db.flush()
        exam = Assignment(
            class_id=group.id,
            title="3.9 | מבחן מסכם",
            instructions="ענו על כל השאלות.",
            rubric_path="assignments/exam_units_1_3_a.json",
            rubric_sha256="",
            exam_config="units_1_3",
            published=True,
        )
        quiz = Assignment(
            class_id=group.id,
            title="3.8 | בוחן קלט",
            instructions="ענו על כל השאלות.",
            rubric_path="assignments/exam_unit3_input_a.json",
            rubric_sha256="",
            exam_config="unit3_input",
            published=True,
        )
        regular = Assignment(
            class_id=group.id,
            title="4.2 | תרגול פונקציות",
            instructions="הגישו את הקוד.",
            rubric_path="assignments/lesson_4.json",
            rubric_sha256="",
            exam_config="",
            published=True,
        )
        db.add_all([exam, quiz, regular])
        db.flush()
        other_group = ClassGroup(
            name=f"כיתה של מורה אחר {tag}",
            code=f"RPTX{tag:02d}",
            teacher_id=other_id,
        )
        db.add(other_group)
        db.flush()
        foreign = Assignment(
            class_id=other_group.id,
            title="מטלה של מורה אחר",
            instructions="",
            rubric_path="assignments/lesson_4.json",
            rubric_sha256="",
            exam_config="",
            published=True,
        )
        db.add(foreign)
        db.commit()
        info.update(
            group_name=group.name,
            exam_id=exam.id,
            quiz_id=quiz.id,
            regular_id=regular.id,
            foreign_id=foreign.id,
        )
    return info


def _item(info: dict, assignment_id: int, index: int, outcome: str, **extra) -> dict:
    student_id, name = info["students"][index]
    item = {
        "submission_id": extra.pop("submission_id", 9000 + index),
        "assignment_id": assignment_id,
        "student": name,
        "outcome": outcome,
        "llm_state": extra.pop("llm_state", ""),
        "reason": extra.pop("reason", ""),
    }
    item.update(extra)
    return item


def _run(db, items: list[dict], *, age_hours: float = 1.0) -> ExamAutoCheckRun:
    counts = {"written": 0, "deferred": 0, "failed": 0}
    for item in items:
        outcome = item.get("outcome") if isinstance(item, dict) else None
        if outcome in counts:
            counts[outcome] += 1
    run = ExamAutoCheckRun(
        ran_at=utcnow() - timedelta(hours=age_hours),
        source="scheduler",
        selected=len(items),
        written=counts["written"],
        deferred=counts["deferred"],
        failed=counts["failed"],
        detail_json=json.dumps(items, ensure_ascii=False),
    )
    db.add(run)
    db.flush()
    return run


def _hand_in(
    db,
    assignment_id: int,
    student_id: int,
    *,
    status: str = SUBMISSION_GRADED,
    job_state: str | None = None,
    job_error: str = "",
    score: int | None = None,
) -> int:
    """A submission with an optional grading job and grade (for requeued rows)."""
    now = utcnow()
    submission = Submission(
        assignment_id=assignment_id,
        student_id=student_id,
        text="print(1)",
        revision=1,
        status=status,
        first_submitted_at=now,
        submitted_at=now,
    )
    db.add(submission)
    db.flush()
    if job_state is not None:
        db.add(
            GradingJob(
                submission_id=submission.id,
                revision=1,
                state=job_state,
                error=job_error,
            )
        )
    if score is not None:
        db.add(
            Grade(
                submission_id=submission.id,
                revision=1,
                score=score,
                configured_model="test-model",
            )
        )
    db.flush()
    return submission.id


def _report(app, info: dict, **kwargs) -> dict:
    with app.state.session_factory() as db:
        return auto_status.recent_report(db, info["teacher_id"], TZ, **kwargs)


# ------------------------------------------------------------- outcome mapping
def test_report_lists_every_outcome_newest_first(app):
    info = _seed(app)
    with app.state.session_factory() as db:
        _run(
            db,
            [_item(info, info["exam_id"], 0, "written", score=90)],
            age_hours=5,
        )
        _run(
            db,
            [
                _item(
                    info,
                    info["exam_id"],
                    1,
                    "deferred",
                    reason="HTTP 429 Too Many Requests (Groq)",
                ),
                _item(
                    info,
                    info["exam_id"],
                    2,
                    "failed",
                    reason="HTTP 413 Payload Too Large (Groq)",
                ),
                _item(info, info["exam_id"], 3, "off_confirmed"),
            ],
            age_hours=1,
        )
        db.commit()

    report = _report(app, info)

    assert report["hours"] == 24
    rows = report["rows"]
    assert len(rows) == 4
    # Newest run first, items in log order inside the run.
    assert [row["verdict"] for row in rows] == ["pending", "failed", "ok", "ok"]
    assert rows[0]["status_label"] == "נדחה — ינוסה שוב"
    assert "429" in rows[0]["reason_he"]
    assert rows[1]["status_label"] == "נכשל"
    assert "413" in rows[1]["reason_he"]
    assert rows[2]["status_label"] == "הושלם — אין שאלות פתוחות"
    assert rows[3]["status_label"] == "הצליח — נבדק ונשמר"
    assert rows[3]["score"] == 90
    for row in rows:
        assert row["class_name"] == info["group_name"]
        assert row["kind"] == "מבחן"
        assert row["assignment_title"] == "3.9 | מבחן מסכם"
    assert rows[0]["student"] == info["students"][1][1]
    assert report["counts"] == {"ok": 2, "failed": 1, "pending": 1}


def test_report_labels_exam_quiz_and_homework(app):
    info = _seed(app)
    with app.state.session_factory() as db:
        _run(
            db,
            [
                _item(info, info["exam_id"], 0, "written", score=80),
                _item(info, info["quiz_id"], 1, "written", score=70),
                _item(info, info["regular_id"], 2, "written", score=60),
            ],
        )
        db.commit()

    rows = _report(app, info)["rows"]

    assert [row["kind"] for row in rows] == ["מבחן", "בוחן", "מטלה"]


def test_report_excludes_old_runs_and_other_teachers(app):
    info = _seed(app)
    with app.state.session_factory() as db:
        _run(
            db,
            [_item(info, info["exam_id"], 0, "written", score=90)],
            age_hours=25,
        )
        _run(
            db,
            [
                _item(info, info["foreign_id"], 1, "written", score=50),
                _item(info, info["exam_id"], 2, "written", score=85),
            ],
            age_hours=2,
        )
        db.commit()

    report = _report(app, info)

    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert row["student"] == info["students"][2][1]
    assert row["score"] == 85
    assert report["counts"] == {"ok": 1, "failed": 0, "pending": 0}


# ------------------------------------------------- requeued → the real job fate
@pytest.mark.parametrize(
    ("job_state", "job_error", "score", "verdict", "label"),
    [
        (SUBMISSION_GRADED, "", 85, "ok", "הצליח — נבדק ונשמר"),
        (
            SUBMISSION_FAILED,
            "LLM API error: HTTP 503 Service Unavailable (Gemini)",
            None,
            "failed",
            "נכשל",
        ),
        (
            SUBMISSION_DEFERRED,
            "daily quota exhausted (Groq)",
            None,
            "pending",
            "ממתין לניסיון חוזר",
        ),
        (SUBMISSION_QUEUED, "", None, "pending", "בבדיקה כרגע"),
        (None, "", None, "pending", "נשלח לבדיקה מחדש"),
    ],
)
def test_requeued_row_shows_the_jobs_final_outcome(
    app, job_state, job_error, score, verdict, label
):
    info = _seed(app)
    student_id, _ = info["students"][0]
    with app.state.session_factory() as db:
        submission_id = _hand_in(
            db,
            info["regular_id"],
            student_id,
            status=SUBMISSION_FAILED if job_state == SUBMISSION_FAILED else SUBMISSION_GRADED,
            job_state=job_state,
            job_error=job_error,
            score=score,
        )
        _run(
            db,
            [
                _item(
                    info,
                    info["regular_id"],
                    0,
                    "requeued",
                    submission_id=submission_id,
                )
            ],
        )
        db.commit()

    rows = _report(app, info)["rows"]

    assert len(rows) == 1
    row = rows[0]
    assert row["verdict"] == verdict
    assert row["status_label"] == label
    assert row["kind"] == "מטלה"
    assert row["score"] == score
    if job_state == SUBMISSION_FAILED:
        assert "503" in row["reason_he"]
    if job_state == SUBMISSION_DEFERRED:
        assert "המכסה היומית" in row["reason_he"]


# ------------------------------------------------------------- bounds and junk
def test_report_caps_rows_and_counts_after_the_cap(app, monkeypatch):
    info = _seed(app)
    monkeypatch.setattr(auto_status, "RECENT_MAX_ROWS", 2)
    with app.state.session_factory() as db:
        _run(
            db,
            [
                _item(info, info["exam_id"], index, "written", score=90)
                for index in range(4)
            ],
        )
        db.commit()

    report = _report(app, info)

    assert len(report["rows"]) == 2
    assert report["counts"] == {"ok": 2, "failed": 0, "pending": 0}


def test_report_tolerates_broken_detail_json(app):
    info = _seed(app)
    with app.state.session_factory() as db:
        for payload in ("not-json", '{"a": 1}', "[]"):
            db.add(
                ExamAutoCheckRun(
                    ran_at=utcnow() - timedelta(hours=1),
                    source="scheduler",
                    detail_json=payload,
                )
            )
        _run(
            db,
            [
                "junk-entry",
                {"outcome": "written"},  # no assignment_id → skipped
                _item(info, info["exam_id"], 0, "written", score=95),
            ],
        )
        db.commit()

    report = _report(app, info)

    assert len(report["rows"]) == 1
    assert report["rows"][0]["score"] == 95


# --------------------------------------------------------------- the live page
def test_screen_shows_the_report_table_and_stays_read_only(app, client):
    info = _seed(app)
    student_id, name = info["students"][0]
    with app.state.session_factory() as db:
        submission_id = _hand_in(db, info["exam_id"], student_id)
        _run(
            db,
            [
                _item(
                    info,
                    info["exam_id"],
                    0,
                    "written",
                    score=90,
                    submission_id=submission_id,
                ),
                _item(
                    info,
                    info["exam_id"],
                    1,
                    "failed",
                    reason="HTTP 413 Payload Too Large (Groq)",
                ),
            ],
        )
        db.commit()
        runs_before = db.query(ExamAutoCheckRun).count()

    login(client, info["teacher_username"], PASSWORD)
    response = client.get(SCREEN)

    assert response.status_code == 200
    html = response.text
    assert "דוח 24 השעות האחרונות" in html
    assert 'class="recent-table"' in html
    assert 'class="table-scroll"' in html
    assert info["group_name"] in html
    assert "הצליחו: 1" in html
    assert "נכשלו: 1" in html
    assert "הצליח — נבדק ונשמר" in html
    assert f"/teacher/submissions/{submission_id}" in html
    assert name in html
    assert "413" in html
    # Looking at the report changes nothing.
    with app.state.session_factory() as db:
        assert db.query(ExamAutoCheckRun).count() == runs_before


def test_screen_without_recent_runs_says_so(app, client):
    info = _seed(app)
    login(client, info["teacher_username"], PASSWORD)

    response = client.get(SCREEN)

    assert response.status_code == 200
    assert "לא תועדו בדיקות אוטומטיות" in response.text
    assert 'class="recent-table"' not in response.text