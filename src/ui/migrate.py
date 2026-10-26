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

    try:
        items = list(service.list_course_work_items(course.id) or [])
    except Exception as exc:
        st.error(f"Failed to list coursework: {exc}")
        return

    draft_items = [
        item for item in items
        if str((item or {}).get("state") or "").strip().upper() == "DRAFT"
    ]
    if not draft_items:
        st.info("No draft assignments found for this course.")
        return

    draft_labels = [
        str((item or {}).get("title") or (item or {}).get("name") or f"Draft {i + 1}")
        for i, item in enumerate(draft_items)
    ]
    try:
        draft_index = int(st.selectbox("Draft to publish", draft_labels))
    except (TypeError, ValueError):
        draft_index = 0
    if draft_index < 0 or draft_index >= len(draft_items):
        draft_index = 0

    selected = draft_items[draft_index]
    work_id = (selected or {}).get("id") or (selected or {}).get("courseWorkId")

    st.markdown("---")
    st.subheader("Publish settings")

    due_date = st.date_input("Due date", value=None, help="Optional due date for the assignment")
    due_time = st.time_input("Due time", value=None, help="Optional due time")
    max_points = st.number_input("Max points", min_value=0, value=0, step=1, help="Maximum score for the assignment")
    make_copy = st.checkbox("Give each student a copy of attachments", value=False, help="When on, every student gets their own copy of any attached files")

    if st.button("Publish draft"):
        body = {"state": "PUBLISHED"}
        mask_parts = ["state"]

        if due_date is not None:
            body["dueDate"] = {"year": due_date.year, "month": due_date.month, "day": due_date.day}
            mask_parts.append("dueDate")
        if due_time is not None:
            body["dueTime"] = {"hours": due_time.hour, "minutes": due_time.minute}
            mask_parts.append("dueTime")
        if max_points and max_points > 0:
            body["maxPoints"] = int(max_points)
            mask_parts.append("maxPoints")

        if make_copy:
            materials = (selected or {}).get("materials") or []
            updated_materials = []
            for mat in materials:
                mat = dict(mat)
                drive = mat.get("driveFile") or {}
                link = mat.get("link") or {}
                if drive or link:
                    mat["shareMode"] = "STUDENT_COPY"
                updated_materials.append(mat)
            if updated_materials:
                body["materials"] = updated_materials
                mask_parts.append("materials")

        try:
            _execute = getattr(service, "_execute", None)
            _classroom = getattr(service, "classroom", None)
            if callable(_execute) and _classroom is not None:
                _execute(
                    _classroom.courses().courseWork().patch(
                        courseId=course.id, id=work_id,
                        updateMask=",".join(mask_parts), body=body
                    ),
                    "publish draft with settings",
                )
            else:
                for _name in ("publish_course_work", "publish_course_work_draft", "publish_draft", "release_draft", "publish"):
                    _fn = getattr(service, _name, None)
                    if callable(_fn):
                        try:
                            _fn(course.id, work_id)
                        except TypeError:
                            _fn(course_id=course.id, course_work_id=work_id)
                        break
            st.success("Draft published.")
        except Exception as exc:
            st.error(f"Failed to publish draft: {exc}")
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
