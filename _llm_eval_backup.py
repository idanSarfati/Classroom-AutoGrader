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

logger = logging.getLogger(__name__)


# Union of rubric schemas the evaluator understands. The legacy path is a flat
# list of tasks (``AssignmentConfig``); the richer path is nested sections each
# containing questions (``AssignmentSpec``).
Rubric = AssignmentConfig | AssignmentSpec

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

# --- Input trimming --------------------------------------------------------
# The daily token cap (TPD, 200k/day on Groq free) is consumed by the *input*
# side too, and the input is almost entirely the student's pasted document.
# Middle-school answers are short, but a student who pastes a full web page, a
# book chapter or a spreadsheet dump turns one submission into 10k tokens -
# and a class of 40 then exhausts the day's quota before lunch.
#
# A 6000-char cap (~1.5k tokens, comfortably above any genuine middle-school
# answer) keeps the quota for students who need it. The head and tail are both
# kept because the task list is usually answered in order: an answer dropped
# from the middle is far more likely to be an appendix/paste artefact than
# genuinely graded work. The cut is always announced in the prompt, so the
# evaluator reports "the document was too long to review in full" instead of
# silently inventing a verdict about text it never saw.
MAX_DOC_CHARS = 6000

# Slack kept at the cut point so the elision marker never lands mid-word and
# the two retained halves still read as whole sentences.
_TRIM_SEARCH_CHARS = 400
_TRIM_MARKER = (
    "\n[... document trimmed for the token budget: "
    "{omitted} characters omitted ...]\n"
)


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


# Pacing between sequential student evaluations: Groq meters output tokens
# per minute, so back-to-back long completions burst the bucket even when
# each one is under MAX_TOKENS. Callers sleep this long after each student.
EVAL_PAUSE_SECONDS = 2.0

# ``reasoning_effort="none"`` selects Qwen 3.8's instruct (non-thinking) mode.
# Grading is a bounded extraction/assessment task, and thinking traces would
# multiply output tokens against Groq's free-tier token-per-minute budget.
# Switch to "default"/"low"/"medium"/"high" to trade speed for deeper analysis.
REASONING_EFFORT = "none"

# Transient statuses worth retrying *locally*: overloaded backend and the
# gateway timeouts Groq returns while a request is queued behind load.
#
# 429 is deliberately NOT here. A rate limit (per-minute OTPM or per-day TPD)
# is not a transport hiccup to sleep through - it is a routing decision owned
# by LLMRouter, which rotates to the next key or hands straight over to the
# fallback provider. Retrying it here burned ~14s of exponential backoff on
# the same dead key before _call_completion's 429 handler ever ran, which is
# exactly the stall/fallback-bypass this routing layer exists to prevent. The
# router reports the 429 back the moment _create_completion reraises it.
_RETRYABLE_STATUS_CODES = (500, 502, 503, 504)

# Retry budget: honour Groq's Retry-After when it tells us
# exactly when the bucket refills, else short exponential backoff (2s, 4s).
# Kept deliberately small (2 attempts here): _call_completion already re-serves
# a throttled request through the router up to _RATE_LIMIT_RETRIES times, so a
# big tenacity budget on top only multiplied sleeps - a 429 slept 10s here AND
# 10s+ there before ever reaching the next key. Fast failure into the router
# (which rotates keys / fails over to Gemini) beats slow retries on one key.
_MAX_ATTEMPTS = 2
_BACKOFF_MULTIPLIER = 2.0
_MAX_BACKOFF_SECONDS = 5.0
_MAX_RETRY_AFTER_SECONDS = 10.0

# --- Request parallelism ---------------------------------------------------
# The grading loop is I/O-bound (each student = one HTTP round trip + waiting),
# so grading students one-by-one leaves the machine idle ~95% of the time.
# EVAL_MAX_WORKERS bounds a small thread pool (see grade_students_batch):
# each worker grades with its own router snapshot and the shared router stays
# thread-safe via its internal lock. Kept conservative (3): Groq free allows
# 30 RPM (one request every 2s), Gemini free ~15 RPM, and the AdaptivePacer
# already spaces same-worker calls by 2s. Three workers issuing ~30-60s LLM
# calls each stay well under both per-minute caps while cutting a 20-student
# batch from ~20 min to ~7 min. EVAL_MAX_WORKERS=1 restores strict sequential
# grading (e.g. for debugging).
def _max_workers() -> int:
    """Worker count for batch grading, from ``EVAL_MAX_WORKERS`` (default 3)."""
    import os as _os

    try:
        return max(1, int(_os.getenv("EVAL_MAX_WORKERS", "3")))
    except ValueError:
        return 3

# Per-minute re-serve budget for ONE grading request. Tenacity never sleeps on
# a 429 (routing owns it) and the router only re-serves a throttled attempt
# once - which is not always enough on Groq's ~1000-OTPM free tier: the key
# cooldown can expire in the middle of the per-minute window, the rotated key
# or the fallback can be throttled too, and with no router there is no rotation
# at all. Each of those used to surface as LLMAPIError -> an ERROR row for the
# student in the exported CSV. Every extra round first waits the provider's
# ``retry-after`` hint (or escalates 10s -> 20s -> 40s when it stays silent)
# and then re-routes, so a bulk run rides out OTPM exhaustion instead of
# failing students out of it.
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BACKOFF_BASE = 10.0
# Escalation ceiling for the per-minute re-serve waits (10s -> 20s -> 40s).
# Separate from _MAX_RETRY_AFTER_SECONDS (the tenacity layer's cap, kept
# small so transport retries fail fast into the router): this loop's waits
# are the actual rate-limit recovery, and capping them at 10s flattened the
# escalation the tests pin.
_RATE_LIMIT_WAIT_CAP = 60.0

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


# --- Resilient routing: key pool + fallback + adaptive pacing ---------------
# Built lazily from settings so import-time never needs credentials. The pool
# is shared by the whole batch: every student goes through the same router.
_ROUTER: Any = None
_ROUTER_READY = False
_PACER: Any = None


def _router() -> Any:
    """Shared LLMRouter (or None when no key is configured at all)."""
    global _ROUTER, _ROUTER_READY
    if _ROUTER_READY:
        return _ROUTER
    _ROUTER_READY = True
    try:
        from src.llm_router import build_router_from_settings
    except Exception:
        return None
    try:
        _ROUTER = build_router_from_settings(settings)
    except Exception as exc:
        logger.warning("LLM router disabled (%s); single-key mode.", exc)
        _ROUTER = None
    if _ROUTER is not None:
        logger.info("LLM routing: %s.", _ROUTER.describe())
    return _ROUTER


def _pacer() -> Any:
    """Shared AdaptivePacer that replaces the fixed EVAL_PAUSE_SECONDS sleep."""
    global _PACER
    if _PACER is None:
        try:
            from src.llm_router import AdaptivePacer
        except Exception:
            return None
        _PACER = AdaptivePacer(base_seconds=EVAL_PAUSE_SECONDS)
    return _PACER


def paced_pause(on_tick: Any = None) -> None:
    """Sleep between students: adaptive when available, fixed otherwise.

    ``on_tick(remaining, total, reason)`` is forwarded to the pacer so a UI can
    render a live countdown across the wait instead of holding a frozen frame;
    without it the sleep is exactly the single blocking call it has always been.
    """
    pacer = _pacer()
    if pacer is None:
        from src.llm_router import ticked_sleep

        ticked_sleep(EVAL_PAUSE_SECONDS, on_tick, "pacing")
    else:
        pacer.pause(on_tick)


# --- Audit verdicts attached to GradingRow -----------------------------------
# ``GradingRow`` lives in ``src.main`` (importing it here would cycle), so the
# verdict travels on a sidecar map keyed by ``id(result)``: callers attach it
# to the row right after evaluation. Entries are tiny and popped on read.
_AUDIT_SIDECAR: dict[int, Any] = {}


def last_audit_for(result: Any) -> Any | None:
    """Pop the audit verdict recorded for ``result`` (None when absent)."""
    try:
        return _AUDIT_SIDECAR.pop(id(result), None)
    except Exception:
        return None


def _run_audit(
    result: GradingResult,
    config: Rubric,
    label: str,
) -> Any | None:
    """Run the deterministic audit pass; never breaks grading on error."""
    if not getattr(settings, "llm_audit_enabled", True):
        return None
    try:
        from src.grade_audit import audit_grading_result
    except Exception as exc:
        logger.debug("Audit pass unavailable: %s", exc)
        return None
    try:
        verdict = audit_grading_result(
            result,
            config,
            autofix=getattr(settings, "llm_audit_autofix", True),
        )
    except Exception as exc:  # noqa: BLE001 - audit must never break grading
        logger.warning("Audit pass failed for %s: %s", label, exc)
        return None
    if verdict is not None and not verdict.ok:
        logger.warning("Audit [%s]: %s", label, verdict.summary())
    try:
        _AUDIT_SIDECAR[id(result)] = verdict
    except Exception:
        pass
    return verdict



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

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        # The provider's ``retry-after`` hint parsed from the original 429
        # response. It is captured at the raise site because the APIStatusError
        # (and its headers) is gone by the time the batch runner decides
        # whether to wait-and-retry this student or surface the failure.
        self.retry_after = retry_after


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
            try:
                from src.llm_router import split_keys as _split

                api_key = (_split(None, settings.groq_api_keys) or [None])[0]
            except Exception:
                api_key = None
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
        assignment_id = rubric.assignment_id
        assignment_title = rubric.title
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
    """Retry only transient failures: HTTP 5xx or network drops.

    429 is intentionally excluded: a rate limit is handled by the router
    (rotate key / fail over to the fallback), never slept through here. See
    :data:`_RETRYABLE_STATUS_CODES`.
    """
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
    *,
    model: str | None = None,
) -> Any:
    """One Groq chat completion, retried on 429/5xx/network drops (tenacity).

    ``response_format`` defaults to the strict JSON-Schema payload; the
    fallback stages pass :data:`_JSON_OBJECT_FORMAT` or ``None`` for free text.
    ``model`` overrides the request model (fallback provider's model).

    The Groq-only reasoning knobs (``reasoning_effort`` + ``extra_body``) are
    sent **only** when the request is really going to Groq. A per-minute Groq
    429 parks the Groq keys and routing hands the rest of the batch to the
    fallback (Gemini/OpenRouter); those providers reject unknown body fields
    outright - Gemini answers ``HTTP 400: Unknown name "reasoning_format"`` -
    which used to fail *every* fallback-routed student across *every* remaining
    group. In a daily check that parks 10 of 14 groups off the Release tab at
    once. So they are attached here, on the wire, where the model is known, and
    simply left off for any other provider.
    """
    routed_model = model or settings.groq_model
    # Groq or not is decided by where the request is actually going, not by the
    # model name: the primary model is e.g. "qwen/qwen3.8-27b", which carries no
    # "groq" hint, so a name test would wrongly strip the reasoning knobs from
    # the real Groq path. The client's base URL is the honest signal.
    base_url = str(getattr(client, "base_url", "") or "")
    is_groq = "groq" in base_url.lower() or (
        not base_url and "groq" in str(routed_model).lower()
    )
    request: dict[str, Any] = {
        "model": routed_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    if is_groq:
        # ``reasoning_effort="none"`` selects Qwen's instruct (non-thinking)
        # mode, and ``reasoning_format="hidden"`` keeps Groq from folding any
        # thinking trace into ``message.content``. Both are Groq extensions and
        # are hard 400s on Gemini/OpenRouter, so they are never sent to those.
        request["reasoning_effort"] = REASONING_EFFORT
        request["extra_body"] = {"reasoning_format": "hidden"}
    if response_format is not None:
        if not is_groq and isinstance(response_format, dict):
            kind = response_format.get("type")
            if kind == "json_schema":
                response_format = {"type": "json_object"}
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


def _rate_limit_wait(exc: LLMAPIError, attempt: int) -> float:
    """Seconds to pause before re-serving a request the provider throttled.

    The provider's ``retry-after`` hint wins when present: it knows when its
    per-minute bucket refills (OTPM windows roll on the minute), and guessing
    shorter only buys another 429. With no hint we escalate
    10s -> 20s -> 40s (capped by :data:`_MAX_RETRY_AFTER_SECONDS`) - long
    enough for a silent bucket to roll over, short enough that the batch keeps
    moving. The router's own ceiling (``src.llm_router.MAX_RETRY_AFTER_SECONDS``)
    bounds a bad hint so one request can never hang a whole run.
    """
    from src.llm_router import MAX_RETRY_AFTER_SECONDS

    hint = float(getattr(exc, "retry_after", None) or 0.0)
    backoff = min(
        _RATE_LIMIT_BACKOFF_BASE * (2 ** (attempt - 1)),
        _RATE_LIMIT_WAIT_CAP,
    )
    return min(max(backoff, hint), MAX_RETRY_AFTER_SECONDS)


def _call_completion(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
    *,
    _key_index: int = 0,
) -> Any:
    """One graded request, waiting out a per-minute 429 instead of failing.

    The inner attempt (:func:`_call_completion_once`) owns routing: it rotates
    to the next Groq key, honours key cooldowns, and hands over to the fallback
    provider. One re-serve is still not enough on a tight OTPM bucket - the
    re-served attempt can be throttled too (the cooldown expired mid-window,
    the rotated key is limited as well, the fallback has its own per-minute
    cap, or there is no router at all). Tenacity deliberately never sleeps on
    a 429, so without this loop every one of those cases surfaced as
    ``LLMAPIError`` -> an ERROR row for the student.

    So a per-minute 429 re-runs the whole routing decision after a pause that
    honours the provider's ``retry-after`` hint (or an escalating back-off when
    the hint is missing), up to :data:`_RATE_LIMIT_RETRIES` extra rounds, and
    only then surfaces the failure. A *daily* quota still fails fast as
    ``DailyQuotaExhausted`` - it cannot refill inside this run.
    """
    from src.llm_router import ticked_sleep

    attempts = 0
    while True:
        try:
            return _call_completion_once(
                client,
                system_prompt,
                user_prompt,
                response_format,
                _key_index=_key_index,
            )
        except LLMAPIError as exc:
            if exc.status_code != 429 or attempts >= _RATE_LIMIT_RETRIES:
                raise
            attempts += 1
            wait = _rate_limit_wait(exc, attempts)
            logger.warning(
                "Rate limit hit (per-minute); backing off %.1fs before "
                "re-routing (attempt %d/%d): %s",
                wait,
                attempts,
                _RATE_LIMIT_RETRIES,
                _one_line(exc),
            )
            ticked_sleep(wait, None, "rate-limit backoff")


def _call_completion_once(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
    *,
    _key_index: int = 0,
) -> Any:
    """One graded request with transport/HTTP errors mapped to our own types.

    Rate-limit resilience: on HTTP 429 the shared router cools the serving key
    (honoring the ``retry-after`` header) and the *same attempt* is re-served
    by the next key - or by the fallback provider when every key is cooling.
    If that re-served attempt is throttled *again*, the wait-and-retry budget
    in :func:`_call_completion` takes over rather than failing the student.
    The adaptive pacer stretches the inter-student pause after each 429.

    The first attempt is routed too, rather than sent on whatever client the
    caller cached. That distinction is the whole TPD fix: ``_get_client()`` is
    built once from a single Groq key, so reusing it blindly fired a fresh 429
    at a bucket the router had *already* parked, for every remaining student,
    instead of letting Gemini serve them. Asking the router first means an
    exhausted pool costs one wasted request, not one per student.
    """
    from src.llm_router import DailyQuotaExhausted, is_daily_quota_error, parse_retry_after as _parse_ra

    router = _router()
    if router is not None:
        try:
            decision = router.pick()
        except DailyQuotaExhausted:
            raise
        except Exception:
            decision = None
        if decision is not None:
            if decision.endpoint.is_fallback:
                # Every Groq key is parked: serve the fallback directly, on the
                # decision already taken, so this request never touches Groq
                # and `fallback_uses` is not counted twice by a second pick().
                return _serve_decision(
                    decision, router, system_prompt, user_prompt, response_format
                )
            # A healthy Groq key: build from it so key rotation actually
            # reaches the wire and the 429 below is reported against the key
            # that was really throttled (never blindly index 0).
            client = _client_for(decision)
            _key_index = decision.key_index
    try:
        return _create_completion(
            client, system_prompt, user_prompt, response_format
        )
    except APIStatusError as exc:
        if exc.status_code == 429:
            daily = _note_rate_limited(exc, _key_index, _parse_ra)
            if daily:
                # The daily token quota is shared by every key in the
                # organization, so no other Groq key can serve this request.
                if router is not None and router.has_fallback:
                    # A fallback is configured: hand over immediately. pick()
                    # cannot return a Groq key now - they are all parked.
                    return _serve_from_router(router, system_prompt, user_prompt, response_format)
                # Nothing to hand over to. Fail fast and clearly rather than
                # burning the retry budget on a provider that is done until the
                # quota resets (hours away).
                raise DailyQuotaExhausted(
                    f"Groq daily token quota reached and no fallback provider is "
                    f"configured: {_error_detail(exc)}"
                ) from exc
            if router is not None:
                return _serve_from_router(
                    router, system_prompt, user_prompt, response_format
                )
        raise LLMAPIError(
            f"Groq API request failed (HTTP {exc.status_code}): "
            f"{_error_detail(exc)}",
            status_code=exc.status_code,
            # Carried so _call_completion can honour the hint on its wait
            # before re-serving this student instead of writing an ERROR row.
            retry_after=(_parse_ra(exc) if exc.status_code == 429 else None),
        ) from exc
    except ValidationError as exc:
        raise LLMJSONParsingError(
            f"Groq response violated the GradingResult schema: {_one_line(exc)}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - boundary wraps unexpected failures
        raise LLMEvaluationError(f"Groq evaluation failed: {exc}") from exc


def _serve_from_router(
    router: Any,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
) -> Any:
    """Re-serve one attempt from whatever the router can still reach.

    Single home for the "primary key failed, try the next one" path, used by
    both the per-minute retry and the daily-quota hand-over, so the two cannot
    drift apart in behaviour.
    """
    return _serve_decision(
        router.pick(), router, system_prompt, user_prompt, response_format
    )


def _serve_decision(
    decision: Any,
    router: Any,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any] | None,
) -> Any:
    """Send one attempt on an *already-taken* routing decision.

    Split out of :func:`_serve_from_router` so a caller that has already called
    ``router.pick()`` reuses that decision instead of picking a second time
    (which inflated ``fallback_uses`` and skipped a key in the rotation).

    A 429 here is attributed to whatever is actually throttled. That matters
    most for the fallback: reporting Gemini's own limit against a *Groq* key
    index used to park a perfectly healthy key, and reporting it as a daily
    quota used to silently fail every remaining student with an opaque error
    instead of saying both providers are out.
    """
    from src.llm_router import DailyQuotaExhausted, is_daily_quota_error, parse_retry_after as _parse_ra

    is_fallback = bool(getattr(decision.endpoint, "is_fallback", False))
    routed = _client_for(decision)
    try:
        response = _create_completion(
            routed,
            system_prompt,
            user_prompt,
            response_format,
            model=decision.endpoint.model or None,
        )
    except APIStatusError as exc:
        if exc.status_code == 404 and is_fallback:
            # A retired fallback model id (e.g. gemini-2.0-flash answering
            # "no longer available"). Retire the id (Gemini endpoints walk
            # GEMINI_MODEL_CANDIDATES) and retry this same student on the next
            # live model instead of failing the whole batch on one dead id.
            from src.llm_router import is_unknown_model_error

            if is_unknown_model_error(_error_detail(exc)):
                try:
                    moved = router.report_unknown_model(
                        decision.key_index, decision.endpoint.model or ""
                    )
                except Exception:
                    moved = False
                if moved:
                    logger.warning(
                        "Fallback model '%s' retired (HTTP 404); retrying on '%s'.",
                        decision.endpoint.model,
                        router.fallback.model,
                    )
                    return _serve_from_router(
                        router, system_prompt, user_prompt, response_format
                    )
            raise LLMAPIError(
                f"{decision.endpoint.name} API request failed "
                f"(HTTP 404): model '{decision.endpoint.model}' is not "
                f"available - set LLM_FALLBACK_MODEL in .env to a live id: "
                f"{_error_detail(exc)}",
                status_code=exc.status_code,
                retry_after=None,
            ) from exc
        if exc.status_code == 429 or _is_retryable_error(exc):
            if exc.status_code == 429:
                if is_fallback:
                    # The fallback is out too. Do not report this against a Groq
                    # key (it never touched one) and do not retry a bucket that
                    # will not refill inside this run - stop with a message a
                    # teacher can act on.
                    if is_daily_quota_error(_error_detail(exc)):
                        raise DailyQuotaExhausted(
                            f"Groq daily quota is exhausted and the fallback "
                            f"({decision.endpoint.name}: {decision.endpoint.model}) "
                            f"is also out of daily quota: {_error_detail(exc)}"
                        ) from exc
                else:
                    _note_rate_limited(exc, decision.key_index, _parse_ra)
            raise LLMAPIError(
                f"{decision.endpoint.name} API request failed "
                f"(HTTP {exc.status_code}): {_error_detail(exc)}",
                status_code=exc.status_code,
                # The routed path re-serves through _call_completion's
                # back-off loop, which needs the provider's own hint.
                retry_after=(_parse_ra(exc) if exc.status_code == 429 else None),
            ) from exc
        raise LLMAPIError(
            f"{decision.endpoint.name} API request failed "
            f"(HTTP {exc.status_code}): {_error_detail(exc)}",
            status_code=exc.status_code,
            retry_after=None,
        ) from exc
    except (APITimeoutError, APIConnectionError) as exc:
        # A hung socket is transient by nature (server hiccup, middlebox
        # dropping an idle keep-alive). Handled like a 5xx so the outer
        # retry re-serves through the router (possibly on another key),
        # instead of writing the student off after one bad connection.
        raise LLMAPIError(
            f"{decision.endpoint.name} API request timed out: {exc}.",
            status_code=None,
            retry_after=None,
        ) from exc
    try:
        if router is not None:
            router.report_success(decision.key_index)
    except Exception:
        pass
    return response


def _client_for(decision: Any) -> OpenAI:
    """Build the SDK client for a routing decision (fresh: keys rotate)."""
    endpoint = decision.endpoint
    return OpenAI(
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
        timeout=_REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )


def _note_rate_limited(exc: BaseException, key_index: int, parse_ra: Any) -> bool:
    """Record a 429 on the router, parking the key if it is a *daily* quota.

    Returns True when the provider said the daily bucket is gone. That is the
    distinction the whole batch depends on: a per-minute 429 refills within the
    minute and is worth cooling for, while a tokens-per-day 429 is shared across
    every key in the organization and cannot be fixed by rotating keys or
    waiting - so the key is parked and the next request goes to the fallback.
    """
    # Imported in *this* scope, not inherited from the callers: Python has no
    # cross-function scope, so the callers' own imports left this helper
    # raising NameError on the very first 429 it existed to record.
    from src.llm_router import is_daily_quota_error

    try:
        retry_after = parse_ra(exc)
    except Exception:
        retry_after = None
    try:
        detail = _error_detail(exc) if isinstance(exc, APIStatusError) else str(exc)
    except Exception:
        detail = ""
    daily = is_daily_quota_error(detail)
    try:
        router = _router()
        if router is not None:
            if daily:
                router.report_daily_exhausted(key_index, retry_after)
            else:
                router.report_rate_limited(key_index, retry_after)
    except Exception:
        pass
    try:
        pacer = _pacer()
        if pacer is not None:
            pacer.report_rate_limited()
    except Exception:
        pass
    return daily


def evaluate_student_submission(
    student_name: str,
    raw_doc_text: str,
    rubric: Rubric,
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
    prompt, doc_trimmed = _build_prompt(student_name, raw_doc_text, rubric)

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
    # Deterministic audit pass (zero tokens): verifies score math, bounds,
    # status coherence, rubric coverage. Auto-fixes a provably-wrong score;
    # flags anything ambiguous. Verdict travels on the sidecar for GradingRow.
    try:
        _run_audit(result, rubric, f"{rubric_title(rubric)}/{student_name}")
        pacer = _pacer()
        if pacer is not None:
            pacer.report_success()
    except Exception:
        pass
    logger.info(
        "Graded '%s' for %s with %s: score=%d (%d task evaluations)%s",
        rubric_title(rubric),
        student_name,
        settings.groq_model,
        result.score,
        len(result.task_evaluations),
        " [document trimmed for the token budget]" if doc_trimmed else "",
    )
    return result


@dataclass
class StudentGradeRequest:
    """One student's grading input for :func:`grade_students_batch`."""

    student_name: str
    raw_doc_text: str
    late_penalty_points: int = 0


@dataclass
class StudentGradeOutcome:
    """One student's grading outcome: result, error, or quota stop."""

    student_name: str
    result: GradingResult | None = None
    error: str | None = None
    quota_exhausted: bool = False


def _grade_one_request(
    req: StudentGradeRequest, rubric: Rubric, stop_event: threading.Event
) -> StudentGradeOutcome:
    """Grade one batch student; never raises (errors ride on the outcome)."""
    try:
        result = evaluate_student_submission(
            req.student_name,
            req.raw_doc_text,
            rubric,
            late_penalty_points=req.late_penalty_points,
        )
        return StudentGradeOutcome(student_name=req.student_name, result=result)
    except DailyQuotaExhausted as exc:
        stop_event.set()
        return StudentGradeOutcome(
            student_name=req.student_name,
            error=f"Daily quota exhausted: {exc}",
            quota_exhausted=True,
        )
    except LLMEvaluationError as exc:
        return StudentGradeOutcome(
            student_name=req.student_name,
            error=f"LLM evaluation failed: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - one bad student never kills a batch
        return StudentGradeOutcome(
            student_name=req.student_name,
            error=f"LLM evaluation failed: {exc}",
        )


def grade_students_batch(
    requests: Sequence[StudentGradeRequest],
    rubric: Rubric,
    *,
    max_workers: int | None = None,
    on_result: Any = None,
    pause: Any = None,
) -> list[StudentGradeOutcome]:
    """Grade many students concurrently, preserving input order.

    The grading loop is I/O-bound (each student = one HTTP round trip +
    waiting), so sequential grading leaves the machine idle ~95% of the time.
    A small thread pool overlaps those waits: each worker grades through the
    shared module router (thread-safe via its own lock, round-robins Groq
    keys across workers), so concurrent workers land on different keys/tiers
    instead of queueing on one - the "rotate the provider" idea, happening
    naturally.

    ``on_result`` (optional) fires on the *worker* thread after each student
    finishes: ``on_result(outcome, done, total)``. UI callers must only use it
    to stash thread-safe state (never touch Streamlit widgets from it - they
    are main-thread only).

    ``pause`` (optional) is called before each student's evaluation is
    submitted to the pool, throttling the request rate so concurrent workers
    do not burst Groq's per-minute output-token bucket (which truncates
    responses mid-JSON). Pass ``paced_pause`` from callers that want the same
    adaptive pacing the dashboard dry-run and daily check use.

    A :class:`~src.llm_router.DailyQuotaExhausted` from any worker stops new
    work from starting (already-running workers finish), and every
    not-yet-started student comes back with ``quota_exhausted=True`` instead
    of N identical errors.
    """
    items = list(requests)
    total = len(items)
    if not total:
        return []
    workers = max(1, max_workers if max_workers is not None else _max_workers())
    workers = min(workers, total)
    # When a pacing pause is supplied, serialize submissions so only one LLM
    # request is in flight at a time. Groq's free tier caps output tokens per
    # minute (~1000 TPM on qwen/qwen3.8-27b); 3 concurrent Hebrew evaluations
    # easily burst that budget and get truncated mid-JSON, which then burns
    # the fallback provider's daily quota. One-at-a-time keeps TPM safely under
    # the limit while the 2s pause keeps RPM under the 30 RPM cap.
    if pause is not None:
        workers = 1
    outcomes: list[StudentGradeOutcome | None] = [None] * total
    stop_event = threading.Event()
    completed = 0
    completed_lock = threading.Lock()

    def _done(outcome: StudentGradeOutcome) -> None:
        nonlocal completed
        with completed_lock:
            completed += 1
            done = completed
        if on_result is not None:
            try:
                on_result(outcome, done, total)
            except Exception:
                logger.debug("batch on_result failed", exc_info=True)

    if workers == 1:
        for index in range(total):
            if stop_event.is_set():
                outcomes[index] = StudentGradeOutcome(
                    student_name=items[index].student_name,
                    error="Not graded: daily quota exhausted earlier in this batch.",
                    quota_exhausted=True,
                )
            else:
                if pause is not None and index > 0:
                    pause()
                outcomes[index] = _grade_one_request(items[index], rubric, stop_event)
            _done(outcomes[index])  # type: ignore[arg-type]
        return [o for o in outcomes if o is not None]

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="grade"
    ) as pool:
        pending: dict[Any, int] = {}
        next_to_start = 0
        # Prime the pool, then feed the next student as each worker frees up:
        # keeps exactly ``workers`` requests in flight without ever starting
        # new work after a quota stop. ``pause`` is applied before each
        # submission so requests to Groq are spaced out instead of bursting
        # the per-minute output-token bucket.
        while next_to_start < min(workers, total) and not stop_event.is_set():
            if pause is not None and next_to_start > 0:
                pause()
            pending[pool.submit(_grade_one_request, items[next_to_start], rubric, stop_event)] = next_to_start
            next_to_start += 1
        while pending:
            finished, _ = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in finished:
                index = pending.pop(future)
                try:
                    outcomes[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - never lose a slot
                    outcomes[index] = StudentGradeOutcome(
                        student_name=items[index].student_name,
                        error=f"LLM evaluation failed: {exc}",
                    )
                _done(outcomes[index])  # type: ignore[arg-type]
                if next_to_start < total and not stop_event.is_set():
                    if pause is not None:
                        pause()
                    pending[pool.submit(_grade_one_request, items[next_to_start], rubric, stop_event)] = next_to_start
                    next_to_start += 1
        for index in range(total):
            if outcomes[index] is None:
                outcomes[index] = StudentGradeOutcome(
                    student_name=items[index].student_name,
                    error="Not graded: daily quota exhausted earlier in this batch.",
                    quota_exhausted=True,
                )
    return [o for o in outcomes if o is not None]
