"""Response parsing: fence/prose/truncation repair, payload coercion, and the deduction-breakdown audit.

Extracted verbatim from :mod:`src.llm_evaluator` (the former monolith).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
)

from config.settings import settings
from src.code_checks import checks_for_assignment, scan_submission
from src.late_policy import apply_late_penalty
from src.llm_router import DailyQuotaExhausted
from src.models import (
    AssignmentConfig,
    AssignmentSpec,
    GradingResult,
    SpecSection,
)

from src.evaluator.errors import (
    LLMJSONParsingError,
)
from src.seams import seam_fn

logger = logging.getLogger(__name__)

# Length of the raw completion echoed into error messages / logs.
_ERROR_SNIPPET_CHARS = 300


# Normalised-key aliases: once schema enforcement is off the model drifts on key
# names. Keys are normalised with :func:`_norm_key` (lowercase, runs of
# non-alphanumerics collapsed to ``_``) and looked up with and without
# underscores, so ``studentName``/``Student Name``/``student-name`` all match.
_GRADING_KEY_ALIASES: dict[str, str] = {
    "student_name": "student_name",
    "student": "student_name",
    "name": "student_name",
    "pupil": "student_name",
    "score": "score",
    "grade": "score",
    "final_grade": "score",
    "total_grade": "score",
    "points": "score",
    "feedback_hebrew": "feedback_hebrew",
    "feedback": "feedback_hebrew",
    "hebrew_feedback": "feedback_hebrew",
    "comment": "feedback_hebrew",
    "comments": "feedback_hebrew",
    "summary_hebrew": "feedback_hebrew",
    "task_evaluations": "task_evaluations",
    "task_evaluation": "task_evaluations",
    "tasks": "task_evaluations",
    "task_results": "task_evaluations",
    "evaluations": "task_evaluations",
    "results": "task_evaluations",
    "deduction_breakdown": "deduction_breakdown",
    "deductions": "deduction_breakdown",
    "deduction": "deduction_breakdown",
    "deductions_breakdown": "deduction_breakdown",
    "score_deductions": "deduction_breakdown",
    "point_deductions": "deduction_breakdown",
    "deduction_items": "deduction_breakdown",
    "breakdown": "deduction_breakdown",
    "why_points_deducted": "deduction_breakdown",
}


# Key drift for the deduction breakdown (loose modes only; strict mode has the
# schema). ``points``/``points_lost``/``deducted`` and reason-ish synonyms all
# land on the canonical DeductionItem keys.
_DEDUCTION_KEY_ALIASES: dict[str, str] = {
    "task_name": "task_name",
    "task": "task_name",
    "name": "task_name",
    "points_deducted": "points_deducted",
    "points_lost": "points_deducted",
    "points": "points_deducted",
    "deducted": "points_deducted",
    "deduction": "points_deducted",
    "reason": "reason",
    "why": "reason",
    "explanation": "reason",
    "note": "reason",
    "notes": "reason",
    "feedback": "reason",
}


_TASK_KEY_ALIASES: dict[str, str] = {
    "task_name": "task_name",
    "task": "task_name",
    "name": "task_name",
    "title": "task_name",
    "student_answer_found": "student_answer_found",
    "student_answer": "student_answer_found",
    "answer_found": "student_answer_found",
    "answer": "student_answer_found",
    "student_text": "student_answer_found",
    "status": "status",
    "verdict": "status",
    "state": "status",
    "result": "status",
    "notes": "notes",
    "note": "notes",
    "comment": "notes",
    "feedback": "notes",
    "observation": "notes",
    "explanation": "notes",
}


# Non-schema spellings of the four allowed verdicts (normalised keys).
_STATUS_ALIASES: dict[str, str] = {
    "correct": "CORRECT",
    "fully_correct": "CORRECT",
    "full_mark": "CORRECT",
    "good": "CORRECT",
    "ok": "CORRECT",
    "complete": "CORRECT",
    "passed": "CORRECT",
    "valid": "CORRECT",
    "partially_correct": "PARTIALLY_CORRECT",
    "partiallycorrect": "PARTIALLY_CORRECT",
    "partial": "PARTIALLY_CORRECT",
    "partially": "PARTIALLY_CORRECT",
    "mostly_correct": "PARTIALLY_CORRECT",
    "minor_issues": "PARTIALLY_CORRECT",
    "incomplete": "PARTIALLY_CORRECT",
    "incorrect": "INCORRECT",
    "wrong": "INCORRECT",
    "invalid": "INCORRECT",
    "error": "INCORRECT",
    "failed": "INCORRECT",
    "fail": "INCORRECT",
    "not_correct": "INCORRECT",
    "missing": "MISSING",
    "not_found": "MISSING",
    "no_answer": "MISSING",
    "none": "MISSING",
    "empty": "MISSING",
    "blank": "MISSING",
    "skipped": "MISSING",
    "not_attempted": "MISSING",
    "absent": "MISSING",
}


# Keys under which the model sometimes wraps the whole result.
_WRAPPER_KEYS: tuple[str, ...] = (
    "grading_result",
    "result",
    "grading",
    "evaluation",
    "response",
    "output",
    "data",
)


def _extract_content(response: Any) -> str:
    """Pull the assistant text out of an OpenAI-compatible chat completion."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        # Groq can return an empty choice list together with an error payload
        # (e.g. when a request is dropped); surface it instead of crashing on
        # a None dereference.
        raise LLMJSONParsingError(
            f"Groq returned no choices in the completion response: {response!r}"
        )
    content = getattr(getattr(choices[0], "message", None), "content", None)
    if not content or not content.strip():
        raise LLMJSONParsingError("Groq returned an empty completion message.")
    return content


# A line that is nothing but a code fence, optionally tagged with a language.
_FENCE_LINE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*[A-Za-z0-9_+.-]*[ \t]*$")


# ``{"a": 1,}`` / ``[1, 2,]`` - the classic hand-written-JSON slip.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


_CLOSE_TO_OPEN = {"}": "{", "]": "["}


def _strip_code_fences(text: str) -> str:
    """Drop markdown fence lines wrapping the payload.

    Only whole lines that consist of a fence (plus optional language tag) are
    removed, so fence characters inside string values survive untouched.
    """
    if "`" not in text and "~" not in text:
        return text.strip()
    kept = [line for line in text.splitlines() if not _FENCE_LINE_RE.match(line)]
    return "\n".join(kept).strip()


def _json_span(text: str) -> str | None:
    """Slice the first balanced ``{...}``/``[...]`` block out of ``text``.

    Quotes and escapes are tracked, so braces inside string values (a student's
    ``print("hi {name}")`` snippet, for instance) do not unbalance the scan.
    """
    openings = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not openings:
        return None
    start = min(openings)
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSE_TO_OPEN.values():
            stack.append(char)
        elif char in _CLOSE_TO_OPEN:
            if stack and stack[-1] == _CLOSE_TO_OPEN[char]:
                stack.pop()
                if not stack:
                    return text[start : index + 1]
            # A stray closer outside any container: ignore, keep scanning.
    # Unbalanced (truncated answer): fall back to the last closer we ever saw.
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end > start else None


def _close_truncated(text: str) -> str | None:
    """Close the containers (and string) a cut-off answer never finished.

    ``{"score": 85, "task_evaluations": [{"notes": "worke`` becomes
    ``{"score": 85, "task_evaluations": [{"notes": "worke"}]}`` - partial, but
    often enough valid to grade. Returns ``None`` when nothing was open.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSE_TO_OPEN.values():
            stack.append(char)
        elif char in _CLOSE_TO_OPEN and stack and stack[-1] == _CLOSE_TO_OPEN[char]:
            stack.pop()
    if not stack and not in_string:
        return None
    suffix = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return text + ('"' if in_string else "") + suffix


def repair_json(raw_text: str | None) -> str:
    """Best-effort cleanup of an LLM answer into a bare JSON document.

    Handles the shapes that break a naive ``json.loads``: markdown code fences,
    prose around the payload, trailing commas and (as a last resort) output that
    was cut off mid-container. The return value is *not* guaranteed to be valid
    JSON - callers still have to parse and validate it.
    """
    if not raw_text:
        return ""
    text = _strip_code_fences(str(raw_text))
    # Strip a BOM / zero-width junk that occasionally prefixes the payload.
    text = text.lstrip("\ufeff\u200b").strip()
    span = _json_span(text)
    if span is not None:
        text = span
    return _TRAILING_COMMA_RE.sub(r"\1", text).strip()


def _repair_candidates(content: str) -> list[str]:
    """Progressively repaired variants of ``content``, best guess first."""
    repaired = repair_json(content)
    candidates = [content.strip(), repaired]
    for source in (repaired, content.strip()):
        if not source:
            continue
        closed = _close_truncated(source)
        if closed is not None:
            candidates.append(closed)
    # De-duplicate while keeping the original order.
    return [text for text in dict.fromkeys(candidates) if text]


def _parse_payload(content: str | None) -> GradingResult:
    """Turn a raw completion into a validated :class:`GradingResult`.

    Tries, in order: the answer as-is, then every repaired variant, and for
    syntactically valid JSON a key/value coercion pass. Validation always runs
    against the untouched model, so nothing but the shape of the payload is
    ever relaxed.
    """
    text = (content or "").strip()
    if not text:
        raise LLMJSONParsingError("Groq returned an empty completion message.")

    failures: list[str] = []
    for candidate in _repair_candidates(text):
        try:
            return _audit_deductions(GradingResult.model_validate_json(candidate))
        except ValidationError as exc:
            failures.append(f"schema: {_one_line(exc)}")
        try:
            data = json.loads(candidate)
        except ValueError as exc:
            failures.append(f"json: {_one_line(exc)}")
            continue
        try:
            return _audit_deductions(
                GradingResult.model_validate(_coerce_payload(data))
            )
        except ValidationError as exc:
            failures.append(f"coerced: {_one_line(exc)}")

    # Nothing parsed: log enough of the raw answer to debug the real cause.
    logger.error(
        "Unparseable Groq payload (%d chars, %d variants): %r",
        len(text),
        len(failures),
        text[:_ERROR_SNIPPET_CHARS],
    )
    raise LLMJSONParsingError(
        "Groq did not return a valid GradingResult payload: "
        + " | ".join(dict.fromkeys(failures))[:_ERROR_SNIPPET_CHARS]
        + f" | content={text[:_ERROR_SNIPPET_CHARS]!r}"
    )


def _one_line(exc: BaseException) -> str:
    """Flatten a multi-line Pydantic/JSON error into a short one-liner."""
    return " ".join(str(exc).split())[:_ERROR_SNIPPET_CHARS]


def _audit_deductions(result: GradingResult) -> GradingResult:
    """Warn (never fail) when the itemised deductions do not add up.

    The prompt asks for ``sum(points_deducted) == 100 - score`` so the teacher
    can trust the breakdown, but a small arithmetic slip must not throw away an
    otherwise valid grade: it is logged and the result is returned as-is.
    """
    itemised = sum(item.points_deducted for item in result.deduction_breakdown)
    expected = 100 - result.score
    if itemised != expected:
        logger.warning(
            "Deduction breakdown sums to %d but 100 - score is %d (%s, score=%d)",
            itemised,
            expected,
            result.student_name or "?",
            result.score,
        )
    return result


def _parse_result(response: Any) -> GradingResult:
    """Validate the completion payload against the GradingResult model."""
    return _parse_payload(_extract_content(response))


def _norm_key(value: Any) -> str:
    """Normalise a key or verdict for alias lookups (lowercase, ``_``-joined)."""
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _canonical_key(value: Any, aliases: dict[str, str]) -> str:
    """Resolve ``value`` against ``aliases`` (underscore-insensitive)."""
    norm = _norm_key(value)
    if norm in aliases:
        return aliases[norm]
    return aliases.get(norm.replace("_", ""), norm)


def _rename_keys(payload: Any, aliases: dict[str, str]) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {_canonical_key(key, aliases): value for key, value in payload.items()}


def _coerce_score(value: Any) -> int | None:
    """Best-effort int 0-100 from ``85``, ``85.0``, ``"85/100"``, ``"ציון: 85"``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
        if not match:
            return None
        number = float(match.group(0).replace(",", "."))
    return max(0, min(100, int(round(number))))


def _coerce_points(value: Any) -> int | None:
    """Best-effort non-negative point count from ``3``, ``-3``, ``"3 points"``.

    Unlike :func:`_coerce_score` this does NOT clamp at 0 before returning, so a
    negative sign (the model's way of writing "-3 points off") is still visible
    to the caller, which normalises it with ``abs()``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    match = re.search(r"-?\d+", str(value))
    if not match:
        return None
    return int(match.group(0))


def _coerce_status(value: Any) -> str | None:
    norm = _norm_key(value)
    if not norm:
        return None
    if norm in _STATUS_ALIASES:
        return _STATUS_ALIASES[norm]
    return _STATUS_ALIASES.get(norm.replace("_", ""))


def _coerce_task(item: Any, index: int) -> dict[str, Any] | None:
    """Normalise one task evaluation; ``None`` for entries that are not objects."""
    if not isinstance(item, dict):
        return None
    task = _rename_keys(item, _TASK_KEY_ALIASES)
    answer = task.get("student_answer_found")
    task["task_name"] = str(task.get("task_name") or f"Task {index + 1}")
    task["student_answer_found"] = "" if answer is None else str(answer)
    task["notes"] = "" if task.get("notes") is None else str(task["notes"])
    status = _coerce_status(task.get("status"))
    if status is None:
        # Unknown/absent verdict: infer from the evidence the model did give.
        status = (
            "PARTIALLY_CORRECT" if task["student_answer_found"].strip() else "MISSING"
        )
    task["status"] = status
    return task


def _coerce_deduction(item: Any) -> dict[str, Any] | None:
    """Normalise one deduction entry (object or free-text line) to schema keys."""
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        # "Task 3: -4 hardcoded string" -> name / points / reason. The name is
        # matched lazily up to a colon, so digits inside the name (Task 3) are
        # not mistaken for the point count.
        match = re.match(
            r"^\s*([^:]{1,80}?)\s*:\s*[-−]?\s*(\d+)\s*(?:points?|pts)?\b\s*(.*)$",
            text,
            re.IGNORECASE,
        )
        if match:
            return {
                "task_name": match.group(1).strip() or "General",
                "points_deducted": _coerce_points(match.group(2)) or 0,
                "reason": match.group(3).strip(" -–:()") or text,
            }
        return {
            "task_name": "General",
            "points_deducted": abs(_coerce_points(text) or 0),
            "reason": text,
        }
    if not isinstance(item, dict):
        return None
    entry = _rename_keys(item, _DEDUCTION_KEY_ALIASES)
    reason = entry.get("reason")
    # "-3" and "3 points" both mean "3 points off"; abs() normalises the sign.
    points = _coerce_points(entry.get("points_deducted"))
    entry["points_deducted"] = abs(points) if points is not None else 0
    entry["task_name"] = str(entry.get("task_name") or "General")
    entry["reason"] = "" if reason is None else str(reason)
    return entry


def _coerce_deductions(value: Any) -> list[dict[str, Any]]:
    """Normalise the deduction breakdown from any shape the model produced."""
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str) or isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [
        entry
        for entry in (_coerce_deduction(item) for item in value)
        if entry is not None
    ]


def _coerce_payload(data: Any) -> Any:
    """Map a loosely-shaped payload onto the GradingResult field names."""
    if isinstance(data, list):
        # Some answers put the result object inside a one-element list.
        objects = [item for item in data if isinstance(item, dict)]
        if len(objects) == 1:
            data = objects[0]
    if not isinstance(data, dict):
        return data

    payload = _rename_keys(data, _GRADING_KEY_ALIASES)
    for key in _WRAPPER_KEYS:
        inner = payload.get(key)
        if not isinstance(inner, dict):
            continue
        inner_keys = {_canonical_key(k, _GRADING_KEY_ALIASES) for k in inner}
        if inner_keys & {"score", "feedback_hebrew", "task_evaluations"}:
            payload = _rename_keys(inner, _GRADING_KEY_ALIASES)
            break

    score = _coerce_score(payload.get("score"))
    if score is not None:
        payload["score"] = score
    feedback = payload.get("feedback_hebrew")
    payload["feedback_hebrew"] = "" if feedback is None else str(feedback)
    payload["student_name"] = str(payload.get("student_name") or "")
    # Always present, even when the loose-mode answer omitted it entirely.
    payload["deduction_breakdown"] = _coerce_deductions(
        payload.get("deduction_breakdown")
    )

    evaluations = payload.get("task_evaluations")
    if isinstance(evaluations, dict):
        # e.g. {"1": {...}, "2": {...}} instead of a list.
        evaluations = list(evaluations.values())
    if isinstance(evaluations, list):
        payload["task_evaluations"] = [
            task
            for task in (
                _coerce_task(item, index) for index, item in enumerate(evaluations)
            )
            if task is not None
        ]
    return payload

# ----------------------------------------------------------------------
# Seam wiring
# ----------------------------------------------------------------------
# Tests monkeypatch these on the ``src.llm_evaluator`` facade. The bodies above
# reference them as plain globals; Python resolves globals at call time,
# so rebinding them here to facade-resolving proxies honours a patch
# without touching any call site.
_parse_result = seam_fn("src.llm_evaluator", "_parse_result", _parse_result)
