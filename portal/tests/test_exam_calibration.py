"""The grading-style calibration loop: ledger, distill, inject, and the screen.

The teacher's corrections are kept without limit, but the model is never handed
all of them. Once a question gathers enough new corrections the portal *distills*
them - one background model call through the same routed transport as grading -
into a short, bounded grading-style profile. Grading then carries only that small
profile (or the last few raw examples until one exists), so the per-paper prompt
stays tiny no matter how large the log grows.

These tests prove the whole chain with a fake transport (no key, network or real
quota):

* the renderers are hard-bounded (distiller input and injected profile block);
* ``summarize_corrections`` parses/caps the profile and keeps the system prompt
  tiny, returns ``None`` when off/empty/malformed, and propagates quota exhaustion;
* ``distill`` respects the threshold, freshness and the teacher's lock, keeps the
  previous profile on a generic failure, and lets quota exhaustion propagate;
* ``service.mark`` injects the profile when one exists and falls back to examples;
* the background distill guards against duplicates and survives a quota wait;
* the teacher screen renders the ledger, distills on demand, and locks a manual
  profile; the adjust route triggers a distill once the threshold is crossed.

The real-exam fixtures are reused from ``test_exam_llm_check``; the fake router /
client / response objects from ``test_exam_llm_routing``.
"""
from __future__ import annotations

import html
import json
import threading
import time
import types

import pytest

from portal.exams import calibration
from portal.exams import checker
from portal.exams import config as exam_config
from portal.exams import feedback
from portal.exams import llm_review
from portal.exams import service as exam_service
from portal.models import ExamGradingProfile, ExamLlmFeedback
from portal.tests.helpers import csrf, login
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
from portal.tests.test_exam_llm_routing import (
    Decision,
    Endpoint,
    FakeClient,
    FakeResponse,
    FakeRouter,
)
from src.llm_router import DailyQuotaExhausted


# --------------------------------------------------------------------- helpers
def _seed(db, n, *, key="c1", exam=EXAM):
    """Store ``n`` distinct corrections for one question (no hand-in id)."""
    for index in range(n):
        feedback.record(
            db, exam_config=exam, item_key=key, answer=f"תשובה {index}",
            model_points=0, teacher_points=index + 1, note=f"הערה {index}",
            maximum=10,
        )
    db.commit()


def _fake_summarize(monkeypatch, text="פרופיל מזוקק"):
    """Replace the model call with a controlled reply; record what it was asked."""
    calls: list[dict] = []

    def fake(title, corrections_text, *, max_chars=500, client=None):
        calls.append(
            {"title": title, "text": corrections_text, "max_chars": max_chars}
        )
        return text

    monkeypatch.setattr(llm_review, "summarize_corrections", fake)
    return calls


def _profile_verdict(text):
    content = json.dumps({"profile": text}, ensure_ascii=False)
    return FakeResponse(payload={"choices": [{"message": {"content": content}}]})


def _wait_for(predicate, timeout=5.0):
    """Bounded wait for a background thread to reach a state (no real quota)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def model_on(monkeypatch):
    """A fully-configured (fake) model endpoint for the transport-level tests."""
    for name in (
        "EXAM_LLM_REVIEW", "EXAM_LLM_MODEL", "EXAM_LLM_API_KEY",
        "EXAM_LLM_BASE_URL", "EXAM_LLM_TIMEOUT", "GROQ_API_KEY",
        "GROQ_API_KEYS", "GROQ_MODEL", "GROQ_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXAM_LLM_REVIEW", "1")
    monkeypatch.setenv("EXAM_LLM_MODEL", "m")
    monkeypatch.setenv("EXAM_LLM_API_KEY", "k")
    llm_review._cache.clear()
    llm_review.reset_routing()
    yield
    llm_review._cache.clear()
    llm_review.reset_routing()


# ------------------------------------------------------------- the renderers
def test_render_distill_input_is_bounded():
    rows = [
        types.SimpleNamespace(
            answer="מ" * 200, note="ה" * 200, maximum=10,
            model_points=0, teacher_points=3,
        )
        for _ in range(100)
    ]
    text = calibration.render_distill_input(rows)
    assert len(text) <= calibration.MAX_DISTILL_INPUT_CHARS
    assert text.count("- תשובה:") <= calibration.MAX_DISTILL_EXAMPLES
    assert text.count("- תשובה:") >= 1


def test_render_distill_input_trims_an_overlong_answer_and_note():
    rows = [
        types.SimpleNamespace(
            answer="x" * 5000, note="y" * 5000, maximum=5,
            model_points=0, teacher_points=2,
        )
    ]
    text = calibration.render_distill_input(rows)
    assert "…" in text
    assert len(text) <= calibration.MAX_DISTILL_INPUT_CHARS


def test_render_distill_input_carries_the_teacher_points_and_note():
    rows = [
        types.SimpleNamespace(
            answer="הגרשיים לא נכונים", note="הבין את הרעיון", maximum=10,
            model_points=0, teacher_points=3,
        )
    ]
    text = calibration.render_distill_input(rows)
    assert "הגרשיים לא נכונים" in text
    assert "הבין את הרעיון" in text
    assert "0/10" in text and "3/10" in text


def test_render_profile_block_is_empty_when_blank():
    assert calibration.render_profile_block("") == ""
    assert calibration.render_profile_block("   ") == ""
    assert calibration.render_profile_block(None) == ""


def test_render_profile_block_is_bounded_and_headed():
    block = calibration.render_profile_block("א" * 5000)
    assert len(block) <= calibration.MAX_PROFILE_BLOCK_CHARS
    assert "סגנון הניקוד של המורה" in block
    assert "…" in block  # clipped, not dumped whole


# ---------------------------------------------------- summarize_corrections
def test_summarize_corrections_parses_and_caps_the_profile(model_on):
    client = FakeClient(_profile_verdict("א" * 800))
    profile = llm_review.summarize_corrections(
        "שאלה 9", "- תשובה: א | אוטומטי: 0/10 | המורה: 3/10",
        max_chars=500, client=client,
    )
    assert profile is not None
    assert len(profile) == 500
    body = client.calls[0]["json"]
    user = next(m["content"] for m in body["messages"] if m["role"] == "user")
    system = next(m["content"] for m in body["messages"] if m["role"] == "system")
    assert "שאלה 9" in user
    assert "- תשובה:" in user
    # The corrections ride in the user prompt; the system prompt stays tiny.
    assert "- תשובה:" not in system


def test_summarize_corrections_returns_none_when_disabled(model_on, monkeypatch):
    monkeypatch.setenv("EXAM_LLM_REVIEW", "0")
    assert llm_review.summarize_corrections("ש", "טקסט", client=FakeClient()) is None


def test_summarize_corrections_returns_none_on_empty_text(model_on):
    assert llm_review.summarize_corrections("ש", "   ", client=FakeClient()) is None


def test_summarize_corrections_returns_none_on_a_malformed_reply(model_on):
    client = FakeClient(
        FakeResponse(payload={"choices": [{"message": {"content": "not json"}}]})
    )
    assert llm_review.summarize_corrections("ש", "טקסט", client=client) is None


def test_summarize_corrections_returns_none_without_a_profile_key(model_on):
    content = json.dumps({"other": "x"}, ensure_ascii=False)
    client = FakeClient(
        FakeResponse(payload={"choices": [{"message": {"content": content}}]})
    )
    assert llm_review.summarize_corrections("ש", "טקסט", client=client) is None


def test_summarize_corrections_propagates_quota_exhaustion(model_on, monkeypatch):
    router = FakeRouter([DailyQuotaExhausted("every key is out for today")])
    monkeypatch.setattr(llm_review, "_router", lambda: router)
    monkeypatch.setattr(llm_review, "_client", lambda timeout: FakeClient())
    with pytest.raises(llm_review.QuotaExhausted):
        llm_review.summarize_corrections("ש", "טקסט")


# --------------------------------------------------------------- needs_distill
def test_needs_distill_threshold_freshness_and_lock(env, monkeypatch):
    with _db(env["app"]) as db:
        _seed(db, 2)
        assert calibration.needs_distill(db, EXAM, "c1") is False
        _seed(db, 1)  # now three
        assert calibration.needs_distill(db, EXAM, "c1") is True
    _fake_summarize(monkeypatch)
    with _db(env["app"]) as db:
        calibration.distill(db, EXAM, "c1")
        db.commit()
        assert calibration.needs_distill(db, EXAM, "c1") is False  # fresh
        _seed(db, 1)  # a fourth correction makes it stale
        assert calibration.needs_distill(db, EXAM, "c1") is True
        calibration.set_profile(db, EXAM, "c1", "נוסח נעול")
        db.commit()
        assert calibration.needs_distill(db, EXAM, "c1") is False  # locked


# -------------------------------------------------------------------- distill
def test_distill_below_the_threshold_is_a_noop(env, monkeypatch):
    calls = _fake_summarize(monkeypatch)
    with _db(env["app"]) as db:
        _seed(db, 2)
        assert calibration.distill(db, EXAM, "c1") is None
        assert calibration.profile_for(db, EXAM, "c1") is None
    assert calls == []


def test_distill_stores_a_profile_at_the_threshold(env, monkeypatch):
    calls = _fake_summarize(monkeypatch, "פרופיל מזוקק")
    with _db(env["app"]) as db:
        _seed(db, 3)
        profile = calibration.distill(db, EXAM, "c1", title="שאלה 9")
        db.commit()
        assert profile is not None
        assert profile.profile == "פרופיל מזוקק"
        assert profile.source_count == 3
        assert profile.locked is False
        assert profile.distilled_at is not None
        assert profile.source_fingerprint == calibration.fingerprint(
            calibration.all_for_item(db, EXAM, "c1")
        )
    assert len(calls) == 1
    assert calls[0]["title"] == "שאלה 9"
    assert calls[0]["max_chars"] == calibration.MAX_PROFILE_CHARS
    assert "- תשובה:" in calls[0]["text"]


def test_distill_skips_when_the_profile_is_already_fresh(env, monkeypatch):
    calls = _fake_summarize(monkeypatch)
    with _db(env["app"]) as db:
        _seed(db, 3)
        calibration.distill(db, EXAM, "c1")
        db.commit()
        calibration.distill(db, EXAM, "c1")
        db.commit()
    assert len(calls) == 1  # the second call sees an unchanged fingerprint


def test_distill_refreshes_when_new_corrections_arrive(env, monkeypatch):
    calls = _fake_summarize(monkeypatch)
    with _db(env["app"]) as db:
        _seed(db, 3)
        calibration.distill(db, EXAM, "c1")
        db.commit()
        _seed(db, 1)  # a fourth correction
        profile = calibration.distill(db, EXAM, "c1")
        db.commit()
        assert profile.source_count == 4
    assert len(calls) == 2


def test_distill_does_not_clobber_a_locked_profile(env, monkeypatch):
    calls = _fake_summarize(monkeypatch)
    with _db(env["app"]) as db:
        _seed(db, 3)
        calibration.set_profile(db, EXAM, "c1", "נוסח ידני")
        db.commit()
        profile = calibration.distill(db, EXAM, "c1")
        db.commit()
        assert profile.profile == "נוסח ידני"
        assert profile.locked is True
    assert calls == []


def test_force_distill_overwrites_a_locked_profile(env, monkeypatch):
    calls = _fake_summarize(monkeypatch, "פרופיל חדש")
    with _db(env["app"]) as db:
        _seed(db, 3)
        calibration.set_profile(db, EXAM, "c1", "נוסח ידני")
        db.commit()
        profile = calibration.distill(db, EXAM, "c1", force=True)
        db.commit()
        assert profile.profile == "פרופיל חדש"
        assert profile.locked is False
    assert len(calls) == 1


def test_distill_keeps_the_previous_profile_on_a_generic_failure(env, monkeypatch):
    with _db(env["app"]) as db:
        _seed(db, 3)
    _fake_summarize(monkeypatch, "ראשון")
    with _db(env["app"]) as db:
        calibration.distill(db, EXAM, "c1")
        db.commit()
    _fake_summarize(monkeypatch, None)  # the model now returns nothing usable
    with _db(env["app"]) as db:
        _seed(db, 1)
        profile = calibration.distill(db, EXAM, "c1")
        db.commit()
        assert profile.profile == "ראשון"


def test_distill_propagates_quota_exhaustion(env, monkeypatch):
    def exhausted(*_a, **_k):
        raise llm_review.QuotaExhausted("out for today")

    monkeypatch.setattr(llm_review, "summarize_corrections", exhausted)
    with _db(env["app"]) as db:
        _seed(db, 3)
        with pytest.raises(llm_review.QuotaExhausted):
            calibration.distill(db, EXAM, "c1")


def test_distill_without_corrections_returns_none(env, monkeypatch):
    calls = _fake_summarize(monkeypatch)
    with _db(env["app"]) as db:
        assert calibration.distill(db, EXAM, "c1") is None
    assert calls == []


# --------------------------------------------------------------- set_profile
def test_set_profile_trims_locks_and_records_the_teacher(env):
    with _db(env["app"]) as db:
        _seed(db, 1)
        profile = calibration.set_profile(
            db, EXAM, "c1", "א" * 5000, teacher_id=env["teacher"]
        )
        db.commit()
        assert len(profile.profile) <= calibration.MAX_PROFILE_CHARS + 1
        assert profile.locked is True
        assert profile.updated_by == env["teacher"]
        assert profile.source_count == 1


def test_profiles_for_exam_returns_only_non_empty_profiles(env):
    with _db(env["app"]) as db:
        calibration.set_profile(db, EXAM, "c1", "פרופיל c1")
        calibration.set_profile(db, EXAM, "a1", "   ")  # blank → excluded
        calibration.set_profile(db, "other_exam", "c1", "אחר")
        db.commit()
        assert calibration.profiles_for_exam(db, EXAM) == {"c1": "פרופיל c1"}
        assert calibration.profiles_for_exam(db, "") == {}


# --------------------------------------------------------------------- ledger
def test_ledger_follows_the_question_order(env):
    with _db(env["app"]) as db:
        _seed(db, 2, key="c1")
        _seed(db, 1, key="a1")
        calibration.set_profile(db, EXAM, "c1", "פרופיל c1")
        db.commit()
        order = [("a1", "שאלה 1"), ("c1", "שאלה 9"), ("d1", "שאלה 11")]
        entries = calibration.ledger(db, EXAM, order=order)
        assert [entry["key"] for entry in entries] == ["a1", "c1", "d1"]
        assert entries[0]["count"] == 1
        assert entries[1]["count"] == 2
        assert entries[2]["count"] == 0  # uncorrected questions still appear
        assert entries[1]["title"] == "שאלה 9"
        assert entries[1]["profile"].profile == "פרופיל c1"
        assert entries[0]["profile"] is None
        assert entries[1]["corrections"][0]["teacher_points"] >= 1


def test_ledger_appends_corrected_questions_missing_from_the_order(env):
    with _db(env["app"]) as db:
        _seed(db, 1, key="c1")
        entries = calibration.ledger(db, EXAM, order=[("a1", "שאלה 1")])
        assert [entry["key"] for entry in entries] == ["a1", "c1"]
        assert entries[1]["count"] == 1


def test_ledger_without_order_uses_the_corrections(env):
    with _db(env["app"]) as db:
        _seed(db, 1, key="c1")
        entries = calibration.ledger(db, EXAM)
        assert [entry["key"] for entry in entries] == ["c1"]


# ----------------------------------------------------- mark injects the profile
def _wire_mark(monkeypatch, env, captured):
    exam_config.clear_cache()
    config = exam_config.load_exam(EXAM)
    version_key = config.versions[0].key

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
    return config


def _run_mark(env, config):
    with _db(env["app"]) as db:
        exam_service.mark(
            db,
            types.SimpleNamespace(id=env["assignment"]),
            types.SimpleNamespace(student_id=env["students"][0]),
            student_name="תלמיד",
            config=config,
        )


def test_mark_injects_the_profile_instead_of_the_raw_examples(env, monkeypatch):
    with _db(env["app"]) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="דוגמה גולמית",
            model_points=0, teacher_points=3, maximum=10,
        )
        calibration.set_profile(db, EXAM, "c1", "סגנון: ניקוד חלקי על כיוון נכון")
        db.commit()
    captured: dict = {}
    config = _wire_mark(monkeypatch, env, captured)
    _run_mark(env, config)
    block = captured["feedback"]["c1"]
    assert "סגנון: ניקוד חלקי על כיוון נכון" in block
    assert "סגנון הניקוד של המורה" in block
    assert "דוגמה גולמית" not in block  # the profile replaces the examples
    assert len(block) <= calibration.MAX_PROFILE_BLOCK_CHARS


def test_mark_falls_back_to_examples_when_there_is_no_profile(env, monkeypatch):
    with _db(env["app"]) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="דוגמה גולמית",
            model_points=0, teacher_points=3, note="הבין את הרעיון", maximum=10,
        )
        db.commit()
    captured: dict = {}
    config = _wire_mark(monkeypatch, env, captured)
    _run_mark(env, config)
    block = captured["feedback"]["c1"]
    assert "דוגמה גולמית" in block
    assert "הבין את הרעיון" in block
    assert len(block) <= feedback.MAX_BLOCK_CHARS


def test_mark_passes_no_feedback_when_neither_profile_nor_examples_exist(
    env, monkeypatch
):
    captured: dict = {}
    config = _wire_mark(monkeypatch, env, captured)
    _run_mark(env, config)
    assert captured["feedback"] == {}


# --------------------------------------------------------- background distill
def test_distill_in_thread_returns_false_when_already_in_flight(env, monkeypatch):
    called: list = []
    monkeypatch.setattr(
        calibration, "distill", lambda *a, **k: called.append(1)
    )
    with calibration._INFLIGHT_LOCK:
        calibration._INFLIGHT.add((EXAM, "c1"))
    try:
        started = calibration.distill_in_thread(
            env["app"].state.session_factory, EXAM, "c1"
        )
        assert started is False
        assert called == []
    finally:
        with calibration._INFLIGHT_LOCK:
            calibration._INFLIGHT.discard((EXAM, "c1"))


def test_distill_in_thread_distills_commits_and_clears_the_token(env, monkeypatch):
    _fake_summarize(monkeypatch, "פרופיל רקע")
    with _db(env["app"]) as db:
        _seed(db, 3)
    factory = env["app"].state.session_factory
    assert calibration.distill_in_thread(factory, EXAM, "c1") is True

    def done():
        with _db(env["app"]) as db:
            return calibration.profile_for(db, EXAM, "c1") is not None

    assert _wait_for(done), "the background distill did not store a profile"
    with _db(env["app"]) as db:
        profile = calibration.profile_for(db, EXAM, "c1")
        assert profile.profile == "פרופיל רקע"
    assert _wait_for(
        lambda: (EXAM, "c1") not in calibration._INFLIGHT
    ), "the in-flight token was not cleared"


def test_distill_in_thread_quota_wait_keeps_the_previous_profile(env, monkeypatch):
    _fake_summarize(monkeypatch, "ראשון")
    with _db(env["app"]) as db:
        _seed(db, 3)
        calibration.distill(db, EXAM, "c1")
        db.commit()

    def exhausted(*_a, **_k):
        raise llm_review.QuotaExhausted("out for today")

    monkeypatch.setattr(llm_review, "summarize_corrections", exhausted)
    with _db(env["app"]) as db:
        _seed(db, 1)  # make the profile stale so a distill is attempted
    factory = env["app"].state.session_factory
    assert calibration.distill_in_thread(factory, EXAM, "c1") is True
    assert _wait_for(lambda: (EXAM, "c1") not in calibration._INFLIGHT)
    with _db(env["app"]) as db:
        profile = calibration.profile_for(db, EXAM, "c1")
        assert profile.profile == "ראשון"  # unchanged by the quota wait


# ------------------------------------------------------------- the teacher UI
def test_calibration_page_shows_the_ledger_and_the_profile(client, app, env, monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: False)
    with _db(app) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="הגרשיים לא נכונים",
            model_points=0, teacher_points=3, note="הבין את הרעיון", maximum=10,
            submission_id=env["submissions"][0],
        )
        calibration.set_profile(db, EXAM, "c1", "תן ניקוד חלקי על כיוון נכון")
        db.commit()
    login(client, "llm-mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/assignments/{env['assignment']}/calibration")
    assert page.status_code == 200, page.text[:600]
    text = html.unescape(page.text)
    assert "סגנון הניקוד שלכם" in text
    assert "תן ניקוד חלקי על כיוון נכון" in text
    assert "הגרשיים לא נכונים" in text
    assert "נעול" in text  # the manual profile is locked


def test_calibration_page_lists_every_question_in_paper_order(client, app, env, monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: False)
    login(client, "llm-mori", TEACHER_PASSWORD)
    page = client.get(f"/teacher/assignments/{env['assignment']}/calibration")
    assert page.status_code == 200, page.text[:600]
    text = html.unescape(page.text)
    # All eleven questions appear even with no corrections at all.
    assert text.count("אין תיקונים לשאלה זו עדיין") == 11


def test_saving_a_profile_locks_it(client, app, env, monkeypatch):
    login(client, "llm-mori", TEACHER_PASSWORD)
    base = f"/teacher/assignments/{env['assignment']}/calibration"
    token = csrf(client, base)
    posted = client.post(
        f"{base}/profile",
        data={"csrf_token": token, "item_key": "c1", "profile": "כללי ניקוד מותאמים"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    with _db(app) as db:
        profile = calibration.profile_for(db, EXAM, "c1")
        assert profile is not None
        assert profile.profile == "כללי ניקוד מותאמים"
        assert profile.locked is True
        assert profile.updated_by == env["teacher"]


def test_saving_an_empty_profile_is_rejected(client, app, env, monkeypatch):
    login(client, "llm-mori", TEACHER_PASSWORD)
    base = f"/teacher/assignments/{env['assignment']}/calibration"
    token = csrf(client, base)
    posted = client.post(
        f"{base}/profile",
        data={"csrf_token": token, "item_key": "c1", "profile": "   "},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    with _db(app) as db:
        assert calibration.profile_for(db, EXAM, "c1") is None


def test_distill_now_runs_in_the_background(client, app, env, monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    captured: list = []
    monkeypatch.setattr(
        calibration, "distill_in_thread",
        lambda factory, name, key, **k: captured.append((name, key, k.get("force"))) or True,
    )
    with _db(app) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="א",
            model_points=0, teacher_points=3, submission_id=env["submissions"][0],
        )
        db.commit()
    login(client, "llm-mori", TEACHER_PASSWORD)
    base = f"/teacher/assignments/{env['assignment']}/calibration"
    token = csrf(client, base)
    posted = client.post(
        f"{base}/distill",
        data={"csrf_token": token, "item_key": "c1"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert captured == [(EXAM, "c1", True)]


def test_distill_now_without_corrections_is_rejected(client, app, env, monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    captured: list = []
    monkeypatch.setattr(
        calibration, "distill_in_thread",
        lambda *a, **k: captured.append(1) or True,
    )
    login(client, "llm-mori", TEACHER_PASSWORD)
    base = f"/teacher/assignments/{env['assignment']}/calibration"
    token = csrf(client, base)
    posted = client.post(
        f"{base}/distill",
        data={"csrf_token": token, "item_key": "c1"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert captured == []


def test_distill_now_when_the_model_is_off_is_rejected(client, app, env, monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: False)
    captured: list = []
    monkeypatch.setattr(
        calibration, "distill_in_thread",
        lambda *a, **k: captured.append(1) or True,
    )
    with _db(app) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="א",
            model_points=0, teacher_points=3, submission_id=env["submissions"][0],
        )
        db.commit()
    login(client, "llm-mori", TEACHER_PASSWORD)
    base = f"/teacher/assignments/{env['assignment']}/calibration"
    token = csrf(client, base)
    posted = client.post(
        f"{base}/distill",
        data={"csrf_token": token, "item_key": "c1"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert captured == []


# ------------------------------------------- the adjust route triggers distill
def test_adjusting_a_question_triggers_distill_at_the_threshold(client, app, env, monkeypatch):
    # Two corrections already stored; the third (this adjust) crosses the threshold.
    with _db(app) as db:
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="א",
            model_points=0, teacher_points=2, submission_id=env["submissions"][1],
        )
        feedback.record(
            db, exam_config=EXAM, item_key="c1", answer="ב",
            model_points=0, teacher_points=3, submission_id=env["submissions"][2],
        )
        db.commit()
    captured: list = []
    monkeypatch.setattr(
        calibration, "distill_in_thread",
        lambda factory, name, key, **k: captured.append((name, key)) or True,
    )
    s0 = env["submissions"][0]
    review = f"/teacher/submissions/{s0}"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/adjust",
        data={"csrf_token": token, "points": "4", "note": "הערה", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert captured == [(EXAM, "c1")]
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 3


def test_adjusting_below_the_threshold_does_not_distill(client, app, env, monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        calibration, "distill_in_thread",
        lambda *a, **k: captured.append(1) or True,
    )
    s0 = env["submissions"][0]
    review = f"/teacher/submissions/{s0}"
    login(client, "llm-mori", TEACHER_PASSWORD)
    token = csrf(client, review)
    posted = client.post(
        f"{review}/questions/c1/adjust",
        data={"csrf_token": token, "points": "4", "note": "הערה", "full": ""},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert captured == []  # only one correction so far
    with _db(app) as db:
        assert db.query(ExamLlmFeedback).count() == 1