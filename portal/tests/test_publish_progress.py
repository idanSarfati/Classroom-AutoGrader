"""The progress script publishes exactly the lessons up to each class target."""
from __future__ import annotations

from portal.scripts.publish_progress import decide, leading_number


def test_leading_number_reads_the_curriculum_prefix() -> None:
    assert leading_number("3.2 | הגשת משימות") == (3, 2)
    assert leading_number("0.3 | משימת התנסות | מצב הרוח שלי :)") == (0, 3)
    assert leading_number("תרגול יחידות 1-2") is None


def test_items_up_to_the_target_are_published() -> None:
    target = (3, 2)
    assert decide("0.3 | משימת התנסות", target)[0] is True
    assert decide("2.3 | תרגול לפיצוח - דף עבודה", target)[0] is True
    assert decide("3.2 | הגשת משימות", target)[0] is True
    assert decide("3.3 | תרגול לפיצוח - דף עבודה", target)[0] is False


def test_units_1_2_practice_follows_unit_2() -> None:
    assert decide("תרגול יחידות 1-2", (2, 3))[0] is True
    assert decide("תרגול יחידות 1-2", (1, 2))[0] is False


def test_unnumbered_titles_are_left_unchanged() -> None:
    assert decide("כותרת ללא מספר", (3, 3)) == (False, "unnumbered (unchanged)")