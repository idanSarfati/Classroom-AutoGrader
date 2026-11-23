"""Teacher screen for expanding the reference class into the school classes.

The page shows the same plan the CLI prints: which classes will be created,
which Classroom course each one maps to, how many lessons/assignments will be
copied, and what will be published or left as a draft. The teacher chooses the
classes and a publication mode per class, then applies.

The screen reads the last Classroom catalog scan and never calls Google, so it
opens instantly. A stale scan is safe: publication falls back to drafts and the
page says so.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from portal.models import User
from portal.sync import (
    MODE_CLASSROOM,
    MODES,
    SyncError,
    apply_plan,
    build_plan,
)
from portal.sync.service import MODE_LABELS
from portal.web.deps import flash, get_db, render, require_teacher, verify_csrf

router = APIRouter(prefix="/teacher")


def _page(request: Request, user: User, db: Session, *, status_code: int = 200):
    plan = None
    error = ""
    try:
        plan = build_plan(db, user.id, mode=MODE_CLASSROOM)
    except SyncError as exc:
        error = str(exc)
    response = render(
        request,
        "teacher_class_sync.html",
        user=user,
        plan=plan,
        error=error,
        modes=MODES,
        mode_labels=MODE_LABELS,
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/sync-classes")
def sync_classes_page(
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    return _page(request, teacher, db)


@router.post(
    "/sync-classes",
    dependencies=[Depends(verify_csrf)],
)
async def apply_sync_classes(
    request: Request,
    teacher: User = Depends(require_teacher),
    db: Session = Depends(get_db),
):
    form = await request.form()
    targets = [
        str(value).strip()
        for value in form.getlist("target")
        if str(value).strip()
    ]
    if not targets:
        flash(request, "לא נבחרו כיתות להרחבה.", kind="error")
        return RedirectResponse("/teacher/sync-classes", status_code=303)

    class_modes = {
        token: str(form.get(f"mode_{token}") or MODE_CLASSROOM)
        for token in targets
    }
    try:
        plan = build_plan(
            db,
            teacher.id,
            targets=targets,
            mode=MODE_CLASSROOM,
            class_modes=class_modes,
        )
        outcome = apply_plan(db, plan)
        db.commit()
    except SyncError as exc:
        db.rollback()
        flash(request, str(exc), kind="error")
        return RedirectResponse("/teacher/sync-classes", status_code=303)
    except Exception as exc:  # noqa: BLE001 - show a useful message, not a 500
        db.rollback()
        flash(request, f"ההרחבה נכשלה: {exc}", kind="error")
        return RedirectResponse("/teacher/sync-classes", status_code=303)

    if outcome.errors:
        flash(
            request,
            outcome.summary_he(),
            kind="error" if not outcome.created_total else "info",
        )
    else:
        flash(request, outcome.summary_he(), kind="success")
    return RedirectResponse("/teacher/sync-classes", status_code=303)
