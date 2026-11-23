"""Unit 4.2 practice sheet: four function exercises, correct rubric binding.

The live 4.2 coursework ("4.2 | הגשת משימות") was once bound to the lesson 2
variables rubric because ``assignments/lesson_4.json`` did not exist and the
fuzzy matcher guessed wrong. These tests pin the new rubric + worksheet pair
so the mistake cannot come back through a future import.
"""
from __future__ import annotations

from portal.grading import adapter
from portal.ingestion.classroom_source import preferred_lesson_rubric
from portal.worksheets import for_rubric
from portal.worksheets.schema import load_by_slug

RUBRIC_PATH = "assignments/lesson_4.json"


def test_sheet_loads_with_the_four_exercises() -> None:
    worksheet = load_by_slug("lesson_4")
    assert worksheet is not None
    assert worksheet.slug == "lesson_4"
    assert [item.number for item in worksheet.items] == [1, 2, 3, 4]
    assert worksheet.note
    # Every exercise is a runnable IDE box (practice, not an exam).
    kinds = {part.kind for item in worksheet.items for part in item.parts}
    assert kinds == {"code"}


def test_rubric_binds_and_matches_the_sheet() -> None:
    binding = adapter.load_rubric(RUBRIC_PATH)
    assert binding.question_count == 4
    assert binding.sha256

    worksheet = for_rubric(RUBRIC_PATH)
    assert worksheet is not None
    assert worksheet.slug == "lesson_4"
    assert len(worksheet.items) == binding.question_count


def test_x2_import_now_suggests_the_lesson_4_rubric() -> None:
    assert preferred_lesson_rubric("4.2 | הגשת משימות") == RUBRIC_PATH
    assert preferred_lesson_rubric(" 4.2 assignment") == RUBRIC_PATH


def test_cake_sale_starter_is_the_teachers_exact_code() -> None:
    worksheet = load_by_slug("lesson_4")
    ex4 = worksheet.items[3]
    code = ex4.parts[0].code
    assert "def calculate_earnings():" in code
    assert 'total_cakes = int(input("Enter the number of cakes collected: "))' in code
    assert 'sold_cakes = int(input("Enter the number of cakes sold: "))' in code
    assert (
        'price_per_cake = float(input("Enter the selling price per cake: "))'
        in code
    )
    assert "expected_earnings = _____________ * price_per_cake" in code
    assert "actual_earnings = sold_cakes * ____________" in code
    assert "earnings_difference = expected_earnings - actual_earnings" in code
    assert (
        'print("Difference between expected and actual earnings:", _______________, "nis")'
        in code
    )
    assert code.rstrip().endswith("calculate_earnings()")


def test_carpet_starter_has_shapes_but_no_loops() -> None:
    worksheet = load_by_slug("lesson_4")
    ex3 = worksheet.items[2]
    code = ex3.parts[0].code
    # Unit 4.2 precedes loops: the base carpet is plain print lines.
    assert "for " not in code and "while " not in code
    assert code.count("def ") == 3
    assert "stars_triangle()" in code
    assert "stars_square()" in code
    assert "stars_line()" in code


def test_collect_and_compose_round_trip() -> None:
    worksheet = load_by_slug("lesson_4")
    form = {
        "answer_ex1__code": (
            "def triangle_perimeter():\n"
            "    a = int(input('side 1: '))\n"
            "    b = int(input('side 2: '))\n"
            "    c = int(input('side 3: '))\n"
            "    print('Perimeter:', a + b + c)\n"
            "triangle_perimeter()"
        ),
        "answer_ex2__code": (
            "def calculator():\n"
            "    x = int(input('num 1: '))\n"
            "    y = int(input('num 2: '))\n"
            "    z = int(input('num 3: '))\n"
            "    print(x + y + z)\n"
            "    print(x * y * z)\n"
            "calculator()"
        ),
        "answer_ex3__code": "stars_line()\nstars_triangle()\nstars_line()",
        "answer_ex4__code": "expected_earnings = total_cakes * price_per_cake",
    }
    values = worksheet.collect(form)
    assert worksheet.count_answered(values) == 4
    text = worksheet.compose(values)
    assert "triangle_perimeter()" in text
    assert "calculator()" in text
    assert "stars_line()" in text
    assert "total_cakes * price_per_cake" in text