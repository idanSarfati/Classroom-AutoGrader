"""The per-part manual-score routes: preview and stored-grade paths + feedback.

A multi-part question (an explanation and a corrected program) gets one score box
per part. These tests drive the two new routes end to end with a fake two-part
model report, proving that:

* a part override on a ready preview row edits that row (the question total is
  the sum of its parts), leaves the stored grade alone, and is captured as a
  *per-question* correction whose note names the part and whose answer is the
  part's own stored text;
* resetting the part retracts the correction and restores the automatic points;
* the same routes work on a stored grade when no preview is ready;
* points outside the part's rubric slice are refused and change nothing.

No key, network or real quota is involved: ``exam_service.mark`` is faked exactly
as in ``test_exam_llm_check``, whose fixtures these tests reuse.
"""
from __future__ import annotations

import html
import json

from portal import worksheets
from portal.exams import config as exam_config
from portal.exams import llm_check
from portal.exams import manual as exam_manual
from portal.exams.paper import ensure_allocation
from portal.grading import service as grading_service
from portal.models import ExamLlmFeedback, ExamRecheckPreview, Grade, Submission
from portal.tests.helpers import csrf, login

from portal.tests.test_exam_llm_check import (  # noqa: F401  (env is a fixture)
    EXAM,
    OLD_SCORE,
    TEACHER_PASSWORD,
    _db,
    _grade,
    _parts_report,
    _start,
    _wire,
    _wire_parts,
    env,
)


def _run_parts_preview(client, app, env, monkeypatch, plan, wire=_wire_parts):
    """Start a model check via the route, running synchronously."""
    app.state.grading_worker.stop()
    wire(monkeypatch, plan)
    monkeypatch.setattr(
        llm_check, "run_in_thread",
        lambda factory, preview_id: llm_check.run(factory, preview_id),
    )
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, base)
    started = client.post(
        base,
        data={"csrf_token": token, "submission_ids": [str(sid) for sid in plan]},
        follow_redirects=False,
    )
    assert started.status_code == 303
    return base


def _seed_part_answers(app, submission_id, explain="ההסבר של התלמיד", code="print(1)"):
    with _db(app) as db:
        submission = db.get(Submission, submission_id)
        worksheets.save_values(
            db, submission, {"c1": {"explain": explain, "code": code}}
        )
        db.commit()


def _seed_parts_grade(env, submission_id, explain, code):
    """Replace the hand-in's grade with a two-part c1 record (no preview)."""
    with _db(env["app"]) as db:
        grade = grading_service.grade_for_revision(db, submission_id, 1)
        exam_manual.write_record(
            grade, _parts_report(explain, code, llm_check.OFF).to_record("תלמיד")
        )
        db.commit()


# ----------------------------------------------------------- the preview path
def test_adjusting_a_preview_part_captures_per_question_feedback(
    client, app, env, monkeypatch
):
    s0 = env["submissions"][0]
    _seed_part_answers(app, s0)
    _run_parts_preview(client, app, env, monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    review = f"/teacher/submissions/{s0}"
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/parts/explain/adjust",
        data={"csrf_token": token, "points": "0", "note": "הבין את הרעיון", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    with _db(app) as db:
        rows = db.query(ExamLlmFeedback).all()
        assert len(rows) == 1
        # Calibration stays per question; the note names the part.
        assert rows[0].item_key == "c1"
        assert rows[0].exam_config == EXAM
        assert rows[0].submission_id == s0
        assert rows[0].teacher_points == 0
        assert rows[0].model_points == 20
        assert rows[0].maximum == 50
        assert "ההסבר" in rows[0].note
        assert "הבין את הרעיון" in rows[0].note
        # The example carries the part's own answer, not the whole question.
        assert rows[0].answer == "ההסבר של התלמיד"
    # The preview row was corrected (explain 0 + code 50 = 50); grade untouched.
    with _db(app) as db:
        preview = db.query(ExamRecheckPreview).one()
        row = llm_check.row_for(preview, s0)
        assert row["new_score"] == 50 and row["manual_kept"] == 1
        question = row["record"]["exam"]["questions"][0]
        assert question["earned"] == 50
        assert question["parts"][0]["manual"]["points"] == 0
    assert _grade(env, s0).score == OLD_SCORE


def test_resetting_a_preview_part_retracts_feedback(client, app, env, monkeypatch):
    s0 = env["submissions"][0]
    _run_parts_preview(client, app, env, monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    review = f"/teacher/submissions/{s0}"
    token = csrf(client, review)
    client.post(
        f"{review}/questions/c1/parts/explain/adjust",
        data={"csrf_token": token, "points": "0", "note": "הערה", "full": ""},
        follow_redirects=False,
    )
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 1
    token = csrf(client, review)
    reset = client.post(
        f"{review}/questions/c1/parts/explain/reset",
        data={"csrf_token": token}, follow_redirects=False,
    )
    assert reset.status_code == 303
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 0
        preview = db.query(ExamRecheckPreview).one()
        row = llm_check.row_for(preview, s0)
        assert row["new_score"] == 70 and row["manual_kept"] == 0
        assert "manual" not in row["record"]["exam"]["questions"][0]["parts"][0]


def test_full_credit_on_a_preview_part(client, app, env, monkeypatch):
    s0 = env["submissions"][0]
    _run_parts_preview(client, app, env, monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    review = f"/teacher/submissions/{s0}"
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/parts/explain/adjust",
        data={"csrf_token": token, "points": "", "note": "", "full": "1"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    with _db(app) as db:
        row = llm_check.row_for(db.query(ExamRecheckPreview).one(), s0)
        # explain full 50 + code 50 = 100.
        assert row["new_score"] == 100
        assert row["record"]["exam"]["questions"][0]["parts"][0]["earned"] == 50


def test_part_points_outside_the_slice_are_refused(client, app, env, monkeypatch):
    s0 = env["submissions"][0]
    _run_parts_preview(client, app, env, monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    review = f"/teacher/submissions/{s0}"
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/parts/explain/adjust",
        data={"csrf_token": token, "points": "51", "note": "", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 0
        row = llm_check.row_for(db.query(ExamRecheckPreview).one(), s0)
        assert row["new_score"] == 70 and row["manual_kept"] == 0


# -------------------------------------------------------- the stored-grade path
def test_adjusting_a_stored_grade_part_writes_the_total(client, app, env):
    s0 = env["submissions"][0]
    _seed_part_answers(app, s0)
    _seed_parts_grade(env, s0, explain=20, code=50)
    review = f"/teacher/submissions/{s0}"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/parts/explain/adjust",
        data={"csrf_token": token, "points": "0", "note": "שגיאת הקלדה", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    grade = _grade(env, s0)
    assert grade.score == 50  # explain 0 + code 50
    question = json.loads(grade.result_json)["exam"]["questions"][0]
    assert question["earned"] == 50
    assert question["parts"][0]["manual"]["points"] == 0
    assert question["parts"][0]["manual"]["auto_points"] == 20
    with _db(app) as db:
        rows = db.query(ExamLlmFeedback).all()
        assert len(rows) == 1
        assert rows[0].item_key == "c1"
        assert rows[0].teacher_points == 0
        assert "ההסבר" in rows[0].note


def test_resetting_a_stored_grade_part_restores_the_automatic(client, app, env):
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
    assert _grade(env, s0).score == 50
    token = csrf(client, review)
    reset = client.post(
        f"{review}/questions/c1/parts/explain/reset",
        data={"csrf_token": token}, follow_redirects=False,
    )
    assert reset.status_code == 303
    grade = _grade(env, s0)
    assert grade.score == 70
    question = json.loads(grade.result_json)["exam"]["questions"][0]
    assert "manual" not in question["parts"][0]
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 0


# ------------------------------------------------------------- the review paper
def _ensure_paper(app, env):
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    with _db(app) as db:
        ensure_allocation(db, config, env["assignment"], env["students"][0])
        db.commit()


def test_multipart_question_renders_one_box_per_part(client, app, env, monkeypatch):
    s0 = env["submissions"][0]
    _run_parts_preview(client, app, env, monkeypatch, {s0: (20, 50, llm_check.REVIEWED)})
    _ensure_paper(app, env)
    review = f"/teacher/submissions/{s0}"
    text = html.unescape(client.get(review).text)
    # One editor per part, under its own answer.
    assert "/questions/c1/parts/explain/adjust" in text
    assert "/questions/c1/parts/code/adjust" in text
    # The question total is the read-only sum, not a second whole-question box.
    assert "/questions/c1/adjust" not in text
    assert "הניקוד הכולל הוא סכום החלקים" in text

    # Grading one part relabels its mark and offers a reset, leaving the other
    # part's box intact.
    token = csrf(client, review)
    client.post(
        f"{review}/questions/c1/parts/explain/adjust",
        data={"csrf_token": token, "points": "0", "note": "שגיאת הקלדה", "full": ""},
        follow_redirects=False,
    )
    text = html.unescape(client.get(review).text)
    assert "הותאם ידנית" in text
    assert "/questions/c1/parts/explain/reset" in text
    assert "ניקוד אוטומטי:" in text
    assert "/questions/c1/parts/code/adjust" in text


def test_single_part_question_keeps_the_whole_question_editor(
    client, app, env, monkeypatch
):
    s0 = env["submissions"][0]
    # The single-part report gives c1 one scored part, so it keeps one box.
    _run_parts_preview(
        client, app, env, monkeypatch, {s0: (90, llm_check.REVIEWED)}, wire=_wire
    )
    _ensure_paper(app, env)
    text = html.unescape(client.get(f"/teacher/submissions/{s0}").text)
    assert "/questions/c1/adjust" in text
    assert "/questions/c1/parts/" not in text