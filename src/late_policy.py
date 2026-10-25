"""Late-submission policy: decide lateness, then charge for it.

This is a **business rule**, so it is applied in code and never delegated to
the grading model. The model already has to keep ``sum(points_deducted) ==
100 - score`` honest (see :func:`src.llm_evaluator._audit_deductions`), and
asking it to also perform a fixed subtraction reliably is how a policy ends up
silently applied to some students and not others. Deterministic code makes the
penalty identical for every late submission, and visible to the teacher as its
own line in the deduction breakdown.

Lateness is resolved from the strongest available signal, in this order:

1. The submission's original hand-in time - from submission history, or the
   ``creationTime`` Classroom reports on the submission resource
   (:attr:`src.models.StudentSubmission.first_submitted_at`). A resubmission
   moves ``submitted_at`` (the last update) but not this instant, so work
   first handed in on time stays on time no matter how late the resubmission
   is. This is the first preference: if the original submission was on time,
   no late penalty is charged for the resubmission.
2. ``lateState`` - Classroom's own verdict (``LATE`` / ``ON_TIME``). It already
   accounts for the teacher's late-submission policy and any grace period, so
   it wins whenever the API reports it.
3. ``late`` - the boolean flag Classroom computes against the due date.
4. The timestamps - ``submitted_at`` compared with the coursework's exact due
   instant. This is an approximation (it is the submission's *last update*,
   not a dedicated hand-in timestamp), so it is only a fallback and a warning
   is logged whenever it decides the outcome. When it is the only evidence of
   lateness and a previous grading comment exists on the document, the comment
   is consulted: it opens with the late notice iff an earlier run already
   charged the penalty for this submission's original hand-in; no notice means
   the original was on time and the late timestamp belongs to a resubmission,
   so no penalty is charged.

If none of the above is available the submission is treated as on time: a
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
_POLICY_FIELDS = ("late_state", "is_late", "submitted_at", "first_submitted_at")


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
    submission: StudentSubmission,
    coursework: Optional[CourseWork] = None,
    *,
    first_submitted_at: Optional[datetime] = None,
    prior_comment: Optional[str] = None,
) -> bool:
    """Whether ``submission`` counts as late for ``coursework``.

    Returns ``False`` whenever the available signals cannot establish that the
    work was late - see the module docstring for the resolution order.

    ``first_submitted_at`` is the original hand-in instant from submission
    history (or the submission resource's creation time); it overrides every
    weaker signal, so a late resubmission of work first handed in on time is
    not penalised. ``prior_comment`` is the text of this app's previous
    grading comment on the submission's document; it is only consulted when
    history is unavailable and the last-update timestamp alone says late.
    """
    _require_policy_fields(submission)
    due = coursework.due_datetime if coursework is not None else None
    due_utc = _as_utc(due) if due is not None else None

    # 1. Classroom's own verdict already accounts for grace periods. This is
    #    the most authoritative signal - if Classroom says the work is late,
    #    it was turned in after the deadline regardless of when the submission
    #    record was first created (creationTime only marks when the draft was
    #    opened, not when it was handed in).
    state = (submission.late_state or "").strip().upper()
    if state:
        if state == "LATE":
            return True
        if state in _ON_TIME_STATES:
            return False
        # An unrecognised state is not a verdict; fall through to the other
        # signals rather than guessing.

    # 2. The boolean flag Classroom computes against the due date.
    if submission.is_late is not None:
        return bool(submission.is_late)

    # 3. Submission history: the original hand-in instant. A resubmission
    #    moves ``submitted_at`` but not this one, so it is the strongest
    #    signal for the "on time originally, late resubmission" case - but
    #    only consulted when Classroom provides no verdict at all.
    first = (
        first_submitted_at
        if first_submitted_at is not None
        else submission.first_submitted_at
    )
    if first is not None:
        if due_utc is None:
            # No deadline to measure against - nothing can be late.
            return False
        late = _as_utc(first) > due_utc
        logger.info(
            "Submission history for %s: first submitted %s, due %s -> late=%s",
            submission.submission_id or "?",
            _as_utc(first).isoformat(),
            due_utc.isoformat(),
            late,
        )
        return late    # 3. The boolean flag.
    if submission.is_late is not None:
        return bool(submission.is_late)

    # 4. Timestamp fallback: the submission's *last update*, not its first
    #    hand-in, so a late resubmission can make on-time work look late.
    if due_utc is not None and submission.submitted_at is not None:
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
        if late and prior_comment is not None:
            # A previous grading run already commented on this document.
            # If it never mentioned lateness, the original hand-in was on
            # time and the late timestamp belongs to a resubmission.
            return _has_late_notice(prior_comment)
        return late

    # 5. No signal at all: never invent a penalty.
    logger.warning(
        "No lateness signal for submission %s; grading it as on time.",
        submission.submission_id or "?",
    )
    return False


def penalty_for(
    submission: StudentSubmission,
    coursework: Optional[CourseWork] = None,
    penalty_points: int = DEFAULT_LATE_PENALTY_POINTS,
    *,
    first_submitted_at: Optional[datetime] = None,
    prior_comment: Optional[str] = None,
) -> int:
    """Points to deduct for ``submission``: the penalty if late, else zero.

    Negative or oversized values are clamped, so a misconfigured
    ``LATE_PENALTY_POINTS`` can only ever make the penalty smaller or equal to
    the whole grade - never push a score below zero.

    ``first_submitted_at`` and ``prior_comment`` are forwarded to
    :func:`is_late`; see there for the resubmission-aware resolution order.
    """
    if penalty_points <= 0 or not is_late(
        submission,
        coursework,
        first_submitted_at=first_submitted_at,
        prior_comment=prior_comment,
    ):
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


def _has_late_notice(text: str) -> bool:
    """True when ``text`` opens with the late-penalty notice.

    Recognises the notice an earlier run of :func:`apply_late_penalty` left
    on the document, whatever its exact wording (it was corrected once), using
    the same marker and length bounds as :func:`_strip_prior_notice`. Used by
    the fallback: when submission history is unavailable and the last-update
    timestamp alone says late, a previous comment that never mentioned
    lateness means the original hand-in was on time.
    """
    text = (text or "").strip()
    if not text:
        return False
    first, _separator, _rest = text.partition("\n\n")
    return _LATE_NOTICE_MARKER in first and len(first) <= _MAX_NOTICE_CHARS


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
