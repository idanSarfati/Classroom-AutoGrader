"""Per-question answer blocks built from the redacted rubric.

A lesson's x.2 assignment is a table of code questions that were shown on the
x.1 slides. Students should answer where they read the requirement: each block
shows one rubric item, then its own code frame, then the next item.

The grading engine still receives **one** document per submission, exactly as
before. This module owns the conversion in both directions:

* :func:`build_questions` flattens the student-safe rubric into display blocks
  (lesson tasks and exam-spec questions alike);
* :func:`compose_answers` writes the blocks into one labelled text - the text
  that is stored, graded and shown on the result page;
* :func:`split_answers` reads that text back into per-block answers when the
  student reopens the page.

The heading format is deliberately simple and stable
(``## שאלה 3: ex3_2 - ...``) so an older single-text submission still grades
exactly as it did, and only the new submissions gain structure.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

ANSWER_FIELD_PREFIX = "answer_"
HEADING_PREFIX = "## שאלה"

# Question types that are answered with a program. Exam specs also contain
# multiple-choice and output-tracing items, which get a plain answer box and no
# "run" button: there is no program of theirs to execute.
CODE_TYPES = frozenset({"code_writing", "debugging"})

# A stdin box is only useful for a program that reads input(). Lesson 3
# introduces input() and uses "קלט" / "input()" in every task description;
# lessons 1-2 never do.
_INPUT_RE = re.compile(
    r"input\s*\(|(?<![A-Za-z])input(?![A-Za-z])|קלט|קולט|קלוט|מהמשתמש",
    re.IGNORECASE,
)


def mentions_input(text: str) -> bool:
    """Whether a task's text or sample code reads from stdin."""
    return bool(_INPUT_RE.search(text or ""))


def rubric_mentions_input(rubric: Mapping[str, Any] | None) -> bool:
    """Fallback check for a rubric with no recognizable per-question items."""
    if not rubric:
        return False
    try:
        text = json.dumps(rubric, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return mentions_input(text)


@dataclass(frozen=True)
class FormQuestion:
    """One answerable block on the assignment page."""

    key: str
    number: int
    id_label: str
    title: str
    prompt: str
    section: str
    weight: int
    type: str
    code: str
    requirements: tuple[str, ...]
    is_code: bool
    needs_stdin: bool

    @property
    def field_name(self) -> str:
        return f"{ANSWER_FIELD_PREFIX}{self.key}"

    @property
    def heading(self) -> str:
        label = " - ".join(part for part in (self.id_label, self.title) if part)
        return label or f"שאלה {self.number}"


def build_questions(rubric: Mapping[str, Any] | None) -> list[FormQuestion]:
    """Flatten a redacted rubric into numbered answer blocks."""
    if not rubric:
        return []
    questions: list[FormQuestion] = []
    if rubric.get("schema") == "spec":
        for section in rubric.get("sections") or []:
            section_name = str(section.get("section_name") or "")
            weight = int(section.get("weight") or 0)
            for question in section.get("questions") or []:
                questions.append(
                    _question(
                        len(questions) + 1,
                        id_label=str(question.get("id") or ""),
                        title="",
                        prompt=str(question.get("question") or ""),
                        section=section_name,
                        weight=weight,
                        type=str(question.get("type") or ""),
                        code=str(question.get("code") or ""),
                        requirements=tuple(
                            str(item) for item in question.get("requirements") or []
                        ),
                    )
                )
        return questions

    for task in rubric.get("tasks") or []:
        questions.append(
            _question(
                len(questions) + 1,
                id_label=str(task.get("id") or ""),
                title=str(task.get("name") or ""),
                prompt=str(task.get("description") or ""),
                section="",
                weight=0,
                type="",
                code="",
                requirements=(),
            )
        )
    return questions


def _question(
    number: int,
    *,
    id_label: str,
    title: str,
    prompt: str,
    section: str,
    weight: int,
    type: str,
    code: str,
    requirements: tuple[str, ...],
) -> FormQuestion:
    is_code = (not type) or type in CODE_TYPES
    task_text = "\n".join((title, prompt, code, *requirements))
    return FormQuestion(
        key=str(number),
        number=number,
        id_label=id_label,
        title=title,
        prompt=prompt,
        section=section,
        weight=weight,
        type=type,
        code=code,
        requirements=requirements,
        is_code=is_code,
        needs_stdin=is_code and mentions_input(task_text),
    )


def compose_answers(
    questions: list[FormQuestion], answers: Mapping[str, str]
) -> str:
    """Build the single document the engine grades.

    Empty answers keep their heading on purpose: the grader can then report a
    missing answer for that specific question instead of guessing which answer
    belongs where.
    """
    blocks = [
        f"{HEADING_PREFIX} {question.number}: {question.heading}\n"
        f"{(answers.get(question.key) or '').strip()}"
        for question in questions
    ]
    return "\n\n".join(blocks).strip()


def has_any_answer(
    questions: list[FormQuestion], answers: Mapping[str, str]
) -> bool:
    return any((answers.get(question.key) or "").strip() for question in questions)


_HEADING_RE = re.compile(rf"^{re.escape(HEADING_PREFIX)} (\d+): .*$", re.MULTILINE)


def split_answers(
    questions: list[FormQuestion], text: str
) -> dict[str, str]:
    """Recover per-question answers from a stored composed submission.

    A submission written before this layout has no headings; it returns an
    empty map and the page shows it as the previous single-text submission.
    """
    matches = list(_HEADING_RE.finditer(text or ""))
    if not matches:
        return {}
    valid = {question.key for question in questions}
    answers: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = match.group(1)
        if key not in valid:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        answers[key] = text[start:end].strip()
    return answers


__all__ = [
    "ANSWER_FIELD_PREFIX",
    "CODE_TYPES",
    "FormQuestion",
    "build_questions",
    "compose_answers",
    "has_any_answer",
    "mentions_input",
    "rubric_mentions_input",
    "split_answers",
]