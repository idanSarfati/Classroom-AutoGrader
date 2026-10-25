"""Re-audit saved evaluation reports - the daily second pair of eyes.

The inline audit (``src.grade_audit``) already vets every grade as it is
produced. This module answers the *other* question the teacher has: "of the
grades I have not released yet, which ones still look wrong?" It does that by
re-running the same deterministic checks over the saved report JSON on disk.

Why re-run instead of trusting the stored verdict: a rubric can be edited, a
teacher can hand-edit a score in the Release tab, and an old report predates a
newly added audit rule. Re-auditing is pure local math - no LLM call, no
tokens, no network - so it costs nothing to redo every day.

Read-only: nothing here writes a grade, comment, or return.
"""
from __future__ import annotations
import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from src.grade_audit import AuditVerdict, audit_grading_result, summarize_verdicts
from src.models import AssignmentConfig, AssignmentSpec, EvaluationReport

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORTS_DIR = PROJECT_ROOT / "exports"
DEFAULT_ASSIGNMENTS_DIR = PROJECT_ROOT / "assignments"
DEFAULT_SPECS_DIR = PROJECT_ROOT / "configs"
DAILY_CHECKS_DIRNAME = "daily_checks"

# Console fallbacks for the badges the UI shows (see ``_console_safe``).
_ASCII_MARKERS = (
    ("\u2705", "[ok]"),
    ("\U0001f527", "[fixed]"),
    ("\u26a0\ufe0f", "[review]"),
    ("\u26a0", "[review]"),
    ("\u2139\ufe0f", "[note]"),
    ("\u2139", "[note]"),
)


@dataclass
class AuditedEntry:
    """One grade, as the audit sees it after re-checking."""

    student_name: str = ""
    score: Optional[int] = None
    original_score: Optional[int] = None
    stored_verdict: Optional[AuditVerdict] = None
    verdict: Optional[AuditVerdict] = None

    @property
    def needs_review(self) -> bool:
        return bool(self.verdict is not None and self.verdict.needs_review)

    @property
    def newly_found(self) -> bool:
        """True when re-auditing caught what the stored verdict missed.

        This is the case the inline audit cannot cover: the teacher edited a
        score in the Release tab, or the report predates an audit rule that was
        added later. The stored verdict still says "clean"; the math now says
        otherwise.
        """
        if self.verdict is None or not self.verdict.findings:
            return False
        stored = self.stored_verdict
        return stored is None or not (stored.flags or [])

    # Readable alias: "repaired now" is the question a teacher actually asks.
    repaired_now = newly_found


@dataclass
class AuditedReport:
    """Audit outcome for one saved report file."""

    path: Optional[Path] = None
    course_name: str = ""
    course_work_title: str = ""
    created_at: str = ""
    entries: list = field(default_factory=list)
    error: Optional[str] = None
    config_error: Optional[str] = None

    @property
    def needs_review_count(self) -> int:
        return sum(1 for e in self.entries if e.needs_review)


@dataclass
class ReportsAuditResult:
    """Rollup over every saved report."""

    reports: list = field(default_factory=list)
    report_count: int = 0
    audited_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    stats: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    findings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "report_count": self.report_count,
            "audited_count": self.audited_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
            "stats": dict(self.stats),
            "rows": list(self.rows),
            "findings": list(self.findings),
            "errors": list(self.errors),
        }


_REPORT_NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?\.json$")


def looks_like_report(path: Path) -> bool:
    """Cheap pre-filter so unrelated JSON in ``exports/`` is not treated as a report.

    ``exports/`` also holds material/state dumps and half-written files. A file
    is audited when either of these holds:

    * it parses and carries the fields :class:`EvaluationReport` requires, or
    * its *name* matches the report stamp the writer uses (``YYYYMMDD-HHMMSS``).

    The second rule is what keeps a genuine problem visible: a report that was
    truncated mid-write is a real failure the teacher must hear about, so it is
    reported as an error rather than silently skipped. A neighbour we never
    wrote - ``broken.json``, ``coursework_materials.json`` - matches neither
    rule and stays out of the daily output.
    """
    if _REPORT_NAME_RE.match(path.name):
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "entries" in data and "course_name" in data


def find_report_files(root: Optional[Path] = None) -> list:
    """Saved ``*.json`` evaluation reports, newest first.

    The daily-check output directory is skipped: those files are scans, not
    graded reports, and auditing them would only add noise.
    """
    base = DEFAULT_EXPORTS_DIR if root is None else root
    if not base.is_dir():
        return []
    found: list = []
    try:
        candidates = list(base.rglob("*.json"))
    except OSError as exc:
        logger.warning("Could not walk %s: %s", base, exc)
        return []
    for path in candidates:
        if DAILY_CHECKS_DIRNAME in path.parts or not path.is_file():
            continue
        if not looks_like_report(path):
            continue
        found.append(path)
    found.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return found


def load_config_for(
    config_id: str, assignments_dir: Optional[Path] = None
) -> tuple:
    """Resolve the rubric for a report's ``assignment_config_id``.

    Returns ``(config, error)``. Rubric coverage is what lets the audit spot a
    missing or invented task, so a report whose rubric cannot be found is
    audited without it (a weaker check) and says so - never silently skipped.
    """
    if not config_id:
        return None, "report has no assignment_config_id"
    if assignments_dir is not None:
        search_dirs = [assignments_dir]
    else:
        # Legacy rubrics live in assignments/, spec rubrics in configs/.
        search_dirs = [DEFAULT_ASSIGNMENTS_DIR, DEFAULT_SPECS_DIR]
    candidates: list[Path] = []
    for base in search_dirs:
        try:
            if base.is_dir():
                candidates.extend(sorted(base.glob("*.json")))
        except OSError as exc:
            return None, f"could not list assignments: {exc}"
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if "sections" in data:
            # Spec rubric: reports store its assignment_title as the config id
            # (see src.main.rubric_config_id).
            if str(data.get("assignment_title", "")) == config_id:
                try:
                    return AssignmentSpec.model_validate(data), None
                except Exception as exc:  # noqa: BLE001 - bad rubric is reported, not fatal
                    return None, f"rubric {path.name} is invalid: {exc}"
        elif str(data.get("assignment_id", "")) == config_id:
            try:
                return AssignmentConfig.model_validate(data), None
            except Exception as exc:  # noqa: BLE001 - bad rubric is reported, not fatal
                return None, f"rubric {path.name} is invalid: {exc}"
    return None, f"no rubric found for '{config_id}'"


def _entry_row(report: AuditedReport, entry: AuditedEntry) -> dict:
    """Flat row for tables/JSON: who, what, and what the audit thinks."""
    verdict = entry.verdict
    # De-duplicated: a task repeated across several students (or several times
    # for one student) is one flag with a count, not the same string 4 times.
    counts: Counter = Counter(f.flag for f in (verdict.findings if verdict else []) if f.flag)
    return {
        "Course": report.course_name,
        "Assignment": report.course_work_title,
        "Student": entry.student_name,
        "Stored score": entry.original_score,
        "Audited score": entry.score,
        "Status": verdict.summary() if verdict is not None else "not audited",
        "Flags": ", ".join(
            flag if n == 1 else f"{flag} x{n}" for flag, n in counts.items()
        ),
        "Needs review": entry.needs_review,
        "Newly found": entry.newly_found,
        "Deductions total": verdict.deductions_total if verdict is not None else 0,
        "Report": report.path.name if report.path is not None else "",
        "Findings": [
            {"flag": f.flag, "message": f.message, "severity": f.severity}
            for f in (verdict.findings if verdict is not None else [])
        ],
    }


def _shorten(exc: Exception, limit: int = 200) -> str:
    """Condense a pydantic error into one readable line.

    ``ValidationError`` renders a multi-line dump with the offending value
    echoed in full; in a daily summary that buries the actual cause, so only
    the field path and the reason are kept.
    """
    text = str(exc).replace("\n", " ").strip()
    match = re.search(r"^(\S+)\s+(.*?)\s+\[", text)
    if match:
        text = f"{match.group(1)}: {match.group(2)}"
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def audit_report_file(
    path: Path,
    assignments_dir: Optional[Path] = None,
    apply_fixes: bool = False,
) -> AuditedReport:
    """Re-audit one saved report. Never raises; records errors on the result."""
    audited = AuditedReport(path=path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        audited.error = f"unreadable JSON: {_shorten(exc)}"
        return audited
    try:
        report = EvaluationReport.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - a bad file must not kill the run
        audited.error = f"not a valid evaluation report: {_shorten(exc)}"
        return audited

    audited.course_name = report.course_name
    audited.course_work_title = report.course_work_title
    audited.created_at = report.created_at.isoformat()

    config, config_error = load_config_for(
        report.assignment_config_id, assignments_dir
    )
    audited.config_error = config_error

    for entry in report.entries:
        stored = AuditVerdict.from_dict(getattr(entry, "audit", None))
        audited.entries.append(
            AuditedEntry(
                student_name=entry.student_name,
                score=entry.result.score,
                original_score=entry.result.score,
                stored_verdict=stored,
                verdict=audit_grading_result(
                    entry.result, config, autofix=bool(apply_fixes)
                ),
            )
        )
    return audited


def audit_saved_reports(
    root: Optional[Path] = None,
    assignments_dir: Optional[Path] = None,
    limit: Optional[int] = None,
    apply_fixes: bool = False,
) -> ReportsAuditResult:
    """Audit every saved report and roll the verdicts up.

    ``apply_fixes`` is deliberately opt-in and defaults to False: the daily job
    reports what is wrong, it does not quietly rewrite stored grades. When True
    a provably-wrong score is corrected in the returned objects only.
    """
    result = ReportsAuditResult()
    paths = find_report_files(root)
    if limit:
        paths = paths[: max(int(limit), 0)]
    verdicts: list = []
    for path in paths:
        result.report_count += 1
        audited = audit_report_file(path, assignments_dir, apply_fixes)
        result.reports.append(audited)
        if audited.error:
            result.error_count += 1
            result.errors.append(f"{path.name}: {audited.error}")
            continue
        if not audited.entries:
            result.skipped_count += 1
            continue
        result.audited_count += 1
        for entry in audited.entries:
            if entry.verdict is None:
                continue
            verdicts.append(entry.verdict)
            row = _entry_row(audited, entry)
            result.rows.append(row)
            if entry.needs_review:
                result.findings.append(
                    {
                        "Course": row["Course"],
                        "Assignment": row["Assignment"],
                        "Student": entry.student_name,
                        "Score": entry.score,
                        "Status": row["Status"],
                        "Flags": row["Flags"],
                        "Report": row["Report"],
                        "Findings": row["Findings"],
                    }
                )
    result.stats = summarize_verdicts(verdicts)
    logger.info(
        "Report audit: %d report(s), %d grade(s), %d clean, %d auto-fixed, "
        "%d need review.",
        result.audited_count,
        result.stats.get("total", 0),
        result.stats.get("clean", 0),
        result.stats.get("autofixed", 0),
        result.stats.get("needs_review", 0),
    )
    return result


def _console_safe(text: str) -> str:
    """Make a summary printable on the legacy Windows console (cp1255).

    The emoji in badges/status are for the Streamlit UI; on a Hebrew code page
    they raise ``UnicodeEncodeError`` and abort the whole daily run, so the
    markers are downgraded to ASCII when stdout cannot encode them.
    """
    try:
        text.encode(sys.stdout.encoding or "ascii")
        return text
    except (UnicodeEncodeError, LookupError):
        pass
    for emoji, ascii_mark in _ASCII_MARKERS:
        text = text.replace(emoji, ascii_mark)
    return text.encode("ascii", "replace").decode("ascii")


def format_audit_summary(result: ReportsAuditResult) -> str:
    """Human-readable block for ``summary.txt`` in the daily-check folder."""
    lines = ["", "GRADE AUDIT (stored reports re-checked, zero tokens)"]
    stats = result.stats or {}
    if not stats.get("total"):
        lines.append("  No stored evaluation reports to audit yet.")
        return "\n".join(lines)
    lines.append(
        f"  {stats.get('total', 0)} grade(s) across {result.audited_count} "
        f"report(s): {stats.get('clean', 0)} clean, "
        f"{stats.get('autofixed', 0)} auto-fixed, "
        f"{stats.get('needs_review', 0)} need review"
        + (
            f", {stats.get('notes', 0)} with batch-level notes."
            if stats.get("notes")
            else "."
        ),
    )
    if stats.get("corrected_points"):
        lines.append(
            f"  Audit corrected {stats['corrected_points']} point(s) of score math."
        )
    if stats.get("flags"):
        lines.append(
            "  Per-grade flags: "
            + ", ".join(f"{k} x{v}" for k, v in stats["flags"].items())
        )
    if stats.get("systemic"):
        lines.append(
            "  Batch-wide (fix once in the rubric/prompt, not per student): "
            + ", ".join(f"{k} x{v}" for k, v in stats["systemic"].items())
        )
    if result.findings:
        lines.append("  Grades needing a human look:")
        for item in result.findings[:25]:
            lines.append(
                f"    - {item['Student']} ({item['Assignment']}, "
                f"score {item['Score']}): {item['Status']}"
            )
        if len(result.findings) > 25:
            lines.append(f"    ... and {len(result.findings) - 25} more.")
    else:
        lines.append("  Nothing flagged: every stored grade is self-consistent.")
    if result.errors:
        lines.append(f"  {len(result.errors)} report(s) could not be read:")
        for message in result.errors[:10]:
            lines.append(f"    - {message}")
    return "\n".join(lines)


def _build_parser() -> "argparse.ArgumentParser":
    parser = argparse.ArgumentParser(
        prog="python -m src.report_audit",
        description=(
            "Re-audit saved evaluation reports against their rubrics. Read-only: "
            "no grades, comments or returns are touched, and no LLM is called."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Directory to scan for report JSON (default: exports/).",
    )
    parser.add_argument(
        "--assignments",
        default=None,
        help="Directory holding rubric configs (default: assignments/).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only audit the N most recently modified reports.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable rollup instead of the text summary.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Exit 1 when any grade still needs a human look."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    root = Path(args.root) if args.root else None
    assignments = Path(args.assignments) if args.assignments else None
    result = audit_saved_reports(
        root=root,
        assignments_dir=assignments,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_console_safe(format_audit_summary(result).lstrip()))
    return 1 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
