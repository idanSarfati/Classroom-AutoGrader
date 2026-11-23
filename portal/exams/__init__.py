"""Timed, per-student randomised exams built on the worksheet machinery.

An exam is an ordinary :class:`~portal.models.Assignment` with ``exam_config``
set. Everything else about it - submissions, jobs, grades, the gradebook, the
teacher review screen - is the existing machinery, unchanged.

Three files describe an exam:

``portal/exams/<name>.json``
    The definition: title, versions, and which paper/rubric each version uses.
``portal/worksheets/<paper>.json``
    The student-facing paper, in the ordinary worksheet format. It contains
    **no answers** and, for an exam, no ``code`` part - only ``code_plain``,
    so there is no Run button next to a "what is the output?" question.
``portal/exams/checks/<name>.json``
    The answer key and the marking rules. This is the file the automatic
    checker reads; see :mod:`portal.exams.checker`.

The definition is validated on load and cached by mtime, so a teacher edit is
picked up without a restart but a broken file can never half-apply.
"""
from __future__ import annotations

from portal.exams.config import (
    ExamConfig,
    ExamVersion,
    ExamConfigError,
    for_assignment,
    load_exam,
)

__all__ = [
    "ExamConfig",
    "ExamVersion",
    "ExamConfigError",
    "for_assignment",
    "load_exam",
]