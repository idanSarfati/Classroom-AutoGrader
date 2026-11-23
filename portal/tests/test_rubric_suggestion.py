"""An x.2 coursework belongs to lesson x, not to an exam-practice spec."""
from __future__ import annotations

import pytest

from portal.ingestion.classroom_source import preferred_lesson_rubric


@pytest.mark.parametrize(
    "title",
    [
        "1.2 | הגשת משימות",
        "2.2 | הגשת משימות",
        "3.2 | הגשת משימות",
        " 1.2 assignment",
    ],
)
def test_x2_titles_pick_the_lesson_rubric(title: str) -> None:
    prefix = title.strip()[0]
    assert preferred_lesson_rubric(title) == f"assignments/lesson_{prefix}.json"


@pytest.mark.parametrize(
    "title",
    [
        "תרגול יחידות 1-2",
        "1.3 | תרגול לפיצוח",
        "1.2.3 - אחר",
        "הגשת משימות 1.2",
        "",
    ],
)
def test_other_titles_keep_the_generic_suggestion(title: str) -> None:
    assert preferred_lesson_rubric(title) == ""


def test_missing_lesson_file_falls_back(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "portal.ingestion.classroom_source.PROJECT_ROOT", tmp_path
    )
    assert preferred_lesson_rubric("5.2 | הגשת משימות") == ""
