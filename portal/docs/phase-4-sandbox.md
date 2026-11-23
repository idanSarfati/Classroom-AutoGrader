# Phase 4 — Secure Python execution sandbox

Students can now try code before submitting it. The assignment editor's
"Run code" button opens a live sandbox session: the program runs in a fresh,
disposable interpreter, its output streams into a terminal panel and each
line typed into the console is fed to `input()`. There is no reload, no saved
file, and no access to the portal or the grading engine.

The sandbox is deliberately conservative, but it is a **Python-level sandbox,
not a hardened container**. Its job is to stop honest beginner mistakes and
obvious mischief (infinite loops, `open("C:\\...")`, `import socket`,
`subprocess`) inside a single school server. A hostile user with local
network access still needs OS-level isolation; see [Deployment](#deployment).

## Components

| File | Purpose |
| --- | --- |
| `portal/sandbox/executor.py` | One-shot executor, limits, child runner and audit hook |
| `portal/sandbox/session.py` | Live stdin-capable session with streamed output |
| `portal/sandbox/ratelimit.py` | Per-student sliding-window limiter |
| `portal/web/student_routes.py` | Session start/poll/input/stop endpoints (JSON) |
| `portal/templates/student_assignment.html` | "Run code" button, output panel, console line |
| `portal/static/app.js` | Session polling, local echo, status labels |
| `portal/tests/test_phase4_sandbox.py` | Executor, session, limiter and route tests |

## Execution pipeline

1. The route checks CSRF, role, code size and the student's rate window.
2. `SandboxSessionStore.start` creates an `InteractiveSession` and acquires
   one of two global run slots (a third simultaneous run gets `SandboxBusy`
   after two seconds). A one-shot run and a live session share the same slots.
3. A `TemporaryDirectory(prefix="portal-sandbox-")` is created and receives
   `_portal_runner.py` plus `student_code.py`. Nothing else is copied in.
4. The child starts as `python -u -X utf8 -I -S _portal_runner.py <timeout>
   <memory_mb> <max_output>` with `cwd` set to that directory and a minimal
   environment (no `PATH`/profile/tokens/proxies; only encoding, hash seed,
   temp vars and, on Windows, `SYSTEMROOT`/`WINDIR`). `-u` makes prompts
   appear immediately, before the program blocks on `input()`.
5. On Windows the child is assigned to a Job Object with a single-process
   limit, process/job memory caps, a CPU-time cap and kill-on-close. On POSIX
   the runner applies `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`,
   `RLIMIT_NOFILE` and `RLIMIT_CORE` before executing student code.
6. The runner installs an audit hook, caps stdout/stderr at 64 KiB each,
   compiles the source as `student_code.py` and `exec`s it in a fresh
   namespace with stdlib-only `sys.path`.
7. Two reader threads drain stdout/stderr into the session (UTF-8 incremental
   decoding, CRLF normalised to LF, output capped). A watcher enforces a
   120-second wall clock and a 60-second inactivity limit; on expiry it kills
   the Job Object (Windows) or process group (POSIX), reports exit code `124`
   and `timed_out: true`. Ending a run always deletes the temporary directory
   and releases the slot.
8. The browser polls the session for output deltas and posts one line at a
   time to the program's stdin. The line is echoed in the console locally, so
   it appears right after the prompt without being counted twice.

`SandboxExecutor.run(code, stdin)` remains available for one-shot runs (used
by the existing tests and the `/sandbox/run` endpoint); it feeds all stdin at
once, so `input()` without stdin still raises `EOFError` there.

The interpreter is the **base** interpreter when the portal itself runs inside
a Windows virtualenv (`sys._base_executable`). A venv `python.exe` is a
redirector that spawns the real interpreter as a second process, which the
Job Object's single-process limit rejects with exit code 101. The runner uses
`-S`, so it never needs venv site-packages.

## Result shape

```json
{
  "stdout": "42\n",
  "stderr": "",
  "exit_code": 0,
  "execution_time_ms": 74,
  "timed_out": false,
  "truncated": false,
  "ok": true,
  "timeout_seconds": 4.0
}
```

`ok` means "finished on its own with exit code 0"; a syntax error or a blocked
operation is a normal result (`exit_code` 1, message in `stderr`), not an HTTP
error. Error messages show only frames from `student_code.py`; the runner's
paths and internals never reach the browser.

`truncated` is true when either stream hit the 64 KiB cap, whether the cut
happened in the child stream wrapper or in the parent.

## What the audit hook blocks

* **Network** — imports of `socket`, `_socket`, `ssl`, `http`, `urllib`,
  `ftplib`, `telnetlib`, `smtplib`, `poplib`, `imaplib`, `xmlrpc`,
  `requests`; and every `socket.*` audit event (connect, bind, DNS, send).
* **Processes** — `subprocess`, `multiprocessing`, `os.system`, `os.exec*`,
  `os.spawn*`, `os.fork`, `os.posix_spawn`, `os.startfile`, `pty.spawn`.
* **Native code** — `ctypes`, `_ctypes`, `ctypes.*` events, `winreg`, `msvcrt`.
* **Introspection shortcuts** — `import gc`, `gc.get_objects`/`get_referrers`/
  `get_referents`, `sys.settrace`, `sys.setprofile`, and `sys._getframe` when
  the requested frame chain reaches the runner's globals (the usual way to
  rewrite the allowed roots).
* **Files** — reads outside the working directory and the interpreter's
  standard library; writes/removals/renames/symlinks outside the working
  directory (including `os.open` flag inspection); `os.chdir` outside it.
  Reading and writing inside the throwaway directory is allowed.

The hook is additive defence. The OS limits (Job Object / rlimits) and the
wall-clock kill still apply if a hook is somehow bypassed.

## Endpoint contract

| Endpoint | Form / query | Meaning |
| --- | --- | --- |
| `POST /student/sandbox/run` | `csrf_token`, `code`, `stdin` | One-shot run (legacy); JSON result as above |
| `POST /student/sandbox/sessions` | `csrf_token`, `code` | Start a live run; returns `{"session_id": ...}` |
| `GET /student/sandbox/sessions/{id}` | `stdout_since`, `stderr_since` | Output deltas and state |
| `POST /student/sandbox/sessions/{id}/input` | `csrf_token`, `line` | Feed one line to the program's stdin |
| `POST /student/sandbox/sessions/{id}/stop` | `csrf_token` | End the run and release its slot |

The poll response is `{"stdout", "stderr", "stdout_offset", "stderr_offset",
"done", "exit_code", "timed_out", "truncated", "execution_time_ms"}`. Offsets
index the session's full output, so the browser appends only what is new.
A session belongs to the student who started it; another student gets `404`.

| Status | Meaning |
| --- | --- |
| `200` | Executed / started / accepted |
| `400` | Missing/incorrect CSRF, or empty code |
| `403` | Authenticated teacher, not a student |
| `404` | Session not found or owned by another student |
| `409` | The program already ended and no longer reads input |
| `413` | Code over 200,000 chars, stdin over 10,000 chars, or a line over 2,000 chars |
| `429` | More than 20 runs in 60 seconds, or all executor slots busy |
| `500` / `503` | The sandbox could not start / is unavailable |

CSRF failures are JSON here (the dependency is awaited by hand) so the fetch
handler can display the message without an HTML error page. Runs are **not**
stored: there is no table, no log of submitted text and no link to the grading
submission.

## Student experience

The assignment editor is an English, LTR IDE (JetBrains Mono, bundled
locally): a "Run code" button in the toolbar, a docked Output console with
stdout/stderr and a status line ("Running…", "Success · 74 ms", "Timed out",
"Exited with code 1"), plus a "Clear" button. While a run is live, a console
line with a `>` prompt sits at the bottom of the output; typing a line and
pressing Enter sends it to `input()`, echoes it at the prompt and lets the
program continue. Output is inserted with `textContent`, never HTML. Without
JavaScript the button does nothing; the submit form is unaffected and the
console input never becomes part of the submitted text.

## Tests

```powershell
.venv\Scripts\python -m pytest portal/tests/test_phase4_sandbox.py -q
```

The executor and session tests start real child interpreters and cover:
stdout/stderr separation, one-shot stdin, prompt-then-input continuation,
output deltas, syntax and runtime errors, timeout (exit 124), inactivity
timeout, output cap, temp-directory cleanup, memory exhaustion, in-cwd
writes, outside reads/writes, network/subprocess/ctypes blocking, env
stripping and the Windows venv workaround. Route tests exercise the session
endpoints for CSRF, ownership, stop and rate limiting.

## Deployment

* Run the portal under a dedicated OS account whose file system access is
  limited to the portal directory and temp folder.
* The audit hook blocks Python-level networking, not raw OS traffic from a
  compromised interpreter. For untrusted internet users, put the portal in a
  container/VM with an outbound firewall or no route, and consider a
  dedicated low-privilege user namespace.
* The rate limiter and run slots are in-process. A multi-worker deployment
  needs a shared limiter, exactly like the login throttle.
* Memory/CPU caps are per run. Two concurrent runs can each use up to
  256 MB, so size the host accordingly.

## Known limits

* The audit hook runs in the **same interpreter** as student code. A
  determined program can walk an exception traceback back to the runner frame
  and change the Python-level checks (the common `sys._getframe`, `gc` and
  tracing routes are blocked). Treat the hook as protection against mistakes
  and casual attempts, not as a security boundary; for adversarial code the
  OS account/container/firewall advice above is mandatory.
* A student can still burn up to four seconds of CPU per run (20 runs/minute),
  which is acceptable for a class but not for anonymous public traffic.
* `os.stat`/existence checks outside the sandbox are not blocked; only content
  reads and writes are. This leaks file metadata, not data.
* The playground does not provide a debugger, packages beyond the standard
  library, or persistence between runs.
* The output cap counts characters in the child and characters in the parent;
  for non-ASCII output the byte budget is approximate (UTF-8 output is
  capped by character count).
