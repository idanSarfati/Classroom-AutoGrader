"""Evaluator configuration: sampling knobs, retry/pacing constants, prompt strings, rubric type alias, and shared helpers built on them.

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


logger = logging.getLogger(__name__)

# Silence SDK console chatter regardless of the application's root log level:
# - httpx / httpx2: per-request "HTTP Request: POST ... 200 OK" INFO lines.
# - openai: verbose request/response DEBUG dumps and retry notices.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# Union of rubric schemas the evaluator understands. The legacy path is a flat
# list of tasks (``AssignmentConfig``); the richer path is nested sections each
# containing questions (``AssignmentSpec``).
Rubric = AssignmentConfig | AssignmentSpec


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
- Link Submissions: An external link is NOT code. If the student's answer consists only of a link (no pasted code), award NO grade (empty score) and ask the student to write/paste the actual code instead of a link. Never treat a link alone as a correct answer.

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
