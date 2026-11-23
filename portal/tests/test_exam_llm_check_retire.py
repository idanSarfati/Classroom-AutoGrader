"""A manual check retires itself once it can no longer write anything (#183).

The "בדיקת הגשות" screen has two very different jobs: run a check over the
hand-ins the teacher picks, and hold the finished verdicts until the teacher
applies them. The second job used to linger forever. A READY preview whose rows
were all signed off, resubmitted, or re-marked by the automatic pass kept
rendering "תוצאות הבדיקה" and the per-student table - and an apply button that
promised "10 הגשות" while the apply would write none of them, because every row
is dropped by the time it is reached. That is exactly what the teacher saw on
ט'3, whose parallel class (ט'4, no leftover preview) shows only the picker.

So the screen now asks the same question the apply asks, row by row
(:func:`portal.exams.llm_check.preview_workload`): what can this check still do?

* nothing to write and nothing genuinely pending -> the preview retires itself,
  the picker returns, and an honest one-line explanation says why;
* a real unwritten verdict -> both cards stay, and the apply button counts only
  the rows the apply will actually write;
* a paper that simply ran out of quota -> it stays on the "ניסיון חוזר" list.
  Being overtaken by a *newer live verdict* is what retires a row, not being
  waiting in general: the automatic pass runs every three hours, and a teacher
  who wants the paper back now still gets the button.

No grade is written or touched by any of this, and the fixtures never call a
model, a key, or the network.
"""
from __future__ import annotations

import html
import json

from portal.exams import llm_check
from portal.grading import service as grading_service
from portal.models import ExamRecheckPreview
from portal.tests.helpers import csrf, login

# Real-exam fixtures and the selective-check helpers, then the ageing helpers the
# newer-live-verdict tests already share.
from portal.tests.test_exam_llm_check import (  # noqa: F401  (env is a fixture)
    EXAM,
    OLD_SCORE,
    TEACHER_PASSWORD,
    _db,
    _grade,
    _start,
    _wire,
    env,
)
from portal.tests.test_exam_feedback import (
    _age_preview,
    _auto_remark,
    _date_grade_back,
)

from portal.timeutil import utcnow


def _sign_off(env, submission_id: int) -> None:
    """The teacher's "סימון ההגשה כנבדקה" on the live grade."""
    with _db(env["app"]) as db:
        grade = grading_service.grade_for_revision(db, submission_id, 1)
        grade.reviewed_at = utcnow()
        grade.reviewed_by = env["teacher"]
        db.commit()


def _preview_row_count(app, preview_id: int) -> int:
    with _db(app) as db:
        preview = db.get(ExamRecheckPreview, preview_id)
        if preview is None:
            return -1
        return len(json.loads(preview.payload_json or "{}").get("rows") or [])


# ------------------------------------------------------------------ retirement
def test_a_check_with_nothing_left_to_write_retires_itself(
    client, app, env, monkeypatch
):
    """The ט'3 leftover: approved rows plus rows the automatic pass re-marked.

    The preview is READY and its payload still lists every paper, but the apply
    would drop all of them, so the screen must stop presenting dead work and go
    back to the picker ט'4 shows.
    """
    app.state.grading_worker.stop()
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s0: (90, llm_check.OFF), s1: (80, llm_check.OFF)})
    preview_id = _start(env, [s0, s1])
    llm_check.run(app.state.session_factory, preview_id)
    _age_preview(env, preview_id)
    _sign_off(env, s0)  # a human judged it: the apply drops the row
    _auto_remark(env, s1, 70)  # a newer live verdict: the apply drops the row

    login(client, "llm-mori", TEACHER_PASSWORD)
    base = f"/teacher/assignments/{env['assignment']}/llm-check"
    text = html.unescape(client.get(base).text)

    assert "בחירת הגשות לבדיקה" in text  # the picker, like the parallel class
    assert "תוצאות הבדיקה" not in text
    assert "לפי תלמיד" not in text
    assert "אין מה להחיל" in text  # and it says why
    assert _preview_row_count(app, preview_id) == -1  # the preview is gone
    assert _grade(env, s0).score == OLD_SCORE  # the preview was never applied
    assert _grade(env, s1).score == 70  # the automatic verdict stands


def test_a_waiting_paper_is_still_offered_for_a_manual_retry(
    client, app, env, monkeypatch
):
    """Retirement must not steal the "ניסיון חוזר" button from a quota starve.

    The automatic pass would get to this paper eventually, but the teacher can
    ask for it now, and the row is genuinely this screen's work: nothing newer
    answered it and no verdict of its own is waiting to be applied.
    """
    app.state.grading_worker.stop()
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED), s1: (OLD_SCORE, llm_check.WAITING)})
    preview_id = _start(env, [s0, s1])
    llm_check.run(app.state.session_factory, preview_id)
    _age_preview(env, preview_id)
    _date_grade_back(env, s0)
    _date_grade_back(env, s1)  # both live marks predate the preview: overtaken by nothing

    login(client, "llm-mori", TEACHER_PASSWORD)
    text = html.unescape(
        client.get(f"/teacher/assignments/{env['assignment']}/llm-check").text
    )
    assert "תוצאות הבדיקה" in text
    assert "החלת הציונים שנבדקו (1)" in text
    assert "ניסיון חוזר לממתינות (1)" in text
    assert _preview_row_count(app, preview_id) == 2  # both rows still live


# ---------------------------------------------------------------- honest count
def test_the_apply_button_counts_only_the_rows_the_apply_will_write(
    client, app, env, monkeypatch
):
    """A signed-off row stays visible but leaves the count it cannot change."""
    app.state.grading_worker.stop()
    s0, s1 = env["submissions"][:2]
    _wire(monkeypatch, {s0: (90, llm_check.REVIEWED), s1: (85, llm_check.REVIEWED)})
    preview_id = _start(env, [s0, s1])
    llm_check.run(app.state.session_factory, preview_id)
    _age_preview(env, preview_id)
    _date_grade_back(env, s1)  # s1 keeps a real verdict that is still to write
    _sign_off(env, s0)

    login(client, "llm-mori", TEACHER_PASSWORD)
    text = html.unescape(
        client.get(f"/teacher/assignments/{env['assignment']}/llm-check").text
    )
    assert "תוצאות הבדיקה" in text
    assert "החלת הציונים שנבדקו (1)" in text  # not (2)
    assert "ניסיון חוזר לממתינות" not in text
    assert "אושר על ידי המורה" in text  # the row is still shown, labelled

    # And the apply really writes just that one paper.
    token = csrf(client, f"/teacher/assignments/{env['assignment']}/llm-check")
    posted = client.post(
        f"/teacher/assignments/{env['assignment']}/llm-check/apply",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert _grade(env, s0).score == OLD_SCORE  # untouched: the approval stands
    assert _grade(env, s1).score == 85  # written by the apply
