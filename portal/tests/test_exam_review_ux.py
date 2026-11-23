"""Two review-screen refinements, driven end to end on the real exam fixtures.

Feature A — saving a score or note must not throw the teacher to the top of the
paper. The four adjust/reset routes redirect back with a ``#question-…`` or
``#part-…`` fragment, and the review paper renders the matching landing anchors,
so the teacher stays on the question they just marked and can keep going.

Feature B — once the teacher signs a hand off ("סימון ההגשה כנבדקה"), the
"בדיקת הגשות" screen shows an "אושר על ידי המורה" badge and keeps that paper out
of the selectable re-check pool. A server-side guard mirrors the hidden checkbox
so a posted id cannot sneak an approved paper back into a run. The sign-off
belongs to the hand-in, not to one grading pass: an apply drops rows the teacher
signed off after the preview was made (keeping their stored score and badge),
and only a new student revision - a fresh Grade row - returns the paper to the
pool unapproved.

No key, network or real quota is involved: these reuse the fake-model fixtures
from ``test_exam_llm_check`` and the two-part seeding from ``test_exam_part_routes``.
"""
from __future__ import annotations

import html
import json

import pytest

from portal.exams import llm_check
from portal.exams import manual as exam_manual
from portal.grading import service as grading_service
from portal.models import (
    SUBMISSION_GRADED,
    Assignment,
    ExamRecheckPreview,
    Grade,
    Submission,
)
from portal.tests.helpers import csrf, login
from portal.timeutil import utcnow

from portal.tests.test_exam_llm_check import (  # noqa: F401  (env is a fixture)
    OLD_SCORE,
    TEACHER_PASSWORD,
    _db,
    _grade,
    _record,
    _start,
    _wire,
    env,
)
from portal.tests.test_exam_part_routes import (
    _ensure_paper,
    _seed_part_answers,
    _seed_parts_grade,
)


# ------------------------------------------- Feature A: stay on the question
def test_question_adjust_lands_back_on_the_question(client, app, env):
    s0 = env["submissions"][0]
    review = f"/teacher/submissions/{s0}"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/adjust",
        data={"csrf_token": token, "points": "5", "note": "", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"] == f"{review}#question-c1"


def test_question_reset_lands_back_on_the_question(client, app, env):
    s0 = env["submissions"][0]
    review = f"/teacher/submissions/{s0}"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, review)
    client.post(
        f"{review}/questions/c1/adjust",
        data={"csrf_token": token, "points": "5", "note": "", "full": ""},
        follow_redirects=False,
    )
    token = csrf(client, review)
    reset = client.post(
        f"{review}/questions/c1/reset",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert reset.status_code == 303
    assert reset.headers["location"] == f"{review}#question-c1"


def test_part_adjust_lands_back_on_the_part(client, app, env):
    s0 = env["submissions"][0]
    _seed_part_answers(app, s0)
    _seed_parts_grade(env, s0, explain=20, code=50)
    review = f"/teacher/submissions/{s0}"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/parts/explain/adjust",
        data={"csrf_token": token, "points": "0", "note": "", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"] == f"{review}#part-c1-explain"


def test_part_reset_lands_back_on_the_part(client, app, env):
    s0 = env["submissions"][0]
    _seed_parts_grade(env, s0, explain=20, code=50)
    review = f"/teacher/submissions/{s0}"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, review)
    client.post(
        f"{review}/questions/c1/parts/explain/adjust",
        data={"csrf_token": token, "points": "0", "note": "", "full": ""},
        follow_redirects=False,
    )
    token = csrf(client, review)
    reset = client.post(
        f"{review}/questions/c1/parts/explain/reset",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert reset.status_code == 303
    assert reset.headers["location"] == f"{review}#part-c1-explain"


def test_review_paper_renders_the_landing_anchors(client, app, env):
    s0 = env["submissions"][0]
    _seed_part_answers(app, s0)
    _seed_parts_grade(env, s0, explain=20, code=50)
    _ensure_paper(app, env)
    login(client, "llm-mori", TEACHER_PASSWORD)
    text = html.unescape(client.get(f"/teacher/submissions/{s0}").text)
    assert 'id="question-c1"' in text
    assert 'id="part-c1-explain"' in text
    assert 'id="part-c1-code"' in text


# ------------------------------------- Feature B: approved badge + exclusion
def _mark_reviewed(env, submission_id):
    """The sign-off the review button performs, without going through the UI."""
    with _db(env["app"]) as db:
        grade = grading_service.grade_for_revision(db, submission_id, 1)
        grade.reviewed_at = utcnow()
        grade.reviewed_by = env["teacher"]
        db.commit()


def test_reviewed_hand_in_gets_the_badge_and_leaves_the_picker(client, app, env):
    app.state.grading_worker.stop()
    s0, s1 = env["submissions"][:2]
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)

    # Before sign-off every hand-in is selectable and nothing is approved.
    text = html.unescape(client.get(base).text)
    assert f'name="submission_ids" value="{s0}"' in text
    assert "אושר על ידי המורה" not in text

    # The teacher signs off from the bottom of the submission page.
    review = f"/teacher/submissions/{s0}"
    token = csrf(client, review)
    signed = client.post(
        f"{review}/review",
        data={"csrf_token": token, "note": ""},
        follow_redirects=False,
    )
    assert signed.status_code == 303

    text = html.unescape(client.get(base).text)
    # The approval badge and the approved section are shown.
    assert "אושר על ידי המורה" in text
    assert "אושרו על ידי המורה" in text
    # The signed-off hand-in is no longer a checkbox in the picker...
    assert f'name="submission_ids" value="{s0}"' not in text
    # ...but the others still are.
    assert f'name="submission_ids" value="{s1}"' in text


def test_server_guard_skips_reviewed_hand_ins(env):
    s0, s1 = env["submissions"][:2]
    _mark_reviewed(env, s0)
    with _db(env["app"]) as db:
        assignment = db.get(Assignment, env["assignment"])
        # Only the approved paper was offered -> nothing left to check.
        with pytest.raises(llm_check.LLMCheckError, match="לא נבחרו"):
            llm_check.start(db, assignment, [s0], teacher_id=env["teacher"])
    # Mixed selection: the approved one is dropped at start, the other runs.
    preview_id = _start(env, [s0, s1])
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        data = json.loads(preview.payload_json)
        assert data["submission_ids"] == [s1]
        assert preview.total == 1


def test_extend_guard_skips_reviewed_hand_ins(env, monkeypatch):
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s1: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s1])
    llm_check.run(env["app"].state.session_factory, preview_id)
    _mark_reviewed(env, s0)
    with _db(env["app"]) as db:
        with pytest.raises(llm_check.LLMCheckError, match="לא נבחרו"):
            llm_check.extend(
                db,
                db.get(Assignment, env["assignment"]),
                db.get(ExamRecheckPreview, preview_id),
                [s0],
                teacher_id=env["teacher"],
            )


def test_apply_after_sign_off_keeps_the_badge_and_the_score(client, app, env, monkeypatch):
    """The live bug (#180): a preview was made, the teacher signed papers off,
    then clicked apply. The apply must keep every signed paper's stored score
    AND its approval badge - the fresh model verdict does not overrule a human."""
    app.state.grading_worker.stop()
    s0, s1 = env["submissions"][:2]
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED), s1: (85, llm_check.REVIEWED)})
    preview_id = _start(env, [s0, s1])
    llm_check.run(env["app"].state.session_factory, preview_id)

    # The teacher reads both papers and signs them off before applying.
    _mark_reviewed(env, s0)
    _mark_reviewed(env, s1)
    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 0  # both rows were stale: a human already judged them
    assert _grade(env, s0).score == OLD_SCORE
    assert _grade(env, s1).score == OLD_SCORE

    text = html.unescape(client.get(base).text)
    assert f'name="submission_ids" value="{s0}"' not in text
    assert f'name="submission_ids" value="{s1}"' not in text
    assert "אושרו על ידי המורה" in text
    with _db(env["app"]) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None


def test_new_revision_returns_the_hand_in_to_the_pool(client, app, env):
    """A resubmission opens a fresh revision with a fresh, unapproved Grade:
    the hand-in returns to the selectable pool and loses the badge. The old
    approval stays on the old revision's grade, untouched."""
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    _mark_reviewed(env, s0)
    text = html.unescape(client.get(base).text)
    assert f'name="submission_ids" value="{s0}"' not in text

    with _db(env["app"]) as db:
        submission = db.get(Submission, s0)
        submission.revision = 2
        submission.status = SUBMISSION_GRADED
        grade = Grade(
            submission_id=s0,
            revision=2,
            configured_model="exam-checker",
            late_penalty_points=0,
        )
        exam_manual.write_record(grade, _record(OLD_SCORE, llm_check.OFF))
        db.add(grade)
        db.commit()
    text = html.unescape(client.get(base).text)
    assert f'name="submission_ids" value="{s0}"' in text
    assert "אושרו על ידי המורה" not in text
    with _db(env["app"]) as db:
        old = grading_service.grade_for_revision(db, s0, 1)
        assert old.reviewed_at is not None