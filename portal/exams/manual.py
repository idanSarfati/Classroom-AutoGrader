"""Teacher overrides for an automatically marked exam.

The exam checker is deterministic on purpose: every point comes from a rule in
the answer key, and the same answer always earns the same score. That is what
makes the mark defensible, but it also means a typed answer can lose points for
something the teacher judges irrelevant - a misspelled word in copied output,
an explanation phrased with a word the keyword list did not anticipate.

This module lets the teacher set the points of one question by hand, or give it
full credit in one click, and undo that decision later. Everything is stored
inside the same ``result_json`` the checker wrote, so the student page, the
gradebook, the audit and the CSV export all read one consistent result. The
automatic numbers are kept next to the manual ones; clearing an override puts
them back exactly as they were.
"""
from __future__ import annotations

import json
from typing import Any

from portal.exams import checker
from portal.models import Grade
from portal.timeutil import utcnow

MANUAL_NOTE = "הניקוד עודכן ידנית על ידי המורה."
_RESTORED_NOTE = "התאמת הניקוד בוטלה; הוחזר הניקוד האוטומטי."
_DISQUALIFY_NOTE = "ההגשה נפסלה ונקבע לה ציון קבוע על ידי המורה."
_DISQUALIFY_CLEAR_NOTE = "פסילת ההגשה בוטלה; הוחזר הציון האוטומטי."


class ExamAdjustmentError(ValueError):
    """The hand-in cannot be adjusted, or the requested points are invalid."""


# ------------------------------------------------------------- disqualification
def disqualification(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The whole-paper fixed-score decision stored on a result, or ``None``.

    A disqualification is distinct from a per-question manual override: it locks
    the *final* score of the entire hand-in (for example a paper disqualified for
    copying). It lives at the top level of ``result_json`` so every projection and
    every re-mark path reads one consistent decision.
    """
    if not isinstance(record, dict):
        return None
    value = record.get("disqualification")
    return value if isinstance(value, dict) else None


def is_disqualified(record: dict[str, Any] | None) -> bool:
    return disqualification(record) is not None


def disqualification_feedback(score: int) -> str:
    """The student-facing paragraph for a disqualified hand-in.

    The teacher's reason is never part of this text - it is teacher-only, exactly
    like a calibration note. The student sees that the paper was disqualified and
    the fixed score that was set, nothing more.
    """
    return f"ההגשה נפסלה על ידי המורה. הציון הקבוע שנקבע: {score} מתוך 100."


# ------------------------------------------------------------------ internals
def _result(grade: Grade) -> dict[str, Any]:
    try:
        result = json.loads(grade.result_json or "{}")
    except (TypeError, ValueError) as exc:
        raise ExamAdjustmentError("תוצאת הבדיקה אינה קריאה.") from exc
    if not isinstance(result, dict):
        raise ExamAdjustmentError("תוצאת הבדיקה אינה קריאה.")
    return result


def _questions(result: dict[str, Any]) -> list[dict[str, Any]]:
    exam = result.get("exam")
    questions = exam.get("questions") if isinstance(exam, dict) else None
    if not isinstance(questions, list) or not questions:
        raise ExamAdjustmentError(
            "אפשר להתאים ניקוד רק להגשה שנבדקה על ידי בודק המבחן."
        )
    return questions


def _find(
    questions: list[dict[str, Any]], item_key: str
) -> tuple[int, dict[str, Any]]:
    for index, question in enumerate(questions):
        if str(question.get("key")) == str(item_key):
            return index, question
    raise ExamAdjustmentError("השאלה לא נמצאה בבדיקה של ההגשה הזו.")


def _tasks(result: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = result.get("task_evaluations")
    return tasks if isinstance(tasks, list) else []


def _status(earned: int, points: int, previous: str) -> str:
    if earned <= 0 and previous == checker.MISSING:
        return checker.MISSING
    if points and earned >= points:
        return checker.CORRECT
    if earned > 0:
        return checker.PARTIALLY_CORRECT
    return checker.INCORRECT


def refresh(result: dict[str, Any]) -> None:
    """Recompute every derived field of one result record, in place."""
    questions = _questions(result)
    tasks = _tasks(result)
    maximum = int((result.get("exam") or {}).get("maximum") or 100)
    score = max(0, min(100, sum(int(item.get("earned") or 0) for item in questions)))
    missed = [
        str(item.get("title") or item.get("key") or "")
        for item in questions
        if int(item.get("earned") or 0) < int(item.get("points") or 0)
    ]
    result["score"] = score
    result["feedback_hebrew"] = checker.feedback_text(
        score, maximum, missed, str(result.get("student_name") or "")
    )
    result["deduction_breakdown"] = [
        {
            "task_name": item.get("title") or item.get("key"),
            "points_deducted": int(item.get("points") or 0)
            - int(item.get("earned") or 0),
            "reason": (
                str(tasks[index].get("notes") or "")
                if index < len(tasks)
                else ""
            ),
        }
        for index, item in enumerate(questions)
        if int(item.get("earned") or 0) < int(item.get("points") or 0)
    ]
    review = any(item.get("needs_review") for item in questions)
    result["audit"] = {
        "ok": not review,
        "needs_review": review,
        "summary": (
            "כל השאלות נבדקו אוטומטית"
            if not review
            else f"{sum(1 for item in questions if item.get('needs_review'))} שאלות דורשות מבט של המורה"
        ),
    }
    # A whole-paper disqualification overrides the headline score: the teacher
    # fixed it by hand, so the sum of the questions no longer decides it. The
    # per-question marks stay intact for the teacher's review; only the final
    # score, the feedback and the audit are replaced. Every path that refreshes a
    # record (manual edit, re-mark, apply) therefore keeps the locked score.
    disq = disqualification(result)
    if disq is not None:
        fixed = max(0, min(100, int(disq.get("score") or 0)))
        result["score"] = fixed
        result["feedback_hebrew"] = disqualification_feedback(fixed)
        result["deduction_breakdown"] = []
        result["audit"] = {
            "ok": True,
            "needs_review": False,
            "summary": _DISQUALIFY_NOTE,
        }


def _apply(
    grade: Grade, result: dict[str, Any], questions: list[dict[str, Any]]
) -> None:
    """Recompute every denormalised field from the (possibly adjusted) result."""
    refresh(result)
    audit = result["audit"]
    grade.score = int(result["score"])
    grade.feedback_hebrew = str(result["feedback_hebrew"])
    grade.result_json = json.dumps(result, ensure_ascii=False)
    grade.deduction_json = json.dumps(result["deduction_breakdown"], ensure_ascii=False)
    grade.audit_json = json.dumps(audit, ensure_ascii=False)
    grade.audit_ok = bool(audit["ok"])
    grade.audit_needs_review = bool(audit["needs_review"])
    grade.audit_summary = str(audit["summary"])[:500]


def _log(grade: Grade, message: str) -> None:
    text = (grade.review_note or "").strip()
    combined = f"{text}\n{message}".strip() if text else message
    grade.review_note = combined[-5000:]


def _label(question: dict[str, Any]) -> str:
    return str(question.get("title") or question.get("key") or "")


def _find_part(question: dict[str, Any], part_key: str) -> dict[str, Any]:
    parts = question.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ExamAdjustmentError("לשאלה הזו אין חלקים נפרדים לניקוד.")
    for part in parts:
        if isinstance(part, dict) and str(part.get("part")) == str(part_key):
            return part
    raise ExamAdjustmentError("החלק לא נמצא בשאלה הזו.")


def _recompute_question(question: dict[str, Any]) -> None:
    """A multi-part question's total is the sum of its parts.

    Used after a part-level override: the question no longer carries one manual
    number of its own, so its earned points, status and review flag all follow
    the parts (each part is either the automatic mark or the teacher's override).
    """
    parts = [p for p in (question.get("parts") or []) if isinstance(p, dict)]
    maximum = int(question.get("points") or 0)
    earned = max(0, min(maximum, sum(int(p.get("earned") or 0) for p in parts)))
    question["earned"] = earned
    question["status"] = _status(
        earned, maximum, str(question.get("status") or "")
    )
    question["needs_review"] = any(bool(p.get("needs_review")) for p in parts)


# ---------------------------------------------------------------------- public
def adjust_record(
    record: dict[str, Any],
    item_key: str,
    *,
    points: int | None,
    note: str = "",
    teacher_id: int | None = None,
    teacher_name: str = "",
    full_credit: bool = False,
) -> dict[str, Any]:
    """Set one question's points inside a raw result record, in place.

    This is the override logic without any ``Grade`` persistence, so the same
    code adjusts a stored grade *and* an unapplied model-check preview record.
    It does not refresh the derived fields - the caller does that once, after
    any other edits. Returns what the feedback loop needs: the automatic score
    the teacher overrode (``auto_points``), the new score, the maximum, a compact
    snapshot of the student's answer and the question label.
    """
    questions = _questions(record)
    index, question = _find(questions, item_key)
    maximum = int(question.get("points") or 0)
    wanted = maximum if full_credit else int(points or 0)
    if wanted < 0 or wanted > maximum:
        raise ExamAdjustmentError(
            f"הניקוד חייב להיות בין 0 ל-{maximum} עבור השאלה הזו."
        )
    tasks = _tasks(record)
    task = tasks[index] if index < len(tasks) else None
    if not isinstance(question.get("manual"), dict):
        # Keep the automatic numbers aside so the override is reversible.
        question["manual"] = {
            "auto_points": int(question.get("earned") or 0),
            "auto_status": str(question.get("status") or ""),
            "auto_review": bool(question.get("needs_review")),
            "auto_notes": str(task.get("notes") or "") if task else "",
        }
    previous = int(question["manual"].get("auto_points") or 0)
    question["earned"] = wanted
    question["status"] = _status(
        wanted, maximum, str(question.get("manual", {}).get("auto_status") or "")
    )
    # A human has now looked at this question, so it no longer needs review.
    question["needs_review"] = False
    question["manual"].update(
        {
            "points": wanted,
            "note": (note or "").strip()[:1000],
            "by": teacher_id,
            "by_name": teacher_name,
            "at": utcnow().isoformat(timespec="seconds"),
        }
    )
    if task is not None:
        task["status"] = question["status"]
        base = str(question["manual"].get("auto_notes") or "").strip()
        task["notes"] = f"{base} {MANUAL_NOTE}".strip()
    return {
        "auto_points": previous,
        "points": wanted,
        "maximum": maximum,
        "answer": str(task.get("student_answer_found") or "") if task else "",
        "label": _label(question),
        "full_credit": bool(full_credit),
    }


def clear_record(
    record: dict[str, Any],
    item_key: str,
    *,
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> dict[str, Any]:
    """Put one question's automatic points back inside a raw record, in place."""
    questions = _questions(record)
    index, question = _find(questions, item_key)
    manual = question.get("manual")
    if not isinstance(manual, dict):
        raise ExamAdjustmentError("לשאלה הזו אין התאמת ניקוד ידנית.")
    tasks = _tasks(record)
    task = tasks[index] if index < len(tasks) else None
    question["earned"] = int(manual.get("auto_points") or 0)
    question["status"] = str(manual.get("auto_status") or question.get("status") or "")
    question["needs_review"] = bool(manual.get("auto_review"))
    if task is not None:
        task["status"] = question["status"]
        task["notes"] = str(manual.get("auto_notes") or "")
    question.pop("manual", None)
    return {"label": _label(question)}


def adjust_part_record(
    record: dict[str, Any],
    item_key: str,
    part_key: str,
    *,
    points: int | None,
    note: str = "",
    teacher_id: int | None = None,
    teacher_name: str = "",
    full_credit: bool = False,
) -> dict[str, Any]:
    """Set one *part's* points inside a raw result record, in place.

    For a question with several scored parts (an explanation and a corrected
    program, say) the teacher grades each part within its own rubric slice; the
    question total becomes the sum of its parts. Editing a part supersedes any
    legacy whole-question override, so the two never contradict each other. Like
    :func:`adjust_record`, this does not refresh the derived fields - the caller
    does that once - and returns what the feedback loop needs.
    """
    questions = _questions(record)
    index, question = _find(questions, item_key)
    part = _find_part(question, part_key)
    maximum = int(part.get("points") or 0)
    wanted = maximum if full_credit else int(points or 0)
    if wanted < 0 or wanted > maximum:
        raise ExamAdjustmentError(
            f"הניקוד חייב להיות בין 0 ל-{maximum} עבור החלק הזה."
        )
    # A part edit replaces a legacy whole-question override: the total is now
    # the sum of the parts, not one hand-set number for the whole question.
    question.pop("manual", None)
    if not isinstance(part.get("manual"), dict):
        part["manual"] = {
            "auto_points": int(part.get("earned") or 0),
            "auto_review": bool(part.get("needs_review")),
            "auto_note": str(part.get("note") or ""),
        }
    previous = int(part["manual"].get("auto_points") or 0)
    part["earned"] = wanted
    part["needs_review"] = False
    part["manual"].update(
        {
            "points": wanted,
            "note": (note or "").strip()[:1000],
            "by": teacher_id,
            "by_name": teacher_name,
            "at": utcnow().isoformat(timespec="seconds"),
        }
    )
    _recompute_question(question)
    tasks = _tasks(record)
    task = tasks[index] if index < len(tasks) else None
    if task is not None:
        task["status"] = question["status"]
        base = str(task.get("notes") or "").strip()
        if MANUAL_NOTE not in base:
            task["notes"] = f"{base} {MANUAL_NOTE}".strip()
    return {
        "auto_points": previous,
        "points": wanted,
        "maximum": maximum,
        "answer": str(task.get("student_answer_found") or "") if task else "",
        "label": _label(question),
        "part_key": str(part_key),
        "part_label": str(part.get("label") or part_key),
        "full_credit": bool(full_credit),
    }


def clear_part_record(
    record: dict[str, Any],
    item_key: str,
    part_key: str,
    *,
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> dict[str, Any]:
    """Put one part's automatic points back inside a raw record, in place."""
    questions = _questions(record)
    index, question = _find(questions, item_key)
    part = _find_part(question, part_key)
    manual = part.get("manual")
    if not isinstance(manual, dict):
        raise ExamAdjustmentError("לחלק הזה אין התאמת ניקוד ידנית.")
    part["earned"] = int(manual.get("auto_points") or 0)
    part["needs_review"] = bool(manual.get("auto_review"))
    part.pop("manual", None)
    _recompute_question(question)
    tasks = _tasks(record)
    task = tasks[index] if index < len(tasks) else None
    if task is not None:
        task["status"] = question["status"]
    return {"label": _label(question), "part_key": str(part_key)}


def adjust(
    grade: Grade,
    item_key: str,
    *,
    points: int | None,
    note: str = "",
    teacher_id: int | None = None,
    teacher_name: str = "",
    full_credit: bool = False,
) -> dict[str, Any]:
    """Set one question's points on a stored grade; ``full_credit`` ignores ``points``.

    Returns the same info dict as :func:`adjust_record` so the caller can capture
    the teacher's correction for the feedback loop.
    """
    result = _result(grade)
    info = adjust_record(
        result,
        item_key,
        points=points,
        note=note,
        teacher_id=teacher_id,
        teacher_name=teacher_name,
        full_credit=full_credit,
    )
    reason = "מתן ניקוד מלא" if full_credit else "עדכון ניקוד"
    _log(
        grade,
        f"{reason} ידני — {info['label']}: "
        f"{info['auto_points']}→{info['points']}/{info['maximum']}"
        + (f" · {note.strip()}" if (note or "").strip() else ""),
    )
    _apply(grade, result, _questions(result))
    return info


def clear(
    grade: Grade,
    item_key: str,
    *,
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> None:
    """Put the automatic points of one question back."""
    result = _result(grade)
    info = clear_record(
        result, item_key, teacher_id=teacher_id, teacher_name=teacher_name
    )
    _log(grade, f"{_RESTORED_NOTE} — {info['label']}")
    _apply(grade, result, _questions(result))


def adjust_part(
    grade: Grade,
    item_key: str,
    part_key: str,
    *,
    points: int | None,
    note: str = "",
    teacher_id: int | None = None,
    teacher_name: str = "",
    full_credit: bool = False,
) -> dict[str, Any]:
    """Set one part's points on a stored grade; ``full_credit`` ignores ``points``."""
    result = _result(grade)
    info = adjust_part_record(
        result,
        item_key,
        part_key,
        points=points,
        note=note,
        teacher_id=teacher_id,
        teacher_name=teacher_name,
        full_credit=full_credit,
    )
    reason = "מתן ניקוד מלא" if full_credit else "עדכון ניקוד"
    _log(
        grade,
        f"{reason} ידני — {info['label']} · {info['part_label']}: "
        f"{info['auto_points']}→{info['points']}/{info['maximum']}"
        + (f" · {note.strip()}" if (note or "").strip() else ""),
    )
    _apply(grade, result, _questions(result))
    return info


def clear_part(
    grade: Grade,
    item_key: str,
    part_key: str,
    *,
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> None:
    """Put the automatic points of one part back."""
    result = _result(grade)
    info = clear_part_record(
        result, item_key, part_key, teacher_id=teacher_id, teacher_name=teacher_name
    )
    _log(grade, f"{_RESTORED_NOTE} — {info['label']} · {info['part_key']}")
    _apply(grade, result, _questions(result))


def carry_over(
    old_record: dict[str, Any] | None, new_record: dict[str, Any]
) -> int:
    """Keep the teacher's manual points when the checker runs again.

    A re-mark replaces the automatic numbers with a fresh verdict, but a
    question the teacher has already judged must not silently revert. The
    automatic result of the *new* run is stored beside the manual points, so
    clearing the override later restores the improved verdict rather than the
    old one. A whole-paper disqualification is carried the same way, so a locked
    score survives a re-mark. Returns the number of questions kept.
    """
    if not isinstance(old_record, dict) or not isinstance(new_record, dict):
        return 0
    # A disqualification locks the final score, so it travels to the new record
    # before anything else; refresh() below then forces the fixed score even when
    # no individual question was hand-adjusted.
    carried_disq = is_disqualified(old_record)
    if carried_disq:
        new_record["disqualification"] = dict(old_record["disqualification"])
    old_questions = (old_record.get("exam") or {}).get("questions") or []
    if not isinstance(old_questions, list):
        return 0
    by_key = {
        str(question.get("key")): question
        for question in old_questions
        if isinstance(question, dict)
    }
    questions = (new_record.get("exam") or {}).get("questions") or []
    tasks = _tasks(new_record)
    kept = 0
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        old = by_key.get(str(question.get("key")))
        if not isinstance(old, dict):
            continue
        manual = old.get("manual")
        task = tasks[index] if index < len(tasks) else None
        if isinstance(manual, dict):
            maximum = int(question.get("points") or 0)
            wanted = max(0, min(maximum, int(manual.get("points") or 0)))
            question["manual"] = {
                "auto_points": int(question.get("earned") or 0),
                "auto_status": str(question.get("status") or ""),
                "auto_review": bool(question.get("needs_review")),
                "auto_notes": str(task.get("notes") or "") if task else "",
                "points": wanted,
                "note": str(manual.get("note") or ""),
                "by": manual.get("by"),
                "by_name": str(manual.get("by_name") or ""),
                "at": str(manual.get("at") or ""),
            }
            question["earned"] = wanted
            question["status"] = _status(
                wanted, maximum, str(question.get("status") or "")
            )
            question["needs_review"] = False
            if task is not None:
                task["status"] = question["status"]
                base = str(task.get("notes") or "").strip()
                task["notes"] = f"{base} {MANUAL_NOTE}".strip()
            kept += 1
            continue
        # No whole-question override: carry any part-level overrides instead, so
        # a teacher who graded the explanation and the code separately keeps both
        # decisions when the model marks the paper again.
        old_parts = {
            str(p.get("part")): p
            for p in (old.get("parts") or [])
            if isinstance(p, dict)
        }
        carried = False
        for part in question.get("parts") or []:
            if not isinstance(part, dict):
                continue
            old_part = old_parts.get(str(part.get("part")))
            part_manual = (
                old_part.get("manual") if isinstance(old_part, dict) else None
            )
            if not isinstance(part_manual, dict):
                continue
            part_max = int(part.get("points") or 0)
            part_wanted = max(0, min(part_max, int(part_manual.get("points") or 0)))
            part["manual"] = {
                "auto_points": int(part.get("earned") or 0),
                "auto_review": bool(part.get("needs_review")),
                "auto_note": str(part.get("note") or ""),
                "points": part_wanted,
                "note": str(part_manual.get("note") or ""),
                "by": part_manual.get("by"),
                "by_name": str(part_manual.get("by_name") or ""),
                "at": str(part_manual.get("at") or ""),
            }
            part["earned"] = part_wanted
            part["needs_review"] = False
            carried = True
        if carried:
            _recompute_question(question)
            if task is not None:
                task["status"] = question["status"]
                base = str(task.get("notes") or "").strip()
                if MANUAL_NOTE not in base:
                    task["notes"] = f"{base} {MANUAL_NOTE}".strip()
            kept += 1
    if kept or carried_disq:
        refresh(new_record)
    return kept


def disqualify_record(
    record: dict[str, Any],
    *,
    score: int,
    reason: str = "",
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> dict[str, Any]:
    """Lock a whole hand-in to a fixed score inside a raw result record.

    This is the disqualification decision without ``Grade`` persistence, so the
    same code can lock a stored grade. It does not refresh the derived fields -
    the caller does that once. The reason is teacher-only and never reaches the
    student projection. Returns the fixed score and the trimmed reason.
    """
    if not isinstance(record, dict):
        raise ExamAdjustmentError("תוצאת הבדיקה אינה קריאה.")
    try:
        fixed = int(score)
    except (TypeError, ValueError) as exc:
        raise ExamAdjustmentError("הציון הקבוע חייב להיות מספר שלם.") from exc
    if fixed < 0 or fixed > 100:
        raise ExamAdjustmentError("הציון הקבוע חייב להיות בין 0 ל-100.")
    trimmed = (reason or "").strip()[:1000]
    record["disqualification"] = {
        "score": fixed,
        "reason": trimmed,
        "by": teacher_id,
        "by_name": teacher_name,
        "at": utcnow().isoformat(timespec="seconds"),
    }
    return {"score": fixed, "reason": trimmed}


def clear_disqualification_record(
    record: dict[str, Any],
    *,
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> None:
    """Remove a whole-paper disqualification inside a raw record, in place."""
    if not is_disqualified(record):
        raise ExamAdjustmentError("להגשה הזו אין פסילה או ציון קבוע.")
    record.pop("disqualification", None)


def disqualify(
    grade: Grade,
    *,
    score: int,
    reason: str = "",
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> dict[str, Any]:
    """Lock a stored grade to a fixed score and shield it from re-checking.

    Setting ``reviewed_at`` makes the selective model check skip this hand-in
    (a human has decided its fate), and the disqualification record keeps the
    score fixed through any later re-mark. Reversible with
    :func:`clear_disqualification`.
    """
    result = _result(grade)
    info = disqualify_record(
        result,
        score=score,
        reason=reason,
        teacher_id=teacher_id,
        teacher_name=teacher_name,
    )
    _log(
        grade,
        f"{_DISQUALIFY_NOTE} ציון קבוע: {info['score']}"
        + (f" · {info['reason']}" if info["reason"] else ""),
    )
    _apply(grade, result, _questions(result))
    grade.reviewed_at = utcnow()
    grade.reviewed_by = teacher_id
    return info


def clear_disqualification(
    grade: Grade,
    *,
    teacher_id: int | None = None,
    teacher_name: str = "",
) -> None:
    """Undo a disqualification: restore the automatic score and re-check eligibility."""
    result = _result(grade)
    clear_disqualification_record(
        result, teacher_id=teacher_id, teacher_name=teacher_name
    )
    _log(grade, _DISQUALIFY_CLEAR_NOTE)
    _apply(grade, result, _questions(result))
    grade.reviewed_at = None
    grade.reviewed_by = None


def write_record(grade: Grade, record: dict[str, Any]) -> None:
    """Persist one finished result record into an ordinary ``Grade`` row."""
    refresh(record)
    grade.score = int(record.get("score") or 0)
    grade.feedback_hebrew = str(record.get("feedback_hebrew") or "")
    grade.result_json = json.dumps(record, ensure_ascii=False)
    grade.deduction_json = json.dumps(
        record.get("deduction_breakdown") or [], ensure_ascii=False
    )
    audit = record.get("audit") or {}
    grade.audit_json = json.dumps(audit, ensure_ascii=False)
    grade.audit_ok = bool(audit.get("ok", True))
    grade.audit_needs_review = bool(audit.get("needs_review", False))
    grade.audit_summary = str(audit.get("summary") or "")[:500]


__all__ = [
    "ExamAdjustmentError",
    "MANUAL_NOTE",
    "adjust",
    "adjust_part",
    "adjust_part_record",
    "adjust_record",
    "carry_over",
    "clear",
    "clear_disqualification",
    "clear_disqualification_record",
    "clear_part",
    "clear_part_record",
    "clear_record",
    "disqualification",
    "disqualification_feedback",
    "disqualify",
    "disqualify_record",
    "is_disqualified",
    "refresh",
    "write_record",
]
