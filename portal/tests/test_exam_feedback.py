"""The teacher-correction feedback loop: store, bound, inject, and the view fix.

When the teacher overrides the points the model gave a question - the ``ניקוד``
and ``הערה`` fields on the review paper - the correction is stored and, the next
time the model marks the *same* question, a handful of recent corrections travel
with the prompt as compact examples. The whole point is the **bound**: the
injected block is hard-capped so a growing correction history can never swell the
request into a ``429`` from sheer token weight.

These tests prove four links of that chain, plus the view-form fix that shows the
latest model verdict while it is still unapplied:

* the store (:mod:`portal.exams.feedback`) records, replaces, retracts and caps;
* the renderer never crosses :data:`feedback.MAX_BLOCK_CHARS`;
* the DB-aware ``service.mark`` turns stored corrections into bounded blocks and
  hands them to the DB-free checker, which threads them into the model context;
* a manual override on a ready preview row edits that row (not the stored grade),
  survives apply, and is captured / retracted as feedback;
* the review page shows the preview record, with a banner, instead of the old grade.

The model transport itself is exercised in ``test_exam_llm_routing`` (the block is
sent) and ``test_exam_llm_review`` (the checker threads it). No key, network or
real quota is involved here: ``exam_service.mark`` is faked exactly as in
``test_exam_llm_check``, whose fixtures these tests reuse.
"""
from __future__ import annotations

import html
import json
import types
from datetime import timedelta

import pytest

from portal.exams import checker
from portal.exams import config as exam_config
from portal.exams import feedback
from portal.exams import llm_check
from portal.exams import manual as exam_manual
from portal.exams import service as exam_service
from portal.exams.paper import ensure_allocation
from portal.grading import service as grading_service
from portal.models import (
    Assignment,
    ExamLlmFeedback,
    ExamRecheckPreview,
    Submission,
)
from portal.tests.helpers import csrf, login
from portal.timeutil import utcnow

# Reuse the real-exam fixtures and helpers from the selective-check tests.
from portal.tests.test_exam_llm_check import (  # noqa: F401  (env is a fixture)
    EXAM,
    OLD_SCORE,
    TEACHER_PASSWORD,
    _db,
    _grade,
    _record,
    _start,
    _wire,
    env,
)


# ------------------------------------------------------------------ the bound
def test_render_block_is_empty_when_there_is_nothing_to_teach():
    assert feedback.render_block(None) == ""
    assert feedback.render_block([]) == ""


def test_render_block_never_crosses_the_hard_cap():
    """Three full-length corrections cannot all fit; the loop stops at the cap."""
    long_answer = "מ" * feedback.MAX_ANSWER_CHARS
    long_note = "ה" * feedback.MAX_NOTE_CHARS
    examples = [
        {
            "answer": long_answer,
            "note": long_note,
            "teacher_points": 3,
            "model_points": 0,
            "maximum": 10,
        }
        for _ in range(feedback.MAX_EXAMPLES)
    ]
    block = feedback.render_block(examples)
    assert len(block) <= feedback.MAX_BLOCK_CHARS
    # The cap binds before all three full-length examples are added.
    assert block.count("- תשובה:") < feedback.MAX_EXAMPLES
    assert block.count("- תשובה:") >= 1


def test_render_block_keeps_short_examples_up_to_the_example_cap():
    examples = [
        {
            "answer": f"תשובה {index}",
            "note": "הערה קצרה",
            "teacher_points": index + 1,
            "model_points": 0,
            "maximum": 10,
        }
        for index in range(5)  # more than the cap on purpose
    ]
    block = feedback.render_block(examples)
    assert len(block) <= feedback.MAX_BLOCK_CHARS
    # At most MAX_EXAMPLES lines, even though five were offered.
    assert block.count("- תשובה:") == feedback.MAX_EXAMPLES


def test_render_block_trims_a_single_overlong_answer_and_note():
    block = feedback.render_block(
        [
            {
                "answer": "x" * 5000,
                "note": "y" * 5000,
                "teacher_points": 2,
                "model_points": 0,
                "maximum": 5,
            }
        ]
    )
    assert len(block) <= feedback.MAX_BLOCK_CHARS
    assert "…" in block  # clipped on a boundary, not dumped whole


# ------------------------------------------------------------------ the store
def test_record_stores_a_correction_and_for_exam_returns_it(env):
    s0 = env["submissions"][0]
    with _db(env["app"]) as db:
        row = feedback.record(
            db,
            exam_config=EXAM,
            item_key="c1",
            answer="הגרשיים לא נכונים",
            model_points=0,
            teacher_points=3,
            note="הבין את הרעיון, חסר פסיק",
            maximum=10,
            submission_id=s0,
            created_by=env["teacher"],
        )
        db.commit()
        assert row is not None
    with _db(env["app"]) as db:
        buckets = feedback.for_exam(db, EXAM)
        assert set(buckets) == {"c1"}
        example = buckets["c1"][0]
        assert example["teacher_points"] == 3
        assert example["model_points"] == 0
        assert example["maximum"] == 10
        assert example["answer"] == "הגרשיים לא נכונים"
        assert "הבין את הרעיון" in example["note"]


def test_record_is_a_noop_when_the_teacher_agrees_with_the_model(env):
    s0 = env["submissions"][0]
    with _db(env["app"]) as db:
        row = feedback.record(
            db,
            exam_config=EXAM,
            item_key="c1",
            answer="תשובה",
            model_points=5,
            teacher_points=5,  # same number: nothing to teach
            submission_id=s0,
        )
        db.commit()
        assert row is None
        assert db.query(ExamLlmFeedback).count() == 0


def test_record_retracts_an_earlier_correction_when_the_teacher_now_agrees(env):
    s0 = env["submissions"][0]
    with _db(env["app"]) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="א",
            model_points=0, teacher_points=3, submission_id=s0,
        )
        db.commit()
        assert db.query(ExamLlmFeedback).count() == 1
        # The teacher takes it back: the automatic mark was right after all.
        again = feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="א",
            model_points=3, teacher_points=3, submission_id=s0,
        )
        db.commit()
        assert again is None
        assert db.query(ExamLlmFeedback).count() == 0


def test_record_replaces_rather_than_stacks_for_one_hand_in(env):
    s0 = env["submissions"][0]
    with _db(env["app"]) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="א",
            model_points=0, teacher_points=3, submission_id=s0,
        )
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="ב",
            model_points=0, teacher_points=4, note="עדכון", submission_id=s0,
        )
        db.commit()
        rows = db.query(ExamLlmFeedback).all()
        assert len(rows) == 1  # one row per hand-in per question
        assert rows[0].teacher_points == 4
        assert rows[0].answer == "ב"


def test_record_trims_the_answer_and_note_to_the_caps(env):
    s0 = env["submissions"][0]
    with _db(env["app"]) as db:
        row = feedback.record(
            db, exam_config=EXAM, item_key="c1",
            answer="x" * 5000, model_points=0, teacher_points=2,
            note="y" * 5000, submission_id=s0,
        )
        db.commit()
        assert len(row.answer) <= feedback.MAX_ANSWER_CHARS + 1
        assert len(row.note) <= feedback.MAX_NOTE_CHARS + 1


def test_for_exam_caps_each_question_at_the_newest_examples(env):
    with _db(env["app"]) as db:
        # Five corrections for one question (no hand-in id, so none dedupe).
        for index in range(1, 6):
            feedback.record(
                db, exam_config=EXAM, item_key="c1", answer=f"תשובה {index}",
                model_points=0, teacher_points=index * 10, maximum=10,
            )
        db.commit()
        bucket = feedback.for_exam(db, EXAM)["c1"]
        assert len(bucket) == feedback.MAX_EXAMPLES
        # Newest first: the last three corrections, not the first three.
        assert [example["teacher_points"] for example in bucket] == [50, 40, 30]


def test_retract_removes_the_stored_correction(env):
    s0 = env["submissions"][0]
    with _db(env["app"]) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="א",
            model_points=0, teacher_points=3, submission_id=s0,
        )
        db.commit()
        feedback.retract(db, exam_config=EXAM, item_key="c1", submission_id=s0)
        db.commit()
        assert db.query(ExamLlmFeedback).count() == 0


def test_for_exam_is_scoped_to_the_exam(env):
    with _db(env["app"]) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="א",
            model_points=0, teacher_points=3,
        )
        feedback.record(
            db, exam_config="other_exam", item_key="c1", answer="ב",
            model_points=0, teacher_points=4,
        )
        db.commit()
        assert set(feedback.for_exam(db, EXAM)) == {"c1"}
        assert feedback.for_exam(db, EXAM)["c1"][0]["teacher_points"] == 3
        assert feedback.for_exam(db, "") == {}


# ------------------------------------------------- mark threads bounded blocks
def test_mark_hands_the_checker_bounded_blocks_from_the_store(env, monkeypatch):
    """The DB-aware layer renders the store; the checker only receives strings."""
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    assert config is not None
    version_key = config.versions[0].key
    with _db(env["app"]) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="הגרשיים לא נכונים",
            model_points=0, teacher_points=3, note="הבין את הרעיון", maximum=10,
        )
        db.commit()

    captured: dict = {}

    def fake_check(cfg, version, values, *, checks=None, feedback=None, progress=None):
        captured["feedback"] = feedback
        return checker.ExamReport(
            exam=EXAM, version=version_key, version_label="", score=0, maximum=0
        )

    monkeypatch.setattr(exam_service.checker, "check", fake_check)
    # The hand-in needs a version and answers; stub just those prerequisites so
    # the real for_exam + render_block run inside mark.
    monkeypatch.setattr(
        exam_service, "allocation_for",
        lambda db, aid, sid: types.SimpleNamespace(version=version_key),
    )
    monkeypatch.setattr(
        exam_service.worksheets, "load_values",
        lambda db, sub, **k: {"c1": {"explain": "תשובה"}},
    )
    with _db(env["app"]) as db:
        exam_service.mark(
            db,
            types.SimpleNamespace(id=env["assignment"]),
            types.SimpleNamespace(student_id=env["students"][0]),
            student_name="תלמיד",
            config=config,
        )
    block = captured["feedback"]["c1"]
    assert "הגרשיים לא נכונים" in block
    assert "הבין את הרעיון" in block
    assert len(block) <= feedback.MAX_BLOCK_CHARS


def test_mark_passes_no_feedback_when_the_store_is_empty(env, monkeypatch):
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    version_key = config.versions[0].key
    captured: dict = {}

    def fake_check(cfg, version, values, *, checks=None, feedback=None, progress=None):
        captured["feedback"] = feedback
        return checker.ExamReport(
            exam=EXAM, version=version_key, version_label="", score=0, maximum=0
        )

    monkeypatch.setattr(exam_service.checker, "check", fake_check)
    monkeypatch.setattr(
        exam_service, "allocation_for",
        lambda db, aid, sid: types.SimpleNamespace(version=version_key),
    )
    monkeypatch.setattr(
        exam_service.worksheets, "load_values",
        lambda db, sub, **k: {"c1": {"explain": "תשובה"}},
    )
    with _db(env["app"]) as db:
        exam_service.mark(
            db,
            types.SimpleNamespace(id=env["assignment"]),
            types.SimpleNamespace(student_id=env["students"][0]),
            student_name="תלמיד",
            config=config,
        )
    assert captured["feedback"] == {}


# ------------------------------------------------- editing a ready preview row
def test_preview_record_returns_the_latest_model_result(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        row = llm_check.row_for(preview, s0)
        assert row is not None and row["new_score"] == 90
        record = llm_check.preview_record(preview, s0)
        assert record is not None and record["score"] == 90
        # A hand-in that was not checked has no preview record.
        assert llm_check.preview_record(preview, env["submissions"][1]) is None


def test_adjust_row_edits_the_preview_not_the_stored_grade(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        info = llm_check.adjust_row(
            db, db.get(ExamRecheckPreview, preview_id), s0, "c1",
            points=3, note="הבין את הרעיון", teacher_id=env["teacher"],
        )
    assert info["auto_points"] == 90  # the model's verdict the teacher overrode
    assert info["points"] == 3
    assert info["maximum"] == 100
    # The preview row now carries the manual 3 and flags one kept override.
    with _db(env["app"]) as db:
        row = llm_check.row_for(db.get(ExamRecheckPreview, preview_id), s0)
        assert row["new_score"] == 3
        assert row["manual_kept"] == 1
        assert row["changed"] >= 1
        record = row["record"]
        question = record["exam"]["questions"][0]
        assert question["earned"] == 3
        assert question["manual"]["points"] == 3
        assert question["manual"]["auto_points"] == 90
    # The stored grade is untouched until the teacher applies the check.
    assert _grade(env, s0).score == OLD_SCORE


def test_clear_row_restores_the_automatic_score(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        llm_check.adjust_row(db, preview, s0, "c1", points=3, teacher_id=env["teacher"])
    with _db(env["app"]) as db:
        llm_check.clear_row(
            db, db.get(ExamRecheckPreview, preview_id), s0, "c1",
            teacher_id=env["teacher"],
        )
    with _db(env["app"]) as db:
        row = llm_check.row_for(db.get(ExamRecheckPreview, preview_id), s0)
        assert row["new_score"] == 90
        assert row["manual_kept"] == 0
        assert "manual" not in row["record"]["exam"]["questions"][0]


def test_adjust_row_then_apply_writes_the_manual_score(env, monkeypatch):
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED)})
    preview_id = _start(env, [s0])
    llm_check.run(env["app"].state.session_factory, preview_id)
    with _db(env["app"]) as db:
        llm_check.adjust_row(
            db, db.get(ExamRecheckPreview, preview_id), s0, "c1",
            points=3, teacher_id=env["teacher"],
        )
    with _db(env["app"]) as db:
        applied = llm_check.apply(db, db.get(ExamRecheckPreview, preview_id))
    assert applied == 1
    grade = _grade(env, s0)
    assert grade.score == 3  # the teacher's correction wins over the model's 90
    question = json.loads(grade.result_json)["exam"]["questions"][0]
    assert question["manual"]["points"] == 3
    assert question["manual"]["auto_points"] == 90


# ------------------------------------------------------------- the view fix
def _run_preview_through_route(client, app, env, monkeypatch, plan):
    """Start a model check via the route, running the work synchronously."""
    app.state.grading_worker.stop()
    _wire(monkeypatch, plan)
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


def test_view_form_shows_the_latest_model_preview_not_the_old_grade(
    client, app, env, monkeypatch
):
    s0 = env["submissions"][0]
    _run_preview_through_route(client, app, env, monkeypatch, {s0: (90, llm_check.REVIEWED)})
    page = client.get(f"/teacher/submissions/{s0}")
    assert page.status_code == 200, page.text[:600]
    text = html.unescape(page.text)
    # The unapplied-verdict banner and the model's fresh score, not the old 60.
    assert "זהו הציון של בדיקת המודל האחרונה" in text
    assert 'class="score-number">90<' in text
    assert 'class="score-number">60<' not in text


def test_view_form_shows_the_paper_banner_when_the_paper_is_present(
    client, app, env, monkeypatch
):
    """With a real allocation the exam-review banner explains the unapplied check."""
    s0 = env["submissions"][0]
    _run_preview_through_route(client, app, env, monkeypatch, {s0: (90, llm_check.REVIEWED)})
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    with _db(app) as db:
        ensure_allocation(db, config, env["assignment"], env["students"][0])
        db.commit()
    text = html.unescape(client.get(f"/teacher/submissions/{s0}").text)
    assert "מציג את בדיקת המודל האחרונה" in text
    assert "והתיקון נשמר כדי לכוון את בדיקת המודל הבאה" in text


# ------------------------------------------------------- a newer live verdict
def _age_preview(env, preview_id: int, seconds: int = 60) -> None:
    """Backdate the preview's ``generated_at`` (in real life it sits open days)."""
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        data = json.loads(preview.payload_json)
        data["generated_at"] = (
            utcnow() - timedelta(seconds=seconds)
        ).isoformat(timespec="seconds")
        preview.payload_json = json.dumps(data, ensure_ascii=False)
        db.commit()


def _auto_remark(env, submission_id: int, score: int) -> None:
    """The automatic model pass writing a fresh verdict into the live grade."""
    with _db(env["app"]) as db:
        grade = grading_service.grade_for_revision(db, submission_id, 1)
        exam_manual.write_record(grade, _record(score, llm_check.REVIEWED))
        grade.graded_at = utcnow()
        db.commit()


def _date_grade_back(env, submission_id: int, seconds: int = 120) -> None:
    """Make one live grade older than the aged preview (a real hand-in that the
    check did not touch): the fixture writes it seconds ago, which would read
    as newer than a preview aged only a minute."""
    with _db(env["app"]) as db:
        grade = grading_service.grade_for_revision(db, submission_id, 1)
        grade.graded_at = utcnow() - timedelta(seconds=seconds)
        db.commit()


def test_view_form_prefers_a_newer_live_verdict_over_an_open_preview(
    client, app, env, monkeypatch
):
    """#182: the automatic pass re-marks papers while a manual preview is still
    open (only a running check pauses the assignment now). The review page must
    show the newer model verdict - the old preview row carries rules-only
    comments and made it look as if the model had never run."""
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.OFF)})
    preview_id = _start(env, [s0])
    llm_check.run(app.state.session_factory, preview_id)
    _age_preview(env, preview_id)
    _auto_remark(env, s0, 95)
    login(client, "llm-mori", TEACHER_PASSWORD)

    text = html.unescape(client.get(f"/teacher/submissions/{s0}").text)
    # The live 95 (model) is displayed, not the preview's 90 (rules only).
    assert 'class="score-number">95<' in text
    assert 'class="score-number">90<' not in text
    assert "נבדק במודל." in text
    # The unapplied-preview wording is gone; the newer-verdict notice is shown.
    assert "מציג את בדיקת המודל האחרונה" not in text
    assert "זהו הציון של בדיקת המודל האחרונה" not in text
    assert "יש כאן בדיקת מודל ידנית שטרם הוחלה" in text
    assert _grade(env, s0).score == 95  # the automatic write stands


def test_llm_check_screen_marks_the_rows_a_newer_verdict_overtook(
    client, app, env, monkeypatch
):
    """The same rule, on the list that offers the apply button: an overtaken row
    is labelled, so the teacher is not told to apply a verdict the system will
    refuse to write."""
    app.state.grading_worker.stop()
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s0: (90, llm_check.OFF), s1: (80, llm_check.OFF)})
    preview_id = _start(env, [s0, s1])
    llm_check.run(app.state.session_factory, preview_id)
    _age_preview(env, preview_id)
    _date_grade_back(env, s1)
    _auto_remark(env, s0, 95)
    login(client, "llm-mori", TEACHER_PASSWORD)

    text = html.unescape(
        client.get(f"/teacher/assignments/{env['assignment']}/llm-check").text
    )
    assert text.count("כבר עדכן בציון החי") == 1  # only the overtaken row
    assert "חלק מהשורות האלו כבר עדכנו בציון החי" in text


def test_adjusting_a_preview_question_captures_feedback(client, app, env, monkeypatch):
    s0 = env["submissions"][0]
    _run_preview_through_route(client, app, env, monkeypatch, {s0: (90, llm_check.REVIEWED)})
    review = f"/teacher/submissions/{s0}"
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/adjust",
        data={"csrf_token": token, "points": "3", "note": "הבין את הרעיון", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    # The correction is stored to calibrate the next model pass.
    with _db(app) as db:
        rows = db.query(ExamLlmFeedback).all()
        assert len(rows) == 1
        assert rows[0].item_key == "c1"
        assert rows[0].exam_config == EXAM
        assert rows[0].submission_id == s0
        assert rows[0].teacher_points == 3
        assert rows[0].model_points == 90
        assert "הבין את הרעיון" in rows[0].note
    # The preview row was corrected; the stored grade is still the old one.
    with _db(app) as db:
        preview = db.query(ExamRecheckPreview).one()
        row = llm_check.row_for(preview, s0)
        assert row["new_score"] == 3 and row["manual_kept"] == 1
    assert _grade(env, s0).score == OLD_SCORE
    text = html.unescape(client.get(review).text)
    assert "התיקון נשמר כדי לכוון את בדיקת המודל הבאה" in text


def test_resetting_a_preview_question_retracts_feedback(client, app, env, monkeypatch):
    s0 = env["submissions"][0]
    _run_preview_through_route(client, app, env, monkeypatch, {s0: (90, llm_check.REVIEWED)})
    review = f"/teacher/submissions/{s0}"
    token = csrf(client, review)
    client.post(
        f"{review}/questions/c1/adjust",
        data={"csrf_token": token, "points": "3", "note": "הערה", "full": ""},
        follow_redirects=False,
    )
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 1
    token = csrf(client, review)
    reset = client.post(
        f"{review}/questions/c1/reset",
        data={"csrf_token": token}, follow_redirects=False,
    )
    assert reset.status_code == 303
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 0  # the lesson is taken back
        preview = db.query(ExamRecheckPreview).one()
        row = llm_check.row_for(preview, s0)
        assert row["new_score"] == 90 and row["manual_kept"] == 0


def test_adjusting_a_stale_preview_question_edits_the_live_grade(
    client, app, env, monkeypatch
):
    """The manual editors follow the same display rule as the page itself. Once
    the automatic pass has overtaken a preview row the teacher reads the live
    grade, so the correction must land there - writing it into the stale preview
    would be an edit nobody sees and every apply path drops."""
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    _wire(monkeypatch, {s0: (90, llm_check.OFF)})
    preview_id = _start(env, [s0])
    llm_check.run(app.state.session_factory, preview_id)
    _age_preview(env, preview_id)
    _auto_remark(env, s0, 70)
    login(client, "llm-mori", TEACHER_PASSWORD)
    review = f"/teacher/submissions/{s0}"

    posted = client.post(
        f"{review}/questions/c1/adjust",
        data={"csrf_token": csrf(client, review), "points": "4", "note": "", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    text = html.unescape(client.get(review).text)
    assert "בדיקת המודל הישנה לא עודכנה" in text
    assert 'class="score-number">4<' in text
    with _db(app) as db:
        row = llm_check.row_for(db.query(ExamRecheckPreview).one(), s0)
        assert row["new_score"] == 90  # the overtaken preview row was left alone
        assert not row.get("manual_kept")
    assert _grade(env, s0).score == 4

    # Undoing it goes back to the same place: the live grade, automatic points.
    reset = client.post(
        f"{review}/questions/c1/reset",
        data={"csrf_token": csrf(client, review)},
        follow_redirects=False,
    )
    assert reset.status_code == 303
    assert _grade(env, s0).score == 70
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 0


# ------------------------------------------- part marks vs a manual override
def test_manual_override_relabels_the_part_marks_as_superseded_automatic(
    client, app, env, monkeypatch
):
    """A hand-set question score must not leave the part marks claiming the old
    automatic points as if they were final.

    The teacher sets one number per question; the parts underneath are the
    automatic breakdown. Before an override they show their normal score; after
    one they become the labelled, dimmed *automatic* breakdown (superseded), and
    the question's manual total is the single final number. This is the fix for
    "the whole score updated but the question's own part still showed 2/5".
    """
    s0 = env["submissions"][0]
    _run_preview_through_route(
        client, app, env, monkeypatch, {s0: (90, llm_check.REVIEWED)}
    )
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    with _db(app) as db:
        ensure_allocation(db, config, env["assignment"], env["students"][0])
        db.commit()
    review = f"/teacher/submissions/{s0}"

    # Before any override the part shows its normal automatic-score framing.
    before = html.unescape(client.get(review).text)
    assert "exam-part-superseded" not in before
    assert "ניקוד אוטומטי:" not in before

    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/adjust",
        data={"csrf_token": token, "points": "0", "note": "לא נכון", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    after = html.unescape(client.get(review).text)
    # The question carries the manual 0 and the override badge.
    assert "ניקוד לשאלה: <strong>0</strong>" in after
    assert "הותאם ידנית" in after
    # The part mark is now the labelled automatic breakdown, not a final score.
    assert "exam-part-superseded" in after
    assert "ניקוד אוטומטי:" in after
    assert "הוחלף בניקוד ידני" in after
    # The clarifying note ties the parts to the manual total.
    assert "הפירוט לפי חלקים למעלה הוא הניקוד האוטומטי המקורי" in after