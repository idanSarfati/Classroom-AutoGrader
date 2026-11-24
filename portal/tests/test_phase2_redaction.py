"""Phase 2 security tests: rubric redaction and the student assignment view.

The first half of this file is parametrised over *every* rubric that exists in
``assignments/`` and ``configs/``. That is deliberate: a new rubric file must
inherit the redaction guarantees automatically, without anyone remembering to
add a test.
"""
from __future__ import annotations

import html
import json
import re

import pytest
from sqlalchemy import select

from portal.grading import adapter, redaction
from portal.models import ROLE_TEACHER, Assignment, ClassGroup
from portal.paths import RUBRIC_ROOTS
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"


def _rubric_files():
    files = []
    for root in RUBRIC_ROOTS:
        files.extend(sorted(root.glob("*.json")))
    return files


RUBRIC_FILES = _rubric_files()
RUBRIC_IDS = [f"{path.parent.name}/{path.stem}" for path in RUBRIC_FILES]


def _db(app):
    return app.state.session_factory()


def _all_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _all_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _all_keys(item)


def _page_rubric(redacted: dict) -> dict[str, str]:
    """Flatten visible prose fields for leak assertions (code excluded)."""
    parts: list[str] = [
        str(redacted.get("assignment_title") or ""),
        str(redacted.get("title") or ""),
        str(redacted.get("topic") or ""),
    ]
    for section in redacted.get("sections") or []:
        parts.append(str(section.get("section_name") or ""))
        for question in section.get("questions") or []:
            parts.append(str(question.get("question") or ""))
            parts.extend(str(item) for item in question.get("requirements") or [])
    for task in redacted.get("tasks") or []:
        parts.append(str(task.get("name") or ""))
        parts.append(str(task.get("description") or ""))
    return {"prose": " \n ".join(parts)}


@pytest.mark.parametrize("path", RUBRIC_FILES, ids=RUBRIC_IDS)
def test_redaction_never_emits_sensitive_keys(path) -> None:
    binding = adapter.load_rubric(path)
    redacted = redaction.redact_rubric(binding.rubric)

    assert not (set(_all_keys(redacted)) & redaction.SENSITIVE_KEYS)
    if redacted["schema"] == "spec":
        assert redacted["sections"], path
        assert all(section["questions"] for section in redacted["sections"]), path
    else:
        assert redacted["tasks"], path


@pytest.mark.parametrize("path", RUBRIC_FILES, ids=RUBRIC_IDS)
def test_redaction_masks_answers_embedded_in_visible_prose(path) -> None:
    binding = adapter.load_rubric(path)
    source = binding.rubric.model_dump()
    redacted = redaction.redact_rubric(binding.rubric)
    if "sections" not in source:
        pytest.skip("legacy task rubric has no answer-bearing fields")

    prose = _page_rubric(redacted)["prose"].casefold()
    for section_source, section_view in zip(
        source["sections"], redacted["sections"]
    ):
        for question_source, question_view in zip(
            section_source["questions"], section_view["questions"]
        ):
            for key in redaction.SENSITIVE_KEYS:
                value = question_source.get(key)
                if not isinstance(value, str) or len(value.strip()) < 2:
                    continue
                assert value.casefold() not in prose, (
                    path,
                    question_source.get("id"),
                    key,
                    value[:60],
                )
            # A code block that is byte-for-byte the answer must not be shown.
            if "code" in question_view:
                for key in redaction.SENSITIVE_KEYS:
                    value = question_source.get(key)
                    if not isinstance(value, str) or len(value.strip()) < 2:
                        continue
                    assert (
                        " ".join(question_view["code"].split()).casefold()
                        != " ".join(value.split()).casefold()
                    ), (path, question_source.get("id"), key)


@pytest.mark.parametrize("path", RUBRIC_FILES, ids=RUBRIC_IDS)
def test_redaction_leaves_no_answer_value_in_any_prose(path) -> None:
    """Strongest file-level guarantee: no answer survives in visible prose.

    The per-question test above only inspects the prose of the question that
    owns the answer. This one scans *every* visible prose field - title,
    topic, section names, all questions, requirements and legacy task text -
    so an answer copied into a neighbouring field cannot hide either.

    Whitespace is normalised on both sides because the redactor scrubs the
    collapsed variant as well. ``code`` is excluded on purpose: it is the
    program under discussion, and the test above proves it is never
    byte-for-byte an answer. Single-character multiple-choice labels are also
    excluded - scrubbing every ``"א"`` would destroy ordinary Hebrew prose.
    """
    binding = adapter.load_rubric(path)
    redacted = redaction.redact_rubric(binding.rubric)
    prose = " ".join(_page_rubric(redacted)["prose"].split()).casefold()

    leaked = [
        value
        for value in redaction.collect_sensitive_values(binding.rubric)
        if len(value.strip()) >= 2
        and " ".join(value.split()).casefold() in prose
    ]
    assert not leaked, (path, leaked[:5])


def test_known_embedded_answer_is_scrubbed() -> None:
    binding = adapter.load_rubric("configs/units_1_2_spec.json")
    redacted = redaction.redact_rubric(binding.rubric)
    question = redacted["sections"][0]["questions"][1]

    assert question["id"] == "q2"
    assert "first name" not in question["question"].casefold()
    assert redaction.REDACTION_MARKER in question["question"]
    assert "איזה שם משתנה" in question["question"]
    assert "correct_answer" not in question


def test_legacy_task_rubric_keeps_descriptions() -> None:
    binding = adapter.load_rubric("assignments/lesson_1.json")
    redacted = redaction.redact_rubric(binding.rubric)

    assert redacted["schema"] == "config"
    assert redacted["title"].startswith("שיעור 1")
    assert [task["name"] for task in redacted["tasks"]] == [
        "פקודת הדפסה",
        "פקודת הורד שורה",
        "פקודת טאב",
        "משימה מסכמת",
    ]
    assert "print(" in redacted["tasks"][0]["description"]


def test_redacted_view_has_no_file_path_or_hash() -> None:
    binding = adapter.load_rubric("configs/units_1_2_spec.json")
    redacted = redaction.redact_rubric(binding.rubric)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert binding.sha256 not in serialized
    assert "rubric_path" not in serialized
    assert "rubric_sha256" not in serialized
    assert "units_1_2_spec.json" not in serialized


def _seed_teacher(app) -> int:
    return create_user(
        app, "mori", TEACHER_PASSWORD, role=ROLE_TEACHER, display_name="מורי כהן"
    )


def _create_class(client, name: str = "כיתה ח׳ 2") -> int:
    token = csrf(client, "/teacher")
    response = client.post(
        "/teacher/classes",
        data={"csrf_token": token, "name": name},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])


def _provision_student(client, class_id: int, username: str = "noa") -> None:
    token = csrf(client, f"/teacher/classes/{class_id}")
    response = client.post(
        f"/teacher/classes/{class_id}/students",
        data={
            "csrf_token": token,
            "display_name": "נועה כהן",
            "username": username,
            "password": STUDENT_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _create_assignment(
    app, client, class_id: int, rubric_path: str, *, published: bool = True
) -> int:
    token = csrf(client, f"/teacher/classes/{class_id}")
    data = {
        "csrf_token": token,
        "title": "מטלה עם מחוון",
        "instructions": "קראו את השאלות וענו על כולן.",
        "rubric_path": rubric_path,
        "due_at": "",
    }
    if published:
        data["published"] = "on"
    response = client.post(
        f"/teacher/classes/{class_id}/assignments",
        data=data,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with _db(app) as db:
        assignment = db.scalar(
            select(Assignment)
            .where(Assignment.class_id == class_id)
            .order_by(Assignment.id.desc())
        )
        assert assignment is not None
        return assignment.id


def test_student_assignment_page_renders_redacted_spec(
    app, client, new_client
) -> None:
    _seed_teacher(app)
    login(client, "mori", TEACHER_PASSWORD)
    class_id = _create_class(client)
    _provision_student(client, class_id)
    assignment_id = _create_assignment(
        app, client, class_id, "configs/units_1_2_spec.json"
    )

    with _db(app) as db:
        assignment = db.get(Assignment, assignment_id)
        assert assignment is not None
        sha = assignment.rubric_sha256
        rubric_path = assignment.rubric_path

    binding = adapter.load_rubric("configs/units_1_2_spec.json")
    sensitive = redaction.collect_sensitive_values(binding.rubric)

    login(new_client, "noa", STUDENT_PASSWORD)
    response = new_client.get(f"/student/assignments/{assignment_id}")
    assert response.status_code == 200, response.text
    page = response.text

    # The student does see the visible question text and the redaction notice.
    assert "מה יהיה הפלט של" in page
    assert "מחוון המטלה" in page
    assert "התשובות מוסתרות" in page
    assert "first name" not in page.casefold()

    # No rubric file metadata, no answer field names, no answers - raw or
    # HTML-escaped.
    for forbidden in (
        "correct_answer",
        "corrected_code",
        "expected_output",
        "rubric_sha256",
        rubric_path,
        sha,
    ):
        assert forbidden not in page
    for value in sensitive:
        assert value not in page
        escaped = html.escape(value)
        assert escaped not in page
        assert html.escape(value, quote=False) not in page


def test_student_assignment_page_fails_closed_on_hash_mismatch(
    app, client, new_client
) -> None:
    _seed_teacher(app)
    login(client, "mori", TEACHER_PASSWORD)
    class_id = _create_class(client)
    _provision_student(client, class_id)

    with _db(app) as db:
        group = db.get(ClassGroup, class_id)
        assert group is not None
        assignment = Assignment(
            class_id=group.id,
            title="מחוון שהשתנה",
            instructions="הוראות",
            rubric_path="configs/units_1_2_spec.json",
            rubric_sha256="0" * 64,
            published=True,
        )
        db.add(assignment)
        db.commit()
        assignment_id = assignment.id

    login(new_client, "noa", STUDENT_PASSWORD)
    response = new_client.get(f"/student/assignments/{assignment_id}")
    assert response.status_code == 503
    assert "אינה זמינה כרגע" in response.text
    assert "first name" not in response.text.casefold()
    assert "correct_answer" not in response.text
    assert "units_1_2_spec.json" not in response.text


def test_assignment_page_redacts_every_rubric_file(app, client, new_client) -> None:
    """End-to-end HTML check for each rubric, not just the spec sample."""
    _seed_teacher(app)
    login(client, "mori", TEACHER_PASSWORD)
    class_id = _create_class(client)
    _provision_student(client, class_id)
    login(new_client, "noa", STUDENT_PASSWORD)

    for rubric_path in RUBRIC_FILES:
        relative = rubric_path.relative_to(rubric_path.parents[1]).as_posix()
        assignment_id = _create_assignment(app, client, class_id, relative)
        binding = adapter.load_rubric(rubric_path)
        redacted = redaction.redact_rubric(binding.rubric)
        intentionally_visible = json.dumps(redacted, ensure_ascii=False).casefold()

        response = new_client.get(f"/student/assignments/{assignment_id}")
        assert response.status_code == 200, (relative, response.text)
        page = response.text
        # The per-session CSRF token is random base64, not rubric content: a
        # short expected answer (e.g. "77") can collide with it by chance.
        # Strip it so the leak scan only judges real page content.
        for token in set(re.findall(r'name="csrf_token" value="([^"]+)"', page)):
            page = page.replace(token, "")
        # The asset cache-busting query (?v=<mtime>) is a nonce too: a short
        # numeric answer can land inside its digits by chance, exactly like the
        # CSRF token above, and it is not page content.
        page = re.sub(r"\?v=\d+", "", page)
        for key in redaction.SENSITIVE_KEYS:
            if key == "answer":
                # The generic word also appears in a CSS class name; only a
                # JSON key (quoted) would be a real leak here.
                assert '"answer"' not in page, relative
            else:
                assert key not in page, (relative, key)
        assert binding.sha256 not in page, relative
        assert relative not in page.replace("\\", "/"), relative
        for value in redaction.collect_sensitive_values(binding.rubric):
            if value.casefold() in intentionally_visible:
                # The redacted view deliberately exposes this text as visible
                # code or as another question's prose; redaction tests above
                # prove it is not an answer leaking from its own field.
                continue
            assert value not in page, (relative, value[:40])
            assert html.escape(value, quote=False) not in page, (
                relative,
                value[:40],
            )
