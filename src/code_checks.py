"""Deterministic, offline checks over a student's pasted Python source.

The LLM grades *what the answer means*; these helpers grade *what the code
literally contains*, so the model is handed a second, independent opinion
instead of being the sole judge of mechanical facts like "did the student
declare ``first_name``" or "are there five ``input()`` calls".

Everything here is pure string analysis - no execution, no imports of student
code, no network. It is deliberately conservative: a check only reports a miss
when it can prove absence, because a false "MISSING" in the prompt would bias
the model against the student.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ExerciseCheck",
    "ExerciseScan",
    "CodeScan",
    "CHECKS_BY_ASSIGNMENT",
    "checks_for_assignment",
    "scan_submission",
    "is_effectively_empty",
    "normalize_source",
]


# Text a student (or an empty Google Doc) leaves behind that means "nothing
# here" rather than "code". Matched case-insensitively after whitespace trim.
_EMPTY_MARKERS = frozenset({"", "-", "--", "–", "—", "n/a", "na", "none", "null", "."})

# Directional marks, zero-width joiners/spaces, soft hyphen and other invisible
# characters that ride along with RTL (Hebrew) text and would otherwise break
# token matching. Written with explicit escapes so the intent is auditable.
_INVISIBLE_RE = re.compile(
    "["
    "\u200b"  # ZERO WIDTH SPACE
    "\u200c"  # ZERO WIDTH NON-JOINER (very common in Hebrew text)
    "\u200d"  # ZERO WIDTH JOINER
    "\u200e"  # LEFT-TO-RIGHT MARK
    "\u200f"  # RIGHT-TO-LEFT MARK
    "\u202a-\u202e"  # bidi embedding/override controls
    "\u2060-\u2064"  # word joiner / invisible operators
    "\u2066-\u2069"  # bidi isolates
    "\ufeff"  # byte-order mark
    "\u00ad"  # soft hyphen
    "]"
)


def _var_pattern(name: str) -> re.Pattern[str]:
    """Match ``name`` as a whole identifier, never inside ``first_name``."""
    return re.compile(rf"(?<![0-9A-Za-z_]){re.escape(name)}(?![0-9A-Za-z_])")


def _command_pattern(command: str) -> re.Pattern[str]:
    """A call site: the name followed by an opening paren (``input (`` ok)."""
    return re.compile(rf"(?<![0-9A-Za-z_]){re.escape(command)}\s*\(")


# ``var = ...`` but not ``==`` (comparison) and not ``>=``/``<=``/``!=``.
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")
# An assignment whose right-hand side directly calls ``input``.
_INPUT_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:[A-Za-z_][\w.]*\s*\([^)]*?\) *)*input\s*\(",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ExerciseCheck:
    """What must be present in a submission for one exercise."""

    exercise_id: str
    #: Variables that must appear as identifiers (word-boundary matched).
    required_variables: tuple[str, ...] = ()
    #: Functions that must appear as call sites (``name(``).
    required_commands: tuple[str, ...] = ()
    #: Minimum distinct ``x = input(...)`` assignments (ex3_4 style).
    min_input_assignments: int = 0
    #: Minimum distinct ``x = ...`` assignments overall.
    min_assignments: int = 0

    @property
    def is_empty(self) -> bool:
        """A spec with no constraints cannot fail (and must not be reported)."""
        return not (
            self.required_variables
            or self.required_commands
            or self.min_input_assignments
            or self.min_assignments
        )


@dataclass(frozen=True)
class ExerciseScan:
    """Result of applying one :class:`ExerciseCheck` to one submission."""

    check: ExerciseCheck
    missing_variables: tuple[str, ...] = ()
    missing_commands: tuple[str, ...] = ()
    short_input_assignments: int = 0
    short_assignments: int = 0

    @property
    def exercise_id(self) -> str:
        return self.check.exercise_id

    @property
    def ok(self) -> bool:
        """True when nothing is proven missing (unknown counts as fine)."""
        if self.missing_variables or self.missing_commands:
            return False
        if self.short_input_assignments > 0 or self.short_assignments > 0:
            return False
        return True

    @property
    def verdict(self) -> str:
        return "PASS" if self.ok else "MISSING"


@dataclass(frozen=True)
class CodeScan:
    """The whole submission's scan: one :class:`ExerciseScan` per exercise."""

    source: str
    empty: bool
    input_assignments: int = 0
    assignments: int = 0
    input_calls: int = 0
    exercises: tuple[ExerciseScan, ...] = ()

    @property
    def ok(self) -> bool:
        return self.empty or all(scan.ok for scan in self.exercises)

    def summary_lines(self) -> list[str]:
        """Short, prompt-ready lines - one per exercise plus an empty guard."""
        if self.empty:
            return [
                "No code text was found in the submission (document looks "
                "empty or holds only a placeholder)."
            ]
        lines: list[str] = []
        for scan in self.exercises:
            if scan.ok:
                lines.append(f"{scan.exercise_id}: structural check PASS")
                continue
            problems: list[str] = []
            if scan.missing_variables:
                problems.append(
                    "missing variables: " + ", ".join(scan.missing_variables)
                )
            if scan.missing_commands:
                problems.append(
                    "missing calls: "
                    + ", ".join(f"{command}()" for command in scan.missing_commands)
                )
            if scan.short_input_assignments > 0:
                problems.append(
                    f"only {scan.check.min_input_assignments - scan.short_input_assignments}"
                    f" of {scan.check.min_input_assignments} required "
                    "answer-storage variables (x = input(...))"
                )
            if scan.short_assignments > 0:
                problems.append(
                    f"only {scan.check.min_assignments - scan.short_assignments}"
                    f" of {scan.check.min_assignments} required assignments"
                )
            lines.append(f"{scan.exercise_id}: MISSING -> " + "; ".join(problems))
        return lines


# ---------------------------------------------------------------------------
# Assignment -> check specs
# ---------------------------------------------------------------------------

_LESSON_3_CHECKS: tuple[ExerciseCheck, ...] = (
    ExerciseCheck(
        exercise_id="ex3_1",
        required_variables=("name",),
        required_commands=("input", "print"),
    ),
    ExerciseCheck(
        exercise_id="ex3_2",
        required_variables=("first_name", "last_name"),
        required_commands=("input", "print"),
        min_input_assignments=2,
    ),
    ExerciseCheck(
        exercise_id="ex3_3",
        required_variables=("first_name", "last_name", "age", "height"),
        required_commands=("input", "int", "float", "print"),
    ),
    ExerciseCheck(
        exercise_id="ex3_4",
        # The student names their own variables, so only the shape is checked:
        # five answers captured into five variables, then printed.
        required_commands=("print",),
        min_input_assignments=5,
        min_assignments=5,
    ),
)

CHECKS_BY_ASSIGNMENT: dict[str, tuple[ExerciseCheck, ...]] = {
    "lesson_3": _LESSON_3_CHECKS,
}


def checks_for_assignment(assignment_id: str) -> tuple[ExerciseCheck, ...]:
    """Return check specs for an assignment (empty when it has none).

    Rubrics other than lesson_3 (worksheets, free-text answers) are graded by
    the model alone, so callers get an empty tuple and emit no scan block.
    """
    return CHECKS_BY_ASSIGNMENT.get(assignment_id, ())


def scan_submission(
    source: str, checks: tuple[ExerciseCheck, ...] | list[ExerciseCheck]
) -> CodeScan:
    """Apply ``checks`` to ``source`` and report only provable absences.

    Never raises: a malformed or empty document simply scans clean, so the LLM
    (which sees the raw text too) remains the authority on content.
    """
    cleaned = normalize_source(source)
    empty = is_effectively_empty(cleaned)
    assignments = _assigned_variables(cleaned)
    input_assignments = _input_assigned_variables(cleaned)
    input_calls = len(re.findall(r"(?<![0-9A-Za-z_])input\s*\(", cleaned))

    scans: list[ExerciseScan] = []
    if not empty:
        for check in checks:
            if check.is_empty:
                continue
            missing_vars = tuple(
                name
                for name in check.required_variables
                if not _var_pattern(name).search(cleaned)
            )
            missing_cmds = tuple(
                command
                for command in check.required_commands
                if not _command_pattern(command).search(cleaned)
            )
            scans.append(
                ExerciseScan(
                    check=check,
                    missing_variables=missing_vars,
                    missing_commands=missing_cmds,
                    short_input_assignments=max(
                        check.min_input_assignments - len(input_assignments), 0
                    ),
                    short_assignments=max(
                        check.min_assignments - len(assignments), 0
                    ),
                )
            )

    return CodeScan(
        source=cleaned,
        empty=empty,
        input_assignments=len(input_assignments),
        assignments=len(assignments),
        input_calls=input_calls,
        exercises=tuple(scans),
    )


def normalize_source(text: str) -> str:
    """Strip invisible RTL/zero-width characters that break token matching."""
    return _INVISIBLE_RE.sub("", text or "")


def is_effectively_empty(text: str) -> bool:
    """True when ``text`` carries no student work worth scanning."""
    return normalize_source(text).strip().strip(".").strip().lower() in _EMPTY_MARKERS


def _assigned_variables(source: str) -> set[str]:
    found: set[str] = set()
    for line in source.splitlines():
        match = _ASSIGN_RE.match(line)
        if match:
            found.add(match.group(1))
    return found


def _input_assigned_variables(source: str) -> set[str]:
    return set(_INPUT_ASSIGN_RE.findall(source))

    def verdict(self) -> str:
        return "PASS" if self.ok else "MISSING"