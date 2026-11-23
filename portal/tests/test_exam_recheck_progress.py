"""The re-check screen must read as progress, not a freeze.

A model re-check marks each open question with its own call, so one paper can
take a minute or two while the preview row only advances per *paper*. These tests
drive the production path with a fake model (no key, network or quota) and prove:

* the checker fires a per-question progress callback for every item, and a
  callback that raises can never break grading;
* the background run publishes a live snapshot (which paper, which question)
  that the new ``…/llm-check/status`` route streams while running, and clears it
  when the run ends;
* the status route is scoped to the teacher's own assignment;
* the teacher's exam submission page no longer dumps the raw "הקוד שהוגש" card
  (the paper already shows every answer), while an ordinary coding assignment
  still shows it.

The fixtures are reused from ``test_exam_llm_check`` and ``test_exam_part_routes``.
"""
from __future__ import annotations

import html
import threading
import time

from portal.exams import checker
from portal.exams import config as exam_config
from portal.exams import llm_check
from portal.exams.paper import load_paper
from portal.models import ROLE_TEACHER, SUBMISSION_GRADED, Assignment, Submission
from portal.tests.helpers import create_user, login
from portal.timeutil import utcnow

from portal.tests.test_exam_llm_check import (  # noqa: F401  (env is a fixture)
    EXAM,
    TEACHER_PASSWORD,
    _db,
    _report,
    _start,
    env,
)
from portal.tests.test_exam_part_routes import _ensure_paper


# ------------------------------------------------- the per-question callback
def test_checker_progress_covers_every_question(env, monkeypatch):
    monkeypatch.setattr(checker.llm_review, "enabled", lambda: False)
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    version = config.versions[0]
    items = load_paper(version).items
    calls: list[tuple[int, int, str]] = []
    checker.check(
        config, version, {}, progress=lambda i, n, label: calls.append((i, n, label))
    )
    assert len(calls) == len(items)
    assert [c[0] for c in calls] == list(range(1, len(items) + 1))
    assert all(c[1] == len(items) for c in calls)
    assert all(c[2] for c in calls)  # every question carries a readable label


def test_checker_progress_error_never_breaks_grading(env, monkeypatch):
    monkeypatch.setattr(checker.llm_review, "enabled", lambda: False)
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    version = config.versions[0]

    def boom(i, n, label):
        raise RuntimeError("progress bug")

    report = checker.check(config, version, {}, progress=boom)
    assert report.maximum > 0  # the paper was still marked end to end


# ------------------------------------------------------- the status endpoint
def test_status_is_idle_without_a_run(client, app, env):
    app.state.grading_worker.stop()
    login(client, "llm-mori", TEACHER_PASSWORD)
    res = client.get(
        f"/teacher/assignments/{env['assignment']}/llm-check/status"
    )
    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "idle"
    assert data["progress"] is None


def test_running_card_polls_in_place_instead_of_reloading(client, app, env):
    """The running screen carries the live-progress card, not a full-page refresh."""
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    _start(env, [s0])  # leaves the preview RUNNING; the run itself is not started
    text = html.unescape(client.get(base).text)
    assert "בודק הגשות" in text
    # The in-place poller hooks and the single-pass progress line are present.
    assert f'data-llm-status="{base}/status"' in text
    assert "data-llm-question" in text
    assert "llm-spinner" in text
    assert "המודל בודק את כל השאלות הפתוחות" in text
    assert "בפס אחד" in text
    # The old flickering full-page refresh is gone for JS browsers; only the
    # no-JS fallback remains, and the misleading "the page refreshes itself" copy
    # is removed.
    assert '<meta http-equiv="refresh" content="3">' not in text
    assert "<noscript>" in text
    assert "הדף מתרענן לבד" not in text


def test_status_is_scoped_to_the_owner(client, app, env):
    create_user(app, "llm-other", TEACHER_PASSWORD, role=ROLE_TEACHER)
    login(client, "llm-other", TEACHER_PASSWORD)
    res = client.get(
        f"/teacher/assignments/{env['assignment']}/llm-check/status"
    )
    assert res.status_code == 404


def test_status_streams_progress_while_running(client, app, env, monkeypatch):
    """A real background run feeds the route the screen polls, then clears it."""
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)

    entered = threading.Event()
    release = threading.Event()
    seen: dict = {}
    monkeypatch.setattr(llm_check.llm_review, "enabled", lambda: True)
    monkeypatch.setattr(llm_check.llm_review, "pace", lambda *a, **k: None)

    def blocking_mark(db, assignment, submission, *, student_name, config=None, progress=None):
        seen["mid"] = llm_check.progress_for(preview_id)
        if progress is not None:
            progress(6, 11, "שאלה 6 · מעקב ביצוע")
        seen["after_tick"] = llm_check.progress_for(preview_id)
        entered.set()
        release.wait(timeout=5)
        return _report(90, llm_check.REVIEWED)

    monkeypatch.setattr(llm_check.exam_service, "mark", blocking_mark)
    preview_id = _start(env, [s0])
    llm_check.run_in_thread(app.state.session_factory, preview_id)
    try:
        assert entered.wait(timeout=5), "the run never reached the model"
        data = client.get(f"{base}/status").json()
        assert data["state"] == "running"
        assert data["progress"]["submission_index"] == 1
        assert data["progress"]["submission_total"] == 1
        assert data["progress"]["question_index"] == 6
        assert data["progress"]["question_total"] == 11
        assert "מעקב" in data["progress"]["question_label"]
        assert data["progress"]["student_name"]
    finally:
        release.set()

    # The run finishes on its own; the snapshot is cleared and the state flips.
    data = {}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        data = client.get(f"{base}/status").json()
        if data["state"] == "ready":
            break
        time.sleep(0.05)
    assert data["state"] == "ready"
    assert data["progress"] is None
    assert llm_check.progress_for(preview_id) is None
    # The callback chain reached the store: the per-question tick was recorded.
    assert seen["mid"]["submission_index"] == 1
    assert seen["after_tick"]["question_index"] == 6


def test_run_clears_progress_even_when_a_paper_raises(env, monkeypatch):
    s0 = env["submissions"][0]
    monkeypatch.setattr(llm_check.llm_review, "enabled", lambda: True)
    monkeypatch.setattr(llm_check.llm_review, "pace", lambda *a, **k: None)

    def raising_mark(db, assignment, submission, *, student_name, config=None, progress=None):
        raise RuntimeError("network dropped")

    monkeypatch.setattr(llm_check.exam_service, "mark", raising_mark)
    preview_id = _start(env, [s0])
    assert llm_check.run(env["app"].state.session_factory, preview_id) == llm_check.READY
    assert llm_check.progress_for(preview_id) is None


# ------------------------------------------- the redundant code card removal
def test_exam_submission_hides_the_raw_code_card(client, app, env):
    app.state.grading_worker.stop()
    s0 = env["submissions"][0]
    _ensure_paper(app, env)  # the student sat a version, so the paper renders
    login(client, "llm-mori", TEACHER_PASSWORD)
    text = html.unescape(client.get(f"/teacher/submissions/{s0}").text)
    assert "הקוד שהוגש" not in text


def test_regular_assignment_still_shows_the_code_card(client, app, env):
    app.state.grading_worker.stop()
    now = utcnow()
    with _db(app) as db:
        ordinary = Assignment(
            class_id=env["class"],
            title="תרגיל קוד",
            instructions="",
            rubric_path="assignments/lesson_1.json",
            rubric_sha256="",
            exam_config="",
            published=True,
        )
        db.add(ordinary)
        db.flush()
        submission = Submission(
            assignment_id=ordinary.id,
            student_id=env["students"][0],
            text="print('hello')",
            revision=1,
            status=SUBMISSION_GRADED,
            first_submitted_at=now,
            submitted_at=now,
        )
        db.add(submission)
        db.commit()
        submission_id = submission.id
    login(client, "llm-mori", TEACHER_PASSWORD)
    text = html.unescape(client.get(f"/teacher/submissions/{submission_id}").text)
    assert "הקוד שהוגש" in text
    assert "print(" in text