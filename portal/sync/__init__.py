"""Class expansion and cross-class content replication.

The portal starts with one fully populated reference class (ז'3). This package
initializes the remaining school classes, links each one to its own Google
Classroom course, copies the reference lessons and assignments, and resolves
each copy's publication state independently:

* ``classroom`` (default) - published only where the target course itself has
  a matching published item, so a class that has not reached a lesson keeps it
  as a draft;
* ``draft`` - everything stays a draft;
* ``published`` - everything is published (explicit teacher choice);
* ``source`` - mirror the reference class.

Nothing here writes outside ``portal/``. The service is deliberately split
into a read-only planner and an applier so the CLI and the web screen can show
exactly what will happen before touching the database.
"""
from __future__ import annotations

from portal.sync.matching import (
    EXCLUDED_CLASSES,
    TARGET_CLASSES,
    TARGET_CLASS_DISPLAY,
    class_key,
    find_course_matches,
    normalize_class_name,
)
from portal.sync.service import (
    MODE_CLASSROOM,
    MODE_DRAFT,
    MODE_LABELS,
    MODE_PUBLISHED,
    MODE_SOURCE,
    MODES,
    ClassPlan,
    ItemPlan,
    SourceItem,
    SyncError,
    SyncOutcome,
    SyncPlan,
    apply_plan,
    build_plan,
)

__all__ = [
    "EXCLUDED_CLASSES",
    "TARGET_CLASSES",
    "TARGET_CLASS_DISPLAY",
    "MODE_CLASSROOM",
    "MODE_DRAFT",
    "MODE_LABELS",
    "MODE_PUBLISHED",
    "MODE_SOURCE",
    "MODES",
    "ClassPlan",
    "ItemPlan",
    "SourceItem",
    "SyncError",
    "SyncOutcome",
    "SyncPlan",
    "apply_plan",
    "build_plan",
    "class_key",
    "find_course_matches",
    "normalize_class_name",
]
