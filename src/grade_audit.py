"""Deterministic audit pass for LLM grading results (zero extra tokens).

A pure-local second pair of eyes after every evaluation: checks score math,
bounds, status/score coherence, rubric coverage, exempt tasks, typo rule.
Outcome: AuditVerdict (clean / auto-fixed / needs-review). With autofix on
(default) a provably-wrong score is repaired to 100 - sum(deductions).
"""
from __future__ import annotations
import logging
import re
from types import SimpleNamespace
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from src.models import AssignmentSpec

logger = logging.getLogger(__name__)

FLAG_DEDUCTION_SUM_MISMATCH = "deduction_sum_mismatch"
FLAG_SCORE_OUT_OF_RANGE = "score_out_of_range"
FLAG_NEGATIVE_DEDUCTION = "negative_deduction"
FLAG_CORRECT_TASK_WITH_DEDUCTION = "correct_task_with_deduction"
FLAG_ZERO_DEDUCTION_NON_CORRECT = "zero_deduction_non_correct"
FLAG_MISSING_TASK_EVALUATION = "missing_task_evaluation"
FLAG_UNKNOWN_TASK_ID = "unknown_task_id"
FLAG_EXEMPT_TASK_GRADED = "exempt_task_graded"
FLAG_TYPO_DEDUCTION_CLAIM = "typo_deduction_claim"
FLAG_EMPTY_FEEDBACK = "empty_feedback"
# The breakdown itself is impossible: more points deducted than the assignment
# has. Unlike a plain sum mismatch this is not a slipped addition, it means the
# itemisation was fabricated, so there is no correct score to derive from it.
FLAG_DEDUCTIONS_EXCEED_MAX = "deductions_exceed_max"

# Severity tiers. The distinction is what keeps the audit usable:
#
#   error   - the number is provably wrong (the math does not close).
#   warning - this student's grade breaks a rule the teacher set.
#   info    - systemic: identical for every student, because the evaluator
#             omitted a task row or graded a task the rubric marks exempt. The
#             fix is one rubric/prompt change, not N per-grade decisions, so
#             counting these as "needs review" would bury the real problems.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# Flags that describe the whole batch rather than one student. The daily audit
# rolls these up separately so they read as tuning advice, not as work.
SYSTEMIC_FLAGS = frozenset(
    {FLAG_MISSING_TASK_EVALUATION, FLAG_EXEMPT_TASK_GRADED, FLAG_UNKNOWN_TASK_ID}
)

_BLOCKING_SEVERITIES = frozenset({SEVERITY_ERROR, SEVERITY_WARNING})

_TYPO_CLAIM_RE = re.compile(r"(typo|שגיאת? כתיב|שגיאות? כתיב).{0,40}(deduct|הורד|קיזז|-\s*\d)", re.IGNORECASE | re.DOTALL)


@dataclass
class AuditFinding:
    flag: str = ""
    message: str = ""
    severity: str = "warning"


@dataclass
class AuditVerdict:
    ok: bool = True
    autofixed: bool = False
    flags: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    original_score: Optional[int] = None
    final_score: Optional[int] = None
    deductions_total: int = 0

    @property
    def blocking(self) -> list:
        """Findings a human must act on (info-level notes excluded)."""
        return [f for f in self.findings if f.severity in _BLOCKING_SEVERITIES]

    @property
    def systemic(self) -> list:
        """Findings identical across the batch: rubric/prompt advice."""
        return [f for f in self.findings if f.severity == SEVERITY_INFO]

    @property
    def needs_review(self) -> bool:
        """True when a human still has to look at this grade.

        Info-level notes do not qualify: they describe the evaluator's output
        for the whole class, fixed once in the rubric/prompt. Counting them per
        student is exactly what makes a team switch an audit off.
        """
        return bool(self.blocking) and not self.autofixed

    def badge(self) -> str:
        """One-cell status for tables (dashboard, Streamlit, CLI summary)."""
        if self.original_score is None and not self.findings:
            return "-"
        if self.autofixed:
            return f"🔧 auto-fixed: {self.summary()}"
        if self.blocking:
            return f"⚠️ {self.summary()}"
        if self.findings:
            return f"ℹ️ {self.summary()}"
        return "✅ clean"

    def summary(self) -> str:
        if self.ok and not self.autofixed:
            return "audit clean"
        # ``flags`` is de-duplicated while ``findings`` keeps one line per
        # occurrence, so the summary names each problem once and appends the
        # count ("zero_deduction_non_correct x3" instead of repeating it).
        counts: Counter = Counter(f.flag for f in self.findings if f.flag)
        bits = [flag if n == 1 else f"{flag} x{n}" for flag, n in counts.items()]
        prefix = "audit auto-fixed" if self.autofixed else "audit needs review"
        return f"{prefix}: {', '.join(bits)}"

    def to_dict(self) -> dict:
        """JSON-safe form, persisted next to the grade in the report file.

        Deliberately flat and small: the verdict outlives the process (the
        Release tab reads it days later to warn the teacher), so it is stored
        as data rather than kept only in the evaluator's in-memory sidecar.
        """
        return {
            "ok": bool(self.ok),
            "autofixed": bool(self.autofixed),
            "flags": list(self.flags),
            "original_score": self.original_score,
            "final_score": self.final_score,
            "deductions_total": self.deductions_total,
            "summary": self.summary(),
            "findings": [
                {
                    "flag": f.flag,
                    "message": f.message,
                    "severity": f.severity,
                }
                for f in self.findings
            ],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AuditVerdict":
        """Rebuild a verdict from :meth:`to_dict` output (never raises)."""
        if isinstance(data, AuditVerdict):
            return data
        if not isinstance(data, dict):
            return cls()
        verdict = cls(
            ok=bool(data.get("ok", True)),
            autofixed=bool(data.get("autofixed", False)),
            flags=[str(f) for f in (data.get("flags") or [])],
            original_score=data.get("original_score"),
            final_score=data.get("final_score"),
            deductions_total=data.get("deductions_total") or 0,
        )
        for item in data.get("findings") or []:
            if isinstance(item, dict):
                verdict.findings.append(
                    AuditFinding(
                        flag=str(item.get("flag", "")),
                        message=str(item.get("message", "")),
                        severity=str(item.get("severity", "warning")),
                    )
                )
            elif isinstance(item, str):
                verdict.findings.append(AuditFinding(flag=item))
        return verdict


def summarize_verdicts(verdicts: list) -> dict:
    """Rollup for a whole batch: how many were clean/fixed/flagged.

    Used by the daily check and the Streamlit summaries so the teacher sees one
    number ("3 grades need a human look") instead of scrolling every row.
    ``systemic`` counts the findings that are really one batch-wide fix, kept
    apart from the per-grade work so the totals stay actionable.
    """
    total = 0
    clean = 0
    autofixed = 0
    needs_review = 0
    notes = 0
    flags: Counter = Counter()
    systemic: Counter = Counter()
    fixed_points = 0
    for verdict in verdicts or []:
        if verdict is None:
            continue
        total += 1
        if getattr(verdict, "autofixed", False):
            autofixed += 1
            try:
                fixed_points += abs(
                    int(verdict.final_score or 0) - int(verdict.original_score or 0)
                )
            except (TypeError, ValueError):
                pass
        elif getattr(verdict, "needs_review", False):
            needs_review += 1
        elif getattr(verdict, "findings", None):
            notes += 1
        else:
            clean += 1
        for finding in getattr(verdict, "findings", []) or []:
            flag = str(getattr(finding, "flag", "") or "")
            if not flag:
                continue
            if str(getattr(finding, "severity", "")) == SEVERITY_INFO:
                systemic[flag] += 1
            else:
                flags[flag] += 1
    return {
        "total": total,
        "clean": clean,
        "autofixed": autofixed,
        "needs_review": needs_review,
        "notes": notes,
        "corrected_points": fixed_points,
        "flags": dict(flags.most_common()),
        "systemic": dict(systemic.most_common()),
    }


def _norm(name: Any) -> str:
    return str(name or "").strip().casefold()


def _task_map(config: Any) -> dict:
    tasks: list[Any]
    if isinstance(config, AssignmentSpec) or (
        not hasattr(config, "tasks") and hasattr(config, "sections")
    ):
        # AssignmentSpec: flatten questions so the audit can check coverage and
        # exempt tasks per question instead of per legacy task.
        tasks = [
            SimpleNamespace(
                name=f"{section.section_name} - {question.id}",
                exempt=False,
            )
            for section in config.sections
            for question in section.questions
        ]
    else:
        tasks = list(getattr(config, "tasks", []) or [])
    return {_norm(getattr(t, "name", "")): t for t in tasks}


def audit_grading_result(result: Any, config: Any = None, autofix: bool = True) -> AuditVerdict:
    """Audit one GradingResult; optionally repair a provably-wrong score."""
    verdict = AuditVerdict(original_score=getattr(result, "score", None))
    score = getattr(result, "score", 0) or 0
    breakdown = list(getattr(result, "deduction_breakdown", None) or [])
    evals = list(getattr(result, "task_evaluations", None) or [])
    feedback = str(getattr(result, "feedback_hebrew", "") or "")

    def add(flag: str, message: str, severity: str = "warning") -> None:
        verdict.findings.append(AuditFinding(flag=flag, message=message, severity=severity))
        if flag not in verdict.flags:
            verdict.flags.append(flag)

    if not isinstance(score, int) or not (0 <= score <= 100):
        add(FLAG_SCORE_OUT_OF_RANGE, f"Score {score!r} outside 0-100.", severity="error")
    total = 0
    deducted_names: dict = {}
    for item in breakdown:
        pts = getattr(item, "points_deducted", 0) or 0
        if pts < 0:
            add(FLAG_NEGATIVE_DEDUCTION, f"'{getattr(item, 'task_name', '?')}' deducts {pts}.")
        total += max(pts, 0)
        deducted_names[_norm(getattr(item, "task_name", ""))] = max(pts, 0)
    verdict.deductions_total = total
    expected = 100 - total
    if total > 100:
        # More points deducted than exist: the breakdown is not merely mis-added,
        # it is internally impossible, so no score can be derived from it.
        add(
            FLAG_DEDUCTIONS_EXCEED_MAX,
            f"Deductions sum {total}, above the 100-point maximum.",
            severity=SEVERITY_ERROR,
        )
    elif total and score != expected:
        add(
            FLAG_DEDUCTION_SUM_MISMATCH,
            f"Deductions sum {total} => score should be {expected}, got {score}.",
            severity="error",
        )
    # Status/coherence: match evaluations to deductions by task name.
    seen_names: dict = {}
    for ev in evals:
        key = _norm(getattr(ev, "task_name", ""))
        seen_names[key] = ev
        status = str(getattr(ev, "status", "") or "").upper()
        pts = deducted_names.get(key, 0)
        if status == "CORRECT" and pts:
            add(FLAG_CORRECT_TASK_WITH_DEDUCTION, f"Task '{getattr(ev, 'task_name', '?')}' CORRECT but deducts {pts}.")
        if status in ("INCORRECT", "MISSING") and not pts:
            add(FLAG_ZERO_DEDUCTION_NON_CORRECT, f"Task '{getattr(ev, 'task_name', '?')}' is {status} with 0 deducted.")
    if config is not None:
        task_map = _task_map(config)
        for key, task in task_map.items():
            if getattr(task, "exempt", False):
                if key in seen_names or key in deducted_names:
                    add(
                        FLAG_EXEMPT_TASK_GRADED,
                        f"Exempt task '{getattr(task, 'name', key)}' was graded; must be skipped.",
                        severity=SEVERITY_INFO,
                    )
                continue
            if key not in seen_names:
                # Systemic, not per-student: every student is missing the same
                # row when the evaluator stops listing tasks it did not deduct.
                add(
                    FLAG_MISSING_TASK_EVALUATION,
                    f"Rubric task '{getattr(task, 'name', key)}' has no evaluation.",
                    severity=SEVERITY_INFO,
                )
        for key in seen_names:
            if key not in task_map:
                add(
                    FLAG_UNKNOWN_TASK_ID,
                    f"Evaluation references unknown task '{key}'.",
                    severity=SEVERITY_INFO,
                )
    # Systemic info-level notes (missing_task_evaluation, unknown_task_id,
    # exempt_task_graded) are fixed in the rubric/prompt, not per-student.
    # They must not be mistaken for per-grade work that needs a human look.
    verdict.ok = not any(
        f.severity in (SEVERITY_ERROR, SEVERITY_WARNING)
        for f in verdict.findings
    )
    if _TYPO_CLAIM_RE.search(feedback or ""):
        add(FLAG_TYPO_DEDUCTION_CLAIM, "Feedback mentions deducting for typos; typos are free.")
    if not (feedback or "").strip():
        add(FLAG_EMPTY_FEEDBACK, "Feedback is empty.")
    verdict.ok = not verdict.findings
    if verdict.autofixed:
        verdict.ok = not any(f.flag != FLAG_DEDUCTION_SUM_MISMATCH for f in verdict.findings)
    return verdict

