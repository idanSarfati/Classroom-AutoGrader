"""The optional model pass for free-text explanations.

The pass must never decide marking on its own: every failure path returns
``None`` so the keyword rubric marks the paper. These tests use a fake HTTP
client, so no key, network or quota is involved.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from portal.exams import checker, feedback, llm_review
from portal.worksheets.schema import Item, Part

RULE = {
    "kind": "keywords",
    "points": 5,
    "llm": True,
    "groups": [
        {"label": "הסיבה", "points": 4, "words": ["מרכאות"]},
        {"label": "התוצאה", "points": 1, "words": ["מודפס"]},
    ],
}
ITEM = Item(
    key="c1",
    number=9,
    title="מצאו את השגיאה",
    intro="המטרה היא להדפיס Product name: Laptop",
    parts=(
        Part(
            key="bug",
            kind="code_plain",
            code='item_name = "Laptop"\nprint("Product name: item_name")',
            given=True,
        ),
        Part(key="explain", kind="short"),
    ),
)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://example.test/chat")
            raise httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class FakeClient:
    """Records calls and replays canned responses."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        return self.responses.pop(0)


def _reply(points: int, reason: str = "הסבר נכון") -> FakeResponse:
    content = json_module_dumps({"points": points, "reason": reason})
    return FakeResponse(payload={"choices": [{"message": {"content": content}}]})


def json_module_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _enable(monkeypatch, *, model: str = "test-model", key: str = "test-key") -> None:
    monkeypatch.setenv("EXAM_LLM_REVIEW", "1")
    monkeypatch.setenv("EXAM_LLM_MODEL", model)
    monkeypatch.setenv("EXAM_LLM_API_KEY", key)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No key leaks in from the shell, and no verdict survives a test."""
    for name in (
        "EXAM_LLM_REVIEW",
        "EXAM_LLM_MODEL",
        "EXAM_LLM_API_KEY",
        "EXAM_LLM_BASE_URL",
        "EXAM_LLM_TIMEOUT",
        "GROQ_API_KEY",
        "GROQ_API_KEYS",
        "GROQ_MODEL",
        "GROQ_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    llm_review._cache.clear()
    yield
    llm_review._cache.clear()


# --------------------------------------------------------------------- config


def test_the_pass_is_off_without_a_switch_model_and_key() -> None:
    assert llm_review.enabled() is False
    assert llm_review.grade(RULE, "המשתנה בתוך המרכאות") is None


def test_groq_settings_are_accepted_as_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("EXAM_LLM_REVIEW", "yes")
    monkeypatch.setenv("GROQ_MODEL", "llama-test")
    monkeypatch.setenv("GROQ_API_KEYS", "first-key,second-key")
    config = llm_review.settings()
    assert config["enabled"] is True
    assert config["model"] == "llama-test"
    assert config["key"] == "first-key"


# ------------------------------------------------------------------- verdicts


def test_a_model_verdict_becomes_points_note_and_review(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(_reply(4, "מזהה את הסיבה אך לא את התוצאה"))
    verdict = llm_review.grade(RULE, "המשתנה בתוך המרכאות", {"title": "q"}, client=client)
    assert verdict is not None
    points, note, review = verdict
    assert points == 4
    assert "המודל" in note and "התוצאה" in note
    assert review is True
    sent = client.calls[0]
    assert sent["url"].endswith("/chat/completions")
    assert sent["headers"]["Authorization"] == "Bearer test-key"
    assert sent["json"]["response_format"] == {"type": "json_object"}
    # The rubric itself travels in the prompt, not just the question.
    prompt = sent["json"]["messages"][1]["content"]
    assert "4 נקודות" in prompt and "1 נקודות" in prompt


def test_full_marks_do_not_ask_for_review(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(_reply(5, "כל הרכיבים"))
    points, _note, review = llm_review.grade(RULE, "הסבר", {}, client=client)
    assert (points, review) == (5, False)


def test_a_network_failure_falls_back(monkeypatch) -> None:
    _enable(monkeypatch)

    class Broken:
        def post(self, *_args, **_kwargs):
            raise ConnectionError("offline")

    assert llm_review.grade(RULE, "הסבר", {}, client=Broken()) is None


def test_a_reply_that_is_not_json_falls_back(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(
        FakeResponse(payload={"choices": [{"message": {"content": "כן, נראה לי 4"}}]})
    )
    assert llm_review.grade(RULE, "הסבר", {}, client=client) is None


def test_points_outside_the_rubric_are_refused(monkeypatch) -> None:
    _enable(monkeypatch)
    assert llm_review.grade(RULE, "הסבר", {}, client=FakeClient(_reply(9))) is None
    assert llm_review.grade(RULE, "הסבר", {}, client=FakeClient(_reply(-1))) is None


def test_a_400_retries_without_response_format(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(FakeResponse(status_code=400), _reply(3))
    points, _note, _review = llm_review.grade(RULE, "הסבר", {}, client=client)
    assert points == 3
    assert len(client.calls) == 2
    assert "response_format" not in client.calls[1]["json"]


def test_the_same_answer_is_asked_once(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(_reply(4), _reply(1))
    context = {"title": "q9"}
    first = llm_review.grade(RULE, "אותו הסבר", context, client=client)
    second = llm_review.grade(RULE, "אותו הסבר", context, client=client)
    assert first == second
    assert len(client.calls) == 1


# ------------------------------------------------------------------- checker


def test_the_checker_uses_the_model_for_a_flagged_explanation(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    seen: list[dict] = []

    def fake_grade(rule, answer, context=None, *, client=None):
        seen.append({"rule": rule, "answer": answer, "context": context})
        return 4, "הסבר נבדק על ידי המודל: חסרה התוצאה", True

    monkeypatch.setattr(llm_review, "grade", fake_grade)
    points, note, review = checker._mark(
        ITEM, RULE, "המשתנה נמצא בתוך המרכאות"
    )
    assert (points, review) == (4, True)
    assert "המודל" in note
    # The buggy program is sent with the question, not just the answer.
    assert "Product name: item_name" in seen[0]["context"]["code"]
    assert seen[0]["rule"]["points"] == 5


def test_the_checker_falls_back_to_the_rubric_when_the_model_is_silent(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(llm_review, "grade", lambda *args, **kwargs: None)
    points, note, review = checker._mark(
        ITEM, RULE, "המשתנה נמצא בתוך המרכאות ומודפס כמו שהוא"
    )
    assert (points, review) == (5, False)
    assert "המודל" not in note


def test_a_check_without_the_flag_never_calls_the_model(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)

    def explode(*args, **kwargs):
        raise AssertionError("the model must not see an un-flagged check")

    monkeypatch.setattr(llm_review, "grade", explode)
    points, _note, review = checker._mark(
        ITEM, {**RULE, "llm": False}, "המשתנה בתוך המרכאות ומודפס"
    )
    assert (points, review) == (5, False)


# --------------------------------------------------------------- whole item
PARTS = [
    {
        "part": "explain",
        "label": "ההסבר",
        "max": 5,
        "guidance": "4 נקודות על הסיבה, 1 על התוצאה",
        "answer": "הקלט הוא מחרוזת ולכן age + 1 קורס",
    },
    {
        "part": "code",
        "label": "הקוד המתוקן",
        "max": 5,
        "guidance": "1 ריצה, 2 פלט, 2 המרה",
        "answer": "age = int(input('Enter your age: '))\nprint('Next year you will be', age + 1)",
        "evidence": [
            {
                "stdin": "20\n",
                "ok": True,
                "timed_out": False,
                "exit_code": 0,
                "stdout": "Next year you will be 21",
                "stderr": "",
            }
        ],
    },
]

CONTEXT = {
    "title": "שאלה 9 · מצאו את השגיאה",
    "intro": "הסבירו ותקנו את הקוד",
    "code": 'age = input("Enter your age: ")\nnext_year = age + 1',
    "guidelines": "בדוק את שני החלקים בנפרד",
    "reference": 'age = int(input("Enter your age: "))',
}


def _item_reply(points_by_part: dict[str, int]) -> FakeResponse:
    content = json_module_dumps(
        {
            "parts": [
                {"part": key, "points": value, "reason": "כי כך"}
                for key, value in points_by_part.items()
            ],
            "reason": "סיכום",
        }
    )
    return FakeResponse(payload={"choices": [{"message": {"content": content}}]})


def test_an_item_verdict_is_per_part(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(_item_reply({"explain": 4, "code": 5}))
    verdict = llm_review.grade_item(PARTS, CONTEXT, client=client)
    assert verdict == {"explain": (4, "כי כך"), "code": (5, "כי כך")}
    prompt = client.calls[0]["json"]["messages"][1]["content"]
    assert "בדוק את שני החלקים בנפרד" in prompt
    assert "4 נקודות על הסיבה" in prompt
    # The sandbox result travels with the code part, not just the source.
    assert "Next year you will be 21" in prompt
    assert "age = int(input(" in prompt


def test_an_item_reply_must_cover_every_sent_part(monkeypatch) -> None:
    _enable(monkeypatch)
    missing = FakeClient(_item_reply({"explain": 4}))
    assert llm_review.grade_item(PARTS, CONTEXT, client=missing) is None
    extra = FakeClient(_item_reply({"explain": 4, "code": 5, "more": 1}))
    assert llm_review.grade_item(PARTS, CONTEXT, client=extra) is None


def test_an_item_point_above_the_part_maximum_is_refused(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(_item_reply({"explain": 6, "code": 5}))
    assert llm_review.grade_item(PARTS, CONTEXT, client=client) is None


def test_an_item_without_guidelines_never_calls_the_model(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient()
    assert llm_review.grade_item(PARTS, {"title": "q"}, client=client) is None
    assert client.calls == []


def test_the_same_item_is_asked_once(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(_item_reply({"explain": 4, "code": 5}))
    first = llm_review.grade_item(PARTS, CONTEXT, client=client)
    second = llm_review.grade_item(PARTS, CONTEXT, client=client)
    assert first == second
    assert len(client.calls) == 1


# --------------------------------------------------------------- whole paper
def _paper_reply(points_by_item: dict[str, dict[str, int]]) -> FakeResponse:
    content = json_module_dumps(
        {
            "items": [
                {
                    "item": key,
                    "parts": [
                        {"part": p, "points": v, "reason": "כי כך"}
                        for p, v in parts.items()
                    ],
                    "reason": "סיכום",
                }
                for key, parts in points_by_item.items()
            ]
        }
    )
    return FakeResponse(payload={"choices": [{"message": {"content": content}}]})


PAPER_ITEMS = [
    {"item": "c1", "context": CONTEXT, "parts": PARTS},
    {"item": "c2", "context": CONTEXT, "parts": PARTS},
]


def test_a_paper_verdict_covers_every_item_in_one_call(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(
        _paper_reply({"c1": {"explain": 4, "code": 5}, "c2": {"explain": 5, "code": 3}})
    )
    verdict = llm_review.grade_paper(PAPER_ITEMS, client=client)
    assert verdict == {
        "c1": {"explain": (4, "כי כך"), "code": (5, "כי כך")},
        "c2": {"explain": (5, "כי כך"), "code": (3, "כי כך")},
    }
    # One round trip for the whole paper, and both questions travel together.
    assert len(client.calls) == 1
    prompt = client.calls[0]["json"]["messages"][1]["content"]
    assert "=== שאלה c1 ===" in prompt and "=== שאלה c2 ===" in prompt
    assert '"items"' in prompt


def test_a_paper_reply_missing_an_item_omits_just_that_item(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(_paper_reply({"c1": {"explain": 4, "code": 5}}))
    verdict = llm_review.grade_paper(PAPER_ITEMS, client=client)
    assert verdict == {"c1": {"explain": (4, "כי כך"), "code": (5, "כי כך")}}


def test_a_paper_item_above_its_maximum_is_omitted(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(
        _paper_reply({"c1": {"explain": 6, "code": 5}, "c2": {"explain": 5, "code": 3}})
    )
    verdict = llm_review.grade_paper(PAPER_ITEMS, client=client)
    # c1 broke parity (6 > 5) so it is dropped; c2 still stands on its own.
    assert verdict == {"c2": {"explain": (5, "כי כך"), "code": (3, "כי כך")}}


def test_a_paper_reply_that_is_not_a_list_falls_back(monkeypatch) -> None:
    _enable(monkeypatch)
    content = json_module_dumps({"items": "לא רשימה"})
    client = FakeClient(
        FakeResponse(payload={"choices": [{"message": {"content": content}}]})
    )
    assert llm_review.grade_paper(PAPER_ITEMS, client=client) is None


def test_a_paper_item_without_guidelines_never_calls_the_model(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient()
    bad = [{"item": "c1", "context": {"title": "q"}, "parts": PARTS}]
    assert llm_review.grade_paper(bad, client=client) is None
    assert client.calls == []


def test_the_same_paper_is_asked_once(monkeypatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(
        _paper_reply({"c1": {"explain": 4, "code": 5}, "c2": {"explain": 5, "code": 3}})
    )
    first = llm_review.grade_paper(PAPER_ITEMS, client=client)
    second = llm_review.grade_paper(PAPER_ITEMS, client=client)
    assert first == second
    assert len(client.calls) == 1


def test_grade_paper_is_off_without_the_switch(monkeypatch) -> None:
    client = FakeClient()
    assert llm_review.grade_paper(PAPER_ITEMS, client=client) is None
    assert client.calls == []


# ------------------------------------------------------- item in the checker
OPEN_ITEM = Item(
    key="c1",
    number=9,
    title="מצאו את השגיאה",
    intro="הסבירו מהי השגיאה וכתבו את הקוד המתוקן",
    points=10,
    parts=(
        Part(
            key="bug",
            kind="code_plain",
            code='age = input("Enter your age: ")\nnext_year = age + 1',
            given=True,
        ),
        Part(key="explain", kind="short"),
        Part(key="code", kind="code_plain"),
    ),
)

OPEN_RULE = {
    "reference": {
        "code": 'age = int(input("Enter your age: "))\nnext_year = age + 1',
        "stdin": "20\n",
    },
    "llm_review": {
        "guidelines": "בדוק את ההסבר ואת הקוד לפי המחוון",
        "parts": [
            {"part": "explain", "label": "ההסבר", "max": 5, "guidance": "4+1"},
            {"part": "code", "label": "הקוד המתוקן", "max": 5, "guidance": "1+2+2"},
        ],
    },
    "checks": [
        {
            "kind": "keywords",
            "part": "explain",
            "points": 5,
            "groups": [
                {"label": "סיבה", "points": 4, "words": ["מחרוזת"]},
                {"label": "תוצאה", "points": 1, "words": ["typeerror"]},
            ],
        },
        {
            "kind": "program",
            "part": "code",
            "points": 5,
            "stdin": "20\n",
            "criteria": [{"label": "רץ", "points": 5, "all_runs": True}],
        },
    ],
}

OPEN_VALUES = {
    "explain": "הקלט הוא מחרוזת",
    "code": "age = int(input('Enter your age: '))\nprint('Next year you will be', age + 1)",
}


def _sandbox_ok(*_args, **_kwargs):
    return {
        "ok": True,
        "stdout": "Next year you will be 21",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
    }


def _review(item, rule, values, *, feedback=None):
    """Stage one item's check, then run the single-pass model review on it."""
    result, payload = checker._check_item(item, rule, values, feedback=feedback)
    if payload is not None:
        checker._review_open_items(
            [(result, item, rule, payload)],
            feedback=feedback, progress=None, total=1,
        )
    return result


def test_the_item_review_replaces_the_part_marks(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    # Force the per-item fallback so this exercises the same marking contract
    # the batched call holds (the batch path has its own tests).
    monkeypatch.setattr(llm_review, "grade_paper", lambda *a, **k: None)
    seen: list[dict] = []

    def fake_item(parts, context=None, *, client=None):
        seen.append({"parts": parts, "context": context})
        return {"explain": (5, "מזהה את הסיבה"), "code": (3, "הפלט חסר")}

    monkeypatch.setattr(llm_review, "grade_item", fake_item)
    result = _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    assert result.earned == 8
    assert result.needs_review is True
    assert "ההסבר: 5/5 נקודות. מזהה את הסיבה" in result.notes
    assert "הקוד המתוקן: 3/5 נקודות. הפלט חסר" in result.notes
    sent = seen[0]
    by_part = {part["part"]: part for part in sent["parts"]}
    assert by_part["code"]["evidence"][0]["stdout"] == "Next year you will be 21"
    assert sent["context"]["guidelines"].startswith("בדוק את ההסבר")
    assert "age = int(input" in sent["context"]["reference"]


def test_the_item_falls_back_to_the_rules_when_the_model_is_silent(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    monkeypatch.setattr(llm_review, "grade_paper", lambda *a, **k: None)
    monkeypatch.setattr(llm_review, "grade_item", lambda *args, **kwargs: None)
    result = _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    assert result.earned == 9
    assert result.needs_review is False
    assert "נקודות" not in result.notes or "המודל" not in result.notes


def test_a_rubric_that_disagrees_with_the_checks_never_reaches_the_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)

    def explode(*args, **kwargs):
        raise AssertionError("a drifted rubric must not be sent")

    monkeypatch.setattr(llm_review, "grade_paper", explode)
    monkeypatch.setattr(llm_review, "grade_item", explode)
    drifted = {
        **OPEN_RULE,
        "llm_review": {
            **OPEN_RULE["llm_review"],
            "parts": [
                {"part": "explain", "label": "ההסבר", "max": 6, "guidance": ""},
                {"part": "code", "label": "הקוד", "max": 5, "guidance": ""},
            ],
        },
    }
    result = _review(OPEN_ITEM, drifted, OPEN_VALUES)
    assert result.earned == 9


def test_a_disabled_review_keeps_the_deterministic_marks(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: False)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)

    def explode(*args, **kwargs):
        raise AssertionError("the model is off")

    monkeypatch.setattr(llm_review, "grade_paper", explode)
    monkeypatch.setattr(llm_review, "grade_item", explode)
    result = _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    assert result.earned == 9


def test_the_checker_threads_teacher_feedback_into_the_model_context(monkeypatch) -> None:
    """A bounded calibration block for this question reaches the model context."""
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    monkeypatch.setattr(llm_review, "grade_paper", lambda *a, **k: None)
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
    seen: list[dict] = []

    def fake_item(parts, context=None, *, client=None):
        seen.append(context or {})
        return {"explain": (5, "מזהה את הסיבה"), "code": (3, "הפלט חסר")}

    monkeypatch.setattr(llm_review, "grade_item", fake_item)
    _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES, feedback={"c1": block})
    assert seen[0]["feedback"] == block
    assert len(block) <= feedback.MAX_BLOCK_CHARS

    # A question with no stored correction carries no feedback key at all.
    seen.clear()
    _review(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    assert "feedback" not in seen[0]


# ------------------------------------------------- whole paper in the checker
def test_the_whole_paper_is_graded_in_one_call(monkeypatch) -> None:
    """The core speed fix: every open question shares ONE model round trip."""
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    item2 = replace(OPEN_ITEM, key="c2")
    calls = {"paper": 0, "item": 0}

    def fake_paper(items, *, client=None):
        calls["paper"] += 1
        assert {i["item"] for i in items} == {"c1", "c2"}
        return {i["item"]: {"explain": (5, "טוב"), "code": (5, "טוב")} for i in items}

    def fake_item(*a, **k):
        calls["item"] += 1
        return {"explain": (5, "טוב"), "code": (5, "טוב")}

    monkeypatch.setattr(llm_review, "grade_paper", fake_paper)
    monkeypatch.setattr(llm_review, "grade_item", fake_item)
    r1, p1 = checker._check_item(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    r2, p2 = checker._check_item(item2, OPEN_RULE, OPEN_VALUES)
    ticks: list[tuple] = []
    checker._review_open_items(
        [(r1, OPEN_ITEM, OPEN_RULE, p1), (r2, item2, OPEN_RULE, p2)],
        feedback=None,
        progress=lambda i, t, label: ticks.append((i, t, label)),
        total=2,
    )
    assert calls == {"paper": 1, "item": 0}
    assert r1.earned == 10 and r2.earned == 10
    assert r1.llm_reviewed and r2.llm_reviewed
    # One progress tick announces the single pass over all the open questions.
    assert len(ticks) == 1 and ticks[0][2].startswith("המודל בודק")


def test_an_item_the_batch_misses_falls_back_to_its_own_call(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    item2 = replace(OPEN_ITEM, key="c2")
    calls = {"paper": 0, "item": 0}

    def fake_paper(items, *, client=None):
        calls["paper"] += 1
        return {"c1": {"explain": (5, "טוב"), "code": (5, "טוב")}}  # c2 missing

    def fake_item(parts, context=None, *, client=None):
        calls["item"] += 1
        return {"explain": (4, "כמעט"), "code": (5, "טוב")}

    monkeypatch.setattr(llm_review, "grade_paper", fake_paper)
    monkeypatch.setattr(llm_review, "grade_item", fake_item)
    r1, p1 = checker._check_item(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    r2, p2 = checker._check_item(item2, OPEN_RULE, OPEN_VALUES)
    checker._review_open_items(
        [(r1, OPEN_ITEM, OPEN_RULE, p1), (r2, item2, OPEN_RULE, p2)],
        feedback=None, progress=None, total=2,
    )
    assert calls == {"paper": 1, "item": 1}
    assert r1.earned == 10 and r1.llm_reviewed
    assert r2.earned == 9 and r2.llm_reviewed  # 4 + 5 from the fallback


def test_a_quota_stop_flags_every_open_item(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    monkeypatch.setattr(checker, "_run_program", _sandbox_ok)
    item2 = replace(OPEN_ITEM, key="c2")

    def exhausted(*a, **k):
        raise llm_review.QuotaExhausted("out for today")

    monkeypatch.setattr(llm_review, "grade_paper", exhausted)
    monkeypatch.setattr(llm_review, "grade_item", exhausted)
    r1, p1 = checker._check_item(OPEN_ITEM, OPEN_RULE, OPEN_VALUES)
    r2, p2 = checker._check_item(item2, OPEN_RULE, OPEN_VALUES)
    checker._review_open_items(
        [(r1, OPEN_ITEM, OPEN_RULE, p1), (r2, item2, OPEN_RULE, p2)],
        feedback=None, progress=None, total=2,
    )
    assert r1.earned == 9 and r2.earned == 9  # the deterministic marks survive
    assert r1.llm_quota_blocked and r2.llm_quota_blocked
    assert not r1.llm_reviewed and not r2.llm_reviewed
# ----------------------------------------------------------------- prompt size
# The provider rejects requests that are too big (413). The prompt is bounded so
# a long answer, a long sandbox trace, or a whole paper full of open items can
# never grow into such a rejection - clipping or refusing beats a 413 that would
# leave every open part unmarked.


def test_item_prompt_clips_an_overlong_answer() -> None:
    huge = "א" * (llm_review.MAX_ANSWER_CHARS + 5000)
    parts = [dict(part) for part in PARTS]
    parts[0]["answer"] = huge
    prompt = llm_review._item_prompt(parts, CONTEXT)
    assert len(prompt) <= llm_review.MAX_PROMPT_CHARS
    assert huge not in prompt  # clipped, not dumped whole
    assert "### " in prompt  # the per-part headings survive


def test_evidence_output_is_bounded() -> None:
    runs = [
        {
            "stdin": "1\n",
            "ok": True,
            "timed_out": False,
            "exit_code": 0,
            "stdout": "x" * 100000,
            "stderr": "",
        }
    ]
    text = llm_review._format_evidence(runs)
    assert len(text) <= llm_review.MAX_EVIDENCE_CHARS


def test_an_oversized_paper_is_split_into_byte_fitting_calls(monkeypatch) -> None:
    """A paper too heavy for one request is chunked, never sent as a 413.

    Hebrew answers weigh ~2 bytes per character, so the byte ceiling - not the
    char ceiling - is what keeps the body inside the provider's payload limit.
    Every batch must fit both ceilings, and the merged verdict must still cover
    every item of the paper.
    """
    _enable(monkeypatch)

    def _big_part(part: dict) -> dict:
        clone = dict(part)
        clone["answer"] = "א" * 50000
        if part.get("part") == "code":
            clone["evidence"] = [
                {
                    "stdin": "20\n",
                    "ok": True,
                    "timed_out": False,
                    "exit_code": 0,
                    "stdout": "x" * 100000,
                    "stderr": "",
                }
            ]
        return clone

    # Many open items, each with a huge (clipped) answer and a huge run trace:
    # even after the per-part clipping the batched prompt crosses the ceilings.
    items = [
        {
            "item": f"q{index}",
            "context": dict(CONTEXT),
            "parts": [_big_part(part) for part in PARTS],
        }
        for index in range(10)
    ]
    # Sanity: the assembled one-shot prompt really is over the cap.
    assert len(llm_review._paper_prompt(items)) > llm_review.MAX_PROMPT_CHARS

    class SplitClient(FakeClient):
        """Answers each batch with a verdict for exactly the items it carries."""

        def post(self, url, headers=None, json=None):
            self.calls.append(
                {"url": url, "headers": headers or {}, "json": json or {}}
            )
            prompt = json["messages"][1]["content"]
            assert len(prompt) <= llm_review.MAX_PROMPT_CHARS
            assert len(prompt.encode("utf-8")) <= llm_review.MAX_PROMPT_BYTES
            keys = re.findall(r"=== שאלה (\w+) ===", prompt)
            assert keys, "every batch must carry at least one item"
            return _paper_reply({key: {"explain": 5, "code": 5} for key in keys})

    client = SplitClient()
    verdict = llm_review.grade_paper(items, client=client)
    assert len(client.calls) > 1  # genuinely split, not one oversized request
    assert verdict is not None
    assert set(verdict) == {f"q{index}" for index in range(10)}
    for marks in verdict.values():
        assert marks == {"explain": (5, "כי כך"), "code": (5, "כי כך")}


def test_a_hebrew_prompt_over_the_byte_ceiling_is_refused(monkeypatch) -> None:
    """Under the char ceiling but over the byte ceiling: still refused.

    This is the exact shape that produced Groq 413s before the byte check: a
    Hebrew prompt of ~12k characters is ~24KB of UTF-8 body.
    """
    _enable(monkeypatch)
    client = FakeClient()
    prompt = "א" * (llm_review.MAX_PROMPT_BYTES // 2 + 100)
    assert len(prompt) <= llm_review.MAX_PROMPT_CHARS
    assert len(prompt.encode("utf-8")) > llm_review.MAX_PROMPT_BYTES
    verdict = llm_review._ask(
        llm_review.settings(), prompt, max_tokens=100, client=client
    )
    assert verdict is None
    assert client.calls == []  # refused before any request was sent


