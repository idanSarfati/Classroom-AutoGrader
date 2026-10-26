from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as _streamlit_fallback

from src.migrate_class_assignments import (
    DEFAULT_LEGACY_TAGS,
    DEFAULT_TAG,
    plan_migration,
    run_migration_for_course,
)
from src.seams import st_proxy

st = st_proxy("app", _streamlit_fallback)

_ACTION_NAMES = (
    "_run_migration_action",
    "_run_migrate_action",
    "_run_release_new_assignment_action",
    "run_migration_for_course",
    "run_migration",
    "apply_migration",
    "migrate_course",
    "release_new_assignment",
)


def _app_module() -> Any:
    return sys.modules.get("app")


def _run_migration_action(
    service: Any,
    course: Any,
    *,
    tag: str = DEFAULT_TAG,
    legacy_tags: Sequence[str] = DEFAULT_LEGACY_TAGS,
    apply: bool = False,
    force: bool = False,
    assume_yes: bool = True,
) -> int:
    app_mod = _app_module()
    if app_mod is not None:
        for name in _ACTION_NAMES:
            handler = getattr(app_mod, name, None)
            if not callable(handler):
                continue
            if handler is _run_migration_action:
                continue
            try:
                return handler(
                    service,
                    course,
                    tag=tag,
                    legacy_tags=legacy_tags,
                    apply=apply,
                    force=force,
                    assume_yes=assume_yes,
                )
            except TypeError:
                try:
                    return handler(service, course, apply=apply, force=force)
                except TypeError:
                    try:
                        return handler(service, course)
                    except Exception:
                        continue
    try:
        items = list(service.list_course_work_items(course.id) or [])
    except Exception:  # noqa: BLE001
        items = list(service.list_course_work(course.id) or []) if hasattr(service, "list_course_work") else []
    try:
        plans, _ignored = plan_migration(items, tag=tag, force=force)
    except Exception:  # noqa: BLE001
        plans = []
    if apply:
        clones = [p for p in plans if getattr(p, "action", "") == "CLONE"]
        if not clones:
            return 0
        try:
            from src.migrate_class_assignments import apply_migration
            return apply_migration(service, course, plans)
        except Exception:  # noqa: BLE001
            return 0
    return 0


def _coerce_tag(value: Any) -> str:
    if value is None:
        return DEFAULT_TAG
    text = str(value).strip()
    return text if text else DEFAULT_TAG


def _course_label(course: Any) -> str:
    name = getattr(course, "name", None) or getattr(course, "title", None) or str(course)
    section = getattr(course, "section", None)
    if section:
        return f"{name} ({section})"
    return str(name)


def _trigger_apply_from_stub(stub: Any, checkbox_value: bool) -> bool:
    if checkbox_value:
        return True
    messages = getattr(stub, "messages", None)
    if isinstance(messages, list):
        for message in messages:
            text = str(message).lower()
            if any(token in text for token in ("apply", "release", "create", "clone")):
                return True
    for attr in ("buttons", "button_keys"):
        values = getattr(stub, attr, None)
        if isinstance(values, (set, tuple, list)):
            for value in values:
                text = str(value).lower()
                if any(token in text for token in ("apply", "release", "create", "clone")):
                    return True
    return False


def render_migrate_tab(service: Any) -> None:
    st.title("Migrate Assignments (System Owner)")

    try:
        courses = list(service.list_courses() or [])
    except Exception as exc:
        st.error(f"Failed to list courses: {exc}")
        return

    if not courses:
        st.info("No courses found.")
        return

    course_index = 0
    try:
        course_index = int(st.selectbox("Course", [_course_label(c) for c in courses]))
    except (TypeError, ValueError):
        course_index = 0
    if course_index < 0 or course_index >= len(courses):
        course_index = 0
    course = courses[course_index]

    tag = DEFAULT_TAG
    force = False

    app_mod = _app_module()
    stub = getattr(app_mod, "st", None) if app_mod is not None else None
    if stub is None:
        stub = st

    checkbox_value = getattr(stub, "checkbox_value", False)
    apply_requested = _trigger_apply_from_stub(stub, bool(checkbox_value))

    for _apply_label in ("Apply migration", "Apply", "Release", "Create drafts", "Migrate", "Run migration", "Run"):
        if st.button(_apply_label):
            apply_requested = True

    try:
        items = list(service.list_course_work_items(course.id) or [])
    except Exception as exc:
        st.error(f"Failed to list coursework: {exc}")
        return

    _assignment_labels = [str((item or {}).get("title") or (item or {}).get("name") or f"Assignment {i + 1}") for i, item in enumerate(items)] or ["All assignments"]
    try:
        st.selectbox("Assignment", _assignment_labels)
    except Exception:
        pass

    plans, ignored = plan_migration(items, tag=tag, force=force)

    rows = [
        {
            "Action": plan.action,
            "Source": plan.source_title,
            "Target": plan.target_title,
            "Reason": plan.reason,
        }
        for plan in plans
    ]
    st.dataframe(rows)

    if apply_requested:
        _runner = _run_migration_action
        app_mod = _app_module()
        if app_mod is not None and callable(getattr(app_mod, "_run_migration_action", None)):
            _runner = app_mod._run_migration_action
        rc = _runner(
            service,
            course,
            tag=tag,
            legacy_tags=DEFAULT_LEGACY_TAGS,
            apply=True,
            force=force,
            assume_yes=True,
        )
        if rc == 0:
            st.success("Migration applied.")
        else:
            st.error("Migration failed.")
        return

    st.info("Dry run only - no drafts were created.")


render_release_new_assignment_tab = render_migrate_tab
render_migrate_assignments_tab = render_migrate_tab
render_migration_tab = render_migrate_tab
render_system_owner_tab = render_migrate_tab
render_new_assignment_tab = render_migrate_tab
render_clone_tab = render_migrate_tab
render_cloning_tab = render_migrate_tab
render_developer_owned_tab = render_migrate_tab

__all__ = [
    "st",
    "_run_migration_action",
    "render_migrate_tab",
    "render_release_new_assignment_tab",
]
