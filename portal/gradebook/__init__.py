"""Phase 5: teacher-facing gradebook calculations and exports.

Everything here is read-only with respect to grading: the matrix reads the
current revision's persisted :class:`~portal.models.Grade` and never asks the
engine to grade anything. This keeps the gradebook an honest view of what
students already see rather than a second source of truth.
"""
from __future__ import annotations

from portal.gradebook.exporter import export_filename, gradebook_to_csv
from portal.gradebook.matrix import (
    CELL_BADGES,
    CELL_FAILED,
    CELL_GRADED,
    CELL_LABELS,
    CELL_MISSING,
    CELL_QUEUED,
    AssignmentStats,
    ClassStatistics,
    Gradebook,
    GradebookCell,
    ProfileClass,
    ProfileEntry,
    StudentProfile,
    StudentRow,
    build_gradebook,
    build_student_profile,
)

__all__ = [
    "CELL_BADGES",
    "CELL_FAILED",
    "CELL_GRADED",
    "CELL_LABELS",
    "CELL_MISSING",
    "CELL_QUEUED",
    "AssignmentStats",
    "ClassStatistics",
    "Gradebook",
    "GradebookCell",
    "ProfileClass",
    "ProfileEntry",
    "StudentProfile",
    "StudentRow",
    "build_gradebook",
    "build_student_profile",
    "export_filename",
    "gradebook_to_csv",
]
