"""Single-pass LLM evaluation of student submissions (Teacher Context Injection).

One chat completion to Groq (model from ``settings.groq_model``, default
``qwen/qwen3.8-27b``, via the official ``openai`` SDK pointed at Groq's
OpenAI-compatible endpoint ``settings.groq_base_url``) receives the
assignment's task definitions plus the raw text of the student's Google Doc
and returns a structured :class:`~src.models.GradingResult`.

Structured output is enforced server-side with Groq's strict JSON-Schema mode
(``response_format={"type": "json_schema", ...}``, ``strict: true``), so the
payload normally matches the :class:`GradingResult` schema generated from the
Pydantic model, and it is validated again locally as a safety net.

Not every Groq model/runtime honours that mode, and Groq rejects some
structured-output requests with ``HTTP 400: Failed to generate JSON``. When
that happens the evaluator degrades one step at a time (strict schema ->
``json_object`` -> free text) and sanitises each answer locally: markdown
fences, prose around the payload, trailing commas and truncated output are all
repaired by :func:`repair_json` before the strict Pydantic validation runs.
"""

from __future__ import annotations

import json
import logging
import re
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
from src.late_policy import apply_late_penalty
from src.models import AssignmentConfig, GradingResult

logger = logging.getLogger(__name__)

# Silence SDK console chatter regardless of the application's root log level:
# - httpx / httpx2: per-request "HTTP Request: POST ... 200 OK" INFO lines.
# - openai: verbose request/response DEBUG dumps and retry notices.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# Sampling: kept low so repeated grading of the same submission stays close to
# reproducible (the model card's instruct-mode preset of 0.7 favours varied
# prose; a rubric favours consistency).
TEMPERATURE = 0.2

# Output cap: Groq's free tier enforces a tight Output Tokens Per Minute
# (OTPM) budget (~1000 TPM on qwen/qwen3.8-27b). Long Hebrew feedback plus
# per-task notes easily blew past it (HTTP 429 "Request too large") and got
# truncated mid-sentence. 800 keeps every response complete and well under
# the per-request limit; combined with the concise-feedback prompt below and
# the inter-student pacing delay in the callers, this stays inside TPM.
MAX_TOKENS = 800

# Pacing between sequential student evaluations: Groq meters output tokens
# per minute, so back-to-back long completions burst the bucket even when
# each one is under MAX_TOKENS. Callers sleep this long after each student.
EVAL_PAUSE_SECONDS = 2.0

# ``reasoning_effort="none"`` selects Qwen 3.8's instruct (non-thinking) mode.
# Grading is a bounded extraction/assessment task, and thinking traces would
# multiply output tokens against Groq's free-tier token-per-minute budget.
# Switch to "default"/"low"/"medium"/"high" to trade speed for deeper analysis.
REASONING_EFFORT = "none"

# Transient statuses worth retrying (rate limits, overloaded backend, and the
# gateway timeouts Groq returns while a request is queued behind load).
_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)

# Retry budget: up to 4 attempts, honouring Groq's Retry-After when it tells us
# exactly when the bucket refills, else exponential backoff (2s, 4s, 8s...).
_MAX_ATTEMPTS = 4
_BACKOFF_MULTIPLIER = 2.0
_MAX_BACKOFF_SECONDS = 30.0
_MAX_RETRY_AFTER_SECONDS = 60.0

# A full Hebrew feedback plus reasoning can take a while; the SDK default is
# fine for chat but too short for a long grading call behind a cold start.
_REQUEST_TIMEOUT_SECONDS = 180.0

# Groq's strict mode only constrains decoding with this JSON-Schema subset;
# anything else (``title``, ``minimum``/``maximum``, ``format``, ...) is either
# rejected or ignored, so it is stripped before the schema is sent.
_STRICT_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "anyOf",
        "description",
        "enum",
        "items",
        "properties",
        "required",
        "type",
    }
)

# Name Groq reports for the schema (shows up in error payloads/telemetry).
_SCHEMA_NAME = "grading_result"

SYSTEM_PROMPT = """\
You are an expert, supportive middle-school CS teacher grading Python code.

Analyze the Tasks JSON and student document. Return a single-pass JSON evaluation.
- Statuses: CORRECT, PARTIALLY_CORRECT, INCORRECT, MISSING.
- Tasks flagged "exempt": true or in SKIP THESE TASKS must be ignored completely.

Grading Philosophy & Scoring (Target 90-95 for good work):
- Default to encouragement. Hard floor score: 60 (unless empty/spam).
- Deduction Sizing: A minor gap costs 2-5 points TOTAL for the whole assignment (e.g., printing sum instead of type, hardcoding literal strings, missing one print). A good submission with such a gap scores 93-98.
- Score bands: 95-100 (complete), 90-95 (minor gaps, 2-5 pts each), 80-85 (substantial missing parts), 70-79 (several gaps), 60-69 (genuine attempt, little works), <60 (empty/spam).
- Link Submissions: Treat links (Drive, GitHub, Replit) as valid, full-attempt evidence. NEVER dock points for using a link.

Typos & Formatting (0 point penalty):
- Intent over keystrokes. `pint` instead of `print`, curly quotes, stray underscores (`_bob_____`), spacing, case errors -> 0 points deducted, status CORRECT. Mention at most one typo as a friendly heads-up.

Dockable Gaps ONLY (2-5 pts total):
- 1. Missing required output/answer (e.g. unmarked MC options).
- 2. Wrong logic/result.
- 3. Bypassing core requirements (hardcoding literals instead of using variables).
- 4. Main-path crash.

Deduction Breakdown (deduction_breakdown):
- Itemise every lost point: task_name, points_deducted (sum MUST equal 100 - score), reason (short English sentence). Empty list [] if no deductions.

Feedback Guidelines (feedback_hebrew):
- Keep it concise: 3-5 sentences max (~60-100 words).
- Structure: (1) praise specific strength; (2) 1-2 sentences on fixes — **MANDATORY:** Every deduction in deduction_breakdown MUST be explicitly explained here so the student knows why points were lost; (3) encouraging closing.
- Use gentle phrasing ("שימו לב קטן...").
- Invitation to resubmit if score < 100 ("אפשר לתקן את הדברים שצוינו, להגיש שוב בשמחה והציון יעודכן בהתאם!").
- Link boilerplate (exact Hebrew, replaces the fix sentence, score-neutral): "שימו לב: בהגשות הבאות יש להדביק את הקוד עצמו ישירות בתוך המסמך כדי שאוכל לבדוק אותו בקלות ולתת משוב מפורט יותר." (Skip if code was pasted).
- Hebrew: Gender-neutral, warm phrasing (avoid gendered past-tense verbs).

Strict rules: Base evidence ONLY on the document text. Return only the structured result object.
"""


class LLMEvaluationError(Exception):
    """Raised when the Groq request, or its structured output, fails."""


class LLMJSONParsingError(LLMEvaluationError):
    """A completion arrived, but no valid ``GradingResult`` could be read from it.

    Raised for empty/garbled answers after every local repair attempt
    (:func:`_repair_candidates`) failed. Subclasses :class:`LLMEvaluationError`
    so existing callers keep catching it, while
    :func:`evaluate_student_submission` uses the narrower type to decide that a
    second request with a looser ``response_format`` is worth making.
    """


class LLMAPIError(LLMEvaluationError):
    """A Groq request failed at the HTTP level (kept for error handling)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Lazily build the Groq client from settings (GROQ_API_KEY).

    The official ``openai`` SDK is reused against Groq's OpenAI-compatible
    base URL, so no Groq-specific client library is needed. The SDK's own
    retry loop is disabled because tenacity owns retries here (it can pace
    itself with ``Retry-After`` and log every attempt).
    """
    global _client
    if _client is None:
        api_key = settings.groq_api_key
        if not api_key:
            raise LLMEvaluationError(
                "GROQ_API_KEY is not set. Add it to .env (see .env.example) "
                "or export it in your environment."
            )
        _client = OpenAI(
            api_key=api_key,
            base_url=settings.groq_base_url,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
    return _client


def _to_strict_json_schema(node: Any) -> Any:
    """Project a Pydantic JSON schema onto Groq's strict-mode subset.

    Strict mode requires every object to declare ``additionalProperties:
    false`` and to list all of its properties in ``required``; annotations it
    does not understand are dropped. Numeric bounds such as ``score``'s
    ``ge=0/le=100`` therefore leave the wire schema, but they still hold: the
    response is validated against the untouched Pydantic model, and the system
    prompt states the 0-100 range.
    """
    if isinstance(node, list):
        return [_to_strict_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    strict: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _STRICT_SCHEMA_KEYWORDS:
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            # These two are name -> subschema maps, so their keys are field or
            # definition names rather than JSON-Schema keywords: recurse into
            # the values only, never filter the mapping itself.
            strict[key] = {
                name: _to_strict_json_schema(subschema)
                for name, subschema in value.items()
            }
        else:
            strict[key] = _to_strict_json_schema(value)
    properties = strict.get("properties")
    if isinstance(properties, dict) or strict.get("type") == "object":
        strict["additionalProperties"] = False
        if isinstance(properties, dict):
            strict["required"] = sorted(properties)
    return strict


def _build_response_format() -> dict[str, Any]:
    """OpenAI-compatible ``response_format`` enforcing the GradingResult schema."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": _SCHEMA_NAME,
            "strict": True,
            "schema": _to_strict_json_schema(
                GradingResult.model_json_schema()
            ),
        },
    }


# Built once: the schema is static, so there is no need to regenerate it per
# student (and any pydantic-schema mistake surfaces at import time).
_RESPONSE_FORMAT = _build_response_format()

# Degraded mode: Groq's ``json_object`` only guarantees syntactically valid JSON
# (no schema adherence), but it is supported by every model - used when strict
# mode is rejected or when a payload cannot be read.
_JSON_OBJECT_FORMAT: dict[str, Any] = {"type": "json_object"}

# Appended to the system prompt for the fallback stages. Without server-side
# schema enforcement the prompt itself has to pin the key names, otherwise the
# model freely invents its own (``grade``/``comments``/``tasks``...).
_JSON_ONLY_INSTRUCTION = """

OUTPUT FORMAT (overrides any conflicting instruction above):
Reply with ONLY the raw JSON object - no markdown, no code fences, no preamble,
no text before or after it. Use exactly these keys and these types:
{"student_name": string, "score": integer 0-100, "feedback_hebrew": string,
 "deduction_breakdown": [{"task_name": string, "points_deducted": integer,
 "reason": string}],
 "task_evaluations": [{"task_name": string, "student_answer_found": string,
 "status": "CORRECT" | "PARTIALLY_CORRECT" | "INCORRECT" | "MISSING",
 "notes": string}]}
Include one task_evaluations entry per assignment task, in order.
deduction_breakdown is REQUIRED: one entry per task that lost points, with the
points summing to (100 - score), or an empty list [] when nothing was deducted."""

# A truncated answer is unparseable; the fallback stages ask for a shorter one.
_CONCISE_REMINDER = """

IMPORTANT: your previous answer was cut off before the JSON was complete. Keep
the whole JSON short enough to finish: feedback_hebrew of at most 2-3
sentences, and at most 15 words per notes field."""

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


# Header of the per-assignment "these tasks are broken, skip them" block.
# Injected only when a rubric actually flags tasks (see ``TaskDefinition.exempt``).
_EXEMPT_HEADER = "SKIP THESE TASKS ENTIRELY (they are broken in the rubric):"


def _exempt_tasks_block(assignment: AssignmentConfig) -> str:
    """Build the "ignore these tasks" instruction for exempt rubric entries.

    A task marked ``"exempt": true`` in the assignment JSON is known to be
    malformed (e.g. a broken exercise definition), so it must not influence
    the status list, the score or the written feedback. Returns an empty
    string for well-formed rubrics, so their prompt stays unchanged.
    """
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


def _build_prompt(
    student_name: str, raw_doc_text: str, assignment: AssignmentConfig
) -> str:
    """Compose the user message: Teacher Context + raw student document."""
    tasks_json = json.dumps(
        # ``exempt`` is omitted unless true, so the JSON stays clean for
        # rubrics with no broken tasks while still flagging the broken ones.
        [task.model_dump(exclude_defaults=True) for task in assignment.tasks],
        ensure_ascii=False,
        indent=2,
    )
    return (
        f"Assignment ID: {assignment.assignment_id}\n"
        f"Title: {assignment.title}\n\n"
        f"Tasks (JSON):\n{tasks_json}\n"
        f"{_exempt_tasks_block(assignment)}\n"
        f"Student name: {student_name}\n\n"
        "Raw text extracted from the student's Google Doc:\n"
        "--- BEGIN STUDENT DOCUMENT ---\n"
        f"{raw_doc_text}\n"
        "--- END STUDENT DOCUMENT ---\n\n"
        "Evaluate this submission and return the structured grading result."
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


# ---------------------------------------------------------------------------
# JSON repair
#
# Groq models routinely decorate the payload: markdown fences (```json), a
# "Here is the evaluation:" preamble, a trailing remark after the object, or a
# hard cut-off in the middle of the output-token budget. The helpers below turn
# those answers into something ``json.loads``/Pydantic can read, and only then
# is the strict ``GradingResult`` validation applied (never relaxed).
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Schema coercion
#
# Repaired JSON is still validated against the *untouched* Pydantic model; the
# helpers below only massage the keys/values first, so a model that answered in
# its own dialect still lands on the same GradingResult.
# ---------------------------------------------------------------------------


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


def _is_retryable_error(exc: BaseException) -> bool:
    """Retry only transient failures: HTTP 429/5xx or network drops."""
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    # Raised before/without an HTTP response: connection resets, DNS drops,
    # read timeouts, and (defensively) raw transport errors.
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError))


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Parse Groq's ``Retry-After`` header (seconds) when the API sent one."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = headers.get("retry-after") if headers is not None else None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _wait_before_retry(retry_state: Any) -> float:
    """Sleep per ``Retry-After``, else exponentially (2s, 4s, 8s...).

    Groq's free-tier buckets refill per minute, so a server-provided delay is
    treated as a floor rather than a replacement for the backoff.
    """
    attempt = getattr(retry_state, "attempt_number", 1)
    delay = min(_BACKOFF_MULTIPLIER * 2 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
    outcome = getattr(retry_state, "outcome", None)
    if outcome is not None and outcome.failed:
        retry_after = _retry_after_seconds(outcome.exception())
        if retry_after is not None:
            delay = max(delay, retry_after)
    return min(delay, _MAX_RETRY_AFTER_SECONDS)


def _log_retry(retry_state: Any) -> None:
    """Warn before each retry sleep (tenacity ``before_sleep`` callback)."""
    outcome = getattr(retry_state, "outcome", None)
    error = outcome.exception() if outcome is not None and outcome.failed else "?"
    logger.warning(
        "Groq call failed (%s); retrying (attempt %s/%d)...",
        error,
        getattr(retry_state, "attempt_number", "?"),
        _MAX_ATTEMPTS,
    )


@retry(
    retry=retry_if_exception(_is_retryable_error),
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    wait=_wait_before_retry,
    reraise=True,
    before_sleep=_log_retry,
)
def _create_completion(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None = None,
) -> Any:
    """One Groq chat completion, retried on 429/5xx/network drops (tenacity).

    ``response_format`` defaults to the strict JSON-Schema payload; the
    fallback stages pass :data:`_JSON_OBJECT_FORMAT` or ``None`` for free text.
    """
    request: dict[str, Any] = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
        # Groq-specific: never fold thinking text into ``message.content``.
        "extra_body": {"reasoning_format": "hidden"},
    }
    if response_format is not None:
        request["response_format"] = response_format
    return client.chat.completions.create(**request)


# Grading attempts, strongest guarantee first. Groq's strict JSON-Schema mode
# covers only select models and the endpoint can still answer HTTP 400 "Failed
# to generate JSON"; instead of losing the student we walk down this ladder and
# rely on the local repair/validation (``_parse_payload``) to keep the contract.
_STAGES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("json_schema", SYSTEM_PROMPT, _RESPONSE_FORMAT),
    (
        "json_object",
        SYSTEM_PROMPT + _JSON_ONLY_INSTRUCTION,
        _JSON_OBJECT_FORMAT,
    ),
    (
        "text",
        SYSTEM_PROMPT + _JSON_ONLY_INSTRUCTION + _CONCISE_REMINDER,
        None,
    ),
)

# Index of the first stage that produced a usable payload. Groq rejections are
# deterministic for a given model, so remembering the working stage spares the
# batch a doomed round-trip (and its tokens) for every remaining student.
_first_working_stage = 0


def _error_detail(exc: APIError) -> str:
    """Best-effort human-readable message from a Groq error payload."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return str(exc)


def _is_json_generation_error(detail: str) -> bool:
    """True for Groq's structured-output rejection messages (HTTP 400)."""
    lowered = detail.lower()
    return "json" in lowered and (
        "failed to generate" in lowered
        or "could not generate" in lowered
        or "invalid json" in lowered
    )


def _is_degradable(exc: BaseException) -> bool:
    """True when a looser response format could still rescue this failure."""
    if isinstance(exc, LLMJSONParsingError):
        # Payload unusable (fences/prose/truncation/garbage) - ask again.
        return True
    if isinstance(exc, LLMAPIError):
        # 400 = request rejected (e.g. "Failed to generate JSON"); a looser
        # format is a genuinely different request. 401/403/404/5xx are not.
        return exc.status_code == 400
    return False


def _call_completion(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
) -> Any:
    """One graded request with transport/HTTP errors mapped to our own types."""
    try:
        return _create_completion(
            client, system_prompt, user_prompt, response_format
        )
    except APIStatusError as exc:
        raise LLMAPIError(
            f"Groq API request failed (HTTP {exc.status_code}): "
            f"{_error_detail(exc)}",
            status_code=exc.status_code,
        ) from exc
    except ValidationError as exc:
        raise LLMJSONParsingError(
            f"Groq response violated the GradingResult schema: {_one_line(exc)}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - boundary wraps unexpected failures
        raise LLMEvaluationError(f"Groq evaluation failed: {exc}") from exc


def evaluate_student_submission(
    student_name: str,
    raw_doc_text: str,
    assignment: AssignmentConfig,
    late_penalty_points: int = 0,
) -> GradingResult:
    """Grade one submission with a single-pass Groq chat completion.

    Injects the assignment's task definitions (Teacher Context) together with
    the unparsed raw Google Doc text and keeps sampling tight for consistent,
    reproducible evaluations. Transient API failures (HTTP 429/500/502/503/504,
    network drops) are retried up to 4 attempts with exponential backoff.

    ``late_penalty_points`` is applied *after* grading by
    :func:`src.late_policy.apply_late_penalty` - the deadline is a policy, not
    something the model can be trusted to weigh in. It is applied here, at the
    one place every caller goes through, so the subtraction can never be lost
    in prompt drift or in one branch of a caller. The *value* however is the
    caller's to decide, because only the caller holds the submission and the
    coursework needed to tell whether the work was late at all
    (:func:`src.late_policy.penalty_for`). It therefore defaults to ``0``: an
    entry point that omits the argument grades late work identically to
    on-time work rather than inventing a penalty it has no evidence for. Every
    real entry point (``app.py``, ``src/main.py``) passes it explicitly from
    ``settings.late_penalty_points`` - see :func:`load_settings` for how that
    value resolves.

    If the strict JSON-Schema response cannot be produced (Groq answers
    ``HTTP 400: Failed to generate JSON``) or cannot be read back
    (:func:`_parse_payload`), the call degrades to ``json_object`` mode and
    then to free text, and only then raises :class:`LLMEvaluationError`.
    """
    global _first_working_stage

    client = _get_client()
    prompt = _build_prompt(student_name, raw_doc_text, assignment)

    failures: list[str] = []
    result: GradingResult | None = None
    for index in range(_first_working_stage, len(_STAGES)):
        label, system_prompt, response_format = _STAGES[index]
        try:
            response = _call_completion(
                client, system_prompt, prompt, response_format
            )
            result = _parse_result(response)
        except LLMEvaluationError as exc:
            failures.append(f"[{label}] {exc}")
            if not _is_degradable(exc):
                raise
            reason = (
                "structured output rejected by Groq"
                if isinstance(exc, LLMAPIError)
                and _is_json_generation_error(str(exc))
                else "unusable payload"
            )
            logger.warning(
                "Grading attempt '%s' failed (%s: %s); degrading the response format.",
                label,
                reason,
                _one_line(exc),
            )
            continue
        _first_working_stage = index
        break

    if result is None:
        raise LLMEvaluationError(
            "Groq returned no usable grading result after "
            f"{len(failures)} attempt(s): " + " || ".join(failures)
        )

    # The caller's student name is authoritative (guards against drift).
    result.student_name = student_name
    # Deadline policy runs last, on the finished grade, so it is applied the
    # same way for every caller and can never be lost in prompt drift.
    apply_late_penalty(result, late_penalty_points)
    logger.info(
        "Graded '%s' for %s with %s: score=%d (%d task evaluations)",
        assignment.assignment_id,
        student_name,
        settings.groq_model,
        result.score,
        len(result.task_evaluations),
    )
    return result
