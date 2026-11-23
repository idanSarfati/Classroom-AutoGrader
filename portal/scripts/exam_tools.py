"""Build and verify the exam sidecars: rubrics and the answer key.

Two commands, both safe to run any time:

``rubrics``
    Regenerate ``assignments/exam_units_1_3_<v>.json`` from the papers. The
    rubric is the engine's view of the paper - one question per item, its kind,
    text, points and visible code - generated so it cannot drift from what the
    students actually sit. It deliberately carries **no answers**: grading reads
    the check file only, and an answer duplicated next to the options it belongs
    to would break the portal's promise that no rubric leaks an answer to a
    student.

``verify``
    Prove the exam grades itself. For every version it

    1. runs each tracing snippet in the real sandbox and checks that the output
       the answer key expects is what Python actually prints;
    2. builds a "perfect student" answer sheet from the key (the right option,
       the expected lines, the reference corrected program) and asserts the
       deterministic checker awards exactly 100/100 with every question correct.

    A non-zero exit code means the exam is not safe to open; the printed table
    says which question and why.

Usage::

    python -m portal.scripts.exam_tools rubrics
    python -m portal.scripts.exam_tools verify
    python -m portal.scripts.exam_tools verify --exam units_1_3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from portal.exams import checker
from portal.exams.config import ExamConfig, ExamVersion, load_exam
from portal.exams.paper import load_paper
from portal.paths import PROJECT_ROOT
from portal.worksheets.schema import Item, Part

DEFAULT_EXAM = "units_1_3"


# ---------------------------------------------------------------- rubric build
def _snippet(item: Item) -> str:
    for part in item.parts:
        if part.given:
            return part.code
    return ""


def _choice_part(item: Item) -> Part | None:
    for part in item.parts:
        if part.kind == "choice":
            return part
    return None


def _item_type(item: Item, rule: dict[str, Any]) -> str:
    kinds = {str(check.get("kind") or "") for check in rule.get("checks") or []}
    if "choice" in kinds:
        return "multiple_choice"
    if "lines" in kinds:
        return "expected_output"
    if "keywords" in kinds:
        return "debugging"
    return "code_writing"


def build_rubric(
    config: ExamConfig, version: ExamVersion, checks: dict[str, Any]
) -> dict[str, Any]:
    """One engine-format rubric for a version, derived from paper + key."""
    rules = checker.version_rules(checks, version.key)
    paper = load_paper(version)
    sections: list[dict[str, Any]] = []
    for section_name, items in paper.sections():
        questions: list[dict[str, Any]] = []
        for item in items:
            rule = rules[item.key]
            question: dict[str, Any] = {
                "id": item.key,
                "type": _item_type(item, rule),
                "question": " ".join(
                    part for part in (item.title, item.intro) if part
                ).strip(),
            }
            snippet = _snippet(item)
            if snippet:
                question["code"] = snippet
            for check in rule.get("checks") or []:
                kind = str(check.get("kind") or "")
                if kind == "choice":
                    # No ``correct_answer``: the option list is visible prose,
                    # so the answer would sit beside it in plain sight. The
                    # check file remains the single place that knows the key.
                    choice = _choice_part(item)
                    if choice is not None and choice.statements:
                        options = choice.options_for(choice.statements[0])
                        question["question"] += " (" + " / ".join(options) + ")"
                elif kind == "lines":
                    entries = check.get("lines")
                    if isinstance(entries, list) and entries:
                        question["expected_output"] = "\n".join(
                            str(entry.get("expected") or "")
                            for entry in entries
                            if isinstance(entry, dict)
                        )
                    else:
                        question["expected_output"] = "\n".join(
                            str(line) for line in check.get("expected") or []
                        )
                elif kind == "program":
                    reference = (rule.get("reference") or {}).get("code")
                    if reference:
                        question["corrected_code"] = str(reference)
                    labels = [
                        str(criterion.get("label") or "")
                        for criterion in check.get("criteria") or []
                        if isinstance(criterion, dict)
                    ]
                    if any(labels):
                        question["requirements"] = [label for label in labels if label]
                elif kind == "keywords":
                    groups = check.get("groups") or []
                    labels = [
                        str(group.get("label") or "")
                        for group in groups
                        if isinstance(group, dict)
                    ]
                    question["requirements"] = [
                        label for label in labels if label
                    ] or ["זיהוי השגיאה במילים"]
                elif kind == "program_io":
                    requirements = [
                        "התוכנית קולטת את הנתונים ב-input",
                        "התוכנית ממירה מספרים ב-int ו-float",
                        "התוכנית מדפיסה הודעה אחת עם כל הנתונים",
                    ]
                    labels = [
                        str(criterion.get("label") or "")
                        for criterion in check.get("criteria") or []
                        if isinstance(criterion, dict)
                    ]
                    if any(labels):
                        requirements = [label for label in labels if label]
                    reference = (rule.get("reference") or {}).get("code")
                    if reference:
                        question["corrected_code"] = str(reference)
                    question["requirements"] = requirements
            questions.append(question)
        sections.append(
            {
                "section_name": section_name or "המבחן",
                "weight": sum(item.points for item in items),
                "questions": questions,
            }
        )
    return {
        "assignment_title": paper.title or config.title,
        "topic": config.instructions,
        "total_points": sum(section["weight"] for section in sections),
        "sections": sections,
    }


def write_rubrics(exam_name: str) -> list[Path]:
    config = load_exam(exam_name, require_rubric_files=False)
    if config is None:
        raise SystemExit(f"exam {exam_name!r} is missing or invalid")
    checks = checker.load_checks(config)
    written: list[Path] = []
    for version in config.versions:
        path = version.rubric_path
        path.parent.mkdir(parents=True, exist_ok=True)
        rubric = build_rubric(config, version, checks)
        if rubric["total_points"] != config.total_points:
            raise SystemExit(
                f"version {version.key}: rubric totals {rubric['total_points']} "
                f"but the exam declares {config.total_points}"
            )
        path.write_text(
            json.dumps(rubric, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


# ------------------------------------------------------------------- verify
def _perfect_answers(
    config: ExamConfig, version: ExamVersion, rules: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """The answer sheet a flawless student would hand in, taken from the key."""
    paper = load_paper(version)
    answers: dict[str, dict[str, Any]] = {}
    for item in paper.items:
        rule = rules[item.key]
        reference = str((rule.get("reference") or {}).get("code") or "")
        values: dict[str, Any] = {}
        for check in rule.get("checks") or []:
            kind = str(check.get("kind") or "")
            part = str(check.get("part") or "")
            if kind == "choice":
                values[part] = [str(check.get("answer") or "")]
            elif kind == "lines":
                entries = check.get("lines")
                if isinstance(entries, list) and entries:
                    values[part] = "\n".join(
                        str(entry.get("expected") or "")
                        for entry in entries
                        if isinstance(entry, dict)
                    )
                else:
                    values[part] = "\n".join(
                        str(line) for line in check.get("expected") or []
                    )
            elif kind == "keywords":
                groups = check.get("groups") or []
                if groups and all(isinstance(group, dict) for group in groups):
                    values[part] = " ".join(
                        str((group.get("words") or [""])[0]) for group in groups
                    )
                else:
                    values[part] = str(groups[0][0]) if groups else ""
            elif kind in {"program", "program_io"}:
                values[part] = reference
        answers[item.key] = values
    return answers


def _choice_options(item: Item) -> list[str]:
    part = _choice_part(item)
    if part is None or not part.statements:
        return []
    return [str(option) for option in part.options_for(part.statements[0])]


def _rubric_problems(paper, rules: dict[str, Any]) -> list[str]:
    """Prove the מחוון adds up before the exam is opened.

    Every question's checks must add up to the question's own points, and every
    list of lines, keyword groups or program criteria must add up to its check.
    A chosen answer must be one of the options the student actually sees. If
    any of these drift, the maximum stops being 100 and the rubric is unsafe.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for item in paper.items:
        rule = rules.get(item.key)
        if not isinstance(rule, dict):
            problems.append(f"{item.key}: אין סעיף ניקוד")
            continue
        seen.add(item.key)
        checks = rule.get("checks")
        if not isinstance(checks, list) or not checks:
            problems.append(f"{item.key}: אין בדיקות")
            continue
        total = 0
        for check in checks:
            if not isinstance(check, dict):
                problems.append(f"{item.key}: בדיקה אינה אובייקט")
                continue
            kind = str(check.get("kind") or "")
            points = int(check.get("points") or 0)
            total += points
            if kind == "choice":
                answer = str(check.get("answer") or "")
                if answer not in _choice_options(item):
                    problems.append(
                        f"{item.key}: התשובה {answer!r} אינה בין האפשרויות"
                    )
            elif kind == "lines":
                entries = [
                    entry
                    for entry in check.get("lines") or []
                    if isinstance(entry, dict)
                ]
                listed = sum(int(entry.get("points") or 0) for entry in entries)
                if not entries:
                    problems.append(f"{item.key}: אין שורות מחוון")
                elif listed != points:
                    problems.append(
                        f"{item.key}: שורות המחוון שוות {listed} ולא {points}"
                    )
            elif kind == "keywords":
                groups = [
                    group
                    for group in check.get("groups") or []
                    if isinstance(group, dict)
                ]
                listed = sum(int(group.get("points") or 0) for group in groups)
                if not groups:
                    problems.append(f"{item.key}: אין קבוצות מילים")
                elif listed != points:
                    problems.append(
                        f"{item.key}: קבוצות ההסבר שוות {listed} ולא {points}"
                    )
            elif kind in {"program", "program_io"}:
                criteria = [
                    spec
                    for spec in check.get("criteria") or []
                    if isinstance(spec, dict)
                ]
                listed = sum(int(spec.get("points") or 0) for spec in criteria)
                if not criteria:
                    problems.append(f"{item.key}: אין קריטריונים")
                elif listed != points:
                    problems.append(
                        f"{item.key}: הקריטריונים שווים {listed} ולא {points}"
                    )
                if not str((rule.get("reference") or {}).get("code") or "").strip():
                    problems.append(f"{item.key}: אין קוד ייחוס")
            else:
                problems.append(f"{item.key}: סוג בדיקה לא מוכר {kind!r}")
        if total != item.points:
            problems.append(f"{item.key}: סך הבדיקות {total} ולא {item.points}")
    for extra in sorted(set(rules) - seen):
        problems.append(f"{extra}: סעיף ניקוד ללא שאלה")
    return problems


def _check_tracing(
    paper_items: tuple[Item, ...], rules: dict[str, Any]
) -> list[str]:
    """Run every tracing snippet and compare real output with the key."""
    problems: list[str] = []
    for item in paper_items:
        rule = rules.get(item.key) or {}
        snippet = _snippet(item)
        if not snippet:
            continue
        for check in rule.get("checks") or []:
            if str(check.get("kind") or "") != "lines":
                continue
            entries = check.get("lines")
            if isinstance(entries, list) and entries:
                expected = [
                    str(entry.get("expected") or "")
                    for entry in entries
                    if isinstance(entry, dict)
                ]
            else:
                expected = [str(line) for line in check.get("expected") or []]
            run = checker.run_code(snippet, str(check.get("stdin") or ""))
            if not run.get("ok") or run.get("exit_code") or run.get("timed_out"):
                problems.append(
                    f"{item.key}: הקטע לא רץ (exit {run.get('exit_code')}, "
                    f"{run.get('error') or ''})"
                )
                continue
            printed = checker.normalise_line(run.get("stdout") or "")
            for line in expected:
                wanted = "".join(checker.normalise_line(line).split())
                if wanted not in "".join(printed.split()):
                    problems.append(
                        f"{item.key}: הקוד מדפיס {printed!r} ולא {line!r}"
                    )
    return problems


def verify(exam_name: str) -> int:
    config = load_exam(exam_name)
    if config is None:
        print(f"exam {exam_name!r} is missing or invalid")
        return 2
    checks = checker.load_checks(config)
    failures = 0
    for version in config.versions:
        rules = checker.version_rules(checks, version.key)
        paper = load_paper(version)
        label = f"{config.name}/{version.key} ({version.label})"

        problems = _rubric_problems(paper, rules) + _check_tracing(
            paper.items, rules
        )
        answers = _perfect_answers(config, version, rules)
        report = checker.check(config, version, answers, checks=checks)
        if report.score != config.total_points:
            problems.append(
                f"תשובה מושלמת קיבלה {report.score} מתוך {config.total_points}"
            )
        wrong = [q for q in report.questions if q.status != checker.CORRECT]
        for question in wrong:
            problems.append(
                f"{question.key}: סטטוס {question.status} "
                f"({question.earned}/{question.points}) - {question.notes}"
            )

        if problems:
            failures += 1
            print(f"FAIL {label}")
            for problem in problems:
                print(f"     - {problem}")
        else:
            print(
                f"OK   {label}: {len(paper.items)} שאלות, "
                f"{report.score}/{config.total_points} לתשובה מושלמת, "
                f"{report.runtime_ms}ms"
            )

    if failures:
        print(f"\n{failures} גרסאות לא עברו בדיקה; אין לפתוח את המבחן.")
        return 1
    print("\nכל הגרסאות נבדקו: התשובה המושלמת מקבלת 100 וכל קטעי הקוד רצים.")
    return 0


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("rubrics", "verify"))
    parser.add_argument("--exam", default=DEFAULT_EXAM)
    args = parser.parse_args(argv)

    if args.command == "rubrics":
        for path in write_rubrics(args.exam):
            print(f"wrote {path.relative_to(PROJECT_ROOT)}")
        return 0
    return verify(args.exam)


if __name__ == "__main__":
    sys.exit(main())
