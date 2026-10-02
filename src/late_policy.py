"""Late-submission policy: decide lateness, then charge for it.

This is a **business rule**, so it is applied in code and never delegated to
the grading model. The model already has to keep ``sum(points_deducted) ==
100 - score`` honest (see :func:`src.llm_evaluator._audit_deductions`), and
asking it to also perform a fixed subtraction reliably is how a policy ends up
silently applied to some students and not others. Deterministic code makes the
penalty identical for every late submission, and visible to the teacher as its
own line in the deduction breakdown.

Lateness is resolved from the strongest available signal, in this order:

1. ``lateState`` - Classroom's own verdict (``LATE`` / ``ON_TIME``). It already
   accounts for the teacher's late-submission policy and any grace period, so
   it wins whenever the API reports it.
2. ``late`` - the boolean flag Classroom computes against the due date.
3. The timestamps - ``submitted_at`` compared with the coursework's exact due
   instant. This is an approximation (it is the submission's *last update*,
   not a dedicated hand-in timestamp), so it is only a fallback and a warning
   is logged whenever it decides the outcome.

If none of the three is available the submission is treated as on time: a
missing signal must never invent a penalty on a student's grade.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.models import (
    CourseWork,
    DeductionItem,
    GradingResult,
    StudentSubmission,
)

logger = logging.getLogger(__name__)

# Used when neither the caller nor the environment configures a penalty.
DEFAULT_LATE_PENALTY_POINTS = 10

# Label of the synthetic breakdown row, so the teacher can see at a glance
# that the lost points were a deadline penalty and not a content gap.
LATE_PENALTY_TASK_NAME = "Late submission"

LATE_PENALTY_REASON = (
    "Submitted after the deadline, so the standard late penalty was applied."
)

# Opening clause of the notice the student reads. Held separately because it
# never varies, while the tail states how many points were taken.
_LATE_NOTICE_OPENING = "העבודה הוגשה לאחר מועד ההגשה, ולכן"

# Word that appears in every rendering of the notice, present and past, and the
# length bound that distinguishes it from the model's own opening paragraph.
_LATE_NOTICE_MARKER = "איחור"
_MAX_NOTICE_CHARS = 160


def late_penalty_sentence(points: int) -> str:
    """The single sentence shown to the student explaining the deduction.

    ``points`` is the number **actually** charged (not the configured penalty),
    so the figure the student reads always matches the deduction breakdown.

    Hebrew takes the singular for one ("נקודה אחת") - "1 נקודות" is
    ungrammatical - so that one case is rendered separately; from two upward
    the plural is used, matching the wording of the school's own notices.
    """
    if points == 1:
        tail = "הופחה נקודה אחת בגין איחור."
    else:
        tail = f"הופחתו {points} נקודות בגין איחור."
    return f"{_LATE_NOTICE_OPENING} {tail}"

# States Classroom reports for a submission that is not late.
_ON_TIME_STATES = {"ON_TIME", "NEEDS_GRADING"}

# The submission fields this policy reads. Used to turn a stale-schema failure
# into an actionable message (see :func:`_require_policy_fields`).
_POLICY_FIELDS = ("late_state", "is_late", "submitted_at")


def _require_policy_fields(submission: StudentSubmission) -> None:
    """Fail loudly, and usefully, when the model predates this policy.

    A long-running Streamlit server imports ``src.models`` once and then keeps
    the class in ``sys.modules`` across every rerun. Editing the schema does
    not refresh it, so a *newly imported* ``late_policy`` can be handed an
    *old* ``StudentSubmission`` and fail deep inside pydantic with
    ``'StudentSubmission' object has no attribute 'late_state'`` - which says
    nothing about the actual cause. The fix is always the same: restart the
    server.
    """
    missing = [name for name in _POLICY_FIELDS if not hasattr(submission, name)]
    if missing:
        raise TypeError(
            "This StudentSubmission is from an older src.models that predates "
            f"the late-submission policy (missing: {', '.join(missing)}). "
            "A long-running Streamlit server keeps the previously imported "
            "model in sys.modules, so it is still using the old class. "
            "Restart the Streamlit server and run the evaluation again."
        )


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC datetime, assuming UTC when naive.

    Comparing an aware and a naive datetime raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes``. That
    used to abort the whole grading run, which is the opposite of what this
    module promises: a timestamp it cannot reason about must *degrade* to "no
    verdict", never take a student's grade down with it.

    Classroom documents an offset-less timestamp as UTC (and
    :func:`src.classroom_service._parse_rfc3339` already stamps that onto what
    it parses), so a naive value is read as UTC here too rather than guessed at
    in the machine's local zone - otherwise the same submission would be judged
    late or on time depending on which server ran the grading.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def is_late(
    submission: StudentSubmission, coursework: Optional[CourseWork] = None
) -> bool:
    """Whether ``submission`` counts as late for ``coursework``.

    Returns ``False`` whenever the available signals cannot establish that the
    work was late - see the module docstring for the resolution order.
    """
    _require_policy_fields(submission)
    state = (submission.late_state or "").strip().upper()
    if state:
        if state == "LATE":
            return True
        if state in _ON_TIME_STATES:
            return False
        # An unrecognised state is not a verdict; fall through to the other
        # signals rather than guessing.

    if submission.is_late is not None:
        return bool(submission.is_late)

    due = coursework.due_datetime if coursework is not None else None
    if due is not None and submission.submitted_at is not None:
        due_utc = _as_utc(due)
        submitted_utc = _as_utc(submission.submitted_at)
        late = submitted_utc > due_utc
        logger.warning(
            "Classroom reported no lateness verdict for submission %s; "
            "falling back to timestamps (%s > %s -> late=%s).",
            submission.submission_id or "?",
            submitted_utc.isoformat(),
            due_utc.isoformat(),
            late,
        )
        return late

    logger.warning(
        "No lateness signal for submission %s; grading it as on time.",
        submission.submission_id or "?",
    )
    return False


def penalty_for(
    submission: StudentSubmission,
    coursework: Optional[CourseWork] = None,
    penalty_points: int = DEFAULT_LATE_PENALTY_POINTS,
) -> int:
    """Points to deduct for ``submission``: the penalty if late, else zero.

    Negative or oversized values are clamped, so a misconfigured
    ``LATE_PENALTY_POINTS`` can only ever make the penalty smaller or equal to
    the whole grade - never push a score below zero.
    """
    if penalty_points <= 0 or not is_late(submission, coursework):
        return 0
    return max(0, min(100, int(penalty_points)))


def _strip_prior_notice(feedback: str) -> str:
    """Remove a late notice left by an earlier application of the penalty.

    A notice is recognised by *content* - a short opening paragraph that names
    the lateness - rather than by matching the current wording exactly. That
    matters: the wording was once different and misspelled, and a student
    already holding a comment with the old sentence would otherwise end up with
    the corrected one stacked on top of the typo when the run is repeated.

    The length bound keeps the model's own feedback safe, since it is several
    sentences long and is never this short.
    """
    text = (feedback or "").strip()
    if not text:
        return ""
    first, separator, rest = text.partition("\n\n")
    if not separator:
        return text
    if _LATE_NOTICE_MARKER in first and len(first) <= _MAX_NOTICE_CHARS:
        return rest.strip()
    return text


def apply_late_penalty(result: GradingResult, points: int) -> GradingResult:
    """Charge ``points`` for a late submission and say so in the feedback.

    Mutates and returns ``result``. Three things happen together, because a
    penalty that is not visible is indistinguishable from no penalty at all:

    * ``score`` drops, never below zero;
    * a ``LATE_PENALTY_TASK_NAME`` row is added to ``deduction_breakdown`` with
      the points *actually* removed, so ``sum(points_deducted) == 100 - score``
      still holds even when the score was clamped at zero;
    * the Hebrew feedback is prefixed with the reason.

    Calling this twice is safe: the previous late row is refunded and replaced
    rather than stacked, so a re-run cannot charge the penalty twice.
    """
    if points <= 0:
        return result

    # Reverse any previous charge before applying a fresh one. Dropping the
    # row alone is not enough: the score was already reduced, so without this a
    # re-run would deduct the penalty twice (95 -> 85 -> 75).
    refunded = 0
    remaining: list[DeductionItem] = []
    for item in result.deduction_breakdown:
        if item.task_name == LATE_PENALTY_TASK_NAME:
            refunded += item.points_deducted
        else:
            remaining.append(item)
    result.deduction_breakdown = remaining
    result.score = min(100, result.score + refunded)

    before = result.score
    result.score = max(0, before - points)
    charged = before - result.score
    if charged <= 0:
        # The grade was already 0, so there is nothing left to take.
        return result

    result.deduction_breakdown.append(
        DeductionItem(
            task_name=LATE_PENALTY_TASK_NAME,
            points_deducted=charged,
            reason=LATE_PENALTY_REASON,
        )
    )
    notice = late_penalty_sentence(charged)
    # Strip a notice a previous run prepended, so the student is told once
    # however many times the penalty is re-applied.
    existing = _strip_prior_notice(result.feedback_hebrew)
    result.feedback_hebrew = f"{notice}\n\n{existing}" if existing else notice
    logger.info(
        "Applied a %d-point late penalty to '%s' (score %d -> %d).",
        charged,
        result.student_name or "?",
        before,
        result.score,
    )
    return result
