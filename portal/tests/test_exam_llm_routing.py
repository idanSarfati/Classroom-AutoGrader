"""The exam model pass through the engine's key router.

These tests drive the *production* path (no injected client): a fake router
hands out endpoints and a fake HTTP client replays canned responses, so no key,
network or real quota is involved. They prove three things the single-key
transport could not: a per-minute 429 rotates to the next key, an exhausted
daily quota raises a distinct signal instead of silently falling back, and the
checker turns that signal into a "waiting" paper that keeps its deterministic
score.
"""
from __future__ import annotations

import json

import pytest

from portal.exams import checker, feedback, llm_review
from portal.worksheets.schema import Item, Part
from grading_engine.llm_router import DailyQuotaExhausted


# --------------------------------------------------------------------- fakes
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://example.test/chat")
            raise httpx.HTTPStatusError(
                "boom", request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        return self.responses.pop(0)

    def close(self):
        pass


class Endpoint:
    def __init__(
        self, name="groq", *, is_fallback=False, model="m", key="k",
        base_url="https://api.groq.com/openai/v1",
    ):
        self.name = name
        self.base_url = base_url
        self.model = model
        self.api_key = key
        self.is_fallback = is_fallback


class Decision:
    def __init__(self, endpoint, key_index=0):
        self.endpoint = endpoint
        self.key_index = key_index


class FakeRouter:
    def __init__(self, decisions, *, has_fallback=False):
        self.decisions = list(decisions)
        self.has_fallback = has_fallback
        self.groq_daily_exhausted = False
        self.successes = []
        self.rate_limited = []
        self.daily = []

    def pick(self):
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def report_success(self, key_index):
        self.successes.append(key_index)

    def report_rate_limited(self, key_index, retry_after=None):
        self.rate_limited.append(key_index)
        return 0.0

    def report_daily_exhausted(self, key_index, retry_after=None):
        self.daily.append(key_index)
        self.groq_daily_exhausted = True
        return 0.0

    def report_unknown_model(self, key_index, model):
        return False

    def describe(self):
        return "fake router"


def _verdict(points=5, reason="נכון"):
    content = json.dumps({"points": points, "reason": reason}, ensure_ascii=False)
    return FakeResponse(payload={"choices": [{"message": {"content": content}}]})


def _item_verdict():
    content = json.dumps(
        {"parts": [{"part": "explain", "points": 5, "reason": "מזהה את הסיבה"}]},
        ensure_ascii=False,
    )
    return FakeResponse(payload={"choices": [{"message": {"content": content}}]})


def _rate_limited(detail):
    return FakeResponse(status_code=429, payload={"error": {"message": detail}})


PARTS = [
    {
        "part": "explain",
        "label": "ההסבר",
        "max": 5,
        "guidance": "4 על הסיבה, 1 על התוצאה",
        "answer": "הקלט הוא מחרוזת ולכן צריך להמיר ל-int",
    }
]
CONTEXT = {"title": "מצאו את השגיאה", "guidelines": "בדוק לפי המחוון"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
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


def _wire(monkeypatch, router, client):
    monkeypatch.setattr(llm_review, "_router", lambda: router)
    monkeypatch.setattr(llm_review, "_client", lambda timeout: client)


# ------------------------------------------------------------------ transport
def test_a_healthy_key_serves_the_request_and_is_reported(monkeypatch):
    router = FakeRouter([Decision(Endpoint(), key_index=0)])
    client = FakeClient(_item_verdict())
    _wire(monkeypatch, router, client)
    verdict = llm_review.grade_item(PARTS, CONTEXT)
    assert verdict == {"explain": (5, "מזהה את הסיבה")}
    assert router.successes == [0]
    assert client.calls[0]["headers"]["Authorization"] == "Bearer k"


def test_a_per_minute_limit_rotates_to_the_next_key(monkeypatch):
    router = FakeRouter(
        [Decision(Endpoint(), key_index=0), Decision(Endpoint(), key_index=1)]
    )
    client = FakeClient(
        _rate_limited("Rate limit reached for requests per minute"),
        _item_verdict(),
    )
    _wire(monkeypatch, router, client)
    verdict = llm_review.grade_item(PARTS, CONTEXT)
    assert verdict is not None
    assert router.rate_limited == [0]
    assert router.successes == [1]
    assert len(client.calls) == 2


def test_a_daily_quota_with_no_fallback_raises_quota_exhausted(monkeypatch):
    router = FakeRouter([Decision(Endpoint(), key_index=0)], has_fallback=False)
    client = FakeClient(_rate_limited("Rate limit reached for tokens per day (TPD)"))
    _wire(monkeypatch, router, client)
    with pytest.raises(llm_review.QuotaExhausted):
        llm_review.grade_item(PARTS, CONTEXT)
    assert router.daily == [0]


def test_both_providers_out_of_daily_quota_raises_quota_exhausted(monkeypatch):
    router = FakeRouter(
        [
            Decision(Endpoint(), key_index=0),
            Decision(Endpoint("gemini", is_fallback=True), key_index=-2),
        ],
        has_fallback=True,
    )
    client = FakeClient(
        _rate_limited("tokens per day (TPD) exceeded"),
        _rate_limited("You exceeded your per-day quota"),
    )
    _wire(monkeypatch, router, client)
    with pytest.raises(llm_review.QuotaExhausted):
        llm_review.grade_item(PARTS, CONTEXT)
    assert len(router.daily) == 2


def test_the_router_running_dry_raises_quota_exhausted(monkeypatch):
    router = FakeRouter([DailyQuotaExhausted("every key is out for today")])
    client = FakeClient()
    _wire(monkeypatch, router, client)
    with pytest.raises(llm_review.QuotaExhausted):
        llm_review.grade_item(PARTS, CONTEXT)
    assert client.calls == []


def test_a_network_drop_falls_back_to_the_rules(monkeypatch):
    import httpx

    class Broken:
        def post(self, *a, **k):
            raise httpx.ConnectError("down")

        def close(self):
            pass

    router = FakeRouter([Decision(Endpoint()) for _ in range(8)])
    _wire(monkeypatch, router, Broken())
    # Every attempt drops; the request gives up and returns None (not quota).
    assert llm_review.grade_item(PARTS, CONTEXT) is None


def test_a_groq_request_runs_the_model_in_non_thinking_mode(monkeypatch):
    """The proven engine method: Qwen3 on Groq must not emit a thinking trace.

    Without ``reasoning_effort="none"`` + ``reasoning_format="hidden"`` the model
    thinks past the read timeout and the paper silently falls back to the rules.
    """
    router = FakeRouter([Decision(Endpoint(), key_index=0)])
    client = FakeClient(_item_verdict())
    _wire(monkeypatch, router, client)
    assert llm_review.grade_item(PARTS, CONTEXT) is not None
    body = client.calls[0]["json"]
    assert body["reasoning_effort"] == "none"
    assert body["reasoning_format"] == "hidden"
    # Raw httpx posts the field at the top level, never nested under extra_body.
    assert "extra_body" not in body


def test_a_fallback_request_omits_the_groq_reasoning_fields(monkeypatch):
    """Gemini/Cloudflare answer HTTP 400 on unknown names, so the knobs are Groq-only."""
    router = FakeRouter(
        [
            Decision(
                Endpoint(
                    "gemini",
                    is_fallback=True,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                ),
                key_index=-2,
            )
        ],
        has_fallback=True,
    )
    client = FakeClient(_item_verdict())
    _wire(monkeypatch, router, client)
    assert llm_review.grade_item(PARTS, CONTEXT) is not None
    body = client.calls[0]["json"]
    assert "reasoning_effort" not in body
    assert "reasoning_format" not in body


def test_the_single_key_path_also_suppresses_groq_reasoning(monkeypatch):
    """With no router pool the injected-client send still runs Qwen3 in instruct mode."""
    client = FakeClient(_item_verdict())
    assert llm_review.grade_item(PARTS, CONTEXT, client=client) is not None
    body = client.calls[0]["json"]
    assert body["reasoning_effort"] == "none"
    assert body["reasoning_format"] == "hidden"


def test_the_default_timeout_matches_the_grading_engine(monkeypatch):
    # No EXAM_LLM_TIMEOUT set (the _clean fixture deletes it): default 180s.
    assert llm_review.settings()["timeout"] == 180.0
    monkeypatch.setenv("EXAM_LLM_TIMEOUT", "500")
    assert llm_review.settings()["timeout"] == 300.0  # capped
    monkeypatch.setenv("EXAM_LLM_TIMEOUT", "5")
    assert llm_review.settings()["timeout"] == 5.0  # honoured


# ------------------------------------------------- 413 / dead-key fall-through
def _too_large():
    return FakeResponse(
        status_code=413, payload={"error": {"message": "Payload Too Large"}}
    )


def _unauthorized():
    return FakeResponse(
        status_code=401, payload={"error": {"message": "Unauthorized"}}
    )


def _gemini_decision(key_index=-2):
    return Decision(
        Endpoint(
            "gemini",
            is_fallback=True,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        key_index=key_index,
    )


def test_a_413_cools_the_key_and_hands_over_to_the_next_provider(monkeypatch):
    """Groq's body limit rejected the paper; Gemini takes larger bodies, so the
    request must continue there instead of dying on the first 413."""
    router = FakeRouter(
        [Decision(Endpoint(), key_index=0), _gemini_decision()],
        has_fallback=True,
    )
    client = FakeClient(_too_large(), _item_verdict())
    _wire(monkeypatch, router, client)
    assert llm_review.grade_item(PARTS, CONTEXT) is not None
    assert router.rate_limited == [0]
    assert router.successes == [-2]
    assert len(client.calls) == 2


def test_a_401_parks_the_dead_key_and_tries_the_next_provider(monkeypatch):
    """A revoked key is parked for the day, not probed again on every paper."""
    router = FakeRouter(
        [Decision(Endpoint(), key_index=0), _gemini_decision()],
        has_fallback=True,
    )
    client = FakeClient(_unauthorized(), _item_verdict())
    _wire(monkeypatch, router, client)
    assert llm_review.grade_item(PARTS, CONTEXT) is not None
    assert router.daily == [0]
    assert router.successes == [-2]
    assert len(client.calls) == 2


def test_a_dead_tertiary_key_is_parked_and_the_paper_falls_back(monkeypatch):
    """The Cloudflare 401 that blocked the auto-check: park it, then the rules
    mark the paper (no provider left) instead of looping on the dead key."""
    router = FakeRouter(
        [
            Decision(
                Endpoint(
                    "cloudflare",
                    base_url="https://api.cloudflare.com/client/v4/accounts/x/ai",
                ),
                key_index=-1000,
            ),
            DailyQuotaExhausted("nothing healthy left"),
        ]
    )
    client = FakeClient(_unauthorized())
    _wire(monkeypatch, router, client)
    # The pool ran dry on an auth error, not a quota: the honest HTTP failure
    # surfaces (and _ask turns it into a rules fallback, not a "waiting" paper).
    assert llm_review.grade_item(PARTS, CONTEXT) is None
    assert router.daily == [-1000]
    assert len(client.calls) == 1
    reason = llm_review.recent_failures(1)[0]["reason"]
    assert "quota" not in reason.casefold()


def test_all_keys_parked_on_401_surfaces_the_http_error_not_quota(monkeypatch):
    import httpx

    router = FakeRouter(
        [Decision(Endpoint(), key_index=0), DailyQuotaExhausted("all parked")]
    )
    client = FakeClient(_unauthorized())
    _wire(monkeypatch, router, client)
    with pytest.raises(httpx.HTTPStatusError):
        llm_review._send_routed(
            client, llm_review.settings(), {"model": "m", "messages": []}
        )
    assert router.daily == [0]


def test_a_daily_quota_still_raises_quota_exhausted_after_a_429(monkeypatch):
    """The honest-error branch must not swallow a genuine daily-quota stop."""
    router = FakeRouter(
        [Decision(Endpoint(), key_index=0), DailyQuotaExhausted("all parked")]
    )
    client = FakeClient(_rate_limited("tokens per day (TPD) exceeded"))
    _wire(monkeypatch, router, client)
    with pytest.raises(llm_review.QuotaExhausted):
        llm_review._send_routed(
            client, llm_review.settings(), {"model": "m", "messages": []}
        )


def test_a_dead_legacy_fallback_key_fails_fast(monkeypatch):
    import httpx

    router = FakeRouter([Decision(Endpoint("gemini", is_fallback=True), key_index=-1)])
    client = FakeClient(_unauthorized())
    _wire(monkeypatch, router, client)
    with pytest.raises(httpx.HTTPStatusError):
        llm_review._send_routed(
            client, llm_review.settings(), {"model": "m", "messages": []}
        )
    assert len(client.calls) == 1  # no pointless re-probe of the same dead key


def test_failed_routes_log_provider_status_and_payload_size(monkeypatch, caplog):
    """The debug trail the server journal needs: provider, status, body bytes."""
    import logging

    router = FakeRouter(
        [Decision(Endpoint(), key_index=0), _gemini_decision()],
        has_fallback=True,
    )
    client = FakeClient(_too_large(), _item_verdict())
    _wire(monkeypatch, router, client)
    with caplog.at_level(logging.WARNING, logger="portal.exams.llm_review"):
        assert llm_review.grade_item(PARTS, CONTEXT) is not None
    assert "provider=groq" in caplog.text
    assert "status=413" in caplog.text
    assert "payload=" in caplog.text


# -------------------------------------------------------------------- checker
OPEN_ITEM = Item(
    key="c1",
    number=9,
    title="מצאו את השגיאה",
    intro="הסבירו מהי השגיאה וכתבו את הקוד המתוקן",
    points=10,
    parts=(
        Part(key="bug", kind="code_plain", code='age = input("x")\nnext_year = age + 1', given=True),
        Part(key="explain", kind="short"),
        Part(key="code", kind="code_plain"),
    ),
)
OPEN_RULE = {
    "llm_review": {
        "guidelines": "בדוק את ההסבר ואת הקוד לפי המחוון",
        "parts": [
            {"part": "explain", "label": "ההסבר", "max": 5, "guidance": "4+1"},
            {"part": "code", "label": "הקוד המתוקן", "max": 5, "guidance": "1+2+2"},
        ],
    },
    "checks": [
        {
            "kind": "keywords", "part": "explain", "points": 5,
            "groups": [
                {"label": "סיבה", "points": 4, "words": ["מחרוזת"]},
                {"label": "תוצאה", "points": 1, "words": ["typeerror"]},
            ],
        },
        {
            "kind": "program", "part": "code", "points": 5, "stdin": "20\n",
            "criteria": [{"label": "רץ", "points": 5, "all_runs": True}],
        },
    ],
}
OPEN_VALUES = {
    "explain": "הקלט הוא מחרוזת",
    "code": "age = int(input('x'))\nprint('Next year you will be', age + 1)",
}


def _sandbox_ok(*_a, **_k):
    return {"ok": True, "stdout": "Next year you will be 21", "stderr": "", "exit_code": 0, "timed_out": False}


def _report(result):
    return checker.ExamReport(
        exam="units_1_3", version="a", version_label="גרסה א'",
        score=result.earned, maximum=result.points, questions=[result],
    )


def _review(item, rule, values, *, feedback=None):
    """Stage one item's check, then run the single-pass model review on it."""
    result, payload = checker._check_item(item, rule, values, feedback=feedback)
    if payload is not None:
        checker._review_open_items(
            [(result, item, rule, payload)],
            feedback=feedback, progress=None, total=1,
        )
    return result


def test_quota_exhaustion_keeps_the_score_and_marks_the_paper_waiting(monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)

    def exhausted(*_a, **_k):
        raise llm_review.QuotaExhausted("out for today")

    monkeypatch.setattr(llm_review, "grade_paper", exhausted)
    monkeypatch.setattr(llm_review, "grade_item", exhausted)
    result = _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    # The deterministic marks survive (4 explanation + 5 program = 9).
    assert result.earned == 9
    assert result.llm_eligible is True
    assert result.llm_reviewed is False
    assert result.llm_quota_blocked is True
    assert _report(result).llm_state == "waiting"


def test_a_model_verdict_marks_the_paper_reviewed(monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    # The whole-paper pass returns a verdict for this item in one call.
    monkeypatch.setattr(
        llm_review, "grade_paper",
        lambda *a, **k: {"c1": {"explain": (5, "מזהה את הסיבה"), "code": (3, "הפלט חסר")}},
    )
    result = _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    assert result.earned == 8
    assert result.llm_reviewed is True
    assert result.llm_quota_blocked is False
    assert _report(result).llm_state == "reviewed"


def test_a_disabled_model_leaves_the_paper_off(monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: False)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    monkeypatch.setattr(llm_review, "grade_paper", lambda *a, **k: pytest.fail("model is off"))
    monkeypatch.setattr(llm_review, "grade_item", lambda *a, **k: pytest.fail("model is off"))
    result = _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    assert result.llm_eligible is False
    assert _report(result).llm_state == "off"


def test_a_non_quota_failure_is_failed_not_waiting(monkeypatch):
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    monkeypatch.setattr(llm_review, "grade_paper", lambda *a, **k: None)
    monkeypatch.setattr(llm_review, "grade_item", lambda *a, **k: None)
    result = _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    assert result.earned == 9
    assert result.llm_quota_blocked is False
    assert _report(result).llm_state == "failed"


# ------------------------------------------------- fallback-key counter shim
def _strip_fallback_counters():
    """Return the engine class to its unpatched state (order-independent tests)."""
    from grading_engine.llm_router import _FallbackKeyState

    for name in ("consecutive_429s", "served"):
        if hasattr(_FallbackKeyState, name):
            delattr(_FallbackKeyState, name)


def test_fallback_key_counters_survive_a_429_and_a_success():
    """A fallback (Gemini) 429 used to crash the review with AttributeError.

    ``_FallbackKeyState`` ships without ``consecutive_429s``/``served`` but the
    router does ``+= 1`` on them for pool keys. The portal shim adds the two
    counters; this drives the real router to prove the crash is gone.
    """
    from grading_engine.llm_router import LLMRouter

    _strip_fallback_counters()
    router = LLMRouter(
        groq_keys=["g"],
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
        fallback_keys=["f1"],
    )
    # Without the shim the very first pool-key 429 raises on the += read.
    with pytest.raises(AttributeError):
        router.report_rate_limited(-2, retry_after=0.1)

    llm_review._patch_fallback_key_state()
    wait = router.report_rate_limited(-2, retry_after=0.1)
    assert wait >= 0
    router.report_success(-2)
    key = router._fallback_keys[0]
    assert key.served == 1
    assert key.consecutive_429s == 0  # a success clears the strikes
    # A daily park on a pool key must not raise either.
    router.report_daily_exhausted(-2, retry_after=0.1)


def test_the_production_router_build_repairs_the_counters(monkeypatch):
    """``_router()`` itself must apply the shim, not only the helper in isolation."""
    from types import SimpleNamespace

    _strip_fallback_counters()
    settings = SimpleNamespace(
        groq_api_key="g",
        groq_api_keys="",
        groq_model="m",
        groq_base_url="https://api.groq.com/openai/v1",
        llm_fallback_provider="",
        llm_fallback_model="",
        llm_fallback_api_key="f1",
        llm_fallback_base_url="",
        llm_fallback_api_keys="",
        llm_tertiary_provider="",
        llm_tertiary_model="",
        llm_tertiary_api_key="",
        llm_tertiary_base_url="",
        cloudflare_account_id="",
    )
    monkeypatch.setattr(llm_review, "_exam_router_settings", lambda: settings)
    llm_review.reset_routing()
    router = llm_review._router()
    assert router is not None
    # These two calls crashed before the shim was wired into the build path.
    router.report_rate_limited(-2, retry_after=0.1)
    router.report_success(-2)
    assert router._fallback_keys[0].served == 1


def test_the_open_item_prompt_carries_the_syntax_leniency_rule():
    """The teacher's partial-credit policy is part of every open-item prompt."""
    prompt = llm_review._item_prompt(PARTS, CONTEXT)
    assert "חריג שגיאת תחביר קלה" in prompt
    assert "1-2" in prompt
    # The strict output rule defers to the exception, and the exception explicitly
    # overrides both rule 4 and the part guidance so a missing comma is not zeroed.
    assert "למעט החריג המפורש בכלל 5" in prompt
    assert "גובר על כלל 4" in prompt
    # A missing print comma is named as a *minor* slip, not a critical error, and
    # placing the variable outside the quotes counts as conceptual concatenation.
    assert "פסיק חסר בין ארגומנטים של print" in prompt
    assert "שגיאה קריטית" in prompt
    assert "חיבור נכון רעיונית" in prompt


def test_the_open_item_prompt_awards_partial_credit_for_a_vague_explanation():
    """A vague-but-right-direction explanation earns ~half the cause points, not 0."""
    prompt = llm_review._item_prompt(PARTS, CONTEXT)
    # The teacher's example: "the quotes are wrong" points at the right area even
    # though it never says the variable sits inside the string.
    assert "הגרשיים לא נכונים" in prompt
    assert "כמחצית מנקודות הסיבה" in prompt
    assert "2 מתוך 4" in prompt
    # A general/short answer is not zeroed while the direction is right...
    assert "אל תאפס הסבר רק משום שהוא כללי או מקוצר" in prompt
    # ...but a reversed or wrong diagnosis still gets 0 (rule 3 wins).
    assert "אבחנה הפוכה או שגויה מקבלת 0 לפי כלל 3" in prompt


# ------------------------------------------------------- teacher calibration
def test_the_open_item_prompt_renders_the_teacher_feedback_block():
    """A bounded calibration block travels with the prompt when one is supplied."""
    block = feedback.render_block(
        [
            {
                "answer": "הגרשיים לא נכונים",
                "note": "הבין את הרעיון, חסר פסיק",
                "teacher_points": 3,
                "model_points": 0,
                "maximum": 10,
            }
        ]
    )
    prompt = llm_review._item_prompt(PARTS, {**CONTEXT, "feedback": block})
    assert block in prompt
    assert "הגרשיים לא נכונים" in prompt
    # A question the teacher never corrected gets no calibration header at all.
    plain = llm_review._item_prompt(PARTS, CONTEXT)
    assert "הערות המורה מהגשות קודמות" not in plain


def test_grade_item_sends_the_bounded_feedback_block(monkeypatch):
    """The block reaches the model in the user prompt, never the system prompt."""
    block = feedback.render_block(
        [
            {
                "answer": "הגרשיים לא נכונים",
                "note": "הבין את הרעיון",
                "teacher_points": 3,
                "model_points": 0,
                "maximum": 10,
            }
        ]
    )
    assert len(block) <= feedback.MAX_BLOCK_CHARS
    router = FakeRouter([Decision(Endpoint(), key_index=0)])
    client = FakeClient(_item_verdict())
    _wire(monkeypatch, router, client)
    verdict = llm_review.grade_item(PARTS, {**CONTEXT, "feedback": block})
    assert verdict == {"explain": (5, "מזהה את הסיבה")}
    messages = client.calls[0]["json"]["messages"]
    user = next(m["content"] for m in messages if m["role"] == "user")
    system = next(m["content"] for m in messages if m["role"] == "system")
    assert block in user
    # The system prompt stays tiny, so calibration can never bloat it into a 429.
    assert "הערות המורה מהגשות קודמות" not in system