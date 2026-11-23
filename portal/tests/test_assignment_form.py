"""Per-question worksheet blocks and the single graded document they build."""
from __future__ import annotations

from portal.web import assignment_form


LESSON_RUBRIC = {
    "schema": "config",
    "assignment_id": "lesson_1",
    "title": "שיעור 1",
    "tasks": [
        {"id": "1", "name": "פקודת הדפסה", "description": "הדפיסו שלום"},
        {"id": "2", "name": "הורד שורה", "description": "השתמשו ב-\\n"},
        {"id": "3", "name": "משימה מסכמת", "description": "כתבו תוכנית"},
    ],
}

SPEC_RUBRIC = {
    "schema": "spec",
    "assignment_title": "בוחן",
    "topic": "משתנים",
    "total_points": 100,
    "sections": [
        {
            "section_name": "חלק א",
            "weight": 60,
            "questions": [
                {
                    "id": "q1",
                    "type": "code_writing",
                    "question": "כתבו קוד",
                    "requirements": ["הדפיסו שם"],
                },
                {
                    "id": "q2",
                    "type": "expected_output",
                    "question": "מה הפלט?",
                },
            ],
        },
        {
            "section_name": "חלק ב",
            "weight": 40,
            "questions": [
                {"id": "q3", "type": "multiple_choice", "question": "בחרו"},
            ],
        },
    ],
}


def test_build_questions_flattens_a_lesson_rubric() -> None:
    questions = assignment_form.build_questions(LESSON_RUBRIC)

    assert [question.key for question in questions] == ["1", "2", "3"]
    assert [question.number for question in questions] == [1, 2, 3]
    assert questions[0].title == "פקודת הדפסה"
    assert questions[0].prompt == "הדפיסו שלום"
    assert questions[0].is_code is True
    assert questions[0].field_name == "answer_1"


def test_build_questions_keeps_spec_sections_and_types() -> None:
    questions = assignment_form.build_questions(SPEC_RUBRIC)

    assert [question.id_label for question in questions] == ["q1", "q2", "q3"]
    assert [question.section for question in questions] == ["חלק א", "חלק א", "חלק ב"]
    assert [question.weight for question in questions] == [60, 60, 40]
    # Only program answers get the dark editor and the run button.
    assert questions[0].is_code is True
    assert questions[1].is_code is False
    assert questions[2].is_code is False
    assert questions[0].requirements == ("הדפיסו שם",)


def test_build_questions_handles_missing_rubric() -> None:
    assert assignment_form.build_questions(None) == []
    assert assignment_form.build_questions({}) == []


def test_compose_answers_labels_every_question() -> None:
    questions = assignment_form.build_questions(LESSON_RUBRIC)

    text = assignment_form.compose_answers(
        questions, {"1": "print('a')", "2": "   ", "3": "print('c')"}
    )

    assert "## שאלה 1: 1 - פקודת הדפסה\nprint('a')" in text
    # An empty answer keeps its heading so the grader can mark it missing.
    assert "## שאלה 2: 2 - הורד שורה\n\n" in text
    assert "## שאלה 3: 3 - משימה מסכמת\nprint('c')" in text


def test_split_answers_round_trips_composed_text() -> None:
    questions = assignment_form.build_questions(SPEC_RUBRIC)
    composed = assignment_form.compose_answers(
        questions, {"1": "x = 1\ny = 2", "2": "12", "3": "ב"}
    )

    assert assignment_form.split_answers(questions, composed) == {
        "1": "x = 1\ny = 2",
        "2": "12",
        "3": "ב",
    }


def test_split_answers_treats_plain_text_as_legacy() -> None:
    questions = assignment_form.build_questions(LESSON_RUBRIC)

    assert assignment_form.split_answers(questions, "x = 5\nprint(x)") == {}
    assert assignment_form.split_answers(questions, "") == {}


def test_split_answers_ignores_unknown_question_numbers() -> None:
    questions = assignment_form.build_questions(LESSON_RUBRIC)
    text = "## שאלה 1: 1 - א\nfirst\n\n## שאלה 9: 9 - זר\nninth"

    assert assignment_form.split_answers(questions, text) == {"1": "first"}


def test_has_any_answer_ignores_whitespace() -> None:
    questions = assignment_form.build_questions(LESSON_RUBRIC)

    assert not assignment_form.has_any_answer(questions, {"1": "  ", "2": "\n"})
    assert assignment_form.has_any_answer(questions, {"1": "  ", "2": "print(1)"})


def test_stdin_only_for_tasks_that_read_input() -> None:
    no_input = assignment_form.build_questions(LESSON_RUBRIC)
    assert [question.needs_stdin for question in no_input] == [False, False, False]

    rubric = {
        "schema": "config",
        "tasks": [
            {
                "id": "1",
                "name": "קליטת שם",
                "description": "name = input() ואז print(name)",
            },
            {
                "id": "2",
                "name": "הדפסה",
                "description": "הדפיסו שלום",
            },
        ],
    }
    questions = assignment_form.build_questions(rubric)
    assert [question.needs_stdin for question in questions] == [True, False]


def test_stdin_never_appears_on_a_non_code_answer() -> None:
    rubric = {
        "schema": "spec",
        "sections": [
            {
                "section_name": "חלק א",
                "weight": 100,
                "questions": [
                    {
                        "id": "q1",
                        "type": "multiple_choice",
                        "question": "מה מחזיר input()?",
                    },
                    {
                        "id": "q2",
                        "type": "code_writing",
                        "question": "קלטו גיל",
                        "code": "age = input()",
                    },
                ],
            }
        ],
    }
    questions = assignment_form.build_questions(rubric)

    assert questions[0].needs_stdin is False
    assert questions[1].needs_stdin is True


def test_rubric_mentions_input_scans_the_whole_rubric() -> None:
    assert assignment_form.rubric_mentions_input(LESSON_RUBRIC) is False
    assert (
        assignment_form.rubric_mentions_input(
            {"tasks": [{"description": "קלטו מספר"}]}
        )
        is True
    )
