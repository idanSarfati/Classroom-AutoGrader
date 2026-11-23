"""Phase 4: a bounded subprocess runner for student playground code.

The sandbox is defence in depth, not a hardened container. It combines:

* a fresh interpreter per run (``python -I -S``) in a throwaway directory;
* wall-clock and CPU limits, a memory cap (Job Object on Windows,
  ``RLIMIT_AS`` on POSIX) and a cap on output size;
* a stripped environment and a random working directory that is deleted
  afterwards;
* an audit hook that blocks network, process creation, native-code loading
  and every file operation outside the working directory or the Python
  standard library.

A school deployment should still run this process under a dedicated OS
account/container if untrusted users can reach the network directly. The
Python audit hook shares the interpreter with student code, so reflection can
reach its state; it stops mistakes and casual attempts, not a determined
attacker. The module is deliberately conservative about what student code
may do.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 4.0
DEFAULT_MEMORY_MB = 256
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
MAX_CODE_CHARS = 200_000
MAX_STDIN_CHARS = 10_000
MAX_CONCURRENCY = 2

_ACQUIRE_TIMEOUT_SECONDS = 2.0
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENCY)


class SandboxBusy(RuntimeError):
    """Raised when too many runs are already using the executor."""


class SandboxError(RuntimeError):
    """Raised when the sandbox could not start at all."""


@dataclass(frozen=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: int
    timed_out: bool
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# The runner is a separate, self-contained script: it never imports ``portal``
# and installs the audit hook before the student's code is compiled.
_RUNNER_SOURCE = r'''"""Isolated runner used by portal.sandbox.executor."""
import builtins
import os
import sys
import traceback

TIMEOUT = float(sys.argv[1])
MEMORY_MB = int(sys.argv[2])
MAX_OUTPUT = int(sys.argv[3])
CWD = os.path.realpath(os.getcwd())


def _real(path):
    try:
        value = os.fsdecode(path)
    except (TypeError, ValueError):
        return None
    if not os.path.isabs(value):
        value = os.path.join(CWD, value)
    return os.path.realpath(value)


def _within(path, root):
    if path is None:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except (ValueError, OSError):
        return False


_STDLIB_ROOTS = []
for _candidate in (getattr(sys, "base_prefix", ""), getattr(sys, "prefix", "")):
    if _candidate:
        _STDLIB_ROOTS.append(os.path.realpath(_candidate))
# Allow the interpreter's own Lib directory even if prefix is unusual.
_LIB = os.path.dirname(os.path.realpath(os.__file__))
_STDLIB_ROOTS.append(_LIB)
_STDLIB_ROOTS = tuple(dict.fromkeys(_STDLIB_ROOTS))
_READ_ROOTS = _STDLIB_ROOTS + (CWD,)


def _read_allowed(path):
    if path is None:
        return True
    return any(_within(path, root) for root in _READ_ROOTS)


def _write_allowed(path):
    return path is not None and _within(path, CWD)


_BLOCKED_IMPORTS = frozenset(
    {
        "ctypes",
        "_ctypes",
        "subprocess",
        "_posixsubprocess",
        "multiprocessing",
        "_multiprocessing",
        "_threading_local",
        "socket",
        "_socket",
        "ssl",
        "_ssl",
        "http",
        "urllib",
        "ftplib",
        "telnetlib",
        "smtplib",
        "poplib",
        "imaplib",
        "xmlrpc",
        "requests",
        "winreg",
        "_winreg",
        "msvcrt",
        "gc",
    }
)
_NETWORK_EVENTS = frozenset(
    {
        "socket.__new__",
        "socket.bind",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
        "socket.sendto",
        "socket.sethostname",
    }
)
_PROCESS_EVENTS = frozenset(
    {
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        "pty.spawn",
        "subprocess.Popen",
    }
)
_NATIVE_EVENTS = frozenset(
    {
        "ctypes.dlopen",
        "ctypes.dlsym",
        "ctypes.dlsym/handle",
        "ctypes.dlopen_handle",
        "ctypes.call_function",
    }
)
_WRITE_EVENTS = frozenset(
    {
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.mkdir",
        "os.chmod",
        "os.chown",
        "os.truncate",
        "os.utime",
        "os.link",
        "os.symlink",
    }
)
_READ_EVENTS = frozenset({"os.listdir", "os.scandir"})
_INTROSPECTION_EVENTS = frozenset(
    {
        "gc.get_objects",
        "gc.get_referrers",
        "gc.get_referents",
        "sys.settrace",
        "sys.setprofile",
    }
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_DENIED = PermissionError(
    "הגישה לפעולה הזו חסומה בסביבת ההרצה (קבצים, רשת ותהליכים אסורים)."
)


def _check_write_arguments(args):
    for value in args:
        if isinstance(value, (str, bytes, os.PathLike)):
            if not _write_allowed(_real(value)):
                raise _DENIED


def _audit(event, args):
    if event == "import":
        name = str(args[0] or "")
        if name.split(".")[0] in _BLOCKED_IMPORTS:
            raise _DENIED
        return
    if event == "sys._getframe":
        # Walking out of the student frame would expose the runner's globals,
        # where the allowed roots live. Deny any frame chain that reaches it.
        frame = args[0] if args else None
        while frame is not None:
            if getattr(frame, "f_globals", None) is globals():
                raise _DENIED
            frame = getattr(frame, "f_back", None)
        return
    if event in _INTROSPECTION_EVENTS:
        raise _DENIED
    if event in _NETWORK_EVENTS or event in _PROCESS_EVENTS or event in _NATIVE_EVENTS:
        raise _DENIED
    if event == "open":
        path = args[0]
        if isinstance(path, int):
            return
        target = _real(path)
        mode = args[1] if len(args) > 1 else ""
        flags = args[2] if len(args) > 2 else 0
        if isinstance(mode, str):
            write = any(flag in mode for flag in ("w", "a", "x", "+"))
        else:
            # os.open passes the permission bits as ``mode`` and the real
            # intent in ``flags``.
            write = False
        if isinstance(flags, int) and flags & _WRITE_FLAGS:
            write = True
        if write:
            if not _write_allowed(target):
                raise _DENIED
        elif not _read_allowed(target):
            raise _DENIED
        return
    if event in _WRITE_EVENTS:
        _check_write_arguments(args)
        return
    if event in _READ_EVENTS:
        for value in args:
            if isinstance(value, (str, bytes, os.PathLike)) and not _read_allowed(
                _real(value)
            ):
                raise _DENIED
        return
    if event == "os.chdir":
        target = _real(args[0]) if args else None
        if not _write_allowed(target):
            raise _DENIED


class _CappedStream:
    """Text stream that silently discards everything beyond a byte budget."""

    def __init__(self, stream, limit):
        self._stream = stream
        self._limit = limit
        self._written = 0
        self.truncated = False

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"

    def write(self, text):
        if not text:
            return 0
        if self._written >= self._limit:
            self.truncated = True
            return len(text)
        room = self._limit - self._written
        if len(text) > room:
            text = text[:room]
            self.truncated = True
        self._stream.write(text)
        self._written += len(text)
        return len(text)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        return False


def _apply_posix_limits():
    try:
        import resource
    except ImportError:
        return

    def _set(which, soft, hard=None):
        try:
            resource.setrlimit(which, (soft, hard if hard is not None else soft))
        except (ValueError, OSError):
            pass

    _set(resource.RLIMIT_CORE, 0)
    _set(resource.RLIMIT_CPU, max(1, int(TIMEOUT)), max(1, int(TIMEOUT)) + 1)
    _set(resource.RLIMIT_AS, MEMORY_MB * 1024 * 1024)
    _set(resource.RLIMIT_FSIZE, 1024 * 1024)
    _set(resource.RLIMIT_NOFILE, 64)


def _print_student_exception(exc):
    """Print only the frames that belong to the student's own file."""
    summary = traceback.StackSummary()
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if code.co_filename == "student_code.py":
            summary.append(
                traceback.FrameSummary(
                    code.co_filename, tb.tb_lineno, code.co_name
                )
            )
        tb = tb.tb_next
    text = "".join(summary.format())
    text += "".join(traceback.format_exception_only(type(exc), exc))
    sys.stderr.write(text)


def main():
    _apply_posix_limits()
    code_path = os.path.join(CWD, "student_code.py")
    with builtins.open(code_path, "r", encoding="utf-8") as handle:
        source = handle.read()

    sys.dont_write_bytecode = True
    sys.addaudithook(_audit)
    sys.stdout = _CappedStream(sys.stdout, MAX_OUTPUT)
    sys.stderr = _CappedStream(sys.stderr, MAX_OUTPUT)
    sys.argv = ["student_code.py"]
    sys.path[:] = [entry for entry in sys.path if _read_allowed(_real(entry))]

    namespace = {
        "__name__": "__main__",
        "__file__": "student_code.py",
        "__builtins__": builtins,
    }
    try:
        exec(compile(source, "student_code.py", "exec"), namespace)
    except SystemExit as exc:
        sys.stdout.flush()
        sys.stderr.flush()
        code = exc.code
        if code is None:
            raise SystemExit(0)
        if isinstance(code, int):
            raise SystemExit(code)
        print(code, file=sys.stderr)
        raise SystemExit(1)
    except BaseException as exc:
        # Show the student their own frames only: the runner's paths and
        # source must not leak into the playground terminal.
        _print_student_exception(exc)
        raise SystemExit(1)


try:
    main()
finally:
    # The parent cannot see the capped streams directly, so leave a marker
    # when either one dropped output.
    if getattr(sys.stdout, "truncated", False) or getattr(
        sys.stderr, "truncated", False
    ):
        try:
            with builtins.open(
                os.path.join(CWD, "_portal_truncated"), "w"
            ) as handle:
                handle.write("1")
        except OSError:
            pass
'''


def _child_env(workdir: str) -> dict[str, str]:
    """A minimal environment: no user profile, tokens, proxies or PATH."""
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": workdir,
        "TMP": workdir,
        "TMPDIR": workdir,
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(name)
            if value:
                env[name] = value
    else:
        env["PATH"] = "/usr/bin:/bin"
        env["LANG"] = "C.UTF-8"
    return env


def _sandbox_python() -> str:
    """The interpreter used to launch sandboxed code.

    On Windows a virtualenv's ``python.exe`` is a small redirector that
    spawns the real interpreter as a second process. The runner uses
    ``-S`` (standard library only, no site-packages), so the base
    interpreter behaves identically and avoids that extra spawn - which the
    single-process Job Object below would otherwise reject.
    """
    if os.name == "nt":
        base = getattr(sys, "_base_executable", None)
        if base and base != sys.executable and os.path.isfile(base):
            return base
    return sys.executable


class _WindowsJob:
    """Best-effort Job Object with a memory/CPU cap and kill-on-close."""

    JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, memory_bytes: int, cpu_seconds: float) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.CreateJobObjectW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._info_type = EXTENDED_LIMIT_INFORMATION
        self._info = EXTENDED_LIMIT_INFORMATION()
        flags = (
            self.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | self.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | self.JOB_OBJECT_LIMIT_JOB_MEMORY
            | self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | self.JOB_OBJECT_LIMIT_PROCESS_TIME
        )
        self._info.BasicLimitInformation.LimitFlags = flags
        self._info.BasicLimitInformation.ActiveProcessLimit = 1
        self._info.BasicLimitInformation.PerProcessUserTimeLimit = int(
            max(1.0, cpu_seconds) * 10_000_000
        )
        self._info.ProcessMemoryLimit = memory_bytes
        self._info.JobMemoryLimit = memory_bytes
        self.handle = self._kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            return
        ok = self._kernel32.SetInformationJobObject(
            self.handle,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(self._info),
            ctypes.sizeof(self._info),
        )
        if not ok:
            self.close()

    def assign(self, process: subprocess.Popen) -> bool:
        if not self.handle:
            return False
        try:
            process_handle = int(process._handle)  # type: ignore[attr-defined]
            return bool(
                self._kernel32.AssignProcessToJobObject(
                    self.handle, process_handle
                )
            )
        except (AttributeError, OSError, ValueError):
            return False

    def terminate(self) -> None:
        if self.handle:
            try:
                self._kernel32.TerminateJobObject(self.handle, 1)
            except OSError:
                pass

    def close(self) -> None:
        if self.handle:
            try:
                self._kernel32.CloseHandle(self.handle)
            except OSError:
                pass
            self.handle = None


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[הפלט נקטע]", True


class SandboxExecutor:
    """Run one student program in a bounded subprocess."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        memory_mb: int = DEFAULT_MEMORY_MB,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        python: str | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if memory_mb <= 0:
            raise ValueError("memory_mb must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.memory_mb = int(memory_mb)
        self.max_output_bytes = int(max_output_bytes)
        self.python = python or _sandbox_python()

    def run(self, code: str, stdin: str = "") -> ExecutionResult:
        if not isinstance(code, str):
            raise SandboxError("code must be a string")
        if len(code) > MAX_CODE_CHARS:
            raise SandboxError("code is too long")
        if not _SLOTS.acquire(timeout=_ACQUIRE_TIMEOUT_SECONDS):
            raise SandboxBusy("the sandbox is busy")
        try:
            return self._run_locked(code, stdin or "")
        finally:
            _SLOTS.release()

    def _run_locked(self, code: str, stdin: str) -> ExecutionResult:
        stdin = stdin[:MAX_STDIN_CHARS]
        started = time.perf_counter()
        timed_out = False
        job: _WindowsJob | None = None
        with tempfile.TemporaryDirectory(
            prefix="portal-sandbox-", ignore_cleanup_errors=True
        ) as workdir:
            work = Path(workdir)
            (work / "_portal_runner.py").write_text(
                _RUNNER_SOURCE, encoding="utf-8"
            )
            (work / "student_code.py").write_text(code, encoding="utf-8")

            command = [
                self.python,
                "-X",
                "utf8",
                "-I",
                "-S",
                "_portal_runner.py",
                str(self.timeout_seconds),
                str(self.memory_mb),
                str(self.max_output_bytes),
            ]
            popen_kwargs: dict = {
                "cwd": workdir,
                "env": _child_env(workdir),
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                popen_kwargs["start_new_session"] = True

            try:
                process = subprocess.Popen(command, **popen_kwargs)
            except OSError as exc:
                raise SandboxError(f"could not start the sandbox: {exc}") from exc

            if os.name == "nt":
                job = _WindowsJob(
                    self.memory_mb * 1024 * 1024, self.timeout_seconds
                )
                job.assign(process)

            try:
                try:
                    stdout, stderr = process.communicate(
                        input=stdin, timeout=self.timeout_seconds
                    )
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._terminate(process, job)
                    try:
                        stdout, stderr = process.communicate(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
            finally:
                if job is not None:
                    job.close()

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            child_cut = (work / "_portal_truncated").exists()
            stdout, stdout_cut = _truncate(
                stdout or "", self.max_output_bytes
            )
            stderr, stderr_cut = _truncate(
                stderr or "", self.max_output_bytes
            )
            exit_code = 124 if timed_out else int(process.returncode or 0)
            return ExecutionResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                execution_time_ms=elapsed_ms,
                timed_out=timed_out,
                truncated=stdout_cut or stderr_cut or child_cut,
            )

    @staticmethod
    def _terminate(process: subprocess.Popen, job: "_WindowsJob | None") -> None:
        if job is not None:
            job.terminate()
            return
        if os.name == "nt":
            process.kill()
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass
