"""Manual integration test: single-pass Groq grading of a mock submission.

Usage (from the project root):

    python -m src.test_llm_grading

Loads ``assignments/lesson_1.json``, evaluates a mock student document
(lesson header + worksheet table + 4 print statements) with
:func:`src.llm_evaluator.evaluate_student_submission`, and prints the
resulting JSON plus formatted Hebrew feedback.

Requires ``GROQ_API_KEY`` in ``.env`` or the environment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Support running as a plain script (python src/test_llm_grading.py).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm_evaluator import LLMEvaluationError, evaluate_student_submission  # noqa: E402
from src.models import AssignmentConfig  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSIGNMENT_FILE = PROJECT_ROOT / "assignments" / "lesson_1.json"

# Mock student doc mirroring our real Classroom example: the lesson header,
# the worksheet's table labels, and the student's four print statements.
MOCK_STUDENT_TEXT = r"""שיעור 1 - פקודות הדפסה
שם התלמיד: נועה ישראלי

טבלת פקודות שלמדנו היום:
פקודה | תיאור
print | הדפסת טקסט למסך
\n | ירידת שורה
\t | טאב (הזחה)

התשובות שלי:

שאלה 1 - פקודת הדפסה:
print("Hello World")

שאלה 2 - פקודת הורד שורה:
print("שורה ראשונה\nשורה שנייה")

שאלה 3 - פקודת טאב:
print("שם: \tנועה")

שאלה 4 - משימה מסכמת:
print("תיכון חדש\tכיתה ט'\nשלום עולם")
"""


def load_assignment(path: Path = ASSIGNMENT_FILE) -> AssignmentConfig:
    """Load and validate a teacher assignment config from JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return AssignmentConfig.model_validate(data)


def main() -> int:
    assignment = load_assignment()
    student_name = "נועה ישראלי"
    print(f"Assignment: {assignment.title} ({len(assignment.tasks)} tasks)")
    print(f"Mock student: {student_name}\n")

    try:
        result = evaluate_student_submission(
            student_name, MOCK_STUDENT_TEXT, assignment
        )
    except LLMEvaluationError as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1

    print("=== GradingResult (JSON) ===")
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))

    print("\n=== משוב לסטודנט ===")
    print("=" * 60)
    print(f"תלמיד/ה: {result.student_name} | ציון: {result.score}/100")
    print("=" * 60)
    print(result.feedback_hebrew)
    print("-" * 60)
    for task in result.task_evaluations:
        found = (
            f" | נמצא: {task.student_answer_found}"
            if task.student_answer_found
            else ""
        )
        print(f"[{task.status}] {task.task_name}{found}")
        print(f"    Note: {task.notes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
