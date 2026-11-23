"""A re-check orphaned by a server restart must offer recovery, not spin forever.

The background run lives only in this process's memory. When the server is
restarted mid-run the thread dies but the ``ExamRecheckPreview`` row stays
``running``, so the teacher's progress screen would freeze on a stale count with
no way out. ``llm_check.interrupted`` detects that orphan (running + no live
progress + past a short grace window) and both the page and the status route
report it, so the screen shows a one-click "back to selection" recovery card.

These tests drive the production module and routes with a manually aged running
row; no key, network, sandbox or real quota is involved, and no grade is touched.
"""
from __future__ import annotations

import html
from datetime import timedelta

from portal.exams import llm_check
from portal.models import ExamRecheckPreview
from portal.tests.helpers import csrf, login
from portal.tests.test_exam_llm_check import (
    TEACHER_PASSWORD,
    _db,
    _start,
    env,  # noqa: F401 - pytest fixture reused from the sibling module
)
from portal.timeutil import utcnow


def _age(env, preview_id, seconds):
    """Push a running row's created_at back so it is past the grace window."""
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        preview.created_at = utcnow() - timedelta(seconds=seconds)
        db.commit()


def _orphan(env):
    """A running, owned row with no live progress, older than the grace window."""
    preview_id = _start(env, [env["submissions"][0]])
    _age(env, preview_id, llm_check.ORPHAN_GRACE_SECONDS + 5)
    return preview_id


# ------------------------------------------------------------------- unit
def test_interrupted_is_false_for_none_ready_and_live(env):
    assert llm_check.interrupted(None) is False

    preview_id = _start(env, [env["submissions"][0]])
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        # A freshly started run is inside the grace window: not an orphan.
        assert llm_check.interrupted(preview) is False
        # A finished run is never "interrupted", whatever its age.
        preview.state = llm_check.READY
        db.commit()
        assert llm_check.interrupted(preview) is False


def test_interrupted_is_false_while_progress_is_live(env, monkeypatch):
    preview_id = _orphan(env)
    # A live progress snapshot means the thread is alive, even for an old row.
    monkeypatch.setitem(
        llm_check._PROGRESS, preview_id, llm_check._RunProgress(1)
    )
    with _db(env["app"]) as db:
        assert llm_check.interrupted(db.get(ExamRecheckPreview, preview_id)) is False


def test_interrupted_is_true_for_an_orphaned_running_row(env):
    preview_id = _orphan(env)
    with _db(env["app"]) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        assert preview.state == llm_check.RUNNING
        assert llm_check.owns(preview) is True
        assert llm_check.progress_for(preview_id) is None
        assert llm_check.interrupted(preview) is True


# ------------------------------------------------------------------- routes
def test_status_reports_interrupted_for_an_orphaned_run(client, app, env):
    app.state.grading_worker.stop()
    preview_id = _orphan(env)
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    data = client.get(f"{base}/status").json()
    assert data["state"] == "interrupted"
    assert data["progress"] is None
    # The row is untouched: recovery is the teacher's click, not an auto-delete.
    with _db(app) as db:
        assert db.get(ExamRecheckPreview, preview_id) is not None


def test_screen_offers_recovery_and_cancel_clears_the_orphan(client, app, env):
    app.state.grading_worker.stop()
    preview_id = _orphan(env)
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    text = html.unescape(client.get(base).text)
    assert "הבדיקה נקטעה" in text
    assert f'action="{base}/cancel"' in text
    # The frozen spinner card must not be shown for an orphaned run.
    assert "בודק הגשות" not in text
    assert "data-llm-status" not in text

    token = csrf(client, base)
    cancelled = client.post(
        f"{base}/cancel", data={"csrf_token": token}, follow_redirects=False
    )
    assert cancelled.status_code == 303
    with _db(app) as db:
        assert db.get(ExamRecheckPreview, preview_id) is None
    # Back to a clean selection screen, ready to run again.
    text = html.unescape(client.get(base).text)
    assert "בחירת הגשות לבדיקה" in text


def test_a_live_run_still_shows_the_running_screen(client, app, env, monkeypatch):
    app.state.grading_worker.stop()
    preview_id = _start(env, [env["submissions"][0]])
    monkeypatch.setitem(
        llm_check._PROGRESS, preview_id, llm_check._RunProgress(1)
    )
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    login(client, "llm-mori", TEACHER_PASSWORD)
    assert client.get(f"{base}/status").json()["state"] == "running"
    text = html.unescape(client.get(base).text)
    assert "בודק הגשות" in text
    assert "data-llm-status" in text
    assert "הבדיקה נקטעה" not in text