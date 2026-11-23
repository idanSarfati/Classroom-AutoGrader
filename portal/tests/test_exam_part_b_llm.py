"""Part b (tracing) is model-reviewed against the same per-line points.

These tests use the real exam definition and check file. They prove three
things the parity rule depends on:

* every tracing question carries an ``llm_review`` block whose maximum equals
  the sum of its deterministic line points (and part a never carries one);
* a tracing answer reaches the model with the given code and the guidelines,
  and the model's points replace the rules;
* an exhausted quota keeps the deterministic score and flags the paper as
  waiting, and a drifted rubric is never sent.
"""
from __future__ import annotations

import pytest

from portal.exams import checker, llm_review
from portal.exams import config as exam_config
from portal.exams.paper import load_paper

EXAM = "units_1_3"
B_KEYS = ("b1", "b2", "b3")
A_KEYS = ("a1", "a2", "a3", "a4", "a5")


def _rules(version_key: str) -> dict:
    config = exam_config.load_exam(EXAM)
    return checker.version_rules(checker.load_checks(config), version_key)


def _item(version_key: str, item_key: str):
    paper = load_paper(exam_config.load_exam(EXAM).version(version_key))
    return next(item for item in paper.items if item.key == item_key)


def _review(item, rule, values, *, feedback=None):
    """Stage one item's check, then run the single-pass model review on it."""
    result, payload = checker._check_item(item, rule, values, feedback=feedback)
    if payload is not None:
        checker._review_open_items(
            [(result, item, rule, payload)],
            feedback=feedback, progress=None, total=1,
        )
    return result


def test_every_tracing_rubric_matches_its_deterministic_lines() -> None:
    config = exam_config.load_exam(EXAM)
    for version in config.versions:
        rules = _rules(version.key)
        for key in B_KEYS:
            rule = rules[key]
            check = rule["checks"][0]
            assert check["kind"] == "lines"
            line_points = sum(int(e["points"]) for e in check["lines"])
            block = rule.get("llm_review")
            assert isinstance(block, dict), f"{version.key}/{key} has no llm_review"
            assert str(block.get("guidelines") or "").strip()
            parts = block["parts"]
            assert len(parts) == 1 and parts[0]["part"] == "response"
            # The model maximum, the check points and the line sum are one number.
            assert int(parts[0]["max"]) == int(check["points"]) == line_points
            assert str(parts[0].get("guidance") or "").strip()
        for key in A_KEYS:
            assert "llm_review" not in rules[key], f"{version.key}/{key} must stay deterministic"


def test_a_tracing_answer_is_reviewed_with_the_code_and_guidelines(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)
    # Force the per-item fallback so this exercises the same marking contract
    # the batched call holds (the batch path has its own tests).
    monkeypatch.setattr(llm_review, "grade_paper", lambda *a, **k: None)
    seen: list[dict] = []

    def fake_item(parts, context=None, *, client=None):
        seen.append({"parts": parts, "context": context})
        return {"response": (7, "שורה אחת נכונה")}

    monkeypatch.setattr(llm_review, "grade_item", fake_item)
    rule = _rules("a")["b2"]
    item = _item("a", "b2")
    result = _review(item, rule, {"response": "a = 19\nb = 15"})

    assert result.llm_eligible is True
    assert result.llm_reviewed is True
    assert result.llm_quota_blocked is False
    assert result.earned == 7  # the model's points replace the deterministic 12
    sent = seen[0]
    assert sent["parts"][0]["part"] == "response"
    assert sent["parts"][0]["max"] == 12
    assert sent["parts"][0]["answer"] == "a = 19\nb = 15"
    # The given snippet and the teacher's tracing guidelines travel with it.
    assert "a = 15" in sent["context"]["code"]
    assert "מעקב ריצה" in sent["context"]["guidelines"]


def test_a_tracing_answer_waits_when_the_quota_is_exhausted(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)

    def exhausted(*_a, **_k):
        raise llm_review.QuotaExhausted("every provider reached its daily quota")

    # The whole-paper pass is the one that runs out of quota now.
    monkeypatch.setattr(llm_review, "grade_paper", exhausted)
    monkeypatch.setattr(llm_review, "grade_item", exhausted)
    rule = _rules("a")["b2"]
    item = _item("a", "b2")
    result = _review(item, rule, {"response": "a = 19\nb = 15"})

    # The deterministic marks survive, and the paper is flagged for a retry.
    assert result.earned == 12
    assert result.llm_reviewed is False
    assert result.llm_quota_blocked is True


def test_a_drifted_tracing_rubric_never_reaches_the_model(monkeypatch) -> None:
    monkeypatch.setattr(llm_review, "enabled", lambda: True)

    def explode(*_a, **_k):
        raise AssertionError("a drifted rubric must not be sent")

    monkeypatch.setattr(llm_review, "grade_paper", explode)
    monkeypatch.setattr(llm_review, "grade_item", explode)
    rule = _rules("a")["b2"]
    drifted = {
        **rule,
        "llm_review": {
            **rule["llm_review"],
            "parts": [{"part": "response", "label": "הפלט", "max": 99, "guidance": ""}],
        },
    }
    item = _item("a", "b2")
    result = _review(item, drifted, {"response": "a = 19\nb = 15"})
    assert result.earned == 12
    assert result.llm_reviewed is False