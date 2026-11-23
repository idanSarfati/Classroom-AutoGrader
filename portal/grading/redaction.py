"""Student-safe projection of rubric files.

Rubric JSON is teacher data. It contains the answers (``correct_answer``,
``expected_output``, ``corrected_code``) and, in some files, the answer is
also embedded in the question prose itself. Sending the file to a browser - or
even sending a cleaned copy of the raw dict - is therefore not safe.

This module never forwards the source structure. It builds a **new,
allowlisted dict** containing only what a student is allowed to read:

* the rubric/title, topic and total points;
* sections, their weights and names;
* questions, their type and text;
* the broken code for debugging questions (needed to answer them);
* requirements/criteria for code-writing questions.

Redaction rules
---------------

1. Sensitive keys are never emitted: ``correct_answer``, ``expected_output``,
   ``corrected_code`` and the other names in :data:`SENSITIVE_KEYS`.
2. Question prose and requirements are scrubbed of the sensitive values that
   belong to the same question. This catches answers embedded in text, such as
   ``"התשובה: first name"``, which a pure key allowlist would leak.
3. Titles, topics and section names are scrubbed of **all** sensitive values.
   Single-character answers (usually multiple-choice labels such as ``"א"``)
   are deliberately not scrubbed: replacing every occurrence of one letter
   would destroy normal Hebrew text, and the answer field itself is already
   gone. Every other answer is scrubbed.
4. ``code`` is visible on purpose: it is the subject of debugging and
   output-tracing questions. It is **omitted** only when it is identical to a
   sensitive value, because then the code *is* the answer.

The function has no dependency on ``src/`` or on the portal's web layer; it
works on the pydantic rubric objects returned by the adapter as well as on
plain dicts, which keeps it easy to test against every file in
``assignments/`` and ``configs/``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# Keys whose values must never reach a student. The list is intentionally
# broader than the fields in today's rubric schema so a new answer-bearing
# field does not silently leak before the allowlist is updated.
SENSITIVE_KEYS = frozenset(
    {
        "answer",
        "correct_answer",
        "corrected_code",
        "expected_output",
        "reference_answer",
        "solution",
        "solution_code",
    }
)

# Hebrew for "removed". Visible to students, so it must read naturally.
REDACTION_MARKER = "[הוסר]"

# One-character answers are usually option labels ("א", "ב"); scrubbing every
# occurrence would wreck ordinary Hebrew prose. The answer field is still
# removed, so nothing is leaked through the rubric structure.
_MIN_SCRUB_LENGTH = 2


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return a plain dict for a pydantic model or an existing mapping."""
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    raise TypeError(f"Cannot redact rubric of type {type(value)!r}")


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_strings(item)


def _values_of(node: Mapping[str, Any]) -> tuple[str, ...]:
    """Sensitive strings of one question/section, longest first."""
    found: list[str] = []
    for key, value in node.items():
        if str(key) in SENSITIVE_KEYS:
            found.extend(_iter_strings(value))
    unique: dict[str, str] = {}
    for value in found:
        if isinstance(value, str) and value.strip():
            unique.setdefault(value.casefold(), value)
    return tuple(sorted(unique.values(), key=len, reverse=True))


def collect_sensitive_values(source: Any) -> tuple[str, ...]:
    """Collect every answer-bearing string in a rubric, nested or not."""
    mapping = _as_mapping(source)
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key) in SENSITIVE_KEYS:
                    found.extend(_iter_strings(value))
                else:
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(mapping)
    unique: dict[str, str] = {}
    for value in found:
        if isinstance(value, str) and value.strip():
            unique.setdefault(value.casefold(), value)
    return tuple(sorted(unique.values(), key=len, reverse=True))


def _variants(value: str) -> Iterable[str]:
    """Whitespace variants of an answer, so line breaks cannot hide it."""
    seen: set[str] = set()
    for candidate in (value, value.strip(), " ".join(value.split())):
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _compile_patterns(
    values: Iterable[str],
) -> tuple[tuple[re.Pattern[str], str], ...]:
    patterns: list[tuple[re.Pattern[str], str]] = []
    seen: set[str] = set()
    for value in values:
        if len(value.strip()) < _MIN_SCRUB_LENGTH:
            continue
        for variant in _variants(value):
            key = variant.casefold()
            if key in seen:
                continue
            seen.add(key)
            patterns.append(
                (re.compile(re.escape(variant), re.IGNORECASE), REDACTION_MARKER)
            )
    return tuple(patterns)


def _scrub(text: Any, patterns: tuple[tuple[re.Pattern[str], str], ...]) -> str:
    cleaned = str(text if text is not None else "")
    for pattern, replacement in patterns:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _is_answer_verbatim(text: Any, values: Iterable[str]) -> bool:
    """True when a visible value is exactly an answer, ignoring whitespace."""
    normalized = " ".join(str(text or "").split()).casefold()
    if not normalized:
        return False
    return any(
        normalized == " ".join(value.split()).casefold() for value in values
    )


def redact_rubric(source: Any) -> dict[str, Any]:
    """Return the allowlisted, scrubbed student view of a rubric.

    The result contains no sensitive key, no source object and no reference to
    the rubric file path or hash. Unknown fields are dropped by construction.
    """
    mapping = _as_mapping(source)
    all_patterns = _compile_patterns(collect_sensitive_values(mapping))

    if "sections" in mapping:
        sections: list[dict[str, Any]] = []
        for raw_section in mapping.get("sections") or []:
            section = _as_mapping(raw_section)
            questions: list[dict[str, Any]] = []
            for raw_question in section.get("questions") or []:
                question = _as_mapping(raw_question)
                own_values = _values_of(question)
                own_patterns = _compile_patterns(own_values)
                visible: dict[str, Any] = {
                    "id": question.get("id"),
                    "type": question.get("type"),
                    "question": _scrub(question.get("question"), own_patterns),
                }
                broken_code = question.get("code")
                if (
                    broken_code is not None
                    and str(broken_code).strip()
                    and not _is_answer_verbatim(broken_code, own_values)
                ):
                    # Kept verbatim: it is the program the student must reason
                    # about. Omitted only when the code is itself the answer.
                    visible["code"] = str(broken_code)
                requirements = question.get("requirements")
                if requirements:
                    if isinstance(requirements, (list, tuple)):
                        visible["requirements"] = [
                            _scrub(item, own_patterns) for item in requirements
                        ]
                    else:
                        visible["requirements"] = [
                            _scrub(requirements, own_patterns)
                        ]
                questions.append(visible)
            sections.append(
                {
                    "section_name": _scrub(section.get("section_name"), all_patterns),
                    "weight": section.get("weight", 0),
                    "questions": questions,
                }
            )
        return {
            "schema": "spec",
            "assignment_title": _scrub(mapping.get("assignment_title"), all_patterns),
            "topic": _scrub(mapping.get("topic"), all_patterns),
            "total_points": mapping.get("total_points", 0),
            "sections": sections,
        }

    tasks: list[dict[str, Any]] = []
    for raw_task in mapping.get("tasks") or []:
        task = _as_mapping(raw_task)
        tasks.append(
            {
                "id": task.get("id"),
                "name": _scrub(task.get("name"), all_patterns),
                "description": _scrub(task.get("description"), all_patterns),
            }
        )
    return {
        "schema": "config",
        "assignment_id": mapping.get("assignment_id"),
        "title": _scrub(mapping.get("title"), all_patterns),
        "tasks": tasks,
    }


__all__ = [
    "REDACTION_MARKER",
    "SENSITIVE_KEYS",
    "collect_sensitive_values",
    "redact_rubric",
]
