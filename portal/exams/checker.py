"""Marking of an exam from its answer key.

Most questions are *objectively* checkable: a multiple-choice answer, lines of
output, or a corrected program that is executed in the sandbox and produces the
required text. Those never touch a model, so the deterministic checker stays
instant, private and free, and the same answer always earns the same score.

A free-text explanation is the one place where words are not the answer: the
keyword rubric can see "מרכאות" without seeing that the student put them in the
wrong place. Checks flagged ``"llm": true`` therefore try
:mod:`portal.exams.llm_review` first - it is handed the question and the same
point rubric the teacher wrote - and fall back to the keyword pass when the
model is off, slow or replies with something unusable. Marking never depends on
the network.

Open items go one step further. An item rule may carry an ``llm_review`` block
with the teacher's detailed guidelines and a point rubric for every part. The
whole item - the explanation and the corrected code together - is sent to the
model in one call, together with the sandbox's own output for the student's
program, so the model judges the fix rather than guessing at it. The
deterministic results are computed first and are the fallback: if anything goes
wrong the paper is marked exactly as it would be without the model.

The checker never re-implements the homework evaluator. It is a separate,
exam-only path; ``portal/grading/adapter.py`` and the engine are untouched.

The report is written into the ordinary ``Grade`` row in the same shape the
engine produces, so the teacher review screen, the gradebook, the CSV export
and the student result page all work with no changes.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from portal.exams import llm_review
from portal.exams.config import ExamConfig, ExamVersion
from portal.exams.paper import load_paper
from portal.worksheets.schema import Item

logger = logging.getLogger(__name__)

CORRECT = "CORRECT"
PARTIALLY_CORRECT = "PARTIALLY_CORRECT"
INCORRECT = "INCORRECT"
MISSING = "MISSING"

STATUS_LABELS_HE = {
    CORRECT: "נכון",
    PARTIALLY_CORRECT: "נכון חלקית",
    INCORRECT: "שגוי",
    MISSING: "לא נענה",
}

# A part with no label of its own still needs a name in the teacher's review,
# so the reader knows which answer the points next to it belong to.
PART_LABELS_HE = {
    "code": "הקוד",
    "code_plain": "הקוד",
    "code_line": "השורה",
    "short": "התשובה",
    "table": "הטבלה",
    "choice": "הבחירה",
}

_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
_SPACE_RE = re.compile(r"[ \t\u00a0]+")


class ExamCheckError(RuntimeError):
    """The check file is missing, invalid, or does not match the paper."""


# ---------------------------------------------------------------- normalising
def normalise_line(line: str) -> str:
    """Compare what a student typed with what Python printed.

    Whitespace width and curly quotes are the two differences that never mean
    the student was wrong, so both are erased. Nothing else is: ``30`` and
    ``"30"`` are different answers, and that difference is the question.
    """
    text = (line or "").translate(_QUOTES).replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text).strip()
    return text


def _plain(value: Any) -> str:
    """One typed answer as text; tables and payload lists are handled elsewhere."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value)


def _filled(value: Any) -> bool:
    if isinstance(value, list):
        return any(_plain(item).strip() for item in value)
    return bool(_plain(value).strip())


def output_lines(value: Any) -> list[str]:
    """Split a typed answer into the lines the student meant to print."""
    if isinstance(value, list):
        raw = "\n".join(_plain(item) for item in value)
    else:
        raw = _plain(value)
    lines = [normalise_line(line) for line in raw.replace("\r\n", "\n").split("\n")]
    return [line for line in lines if line]


def _source(value: Any) -> str:
    return _plain(value)


def _answer_text(value: Any) -> str:
    """What the student handed in, for the teacher's review row.

    A multiple-choice answer arrives as a one-entry list; without this the
    review screen would show an empty cell for every option question.
    """
    if isinstance(value, list):
        return " ".join(item for item in (_plain(entry) for entry in value) if item)
    return _plain(value)


# ---------------------------------------------------------------------- report
@dataclass
class QuestionResult:
    key: str
    title: str
    section: str
    points: int
    earned: int
    status: str
    notes: str
    given: str = ""
    expected: str = ""
    needs_review: bool = False
    # Whether this open item was meant for the model, whether the model actually
    # judged it, and whether it was blocked by an exhausted daily quota. The
    # selective re-check reads these to tell "reviewed" from "still waiting".
    llm_eligible: bool = False
    llm_reviewed: bool = False
    llm_quota_blocked: bool = False
    # One entry per answered part: its points, what it earned and why. The
    # teacher reads the reason under the answer it belongs to, not in a list
    # at the end of the paper.
    parts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        prefix = f"{self.section} · " if self.section else ""
        return f"{prefix}{self.title or self.key}"


@dataclass
class ExamReport:
    exam: str
    version: str
    version_label: str
    score: int
    maximum: int
    questions: list[QuestionResult] = field(default_factory=list)
    runtime_ms: int = 0

    @property
    def needs_review(self) -> bool:
        return any(question.needs_review for question in self.questions)

    @property
    def llm_state(self) -> str:
        """How the model pass went for this paper, for the selective re-check.

        ``off``      - the model is disabled, or no open item was answered;
        ``reviewed`` - every eligible open item got a real model verdict;
        ``waiting``  - at least one eligible item was blocked by daily quota;
        ``failed``   - eligible items exist but a non-quota error kept the rules.

        Only ``reviewed`` is a finished model pass; ``waiting`` and ``failed``
        are both retryable, and ``waiting`` is the one the teacher re-runs once
        the quota resets.
        """
        eligible = [question for question in self.questions if question.llm_eligible]
        if not eligible:
            return "off"
        if any(question.llm_quota_blocked for question in eligible):
            return "waiting"
        if all(question.llm_reviewed for question in eligible):
            return "reviewed"
        return "failed"

    def to_record(self, student_name: str) -> dict[str, Any]:
        """The engine-shaped record stored on the ``Grade`` row."""
        tasks = [
            {
                "task_name": question.label,
                "student_answer_found": _shorten(question.given, 300),
                "status": question.status,
                "notes": question.notes,
            }
            for question in self.questions
        ]
        deductions = [
            {
                "task_name": question.label,
                "points_deducted": question.points - question.earned,
                "reason": question.notes,
            }
            for question in self.questions
            if question.earned < question.points
        ]
        return {
            "student_name": student_name,
            "score": self.score,
            "feedback_hebrew": self._feedback(student_name),
            "deduction_breakdown": deductions,
            "task_evaluations": tasks,
            "exam": {
                "name": self.exam,
                "version": self.version,
                "version_label": self.version_label,
                "maximum": self.maximum,
                "llm_state": self.llm_state,
                "questions": [
                    {
                        "key": question.key,
                        "title": question.label,
                        "points": question.points,
                        "earned": question.earned,
                        "status": question.status,
                        "needs_review": question.needs_review,
                        "llm_reviewed": question.llm_reviewed,
                        "llm_quota_blocked": question.llm_quota_blocked,
                        # Per-part marks travel with the question so the review
                        # screen can put each reason under its own answer.
                        "parts": question.parts,
                    }
                    for question in self.questions
                ],
                "runtime_ms": self.runtime_ms,
            },
        }

    def audit(self) -> dict[str, Any]:
        review = [question for question in self.questions if question.needs_review]
        return {
            "ok": not review,
            "needs_review": bool(review),
            "summary": (
                "כל השאלות נבדקו אוטומטית"
                if not review
                else f"{len(review)} שאלות דורשות מבט של המורה"
            ),
        }

    def _feedback(self, student_name: str) -> str:
        return feedback_text(
            self.score,
            self.maximum,
            [
                question.label
                for question in self.questions
                if question.earned < question.points
            ],
            student_name,
        )


def feedback_text(
    score: int, maximum: int, missed: list[str], student_name: str
) -> str:
    """The Hebrew summary paragraph; shared with manual adjustments.

    A teacher override changes the score and the list of missed questions, so
    the stored paragraph has to be rebuilt from the same thresholds the checker
    used. Keeping one function means the two can never drift apart.
    """
    percent = (score / maximum * 100) if maximum else 0
    if percent >= 90:
        opening = f"כל הכבוד, {student_name}! שליטה יפה מאוד בחומר."
    elif percent >= 75:
        opening = f"עבודה טובה, {student_name}. רוב החומר ברור לך."
    elif percent >= 60:
        opening = f"עברת, {student_name}, ויש כאן בסיס טוב."
    elif percent >= 45:
        opening = f"יש לך חלקים טובים, {student_name}, אבל עדיין יש מה לחזק."
    else:
        opening = f"המבחן היה קשה הפעם, {student_name} - לא נורא, נתרגל."
    if not missed:
        return f"{opening} כל השאלות נכונות."
    return f"{opening} כדאי לחזור על: " + ", ".join(missed) + "."


def _shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------- check config
def load_checks(config: ExamConfig) -> dict[str, Any]:
    path = config.check_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExamCheckError(f"check file not found: {path}") from exc
    except ValueError as exc:
        raise ExamCheckError(f"check file is not valid JSON: {path}") from exc
    if not isinstance(data, dict) or data.get("version") not in (1, 2):
        raise ExamCheckError("unsupported check file version")
    if str(data.get("exam") or "") != config.name:
        raise ExamCheckError("the check file belongs to a different exam")
    versions = data.get("versions")
    if not isinstance(versions, dict) or not versions:
        raise ExamCheckError("the check file has no versions")
    for key in config.version_keys:
        if key not in versions:
            raise ExamCheckError(f"the check file is missing version {key!r}")
    return data


def version_rules(checks: Mapping[str, Any], version_key: str) -> dict[str, Any]:
    rules = (checks.get("versions") or {}).get(version_key)
    if not isinstance(rules, dict):
        raise ExamCheckError(f"version {version_key!r} has no rules")
    items = rules.get("items")
    if not isinstance(items, dict) or not items:
        raise ExamCheckError(f"version {version_key!r} has no item rules")
    return items


# ------------------------------------------------------------------ execution
_EXECUTOR = None


def _executor():
    global _EXECUTOR
    if _EXECUTOR is None:
        from portal.sandbox.executor import SandboxExecutor

        _EXECUTOR = SandboxExecutor()
    return _EXECUTOR


def _run_program(code: str, stdin: str) -> dict[str, Any]:
    """Run student code in the existing sandbox, retrying a busy slot once."""
    from portal.sandbox.executor import SandboxBusy, SandboxError

    for attempt in range(4):
        try:
            result = _executor().run(code, stdin)
            return {
                "ok": True,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
            }
        except SandboxBusy:
            time.sleep(0.4 * (attempt + 1))
        except SandboxError as exc:
            logger.warning("exam check could not start the sandbox: %s", exc)
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "sandbox busy"}


def run_code(code: str, stdin: str = "") -> dict[str, Any]:
    """Run a snippet in the sandbox for the exam self-test.

    ``verify`` uses this to prove that the output the answer key expects is
    really what the paper's own code prints.
    """
    return _run_program(code, stdin)


def _remember(rule: Mapping[str, Any], stdin: str, run: dict[str, Any]) -> None:
    """Record one sandbox run for the model, when an item asks for it.

    The evidence list is attached to the check-rule copy by ``_check_item``.
    Keeping the real stdout and exit code lets the model judge the student's
    fix from facts instead of from the source text - and costs no extra run.
    """
    ledger = rule.get("_evidence")
    if not isinstance(ledger, list):
        return
    ledger.append(
        {
            "stdin": stdin,
            "ok": bool(run.get("ok")),
            "timed_out": bool(run.get("timed_out")),
            "exit_code": run.get("exit_code"),
            "stdout": str(run.get("stdout") or "")[:2000],
            "stderr": str(run.get("stderr") or "")[:800],
        }
    )


def _program_verdict(
    code: str, stdin: str, expected_contains: list[str]
) -> tuple[bool, str]:
    if not code.strip():
        return False, ""
    run = _run_program(code, stdin)
    if not run.get("ok"):
        return False, ""
    if run.get("timed_out"):
        return False, "התוכנית לא הסתיימה בזמן."
    if run.get("exit_code"):
        return False, "התוכנית הסתיימה בשגיאה."
    stdout = normalise_line(run.get("stdout") or "")
    return all(normalise_line(item) in stdout for item in expected_contains), stdout


# ---------------------------------------------------------------------- checks
def _choice(rule: Mapping[str, Any], value: Any) -> tuple[int, str, bool]:
    """One option chosen from a radio group (a list with one entry)."""
    chosen = [
        normalise_line(_plain(item))
        for item in (value if isinstance(value, list) else [value])
    ]
    if not any(chosen):
        return 0, "", False
    earned = int(rule.get("points") or 0)
    # ``answers`` is for a part with several statements, each with its own
    # buttons; the common single-statement question uses ``answer``.
    answers = rule.get("answers")
    if isinstance(answers, list) and answers:
        wanted = [normalise_line(str(item)) for item in answers]
        if len(wanted) != len(chosen):
            raise ExamCheckError("a choice rule does not match the paper")
        if all(got == want for got, want in zip(chosen, wanted)):
            return earned, "כל התשובות נכונות.", False
        return 0, "לפחות אחת התשובות אינה הנכונה.", False
    answer = normalise_line(str(rule.get("answer") or ""))
    picked = next((item for item in chosen if item), "")
    if picked == answer:
        return earned, "התשובה נכונה.", False
    return 0, "התשובה אינה הנכונה.", False


def _lines(rule: Mapping[str, Any], value: Any) -> tuple[int, str, bool]:
    """Compare typed output with the expected output, line by line.

    The rubric names each required line and its points. A line earns its
    points only when it appears in the same order as the real output; extra
    lines a student adds are ignored, because pasting the code above the
    answer is common and teaches nothing to punish. ``ignore_spaces`` is for
    questions that ask what Python printed: ``x = 19`` and ``x=19`` are the
    same answer. Every other difference still counts.
    """
    squash = bool(rule.get("ignore_spaces"))
    entries = rule.get("lines")
    if not isinstance(entries, list) or not entries:
        # Older rule shape: one flat list, points split as evenly as possible.
        expected_lines = [str(line) for line in rule.get("expected") or []]
        total = int(rule.get("points") or 0)
        base, extra = (
            divmod(total, len(expected_lines)) if expected_lines else (0, 0)
        )
        entries = [
            {
                "expected": line,
                "points": base + (1 if index < extra else 0),
            }
            for index, line in enumerate(expected_lines)
        ]
    wanted: list[tuple[str, list[str], int]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ExamCheckError("a line rule must be an object")
        wanted.append(
            (
                _comparison(normalise_line(str(entry.get("expected") or "")), squash),
                [
                    _comparison(normalise_line(str(item)), squash)
                    for item in entry.get("accept") or []
                ],
                int(entry.get("points") or 0),
            )
        )
    given = [_comparison(line, squash) for line in output_lines(value)]
    if not given:
        return 0, "", False
    if not wanted:
        return 0, "", True
    earned = 0
    matched = 0
    cursor = 0
    for expected, accepts, points in wanted:
        index = cursor
        while index < len(given) and not (
            given[index] == expected or given[index] in accepts
        ):
            index += 1
        if index < len(given):
            earned += points
            matched += 1
            cursor = index + 1
        # A line that was not found leaves the cursor alone: a missing first
        # line must not eat the line the student did write.
    if matched == len(wanted):
        return earned, "הפלט נכון.", False
    if matched:
        return (
            earned,
            f"{matched} מתוך {len(wanted)} שורות נכונות (לפי הסדר).",
            False,
        )
    return 0, "הפלט אינו תואם.", False


def _comparison(text: str, squash: bool) -> str:
    if not squash:
        return text
    return "".join(text.split())


def _keywords(rule: Mapping[str, Any], value: Any) -> tuple[int, str, bool]:
    """Award the explanation points named by each concept in the rubric.

    A concept group is one thing the teacher wants said: *this* is the cause,
    *this* is what the output does, *this* is the fix. Full points require every
    group, so one broad word ("מרכאות", "int") can no longer carry the whole
    explanation; a correct sentence in the teacher's own words still earns the
    group it belongs to.

    ``reject_if`` names statements that are the opposite of the expected
    diagnosis ("לא שמו מרכאות" when the quotes are the problem). A match there
    earns nothing and asks the teacher to look, instead of quietly giving the
    cause group for sharing a word with the right answer. ``reject_all`` catches
    the same mistake when the student puts the variable between the words
    ("לא שמו item_name מרכאות"): each inner list is an "any of these" set and
    all sets must match. ``reject_unless`` excuses a false alarm - an answer
    that also says where the quotes belong ("the variable is inside the
    string", "quote only the sentence") is describing the bug, not asking for
    more quotes - and overrides both rejection paths.

    ``review_unless_full`` marks a partial explanation for the teacher: the
    keyword pass is deliberately conservative, so anything short of full is a
    candidate for a human eye rather than a silent deduction.
    """
    text = normalise_line(_source(value)).lower()
    if not text:
        return 0, "", False
    groups = rule.get("groups") or []
    total = int(rule.get("points") or 0)
    excused = [str(word).lower() for word in rule.get("reject_unless") or []]
    # An answer that also explains where the quotes really belong ("take them
    # off the variable", "quote only the sentence") is describing the bug, not
    # asking for the reversed fix; it must reach the concept groups below.
    describes_the_bug = any(_mentions(text, word) for word in excused)
    rejected = [str(word).lower() for word in rule.get("reject_if") or []]
    if (
        not describes_the_bug
        and rejected
        and any(_mentions(text, word) for word in rejected)
    ):
        return 0, "ההסבר מציין את הסיבה ההפוכה לזו שבמחוון.", True
    reject_sets = [
        [str(word).lower() for word in group or []]
        for group in rule.get("reject_all") or []
        if isinstance(group, list)
    ]
    if (
        not describes_the_bug
        and reject_sets
        and all(
            any(_mentions(text, word) for word in group) for group in reject_sets
        )
    ):
        return 0, "ההסבר מציין את הסיבה ההפוכה לזו שבמחוון.", True
    if groups and all(isinstance(group, dict) for group in groups):
        earned = 0
        hit = 0
        missing: list[str] = []
        for group in groups:
            words = [str(word) for word in group.get("words") or []]
            unless = [str(word).lower() for word in group.get("unless") or []]
            if any(_mentions(text, word.lower()) for word in words) and not any(
                _mentions(text, word) for word in unless
            ):
                earned += int(group.get("points") or 0)
                hit += 1
            else:
                missing.append(str(group.get("label") or ""))
        earned = min(earned, total)
        if earned >= total:
            return earned, "ההסבר מזהה את השגיאה.", False
        if earned:
            note = f"ההסבר מכסה {hit} מתוך {len(groups)} רכיבים."
            lacking = ", ".join(item for item in missing if item)
            if lacking:
                note += f" חסר: {lacking}."
            return earned, note, bool(rule.get("review_unless_full"))
        # A correct explanation in a student's own words can miss every keyword.
        return 0, "ההסבר לא זוהה אוטומטית וייבדק על ידי המורה.", True
    # Older rule shape: one minimum number of groups, all points or nothing.
    minimum = int(rule.get("min_groups") or 1)
    hits = sum(
        1
        for group in groups
        if any(_mentions(text, str(word).lower()) for word in group)
    )
    earned = total if hits >= minimum else 0
    if earned:
        return earned, "ההסבר מזהה את השגיאה.", False
    return 0, "ההסבר לא זוהה אוטומטית וייבדק על ידי המורה.", True


def _mentions(text: str, word: str) -> bool:
    """True when a lowered answer mentions ``word``.

    Hebrew is matched as a substring, so a prefixed form the student really
    wrote (``במרכאות``, ``המחרוזת``, ``ולא מספר``) still counts. Latin words
    and code tokens are matched only at the start of a word, so ``int`` inside
    ``print`` and ``str`` inside ``strong`` do not earn points for an answer
    that never named them. Punctuation such as ``+`` is matched as it is,
    because students glue it to the operands.
    """
    if not word:
        return False
    if not word.isascii():
        return word in text
    if word[0].isalnum() or word[0] == "_":
        return _has_token(text, word)
    return word in text


def _has_token(code: str, token: str) -> bool:
    """True when ``token`` appears as code, not inside a longer name.

    Plain substring search would find ``int(`` inside ``print(``, which would
    hand the conversion points to a program that never converted anything.
    A match that continues a name (letter, digit or underscore before it) does
    not count.
    """
    if not token:
        return False
    start = 0
    while True:
        index = code.find(token, start)
        if index < 0:
            return False
        before = code[index - 1] if index else ""
        if not (before.isalnum() or before == "_"):
            return True
        start = index + 1


def _line_contains(stdout: str, groups: list[Any]) -> bool:
    """True when one printed line contains something from every group."""
    for raw in (stdout or "").splitlines():
        line = normalise_line(raw)
        if all(
            any(
                normalise_line(str(alternative)) in line
                for alternative in (group if isinstance(group, list) else [group])
            )
            for group in groups
        ):
            return True
    return False


def _rule_criteria(
    rule: Mapping[str, Any], code: str, runs: list[dict[str, Any]]
) -> tuple[int, str, bool] | None:
    """Mark a program by the rubric's criteria; ``None`` for an older rule.

    Each criterion names its own points and the evidence that earns them: the
    program ran, one printed line holds the required values, or the source
    really performs the required conversion. A criterion is independent, so a
    hardcoded print or a missing conversion loses exactly the points the
    teacher assigned to it.
    """
    specs = rule.get("criteria")
    if not isinstance(specs, list) or not specs:
        return None
    executed: list[dict[str, Any]] = []
    for spec in runs:
        stdin = str(spec.get("stdin") or "")
        outcome = _run_program(code, stdin)
        _remember(rule, stdin, outcome)
        executed.append(outcome)

    def ran(index: int) -> bool:
        if not (0 <= index < len(executed)):
            return False
        run = executed[index]
        return bool(
            run.get("ok")
            and not run.get("timed_out")
            and not run.get("exit_code")
        )

    earned = 0
    passed = 0
    missing: list[str] = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise ExamCheckError("a program criterion must be an object")
        points = int(spec.get("points") or 0)
        ok = True
        if spec.get("all_runs"):
            ok = bool(executed) and all(ran(index) for index in range(len(executed)))
        if ok and "run" in spec:
            index = int(spec["run"])
            ok = ran(index)
            if ok and spec.get("line"):
                ok = _line_contains(
                    executed[index].get("stdout") or "", spec["line"]
                )
        if ok and spec.get("stdout_has"):
            stdout = normalise_line(executed[0].get("stdout") or "") if executed else ""
            ok = all(
                any(
                    normalise_line(str(alternative)) in stdout
                    for alternative in (
                        group if isinstance(group, list) else [group]
                    )
                )
                for group in spec["stdout_has"]
            )
        if ok and spec.get("source_has"):
            ok = all(_has_token(code, str(token)) for token in spec["source_has"])
        if ok and spec.get("source_any"):
            ok = any(_has_token(code, str(token)) for token in spec["source_any"])
        if ok:
            earned += points
            passed += 1
        else:
            missing.append(str(spec.get("label") or ""))
    earned = min(earned, int(rule.get("points") or 0))
    if passed == len(specs):
        return earned, "התוכנית עונה על כל הדרישות.", False
    note = f"התוכנית עומדת ב-{passed} מתוך {len(specs)} קריטריונים."
    lacking = ", ".join(item for item in missing if item)
    if lacking:
        note += f" חסר: {lacking}."
    return earned, note, True


def _program(rule: Mapping[str, Any], value: Any) -> tuple[int, str, bool]:
    code = _source(value)
    if not code.strip():
        return 0, "", False
    mark = _rule_criteria(rule, code, [{"stdin": str(rule.get("stdin") or "")}])
    if mark is not None:
        return mark
    expected = [str(item) for item in rule.get("expected_contains") or []]
    passed, _stdout = _program_verdict(
        code, str(rule.get("stdin") or ""), expected
    )
    if passed:
        return int(rule.get("points") or 0), "התוכנית רצה ופלטה את הנדרש.", False
    return 0, "התוכנית לא רצה או שהפלט שלה שגוי.", True


def _program_io(rule: Mapping[str, Any], value: Any) -> tuple[int, str, bool]:
    """Run a full program under each declared input and mark it on facts.

    A full-program question is graded on three kinds of evidence: the program
    runs, its printed output contains the data it read, and (when the teacher
    declares it) its source really uses the required conversions. Two different
    inputs make a hardcoded ``print`` fail the second run, which is exactly the
    mistake a full-program question is there to catch.
    """
    code = _source(value)
    points = int(rule.get("points") or 0)
    if not code.strip():
        return 0, "", False

    runs = rule.get("runs")
    if not isinstance(runs, list) or not runs:
        runs = [
            {
                "stdin": rule.get("stdin"),
                "must_contain": rule.get("must_contain") or [],
            }
        ]

    mark = _rule_criteria(rule, code, runs)
    if mark is not None:
        return mark

    criteria: list[bool] = []
    all_ran = True
    for run_spec in runs:
        if not isinstance(run_spec, dict):
            raise ExamCheckError("a program run must be an object")
        run = _run_program(code, str(run_spec.get("stdin") or ""))
        _remember(rule, str(run_spec.get("stdin") or ""), run)
        ran = bool(
            run.get("ok") and not run.get("timed_out") and not run.get("exit_code")
        )
        all_ran = all_ran and ran
        stdout = normalise_line(run.get("stdout") or "") if ran else ""
        wanted = [str(item) for item in run_spec.get("must_contain") or []]
        if wanted:
            criteria.append(
                all(normalise_line(item) in stdout for item in wanted)
            )
    criteria.insert(0, all_ran)

    tokens = [str(item) for item in rule.get("source_requires") or []]
    if tokens:
        criteria.append(all(token in code for token in tokens))

    passed_count = sum(1 for item in criteria if item)
    earned = (
        round(points * passed_count / len(criteria)) if criteria else 0
    )
    if passed_count == len(criteria):
        return earned, "התוכנית עונה על כל הדרישות.", False
    if all_ran:
        return (
            earned,
            f"התוכנית רצה אך משלימה {passed_count} מתוך {len(criteria)} דרישות.",
            True,
        )
    return 0, "התוכנית לא רצה.", True


_KINDS = {
    "choice": _choice,
    "lines": _lines,
    "keywords": _keywords,
    "program": _program,
    "program_io": _program_io,
}


# --------------------------------------------------------------------- entry
def check(
    config: ExamConfig,
    version: ExamVersion,
    values: Mapping[str, Mapping[str, Any]],
    *,
    checks: dict[str, Any] | None = None,
    feedback: Mapping[str, str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> ExamReport:
    """Mark one student's structured answers against the answer key.

    ``feedback`` maps a question key to an already-rendered, bounded block of the
    teacher's past corrections (see :mod:`portal.exams.feedback`). It is fetched
    by the DB-aware caller and passed in as plain strings, so the checker stays
    free of any database access; each block travels with that question's model
    prompt as calibration.

    ``progress`` is an optional ``(index, total, label)`` callback fired before
    each question is marked and once more when the model pass begins, so a long
    run shows what it is doing. It is purely observational: it never changes a
    mark, and any error it raises is swallowed so reporting can never break
    grading. The deterministic items finish instantly; all the open items are
    then graded in ONE model call (the fast path the original worksheet
    evaluator used), with a per-item call only for an item the batch misses.
    """
    started = time.perf_counter()
    checks = checks if checks is not None else load_checks(config)
    rules = version_rules(checks, version.key)
    worksheet = load_paper(version)

    results: list[QuestionResult] = []
    open_items: list[
        tuple[QuestionResult, Item, Mapping[str, Any], dict[str, Any]]
    ] = []
    total = len(worksheet.items)
    for index, item in enumerate(worksheet.items, start=1):
        if progress is not None:
            try:
                progress(index, total, f"{item.label} · {item.title}".strip(" ·"))
            except Exception:  # noqa: BLE001 - reporting must never break grading
                logger.debug("exam progress callback failed", exc_info=True)
        item_values = values.get(item.key) or {}
        rule = rules.get(item.key)
        if rule is None:
            raise ExamCheckError(
                f"version {version.key}: no check rule for item {item.key!r}"
            )
        result, payload = _check_item(item, rule, item_values, feedback=feedback)
        results.append(result)
        if payload is not None:
            open_items.append((result, item, rule, payload))

    # One model pass over every open question, instead of one call per question.
    _review_open_items(
        open_items, feedback=feedback, progress=progress, total=total
    )

    maximum = sum(item.points for item in worksheet.items)
    score = sum(result.earned for result in results)
    return ExamReport(
        exam=config.name,
        version=version.key,
        version_label=version.label,
        score=max(0, min(100, score)),
        maximum=maximum,
        questions=results,
        runtime_ms=int((time.perf_counter() - started) * 1000),
    )


def _llm_context(item: Item) -> dict[str, Any]:
    """What the model needs to judge one answer: the prompt and its code.

    The buggy program is a read-only part marked ``given``; without it the
    model cannot tell which quotes the student is talking about.
    """
    given = [
        part.code.strip()
        for part in item.parts
        if part.given and part.code.strip()
    ]
    return {
        "title": f"{item.label} · {item.title}".strip(" ·"),
        "intro": item.intro,
        "code": "\n\n".join(given),
    }


def _mark(
    item: Item, rule: Mapping[str, Any], value: Any
) -> tuple[int, str, bool]:
    """Mark one check, giving a flagged free-text answer to the model first.

    ``"llm": true`` is set only on explanation checks. The model receives the
    rubric's own concept groups and points, so it cannot invent a marking
    scheme; any failure returns ``None`` and the keyword pass decides.
    """
    kind = str(rule.get("kind") or "")
    function = _KINDS.get(kind)
    if function is None:
        raise ExamCheckError(f"item {item.key!r}: unknown check kind {kind!r}")
    if (
        kind == "keywords"
        and rule.get("llm")
        and _filled(value)
        and llm_review.enabled()
    ):
        try:
            verdict = llm_review.grade(dict(rule), _source(value), _llm_context(item))
        except llm_review.QuotaExhausted:
            verdict = None
        if verdict is not None:
            return verdict
        logger.info(
            "no model verdict for %s; marking with the keyword rubric", item.key
        )
    return function(rule, value)


def _llm_item_payload(
    item: Item,
    rule: Mapping[str, Any],
    config: Mapping[str, Any],
    parts_seen: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    feedback: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Validate and assemble one open item's model request, without calling it.

    Returns ``{"answers", "context", "wanted", "by_part"}`` or ``None`` when the
    item is not eligible or the rubric drifted. Shared by the whole-paper batch
    and the per-item fallback so both send exactly the same question context.

    The block is checked against the actual checks before anything is sent:
    a part that is missing from the rubric, or whose maximum disagrees with
    the check's points, means the answer key and the guidelines have drifted
    apart. That is a configuration bug, so the paper is marked by the rules.
    """
    if not llm_review.enabled():
        return None
    specs = config.get("parts")
    guidelines = str(config.get("guidelines") or "").strip()
    if not isinstance(specs, list) or not specs or not guidelines:
        logger.warning(
            "item %s: llm_review is incomplete; marking with the rules", item.key
        )
        return None
    if not parts_seen or any(not entry["part"] for entry in parts_seen):
        return None
    by_part = {entry["part"]: entry for entry in parts_seen}
    if len(by_part) != len(parts_seen):
        return None
    wanted: list[Mapping[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, dict):
            return None
        key = str(spec.get("part") or "")
        entry = by_part.get(key)
        if entry is None or int(spec.get("max") or 0) != entry["points"]:
            logger.warning(
                "item %s: llm_review part %r does not match the checks",
                item.key,
                key,
            )
            return None
        wanted.append(spec)
    if {str(spec.get("part") or "") for spec in wanted} != set(by_part):
        logger.warning(
            "item %s: llm_review does not cover every check", item.key
        )
        return None

    answers: list[dict[str, Any]] = []
    for spec in wanted:
        entry = by_part[str(spec["part"])]
        answer = str(entry["answer"]).strip()
        if not answer:
            continue
        answers.append(
            {
                "part": entry["part"],
                "label": str(spec.get("label") or entry["part"]),
                "max": entry["points"],
                "guidance": str(spec.get("guidance") or ""),
                "answer": answer,
                "evidence": evidence
                if entry["kind"] in {"program", "program_io"}
                else [],
            }
        )
    if not answers:
        return None

    context = _llm_context(item)
    context["guidelines"] = guidelines
    reference = rule.get("reference")
    if isinstance(reference, Mapping):
        context["reference"] = str(reference.get("code") or "")
    # The teacher's bounded calibration for this exact question, if any. Already
    # rendered to a capped string upstream, so it adds a fixed, small token cost.
    block = str((feedback or {}).get(item.key) or "").strip()
    if block:
        context["feedback"] = block
    return {
        "answers": answers,
        "context": context,
        "wanted": wanted,
        "by_part": by_part,
    }


def _marks_from_verdict(
    wanted: Sequence[Mapping[str, Any]],
    by_part: Mapping[str, dict[str, Any]],
    verdict: Mapping[str, tuple[int, str]],
) -> list[tuple[int, int, str, bool]]:
    """Turn a ``part -> (points, reason)`` verdict into per-check marks.

    One ``(points, maximum, note, review)`` per check, in paper order. A part the
    verdict did not cover keeps its deterministic mark, so a partial reply never
    silently zeroes an answer.
    """
    marked: list[tuple[int, int, str, bool]] = []
    for spec in wanted:
        entry = by_part[str(spec["part"])]
        label = str(spec.get("label") or entry["part"])
        judged = verdict.get(entry["part"])
        if judged is None:
            marked.append((entry["earned"], entry["points"], entry["note"], False))
            continue
        points, reason = judged
        note = f"{label}: {points}/{entry['points']} נקודות."
        if reason:
            note += f" {reason}"
        marked.append(
            (points, entry["points"], note, points < entry["points"])
        )
    return marked


def _llm_item_review(
    item: Item,
    rule: Mapping[str, Any],
    config: Mapping[str, Any],
    parts_seen: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    feedback: Mapping[str, str] | None = None,
) -> list[tuple[int, int, str, bool]] | None:
    """Give one open item to the model as its own call (the per-item fallback).

    Used when the whole-paper batch missed or malformed this item. Returns one
    ``(points, maximum, note, review)`` per check, in paper order, or ``None`` to
    keep the deterministic marks. The model receives the question, the given
    program, the teacher's detailed guidelines, each part's own rubric and the
    sandbox output, so the explanation and the corrected code are judged against
    the same evidence a teacher would use.
    """
    payload = _llm_item_payload(
        item, rule, config, parts_seen, evidence, feedback=feedback
    )
    if payload is None:
        return None
    verdict = llm_review.grade_item(payload["answers"], payload["context"])
    if verdict is None:
        logger.info(
            "no item verdict for %s; keeping the deterministic marks", item.key
        )
        return None
    return _marks_from_verdict(payload["wanted"], payload["by_part"], verdict)


def _apply_marks(
    result: QuestionResult,
    item: Item,
    answered: bool,
    marked: Sequence[tuple[int, int, str, bool]],
) -> None:
    """Fold a model verdict into an already-marked question, in place.

    Mirrors the deterministic tail of :func:`_check_item`: the model's points
    replace the rules per part, the score and status are recomputed from them,
    and the reason under each answer becomes the one that decided its points.
    """
    points = int(item.points or 0)
    earned = sum(entry[0] for entry in marked)
    notes = [entry[2] for entry in marked if entry[2]]
    needs_review = any(entry[3] for entry in marked)
    for mark, entry in zip(result.parts, marked):
        mark["earned"] = entry[0]
        mark["note"] = entry[2] or mark["note"]
        mark["needs_review"] = bool(entry[3])

    earned = max(0, min(points, earned))
    if not answered:
        status = MISSING
    elif earned >= points:
        status = CORRECT
    elif earned > 0:
        status = PARTIALLY_CORRECT
    else:
        status = INCORRECT
    if status != MISSING and not notes:
        notes.append("התשובה נבדקה אוטומטית.")
    result.earned = earned
    result.status = status
    result.notes = " ".join(notes).strip()
    result.needs_review = needs_review and status != MISSING
    result.llm_reviewed = True


def _review_open_items(
    entries: list[tuple[QuestionResult, Item, Mapping[str, Any], dict[str, Any]]],
    *,
    feedback: Mapping[str, str] | None,
    progress: Callable[[int, int, str], None] | None,
    total: int,
) -> None:
    """Grade every open item in ONE model pass, then fall back per item.

    Mirrors the original worksheet evaluator (``src/evaluator/core.py``): the
    whole paper's open questions travel in a single request and come back as one
    JSON, so a six-question paper costs one round trip instead of six. Any item
    the batch missed or malformed is retried on its own (the old per-item path),
    and a quota stop leaves every open item on its deterministic marks, flagged
    so the selective re-check can retry the hand-in once the quota resets.
    """
    if not entries:
        return
    if progress is not None:
        try:
            progress(total, total, "המודל בודק את כל השאלות הפתוחות בפס אחד…")
        except Exception:  # noqa: BLE001 - reporting must never break grading
            logger.debug("exam progress callback failed", exc_info=True)

    request = [
        {
            "item": item.key,
            "context": payload["context"],
            "parts": payload["answers"],
        }
        for _, item, _, payload in entries
    ]
    try:
        verdicts = llm_review.grade_paper(request)
    except llm_review.QuotaExhausted:
        # Out of daily quota for the whole pass: keep the deterministic marks so
        # the paper still has a score, and flag every open item as waiting.
        for result, item, _, _ in entries:
            result.llm_quota_blocked = True
            logger.info(
                "item %s: model quota exhausted; keeping the deterministic marks",
                item.key,
            )
        return
    verdicts = verdicts or {}

    for result, item, rule, payload in entries:
        verdict = verdicts.get(item.key)
        if verdict is not None:
            marked = _marks_from_verdict(
                payload["wanted"], payload["by_part"], verdict
            )
        else:
            # The batch missed or malformed this one item; give it its own call.
            try:
                marked = _llm_item_review(
                    item,
                    rule,
                    payload["review_config"],
                    payload["parts_seen"],
                    payload["evidence"],
                    feedback=feedback,
                )
            except llm_review.QuotaExhausted:
                result.llm_quota_blocked = True
                logger.info(
                    "item %s: model quota exhausted; keeping the deterministic marks",
                    item.key,
                )
                continue
        if marked is not None:
            _apply_marks(result, item, payload["answered"], marked)


def _check_item(
    item: Item,
    rule: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    feedback: Mapping[str, str] | None = None,
) -> tuple[QuestionResult, dict[str, Any] | None]:
    """Mark one item deterministically and, if it is open, stage its model request.

    Returns ``(result, payload)``. ``result`` carries the rule-based marks; the
    model verdict is applied later by :func:`_review_open_items` so the whole
    paper's open questions can share ONE call. ``payload`` is the assembled
    request (plus the bits the per-item fallback needs) or ``None`` when the item
    is not eligible for the model, in which case the deterministic result stands.
    """
    points = int(item.points or 0)
    checks = rule.get("checks") or []
    if not isinstance(checks, list) or not checks:
        raise ExamCheckError(f"item {item.key!r} has no checks")
    earned = 0
    notes: list[str] = []
    given_parts: list[str] = []
    expected_parts: list[str] = []
    needs_review = False
    answered = False
    review_config = rule.get("llm_review")
    parts_seen: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    labels = _part_labels(item)

    for check_rule in checks:
        if not isinstance(check_rule, dict):
            raise ExamCheckError(f"item {item.key!r}: a check must be an object")
        part_key = str(check_rule.get("part") or "")
        value = values.get(part_key)
        filled = _filled(value)
        if filled:
            answered = True
        given_parts.append(_shorten(_answer_text(value), 120))
        expected_parts.append(_expected_hint(check_rule))
        sub_points = int(check_rule.get("points") or points)
        check_rule = {
            **check_rule,
            "points": sub_points,
            # The sandbox runs of this check are kept for the item review.
            "_evidence": evidence,
        }
        if isinstance(review_config, Mapping):
            # The item review covers the explanation too; a second, per-check
            # model call would only spend quota on the same answer.
            check_rule["llm"] = False
        got, note, review = _mark(item, check_rule, value)
        got = min(got, sub_points)
        earned += got
        if note:
            notes.append(note)
        needs_review = needs_review or review
        parts_seen.append(
            {
                "part": part_key,
                "kind": str(check_rule.get("kind") or ""),
                "points": sub_points,
                "earned": got,
                "note": note,
                "answer": _source(value),
            }
        )
        marks.append(
            {
                "part": part_key,
                "label": labels.get(part_key, PART_LABELS_HE.get(str(check_rule.get("kind") or ""), "התשובה")),
                "points": sub_points,
                "earned": got,
                "note": note or ("התשובה נבדקה אוטומטית." if filled else "לא נענה."),
                "needs_review": bool(review),
            }
        )

    llm_eligible = (
        isinstance(review_config, Mapping) and answered and llm_review.enabled()
    )
    # Stage the model request instead of sending it here. Every open item of the
    # paper is collected and graded in ONE call by _review_open_items (the fast
    # path the original worksheet evaluator used); the per-item call survives
    # only as a fallback for an item the batch misses.
    payload: dict[str, Any] | None = None
    if llm_eligible:
        payload = _llm_item_payload(
            item, rule, review_config, parts_seen, evidence, feedback=feedback
        )
        if payload is not None:
            payload["parts_seen"] = parts_seen
            payload["evidence"] = evidence
            payload["answered"] = answered
            payload["review_config"] = review_config

    earned = max(0, min(points, earned))
    if not answered:
        status = MISSING
        note = "לא נענה."
    elif earned >= points:
        status = CORRECT
    elif earned > 0:
        status = PARTIALLY_CORRECT
    else:
        status = INCORRECT
    if status != MISSING and not notes:
        notes.append("התשובה נבדקה אוטומטית.")
    result = QuestionResult(
        key=item.key,
        # The paper repeats the same prompt for several sub-questions (three
        # tracing questions share one title), so the number the student saw is
        # part of the label; otherwise the teacher's review cannot tell which
        # answer belongs to which question.
        title=f"{item.label} · {item.title}".strip(" ·"),
        section=item.section,
        points=points,
        earned=earned,
        status=status,
        notes=" ".join(notes).strip(),
        given="; ".join(part for part in given_parts if part),
        expected="; ".join(part for part in expected_parts if part),
        needs_review=needs_review and status != MISSING,
        llm_eligible=llm_eligible,
        llm_reviewed=False,
        llm_quota_blocked=False,
        parts=marks,
    )
    return result, payload


def _part_labels(item: Item) -> dict[str, str]:
    """The name each answer carries on the paper, for the review screen."""
    labels: dict[str, str] = {}
    for part in item.parts:
        if part.given:
            continue
        label = (part.label or "").strip() or _shorten(part.prompt, 60)
        labels[part.key] = label or PART_LABELS_HE.get(part.kind, "התשובה")
    return labels


def _expected_hint(rule: Mapping[str, Any]) -> str:
    if rule.get("kind") == "choice":
        return str(rule.get("answer") or "")
    if rule.get("kind") == "lines":
        entries = rule.get("lines")
        if isinstance(entries, list) and entries:
            return " / ".join(
                str(entry.get("expected") or "")
                for entry in entries
                if isinstance(entry, dict)
            )
        return " / ".join(str(line) for line in rule.get("expected") or [])
    return ""


__all__ = [
    "CORRECT",
    "ExamCheckError",
    "ExamReport",
    "INCORRECT",
    "MISSING",
    "PARTIALLY_CORRECT",
    "QuestionResult",
    "STATUS_LABELS_HE",
    "check",
    "feedback_text",
    "load_checks",
    "normalise_line",
    "output_lines",
    "run_code",
    "version_rules",
]