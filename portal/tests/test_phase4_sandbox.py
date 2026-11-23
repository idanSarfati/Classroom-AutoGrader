"""Phase 4 tests: the execution sandbox and the student playground endpoint.

Executor tests launch a real child interpreter (standard library only), so
they exercise the actual audit hook, wall-clock timeout, memory cap and
file-system rules instead of a mock. Timeouts are shortened and output caps
are tiny to keep the suite fast.

Route tests replace the app's executor and rate limiter with fakes, so no
HTTP test ever starts a process or depends on machine speed.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from portal.grading import adapter
from portal.models import (
    ROLE_STUDENT,
    ROLE_TEACHER,
    Assignment,
    ClassGroup,
    Enrollment,
)
from portal.sandbox import executor as executor_module
from portal.sandbox.executor import (
    MAX_CODE_CHARS,
    MAX_STDIN_CHARS,
    ExecutionResult,
    SandboxBusy,
    SandboxError,
    SandboxExecutor,
)
from portal.sandbox.ratelimit import RunLimiter
from portal.sandbox.session import InteractiveSession, SandboxSessionStore
from portal.tests.helpers import create_user, csrf, login

TEACHER_PASSWORD = "TeacherPass123"
STUDENT_PASSWORD = "StudentPass123"


def _db(app):
    return app.state.session_factory()


# ------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def sandbox() -> SandboxExecutor:
    # One second is enough for healthy programs and keeps the timeout test
    # short; the production default is four seconds.
    return SandboxExecutor(timeout_seconds=1.0, max_output_bytes=4096)


@pytest.fixture
def outside_file() -> Path:
    """A file that exists outside the sandbox's working directory."""
    handle, name = tempfile.mkstemp(prefix="portal-sandbox-outside-", suffix=".txt")
    os.close(handle)
    path = Path(name)
    path.write_text("secret", encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _seed(app) -> dict:
    teacher_id = create_user(
        app, "sandbox-mori", TEACHER_PASSWORD, role=ROLE_TEACHER
    )
    student_id = create_user(
        app, "sandbox-noa", STUDENT_PASSWORD, role=ROLE_STUDENT
    )
    binding = adapter.load_rubric("assignments/lesson_1.json")
    with _db(app) as db:
        group = ClassGroup(
            name="כיתת ארגז חול", code="SAND01", teacher_id=teacher_id
        )
        db.add(group)
        db.flush()
        db.add(Enrollment(class_id=group.id, student_id=student_id))
        assignment = Assignment(
            class_id=group.id,
            title="תרגול הרצת קוד",
            instructions="נסו את הקוד שלכם לפני ההגשה.",
            rubric_path=str(binding.path),
            rubric_sha256=binding.sha256,
            published=True,
        )
        db.add(assignment)
        db.commit()
        return {
            "assignment": assignment.id,
            "student": student_id,
            "teacher": teacher_id,
            "class": group.id,
        }


@pytest.fixture
def seeded(app, client):
    data = _seed(app)
    # No grading jobs exist, but stopping the worker keeps the fixture
    # deterministic and ensures a stray job can never reach an external API.
    app.state.grading_worker.stop()
    return data


class FakeExecutor:
    """Records calls and returns a canned result (or raises a canned error)."""

    timeout_seconds = 4.0

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def run(self, code: str, stdin: str = "") -> ExecutionResult:
        self.calls.append((code, stdin))
        if self.error is not None:
            raise self.error
        return self.result or ExecutionResult(
            stdout="ok\n",
            stderr="",
            exit_code=0,
            execution_time_ms=11,
            timed_out=False,
        )


def _login_student(client, username: str = "sandbox-noa") -> None:
    response = login(client, username, STUDENT_PASSWORD)
    assert response.status_code == 303, response.text


def _login_teacher(client, username: str = "sandbox-mori") -> None:
    response = login(client, username, TEACHER_PASSWORD)
    assert response.status_code == 303, response.text


def _run(
    client,
    token: str,
    *,
    code: str = "print(1)",
    stdin: str = "",
    follow_redirects: bool = True,
):
    return client.post(
        "/student/sandbox/run",
        data={"csrf_token": token, "code": code, "stdin": stdin},
        headers={"Accept": "application/json"},
        follow_redirects=follow_redirects,
    )


# ------------------------------------------------------------ executor: happy
#                                                            path and limits


def test_runs_code_and_returns_stdout(sandbox: SandboxExecutor) -> None:
    result = sandbox.run("print('hello from the sandbox')")

    assert result.exit_code == 0
    assert result.stdout == "hello from the sandbox\n"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.truncated is False
    assert isinstance(result.execution_time_ms, int)
    assert 0 <= result.execution_time_ms < 5000


def test_passes_stdin_to_input(sandbox: SandboxExecutor) -> None:
    result = sandbox.run("name = input()\nprint('hi', name)", stdin="דנה\n")

    assert result.exit_code == 0
    assert result.stdout == "hi דנה\n"


def test_keeps_stdout_and_stderr_separate(sandbox: SandboxExecutor) -> None:
    code = "import sys\nprint('to stdout')\nprint('to stderr', file=sys.stderr)"
    result = sandbox.run(code)

    assert result.exit_code == 0
    assert result.stdout == "to stdout\n"
    assert result.stderr == "to stderr\n"


def test_result_serialises_to_json_ready_dict(sandbox: SandboxExecutor) -> None:
    payload = sandbox.run("print('x')").to_dict()

    assert set(payload) == {
        "stdout",
        "stderr",
        "exit_code",
        "execution_time_ms",
        "timed_out",
        "truncated",
    }


def test_output_is_capped_and_flagged() -> None:
    executor = SandboxExecutor(timeout_seconds=2.0, max_output_bytes=200)

    result = executor.run("print('x' * 5000)")

    assert result.exit_code == 0
    assert result.truncated is True
    assert result.stdout.count("x") <= 200


def test_working_directory_is_removed_after_the_run(
    sandbox: SandboxExecutor,
) -> None:
    temp_root = Path(tempfile.gettempdir())
    before = set(temp_root.glob("portal-sandbox-*"))

    result = sandbox.run("open('notes.txt', 'w').write('hi')\nprint('done')")

    after = set(temp_root.glob("portal-sandbox-*"))
    assert result.exit_code == 0
    assert result.stdout == "done\n"
    assert after - before == set()


def test_write_inside_working_directory_is_allowed(
    sandbox: SandboxExecutor,
) -> None:
    code = (
        "with open('notes.txt', 'w', encoding='utf-8') as handle:\n"
        "    handle.write('שלום')\n"
        "print(open('notes.txt', encoding='utf-8').read())\n"
    )
    result = sandbox.run(code)

    assert result.exit_code == 0
    assert result.stdout == "שלום\n"


# ------------------------------------------------------ executor: error paths


def test_syntax_error_is_reported_without_runner_internals(
    sandbox: SandboxExecutor,
) -> None:
    result = sandbox.run("def broken(:\n    pass\n")

    assert result.exit_code == 1
    assert result.timed_out is False
    assert "SyntaxError" in result.stderr
    assert "student_code.py" in result.stderr
    assert "_portal_runner.py" not in result.stderr


def test_runtime_error_shows_only_student_frames(
    sandbox: SandboxExecutor,
) -> None:
    result = sandbox.run("raise ValueError('boom')\n")

    assert result.exit_code == 1
    assert "ValueError: boom" in result.stderr
    assert "student_code.py" in result.stderr
    assert "_portal_runner.py" not in result.stderr


def test_infinite_loop_hits_the_timeout(sandbox: SandboxExecutor) -> None:
    result = sandbox.run("while True:\n    pass\n")

    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.execution_time_ms < 5000
    assert result.stdout == ""


def test_input_without_stdin_fails_cleanly(sandbox: SandboxExecutor) -> None:
    result = sandbox.run("input('number: ')\n")

    assert result.exit_code == 1
    assert "EOFError" in result.stderr


def test_memory_limit_turns_growth_into_memory_error() -> None:
    executor = SandboxExecutor(timeout_seconds=3.0, memory_mb=64)

    result = executor.run("buffer = bytearray(200 * 1024 * 1024)\n")

    assert result.timed_out is False
    assert result.exit_code != 0
    assert "MemoryError" in result.stderr


# --------------------------------------------------- executor: malicious code


def test_network_imports_are_blocked(sandbox: SandboxExecutor) -> None:
    for snippet in (
        "import socket\n",
        "__import__('socket')\n",
        "import urllib.request\n",
    ):
        result = sandbox.run(snippet)
        assert result.exit_code == 1, snippet
        assert "PermissionError" in result.stderr, snippet
        assert "_portal_runner.py" not in result.stderr, snippet


def test_reading_a_file_outside_the_sandbox_is_blocked(
    sandbox: SandboxExecutor, outside_file: Path
) -> None:
    result = sandbox.run(f"open({str(outside_file)!r}).read()\n")

    assert result.exit_code == 1
    assert "PermissionError" in result.stderr
    assert "secret" not in result.stdout


def test_os_open_for_writing_outside_is_blocked(
    sandbox: SandboxExecutor, outside_file: Path
) -> None:
    code = f"import os\nos.open({str(outside_file)!r}, os.O_WRONLY)\n"
    result = sandbox.run(code)

    assert result.exit_code == 1
    assert "PermissionError" in result.stderr


def test_subprocess_and_native_code_are_blocked(
    sandbox: SandboxExecutor,
) -> None:
    for snippet in (
        "import subprocess\n",
        "import ctypes\n",
        "import multiprocessing\n",
        "import os\nos.system('echo hi')\n",
    ):
        result = sandbox.run(snippet)
        assert result.exit_code == 1, snippet
        assert "PermissionError" in result.stderr, snippet


def test_reflection_shortcuts_to_the_runner_are_blocked(
    sandbox: SandboxExecutor,
) -> None:
    for snippet in (
        "import sys\nsys._getframe(0)\n",
        "import gc\n",
        "import sys\nsys.settrace(lambda *args: None)\n",
    ):
        result = sandbox.run(snippet)
        assert result.exit_code == 1, snippet
        assert "PermissionError" in result.stderr, snippet

    # The concrete escape attempt must not reach the file system either.
    exploit = (
        "import sys, os\n"
        "g = sys._getframe(0).f_back.f_globals\n"
        "g['_READ_ROOTS'] = (os.path.realpath('C:/'),)\n"
        "print('READ OK', len(open('C:/Windows/win.ini').read()))\n"
    )
    result = sandbox.run(exploit)
    assert result.exit_code == 1
    assert "READ OK" not in result.stdout


def test_environment_is_stripped(sandbox: SandboxExecutor) -> None:
    result = sandbox.run("import os\nprint('\\n'.join(sorted(os.environ)))\n")

    assert result.exit_code == 0
    names = set(result.stdout.split())
    assert "USERPROFILE" not in names
    assert "APPDATA" not in names
    assert "HOME" not in names
    if os.name == "nt":
        # PATH is deliberately absent; Windows still needs its system root.
        assert "PATH" not in names
        assert "SYSTEMROOT" in names


def test_venv_redirector_is_avoided_on_windows(
    sandbox: SandboxExecutor,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only virtualenv behaviour")
    base = getattr(sys, "_base_executable", None)
    if not base or base == sys.executable:
        pytest.skip("not running inside a virtualenv")

    assert sandbox.python == base
    assert sandbox.run("print('ok')").stdout == "ok\n"


# --------------------------------------------------------------- rate limiter


def test_run_limiter_sliding_window() -> None:
    now = [1000.0]
    limiter = RunLimiter(max_runs=2, window_seconds=10.0, clock=lambda: now[0])

    assert limiter.allow("student:1") == (True, 0.0)
    now[0] += 1.0
    assert limiter.allow("student:1") == (True, 0.0)

    allowed, retry_after = limiter.allow("student:1")
    assert allowed is False
    assert 0 < retry_after <= 10.0

    # A different student has their own window.
    assert limiter.allow("student:2") == (True, 0.0)

    # The oldest run falls out of the window.
    now[0] += 9.5
    assert limiter.allow("student:1")[0] is True


# ------------------------------------------------------------- HTTP endpoint


def test_playground_is_visible_on_the_assignment_page(
    client, seeded
) -> None:
    _login_student(client)
    page = client.get(f"/student/assignments/{seeded['assignment']}")

    assert page.status_code == 200
    assert 'data-sandbox-url="/student/sandbox/run"' in page.text
    assert 'data-sandbox-sessions-url="/student/sandbox/sessions"' in page.text
    assert "data-run-code" in page.text
    assert "Run code" in page.text
    assert "data-sandbox-output" in page.text
    # input() is answered from the console line while the program runs.
    assert "data-console-line" in page.text
    # There is no separate stdin panel any more.
    assert "data-sandbox-stdin" not in page.text


def test_run_endpoint_returns_structured_output(app, client, seeded) -> None:
    _login_student(client)
    fake = FakeExecutor(
        result=ExecutionResult(
            stdout="42\n",
            stderr="",
            exit_code=0,
            execution_time_ms=15,
            timed_out=False,
        )
    )
    app.state.sandbox_executor = fake
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    response = _run(client, token, code="print(6 * 7)", stdin="ignored")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stdout"] == "42\n"
    assert payload["exit_code"] == 0
    assert payload["ok"] is True
    assert payload["timed_out"] is False
    assert payload["timeout_seconds"] == 4.0
    assert fake.calls == [("print(6 * 7)", "ignored")]


def test_run_endpoint_passes_stdin_to_the_executor(app, client, seeded) -> None:
    _login_student(client)
    fake = FakeExecutor()
    app.state.sandbox_executor = fake
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    response = _run(client, token, code="name = input()", stdin="דנה")

    assert response.status_code == 200
    assert fake.calls == [("name = input()", "דנה")]


def test_run_endpoint_requires_csrf(client, seeded) -> None:
    _login_student(client)

    response = client.post(
        "/student/sandbox/run",
        data={"code": "print(1)"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert "error" in response.json()


def test_run_endpoint_rejects_empty_code(client, seeded) -> None:
    _login_student(client)
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    response = _run(client, token, code="   \n")

    assert response.status_code == 400
    assert "error" in response.json()


def test_run_endpoint_rejects_oversized_code(client, seeded) -> None:
    _login_student(client)
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    response = _run(client, token, code="x" * (MAX_CODE_CHARS + 1))

    assert response.status_code == 413


def test_run_endpoint_rejects_oversized_stdin(client, seeded) -> None:
    _login_student(client)
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    response = _run(client, token, stdin="x" * (MAX_STDIN_CHARS + 1))

    assert response.status_code == 413


def test_run_endpoint_rate_limits_per_student(app, client, seeded) -> None:
    _login_student(client)
    fake = FakeExecutor()
    app.state.sandbox_executor = fake
    app.state.sandbox_limiter = RunLimiter(max_runs=2, window_seconds=60.0)
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    assert _run(client, token).status_code == 200
    assert _run(client, token).status_code == 200

    blocked = _run(client, token)

    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"]
    assert blocked.json()["retry_after"] >= 1
    assert len(fake.calls) == 2


def test_run_endpoint_reports_a_busy_sandbox(app, client, seeded) -> None:
    _login_student(client)
    app.state.sandbox_executor = FakeExecutor(error=SandboxBusy("busy"))
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    response = _run(client, token)

    assert response.status_code == 429
    assert "error" in response.json()


def test_run_endpoint_reports_a_start_failure(app, client, seeded) -> None:
    _login_student(client)
    app.state.sandbox_executor = FakeExecutor(error=SandboxError("broken"))
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    response = _run(client, token)

    assert response.status_code == 500
    assert "error" in response.json()


def test_run_endpoint_is_student_only(app, client, seeded) -> None:
    _login_teacher(client)
    token = csrf(client, "/teacher")

    response = _run(client, token)

    assert response.status_code == 403


def test_run_endpoint_redirects_anonymous_visitors(client, seeded) -> None:
    response = client.post(
        "/student/sandbox/run",
        data={"code": "print(1)"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"].startswith("/login")


def test_executor_busy_raises_instead_of_queueing(monkeypatch) -> None:
    monkeypatch.setattr(executor_module, "_ACQUIRE_TIMEOUT_SECONDS", 0.05)
    executor = SandboxExecutor(timeout_seconds=1.0)
    assert executor_module._SLOTS.acquire(blocking=False)
    assert executor_module._SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(SandboxBusy):
            executor.run("print(1)")
    finally:
        executor_module._SLOTS.release()
        executor_module._SLOTS.release()


# ------------------------------------------------ interactive sessions (input)

PROMPT_CODE = "name = input('enter: ')\nprint('hello', name)\n"


def _session_poll_until(session: InteractiveSession, predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    payload = session.poll(0, 0)
    while not predicate(payload) and time.monotonic() < deadline:
        time.sleep(0.02)
        payload = session.poll(0, 0)
    return payload


def test_session_shows_the_prompt_then_continues_after_input(
    sandbox: SandboxExecutor,
) -> None:
    session = InteractiveSession(sandbox, PROMPT_CODE, owner_id=7)
    try:
        # The prompt must reach the browser while input() is still waiting.
        waiting = _session_poll_until(
            session, lambda payload: "enter:" in payload["stdout"]
        )
        assert "enter:" in waiting["stdout"]
        assert waiting["done"] is False

        session.send_input("Dana")
        finished = _session_poll_until(session, lambda payload: payload["done"])

        assert finished["done"] is True
        assert finished["exit_code"] == 0
        assert finished["timed_out"] is False
        assert finished["stdout"] == "enter: hello Dana\n"
        assert finished["stderr"] == ""
    finally:
        session.stop()


def test_session_polls_return_only_new_output(sandbox: SandboxExecutor) -> None:
    session = InteractiveSession(
        sandbox, "print('one')\nprint('two')\n", owner_id=7
    )
    try:
        finished = _session_poll_until(session, lambda payload: payload["done"])
        full = finished["stdout"]

        assert full == "one\ntwo\n"
        assert finished["stdout_offset"] == len(full)
        assert finished["stderr_offset"] == 0

        again = session.poll(finished["stdout_offset"], finished["stderr_offset"])
        assert again["stdout"] == ""
        assert again["stderr"] == ""
        assert again["stdout_offset"] == finished["stdout_offset"]
        assert again["done"] is True
    finally:
        session.stop()


def test_reported_duration_excludes_time_waiting_for_input(
    sandbox: SandboxExecutor,
) -> None:
    session = InteractiveSession(
        sandbox, "x = input('go: ')\nprint('ok')\n", owner_id=7
    )
    try:
        _session_poll_until(session, lambda payload: "go:" in payload["stdout"])
        time.sleep(0.6)
        session.send_input("x")
        finished = _session_poll_until(session, lambda payload: payload["done"])

        assert finished["exit_code"] == 0
        # Roughly 0.6s was spent typing; it must not inflate the runtime.
        assert finished["execution_time_ms"] < 600
    finally:
        session.stop()


def test_session_refuses_input_after_the_program_ends(
    sandbox: SandboxExecutor,
) -> None:
    session = InteractiveSession(sandbox, "print('done')\n", owner_id=7)
    try:
        _session_poll_until(session, lambda payload: payload["done"])

        with pytest.raises(SandboxError):
            session.send_input("late")
    finally:
        session.stop()


def test_session_ends_after_inactivity(sandbox: SandboxExecutor) -> None:
    session = InteractiveSession(
        sandbox,
        "input('waiting: ')\n",
        owner_id=7,
        wall_seconds=10.0,
        idle_seconds=0.4,
    )
    try:
        finished = _session_poll_until(session, lambda payload: payload["done"])

        assert finished["timed_out"] is True
        assert finished["exit_code"] == 124
        assert "waiting:" in finished["stdout"]
    finally:
        session.stop()


def test_session_store_enforces_ownership_and_releases_on_stop(
    sandbox: SandboxExecutor,
) -> None:
    store = SandboxSessionStore(wall_seconds=10.0, idle_seconds=10.0)
    session = store.start(sandbox, "input()\n", owner_id=3)
    try:
        assert store.get(session.id, 3) is session
        assert store.get(session.id, 4) is None
        assert store.stop(session.id, 4) is False
        assert session.done is False

        assert store.stop(session.id, 3) is True
        assert session.done is True
        assert store.get(session.id, 3).done is True
        assert store.get(session.id, 4) is None
    finally:
        store.close_all()


def _start_session(client, token: str, code: str = PROMPT_CODE):
    return client.post(
        "/student/sandbox/sessions",
        data={"csrf_token": token, "code": code},
        headers={"Accept": "application/json"},
    )


def _poll_session(client, session_id: str, stdout_since: int = 0):
    return client.get(
        f"/student/sandbox/sessions/{session_id}"
        f"?stdout_since={stdout_since}&stderr_since=0",
        headers={"Accept": "application/json"},
    )


def test_session_endpoints_drive_a_live_input_run(app, client, seeded) -> None:
    _login_student(client)
    app.state.sandbox_executor = SandboxExecutor(timeout_seconds=2.0)
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    started = _start_session(client, token)
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]

    deadline = time.monotonic() + 8.0
    stdout = ""
    while time.monotonic() < deadline and "enter:" not in stdout:
        time.sleep(0.02)
        payload = _poll_session(client, session_id).json()
        stdout = payload["stdout"]
    assert "enter:" in stdout

    sent = client.post(
        f"/student/sandbox/sessions/{session_id}/input",
        data={"csrf_token": token, "line": "Dana"},
        headers={"Accept": "application/json"},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"ok": True}

    deadline = time.monotonic() + 8.0
    payload = _poll_session(client, session_id).json()
    while time.monotonic() < deadline and not payload["done"]:
        time.sleep(0.02)
        payload = _poll_session(client, session_id).json()

    assert payload["done"] is True
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "enter: hello Dana\n"


def test_session_endpoints_require_csrf(client, seeded) -> None:
    _login_student(client)

    response = client.post(
        "/student/sandbox/sessions",
        data={"code": "print(1)"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert "error" in response.json()


def test_session_endpoints_are_student_only(client, seeded) -> None:
    _login_teacher(client)
    token = csrf(client, "/teacher")

    response = _start_session(client, token)

    assert response.status_code == 403


def test_session_endpoint_rate_limits_starts(app, client, seeded) -> None:
    _login_student(client)
    app.state.sandbox_executor = SandboxExecutor(timeout_seconds=2.0)
    app.state.sandbox_limiter = RunLimiter(max_runs=1, window_seconds=60.0)
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    first = _start_session(client, token, code="print(1)\n")
    assert first.status_code == 200, first.text
    second = _start_session(client, token, code="print(2)\n")

    assert second.status_code == 429
    assert second.headers["Retry-After"]
    assert second.json()["retry_after"] >= 1


def test_session_poll_is_bound_to_its_owner(app, client, seeded, new_client) -> None:
    _login_student(client)
    app.state.sandbox_executor = SandboxExecutor(timeout_seconds=2.0)
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    started = _start_session(client, token, code="input()\n")
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]

    create_user(
        app, "sandbox-other", STUDENT_PASSWORD, role=ROLE_STUDENT
    )
    response = login(new_client, "sandbox-other", STUDENT_PASSWORD)
    assert response.status_code == 303, response.text
    other_token = csrf(new_client, "/student")

    assert _poll_session(new_client, session_id).status_code == 404
    assert new_client.post(
        f"/student/sandbox/sessions/{session_id}/input",
        data={"csrf_token": other_token, "line": "x"},
        headers={"Accept": "application/json"},
    ).status_code == 404
    assert client.post(
        f"/student/sandbox/sessions/{session_id}/stop",
        data={"csrf_token": token},
        headers={"Accept": "application/json"},
    ).status_code == 200


def test_session_stop_endpoint_ends_the_program(app, client, seeded) -> None:
    _login_student(client)
    app.state.sandbox_executor = SandboxExecutor(timeout_seconds=2.0)
    token = csrf(client, f"/student/assignments/{seeded['assignment']}")

    started = _start_session(client, token, code="input()\n")
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]

    stopped = client.post(
        f"/student/sandbox/sessions/{session_id}/stop",
        data={"csrf_token": token},
        headers={"Accept": "application/json"},
    )

    assert stopped.status_code == 200
    payload = _poll_session(client, session_id).json()
    assert payload["done"] is True
    assert payload["timed_out"] is False
