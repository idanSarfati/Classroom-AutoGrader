"""Interactive sandbox sessions for the student IDE console.

The one-shot executor feeds all stdin at once, so ``input()`` with no stdin
raises ``EOFError``. A session keeps the same isolated runner alive between
HTTP polls instead: its output is streamed in order, and each submitted line
is written to the child's stdin, so the prompt appears before the student
answers and the program continues afterwards.

The isolation is unchanged - the same runner script, audit hook, Job Object
(Windows) and stdlib-only interpreter as :mod:`portal.sandbox.executor`. Only
the transport differs: pipes are drained by background threads and the
session is bounded by a wall-clock and an inactivity timeout.
"""
from __future__ import annotations

import codecs
import os
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from portal.sandbox.executor import (
    MAX_CODE_CHARS,
    MAX_STDIN_CHARS,
    _ACQUIRE_TIMEOUT_SECONDS,
    _RUNNER_SOURCE,
    _SLOTS,
    _WindowsJob,
    _child_env,
    SandboxBusy,
    SandboxError,
    SandboxExecutor,
)

INTERACTIVE_WALL_SECONDS = 120.0
INTERACTIVE_IDLE_SECONDS = 60.0
DONE_RETENTION_SECONDS = 30.0
MAX_INPUT_LINE_CHARS = 2_000


class InteractiveSession:
    """One live sandbox run with an open stdin pipe."""

    def __init__(
        self,
        executor: SandboxExecutor,
        code: str,
        *,
        owner_id: int,
        wall_seconds: float = INTERACTIVE_WALL_SECONDS,
        idle_seconds: float = INTERACTIVE_IDLE_SECONDS,
    ) -> None:
        if not isinstance(code, str):
            raise SandboxError("code must be a string")
        if len(code) > MAX_CODE_CHARS:
            raise SandboxError("code is too long")
        if not _SLOTS.acquire(timeout=_ACQUIRE_TIMEOUT_SECONDS):
            raise SandboxBusy("the sandbox is busy")

        self.id = secrets.token_urlsafe(18)
        self.owner_id = int(owner_id)
        self._executor = executor
        self._wall_seconds = max(1.0, float(wall_seconds))
        self._idle_seconds = max(1.0, float(idle_seconds))
        self._lock = threading.Lock()
        self._slot = True
        self._tempdir: tempfile.TemporaryDirectory | None = None
        self._job: _WindowsJob | None = None
        self._threads: list[threading.Thread] = []
        self._watcher: threading.Thread | None = None
        self.process: subprocess.Popen | None = None

        self.stdout = ""
        self.stderr = ""
        self.stdin_chars = 0
        self.done = False
        self.exit_code: int | None = None
        self.timed_out = False
        self.truncated = False
        self.execution_time_ms: int | None = None
        self.done_at: float | None = None
        self.started = time.monotonic()
        self.last_activity = self.started
        # Time the program spent blocked between a prompt and the student's
        # answer. It is not execution time, so the reported duration excludes
        # it; otherwise a slow typist makes every run look slow.
        self._last_output_at = self.started
        self._input_wait_ms = 0
        self._released = False

        try:
            self._spawn(code)
        except BaseException:
            self._cleanup()
            raise

    # ------------------------------------------------------------- lifecycle

    def _spawn(self, code: str) -> None:
        self._tempdir = tempfile.TemporaryDirectory(
            prefix="portal-sandbox-", ignore_cleanup_errors=True
        )
        work = Path(self._tempdir.name)
        (work / "_portal_runner.py").write_text(_RUNNER_SOURCE, encoding="utf-8")
        (work / "student_code.py").write_text(code, encoding="utf-8")

        command = [
            self._executor.python,
            "-u",
            "-X",
            "utf8",
            "-I",
            "-S",
            "_portal_runner.py",
            str(self._executor.timeout_seconds),
            str(self._executor.memory_mb),
            str(self._executor.max_output_bytes),
        ]
        kwargs: dict = {
            "cwd": self._tempdir.name,
            "env": _child_env(self._tempdir.name),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True

        try:
            self.process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise SandboxError(f"could not start the sandbox: {exc}") from exc

        if os.name == "nt":
            self._job = _WindowsJob(
                self._executor.memory_mb * 1024 * 1024,
                self._executor.timeout_seconds,
            )
            self._job.assign(self.process)

        limit = self._executor.max_output_bytes
        for name, pipe in (
            ("stdout", self.process.stdout),
            ("stderr", self.process.stderr),
        ):
            if pipe is None:
                continue
            thread = threading.Thread(
                target=self._pump,
                args=(name, pipe, limit),
                name=f"portal-sandbox-{name}-{self.id[:6]}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

        self._watcher = threading.Thread(
            target=self._watch,
            name=f"portal-sandbox-watch-{self.id[:6]}",
            daemon=True,
        )
        self._watcher.start()

    def _pump(self, name: str, pipe, limit: int) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # Windows text-mode stdout writes CRLF into the pipe. The console
        # renders LF, so translate here and keep a trailing CR for the next
        # chunk instead of leaving a stray carriage return mid-line.
        pending_cr = False

        def emit(text: str, *, final: bool = False) -> None:
            nonlocal pending_cr
            if pending_cr:
                text = "\r" + text
                pending_cr = False
            if not final and text.endswith("\r"):
                text = text[:-1]
                pending_cr = True
            text = text.replace("\r\n", "\n")
            if text:
                self._append(name, text, limit)

        try:
            while True:
                chunk = os.read(pipe.fileno(), 4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    emit(text)
            emit(decoder.decode(b"", final=True), final=True)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _append(self, name: str, text: str, limit: int) -> None:
        if not text:
            return
        with self._lock:
            current = getattr(self, name)
            room = limit - len(current)
            if room <= 0:
                self.truncated = True
                return
            if len(text) > room:
                text = text[:room]
                self.truncated = True
            setattr(self, name, current + text)
            now = time.monotonic()
            self.last_activity = now
            self._last_output_at = now

    def _watch(self) -> None:
        while True:
            if self.process.poll() is not None:
                break
            now = time.monotonic()
            if (
                now - self.started > self._wall_seconds
                or now - self.last_activity > self._idle_seconds
            ):
                self.timed_out = True
                self._kill()
                break
            time.sleep(0.1)
        self._finalize()

    def _finalize(self) -> None:
        if self.process is None:
            return
        with self._lock:
            if self.done:
                return
        if self.process.poll() is None:
            self._kill()
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        for thread in self._threads:
            thread.join(timeout=2.0)
        with self._lock:
            if self.done:
                return
            self.done = True
            self.done_at = time.monotonic()
            return_code = self.process.returncode
            self.exit_code = 124 if self.timed_out else int(return_code or 0)
            elapsed_ms = int((self.done_at - self.started) * 1000)
            self.execution_time_ms = max(0, elapsed_ms - self._input_wait_ms)
        self._cleanup()

    def _kill(self) -> None:
        if self.process is None:
            return
        if self._job is not None:
            self._job.terminate()
            return
        if os.name == "nt":
            try:
                self.process.kill()
            except OSError:
                pass
            return
        try:
            os.killpg(os.getpgid(self.process.pid), 9)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.process.kill()
            except OSError:
                pass

    def _cleanup(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            job = self._job
            self._job = None
            tempdir = self._tempdir
            self._tempdir = None
        try:
            if self.process is not None and self.process.stdin is not None:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass
        if job is not None:
            job.close()
        if tempdir is not None:
            tempdir.cleanup()
        if self._slot:
            self._slot = False
            _SLOTS.release()

    # ---------------------------------------------------------------- public

    def poll(self, stdout_since: int, stderr_since: int) -> dict:
        with self._lock:
            out = self.stdout[max(0, stdout_since) :]
            err = self.stderr[max(0, stderr_since) :]
            return {
                "stdout": out,
                "stderr": err,
                "stdout_offset": len(self.stdout),
                "stderr_offset": len(self.stderr),
                "done": self.done,
                "exit_code": self.exit_code,
                "timed_out": self.timed_out,
                "truncated": self.truncated,
                "execution_time_ms": self.execution_time_ms,
            }

    def send_input(self, line: str) -> None:
        line = (line or "").replace("\r", "").split("\n")[0]
        line = line[:MAX_INPUT_LINE_CHARS]
        with self._lock:
            if self.done:
                raise SandboxError("the session has ended")
            if self.stdin_chars + len(line) + 1 > MAX_STDIN_CHARS:
                raise ValueError("input is too long")
            now = time.monotonic()
            self.stdin_chars += len(line) + 1
            self.last_activity = now
            # The program has been blocked since its last output; treat that
            # gap as waiting for the student, not as runtime.
            self._input_wait_ms += int((now - self._last_output_at) * 1000)
            self._last_output_at = now
        if self.process is None or self.process.stdin is None:
            raise SandboxError("the session has ended")
        try:
            self.process.stdin.write((line + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise SandboxError("the session has ended") from exc

    def stop(self) -> None:
        with self._lock:
            if self.done:
                return
        self._kill()
        self._finalize()


class SandboxSessionStore:
    """The sessions of one app process, pruned after they finish."""

    def __init__(
        self,
        *,
        wall_seconds: float = INTERACTIVE_WALL_SECONDS,
        idle_seconds: float = INTERACTIVE_IDLE_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, InteractiveSession] = {}
        self._wall_seconds = wall_seconds
        self._idle_seconds = idle_seconds

    def start(
        self, executor: SandboxExecutor, code: str, *, owner_id: int
    ) -> InteractiveSession:
        self._prune()
        session = InteractiveSession(
            executor,
            code,
            owner_id=owner_id,
            wall_seconds=self._wall_seconds,
            idle_seconds=self._idle_seconds,
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(
        self, session_id: str, owner_id: int
    ) -> InteractiveSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.owner_id != int(owner_id):
            return None
        return session

    def stop(self, session_id: str, owner_id: int) -> bool:
        session = self.get(session_id, owner_id)
        if session is None:
            return False
        session.stop()
        return True

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                session.stop()
            except Exception:  # never block shutdown on one session
                pass

    def _prune(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [
                key
                for key, session in self._sessions.items()
                if session.done_at is not None
                and now - session.done_at > DONE_RETENTION_SECONDS
            ]
            for key in expired:
                self._sessions.pop(key, None)
