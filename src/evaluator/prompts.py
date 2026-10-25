"""Prompt construction: document trimming, exempt/code/spec blocks, and the rubric->prompt assembly.

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

from src.evaluator.config import (
    MAX_DOC_CHARS,
    Rubric,
    _TRIM_MARKER,
    _TRIM_SEARCH_CHARS,
)

logger = logging.getLogger(__name__)

def trim_document_text(
    raw_text: str, limit: int = MAX_DOC_CHARS
) -> tuple[str, bool]:
    """Fit a student document inside the token budget.

    Returns ``(text, trimmed)``. Only the middle is dropped; the elision is
    marked explicitly so a truncated review is reported, never guessed at.
    Pure and side-effect free, so the same trim can be replayed in tests.
    """
    text = raw_text if isinstance(raw_text, str) else ""
    if limit <= 0 or len(text) <= limit:
        return text, False

    budget = limit - len(_TRIM_MARKER)
    if budget <= 0:
        # Degenerate limit: keep the head and be loud about it.
        return text[:limit], True

    head = min(len(text), budget // 2)
    tail = min(len(text) - head, budget - head)
    omitted = len(text) - head - tail
    if omitted <= 0:
        return text[:limit], True

    # Back off to a whitespace boundary so neither half starts or ends
    # mid-word, which also keeps JSON-ish payloads from being mangled.
    head = _snap_back(text, head)
    tail_start = len(text) - tail
    if tail_start <= head:
        tail_start = head
    tail_start = _snap_forward(text, tail_start)
    omitted = tail_start - head

    marker = _TRIM_MARKER.format(omitted=omitted)
    combined = text[:head] + marker + text[tail_start:]
    # Re-check after snapping; a pathological run of whitespace can push us back
    # over the limit, and the guarantee "fits in `limit`" is the whole point.
    if len(combined) > limit:
        combined = combined[:limit]
    return combined, True


def _snap_back(text: str, index: int) -> int:
    """Move ``index`` left to the nearest whitespace (bounded search)."""
    floor = max(0, index - _TRIM_SEARCH_CHARS)
    for probe in range(index, floor, -1):
        if text[probe - 1].isspace():
            return probe
    return index


def _snap_forward(text: str, index: int) -> int:
    """Move ``index`` right to the nearest whitespace (bounded search)."""
    ceiling = min(len(text), index + _TRIM_SEARCH_CHARS)
    for probe in range(index, ceiling):
        if text[probe].isspace():
            return probe
    return index


# Header of the per-assignment "these tasks are broken, skip them" block.
# Injected only when a rubric actually flags tasks (see ``TaskDefinition.exempt``).
_EXEMPT_HEADER = "SKIP THESE TASKS ENTIRELY (they are broken in the rubric):"


def _exempt_tasks_block(assignment: Rubric) -> str:
    """Build the "ignore these tasks" instruction for exempt rubric entries.

    A task marked ``"exempt": true`` in the assignment JSON is known to be
    malformed (e.g. a broken exercise definition), so it must not influence
    the status list, the score or the written feedback. Returns an empty
    string for well-formed rubrics, so their prompt stays unchanged.

    Spec rubrics (:class:`AssignmentSpec`) have no per-task ``exempt`` flag -
    every question in a section is gradeable by definition - so they always
    get the empty string.
    """
    if not isinstance(assignment, AssignmentConfig):
        return ""
    exempt = [task for task in assignment.tasks if task.exempt]
    if not exempt:
        return ""
    listed = "\n".join(
        f'  - task id {task.id} (name: "{task.name}")' for task in exempt
    )
    return (
        f"\n{_EXEMPT_HEADER}\n"
        f"{listed}\n"
        "For every task listed above: do NOT evaluate it, do NOT create a "
        "task_evaluations entry for it, do NOT dock points for it, and do NOT "
        "mention it in feedback_hebrew. Compute the score from the remaining "
        "tasks only, as if the skipped task did not exist.\n"
    )


def _code_scan_block(assignment: AssignmentConfig, raw_doc_text: str) -> str:
    """Deterministic structural verdicts for code assignments, or "".

    For rubrics that declare static checks (currently lesson_3), the raw text
    is scanned for the required variables/calls and the result is handed to the
    model as an independent, mechanical second opinion. It is phrased as an
    aid, never as a final score: the model still judges correctness, partial
    credit and output, and a passing scan never overrides what the code does.

    Returns "" for rubrics with no checks (worksheets, free-text answers) and
    for empty submissions, so the prompt is unchanged for the common case.
    """
    checks = checks_for_assignment(assignment.assignment_id)
    if not checks:
        return ""
    scan = scan_submission(raw_doc_text, checks)
    if scan.empty:
        return ""
    lines = "\n".join(f"- {line}" for line in scan.summary_lines())
    return (
        "\nDeterministic code scan (offline, mechanical - verify against the "
        "document, do not treat as the final grade):\n"
        f"{lines}\n"
        "Use this as supporting evidence only. If the scan says a variable or "
        "call is missing but the document clearly contains it, trust the "
        "document and say so in the notes.\n"
    )


def _spec_scan_block(rubric: Rubric, raw_doc_text: str) -> str:
    """Deterministic evidence checks for the hard-rubric question types.

    Multiple choice, expected output and debugging answers are checked against
    the student's document here (offline, mechanical), and the verdicts are
    handed to the model as supporting evidence. The model still produces the
    final per-question verdict. Returns "" when nothing is checkable.
    """
    findings: list[str] = []
    doc_lower = (raw_doc_text or "").lower()

    if isinstance(rubric, AssignmentSpec):
        for section in rubric.sections:
            for q in section.questions:
                if q.type == "multiple_choice":
                    expected = (q.correct_answer or "").lower()
                    if expected and expected not in doc_lower:
                        findings.append(
                            f"- {section.section_name} - {q.id}: "
                            f"multiple_choice answer '{q.correct_answer}' not found in the submission"
                        )
                elif q.type == "expected_output":
                    expected = (q.expected_output or "").strip()
                    if expected:
                        # Exact-match with the student's extracted text when the
                        # answer is a single line; otherwise treat as a block and
                        # report the expected block for the model to compare.
                        if expected in raw_doc_text:
                            pass
                        else:
                            findings.append(
                                f"- {section.section_name} - {q.id}: "
                                f"expected output not present in the submission"
                            )
                elif q.type == "debugging":
                    corrected = (q.corrected_code or "").strip()
                    if corrected and corrected not in raw_doc_text:
                        findings.append(
                            f"- {section.section_name} - {q.id}: "
                            f"corrected code not found in the submission"
                        )

    if not findings:
        return ""
    return (
        "\nSpec evidence scan (offline, mechanical - verify against the "
        "document, do not treat as the final grade):\n"
        + "\n".join(findings)
        + "\nUse this as supporting evidence only. If a finding below appears "
        "in the document in a different form, trust the document and say so "
        "in the notes.\n"
    )


def rubric_title(rubric: Rubric) -> str:
    """Human-readable rubric name for logs/audit labels, either schema."""
    if isinstance(rubric, AssignmentConfig):
        return rubric.title
    return rubric.assignment_title


def rubric_tasks_json(rubric: Rubric) -> str:
    """Compact JSON block of the rubric's questions, for the LLM prompt."""
    if isinstance(rubric, AssignmentConfig):
        # ``exclude_defaults`` keeps ``exempt`` out of the JSON unless true, so
        # the block stays clean for rubrics with no broken tasks.
        return json.dumps(
            [task.model_dump(exclude_defaults=True) for task in rubric.tasks],
            ensure_ascii=False,
            indent=2,
        )

    blocks: list[str] = []
    for section in rubric.sections:
        points = round(section.weight / max(len(section.questions), 1), 2)
        for q in section.questions:
            payload: dict[str, Any] = {
                "id": q.id,
                "type": q.type,
                "section": section.section_name,
                "question": q.question,
                "points": points,
            }
            if q.correct_answer is not None:
                payload["correct_answer"] = q.correct_answer
            if q.expected_output is not None:
                payload["expected_output"] = q.expected_output
            if q.code is not None:
                payload["code"] = q.code
            if q.corrected_code is not None:
                payload["corrected_code"] = q.corrected_code
            if q.requirements is not None:
                payload["requirements"] = q.requirements
            blocks.append(json.dumps(payload, ensure_ascii=False, indent=2))
    return "\n".join(blocks)


def _build_prompt(
    student_name: str, raw_doc_text: str, rubric: Rubric
) -> tuple[str, bool]:
    """Compose the user message: Teacher Context + trimmed student document.

    Returns ``(prompt, doc_trimmed)``. The document is trimmed before it is
    embedded: pasted web pages or pasted chapters are the single biggest
    consumer of the daily (TPD) token budget, and one such submission can cost
    as much as a dozen normal answers. The flag tells the caller to mention the
    trim in the report.
    """
    if isinstance(rubric, AssignmentConfig):
        tasks_json = rubric_tasks_json(rubric)
        # Use the human-facing title for the prompt header so the model
        # evaluates the same assignment the report is named for.
        assignment_id = rubric.assignment_id or rubric.title
        assignment_title = rubric.title or rubric.assignment_id
        code_scan = _code_scan_block(rubric, raw_doc_text)
        spec_scan = ""
        spec_instructions = ""
    else:
        tasks_json = rubric_tasks_json(rubric)
        assignment_id = rubric.assignment_title
        assignment_title = rubric.topic
        code_scan = ""
        spec_scan = _spec_scan_block(rubric, raw_doc_text)
        # The spec questions carry their own types and per-question points;
        # without spelling out the contract the model falls back to the flat
        # task-list habits of the legacy rubrics.
        spec_instructions = (
            "This assignment is graded question-by-question against the "
            "sectioned Tasks (JSON) above:\n"
            "- Return one task_evaluations entry per question, using "
            "task_name = the question's \"id\" (e.g. \"q3\").\n"
            "- Each question's \"points\" is its maximum; deduct proportionally "
            "within the section weight and keep the deduction_breakdown sum "
            "equal to 100 - score.\n"
            "- For multiple_choice compare against \"correct_answer\"; for "
            "expected_output compare the student's output exactly "
            "(whitespace/case differences are typos - free); for debugging "
            "compare against \"corrected_code\"; for code_writing check every "
            "entry in \"requirements\".\n"
        )

    doc_text, doc_trimmed = trim_document_text(raw_doc_text)
    # Say it inside the document, not just in a preamble: the model must not
    # treat a partial review as a complete one.
    tail_note = (
        "\nNOTE: the document above was trimmed for the token budget. If a task "
        "cannot be judged from the visible text, mark it INCORRECT only when "
        "the answer is clearly absent, otherwise say so in its notes.\n"
        if doc_trimmed
        else ""
    )

    prompt = (
        f"Assignment: {assignment_id}\n"
        f"Topic: {assignment_title}\n\n"
        f"Tasks (JSON):\n{tasks_json}\n"
        f"{spec_instructions}"
        f"{_exempt_tasks_block(rubric)}"
        f"{code_scan}"
        f"{spec_scan}"
        f"Student name: {student_name}\n\n"
        "Raw text extracted from the student's Google Doc:\n"
        "--- BEGIN STUDENT DOCUMENT ---\n"
        f"{doc_text}\n"
        "--- END STUDENT DOCUMENT ---\n"
        f"{tail_note}\n"
        "Evaluate this submission and return the structured grading result."
    )
    return prompt, doc_trimmed
