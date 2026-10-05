"""Unit tests for Google API resilience and comment-based feedback delivery.

Run from the project root::

    python -m pytest tests/test_google_resilience.py -q

Pure unit tests: no network, no credentials, no API key. They pin the
behaviours students depend on:

  * a dropped socket (``ConnectionAbortedError`` / ``WinError 10053``) is
    retried transparently instead of crashing the run with a traceback;
  * feedback is delivered as a Drive **comment** on the student's document,
    never by writing into the document body;
  * publishing is idempotent - re-running updates the same comment instead
    of spamming the document with new bubbles, and never touches comments
    the teacher wrote themselves;
  * "success" is only reported once the comment is confirmed present.
"""

from __future__ import annotations

import copy
import re
import socket
import ssl
import sys
from collections.abc import Callable
from http.client import RemoteDisconnected
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import httplib2
import pytest
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.api_retry as api_retry  # noqa: E402
from src.classroom_service import (  # noqa: E402
    COMMENT_OWNERSHIP_TAG,
    DRIVE_COMMENT_GET_FIELDS,
    DRIVE_COMMENT_LIST_FIELDS,
    FEEDBACK_COMMENT_MARKER,
    LEGACY_FEEDBACK_COMMENT_MARKER,
    ClassroomService,
    ClassroomServiceError,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_http_error(status: int) -> HttpError:
    """Build a real HttpError carrying the given HTTP status."""
    resp = httplib2.Response({"status": status})
    return HttpError(
        resp=resp,
        content=b'{"error": {"message": "boom"}}',
        uri="https://www.googleapis.com/test",
    )


def _u16(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (the Docs API index unit)."""
    return len(text.encode("utf-16-le")) // 2


# The Drive v3 ``Comment`` resource, exactly as the API defines it. A partial
# response selector naming anything outside this set is rejected with
# ``HTTP 400: Invalid field selection <name>`` - one wrong name fails the whole
# call. Mirroring the schema here is what stops a bad selector from sailing
# through the unit tests and only exploding against the real API.
_DRIVE_COMMENT_FIELDS = frozenset(
    {
        "id",
        "kind",
        "createdTime",  # note: NOT "created" - that name does not exist
        "modifiedTime",
        "author",
        "content",
        "htmlContent",
        "resolved",
        "deleted",
        "fileId",
        "fileName",
        "quotedFileContent",
        "replies",
    }
)
# Top-level names available on the CommentList returned by comments.list.
_DRIVE_COMMENT_LIST_FIELDS = frozenset({"kind", "nextPageToken", "comments"})
_DRIVE_USER_FIELDS = frozenset(
    {"kind", "displayName", "emailAddress", "photoLink", "me", "permissionId"}
)
_DRIVE_NESTED_FIELDS: dict[str, frozenset[str]] = {
    "comments": _DRIVE_COMMENT_FIELDS,
    "author": _DRIVE_USER_FIELDS,
    "quotedFileContent": frozenset({"value", "mimeType"}),
    "replies": _DRIVE_COMMENT_FIELDS,
}


def _split_top_level(selector: str) -> list[str]:
    """Split a selector on the commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in selector:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return [p for p in parts if p]


def _split_selector(selector: str) -> list[str]:
    """Split a partial-response selector on top-level commas.

    The slash nesting form is normalised first (see
    :func:`_expand_slash_syntax`) so that *every* level of the selector is
    split the same way. Normalising only at the top would leave the nested
    levels untouched, which is exactly where the shipped selector puts its
    slashes - ``comments(id,author/displayName)`` - and the whole point is to
    validate the string the service really sends.
    """
    return _split_top_level(_expand_slash_syntax(selector))


def _expand_slash_syntax(selector: str) -> str:
    """Rewrite the slash nesting form into the parenthesised form.

    Drive accepts two equivalent spellings for a nested field -
    ``comments(author/displayName)`` and ``comments(author(displayName))`` -
    documented as equivalents in "Return specific fields" (Drive API guides).
    The selector the service actually ships uses the slash form, so
    normalising it means one validator covers both, rather than waving it
    through because the fake only understood parentheses.
    """
    expanded: list[str] = []
    for part in _split_top_level(selector):
        name, paren, _rest = part.partition("(")
        name = name.strip()
        if "/" not in name:
            # Slashes *inside* the parentheses are handled one level down, when
            # that level is split in turn.
            expanded.append(part)
            continue
        if paren:
            # Mixing both spellings in one part (``a/b(c)``) is not a form
            # Drive documents, and it is ambiguous; treat it as a bad field.
            raise make_http_error_with_field(name)
        segments = [segment.strip() for segment in name.split("/")]
        if any(not segment for segment in segments):
            raise make_http_error_with_field(name)
        nested = "".join(f"({segment}" for segment in segments[1:])
        expanded.append(f"{segments[0]}{nested})")
    return ",".join(expanded)


def _validate_parts(parts: list[str], allowed: frozenset[str], path: str) -> None:
    """Validate each name in a selector against ``allowed``."""
    for part in parts:
        name, _, rest = part.partition("(")
        name = name.strip()
        if name in {"*", ""}:
            continue
        if name not in allowed:
            # Drive reports the offending field name itself - the production
            # error for a bad selector was "Invalid field selection created".
            raise make_http_error_with_field(name)
        nested = rest.rstrip(")").strip()
        if not nested:
            continue
        child_allowed = _DRIVE_NESTED_FIELDS.get(name)
        if not child_allowed:
            # Sub-selecting inside a leaf (e.g. comments(id(foo))) is invalid.
            raise make_http_error_with_field(f"{path}{name}")
        _validate_parts(
            _split_selector(nested), child_allowed, f"{path}{name}."
        )


def validate_drive_fields(selector: str, top_level: frozenset[str]) -> None:
    """Raise ``HttpError 400`` if ``selector`` names a field Drive does not have.

    Mirrors the real API so an invalid ``fields`` string fails in the unit
    tests rather than in production. Both nesting spellings Drive accepts are
    understood; see :func:`_expand_slash_syntax`.
    """
    _validate_parts(_split_selector(selector), top_level, "")


def selected_fields(selector: str) -> set[str]:
    """Top-level field names a selector asks for, for either selector shape.

    ``comments.list`` wraps the per-comment names in a ``comments(...)``
    sub-selector (``nextPageToken,comments(id,content)``), while ``comments.get``
    names the Comment's own fields directly (``id,content,resolved``). This
    returns the names of the resource the caller is actually reading, so the
    fake can answer with only those.
    """
    parts = _split_selector(selector or "")
    for part in parts:
        name, paren, rest = part.partition("(")
        if name.strip() == "comments" and paren:
            return {
                child.partition("(")[0].strip()
                for child in _split_selector(rest.rstrip(")"))
            }
    return {part.partition("(")[0].strip() for part in parts}


def project_comment(comment: dict, selected: set[str]) -> dict:
    """Narrow a stored comment to the fields a selector asked for.

    Drive returns only the selected fields, which is the behaviour that makes
    a partial response safe to rely on: code that reads a field the selector
    never requested gets nothing back, exactly as it would in production. An
    empty ``selected`` (``fields=*``) leaves the comment whole.
    """
    if not selected or "*" in selected:
        return dict(comment)
    return {name: comment[name] for name in selected if name in comment}


def make_http_error_with_field(name: str) -> HttpError:
    """The exact error Drive returns for an unknown partial-response field."""
    resp = httplib2.Response({"status": 400})
    return HttpError(
        resp=resp,
        content=(
            '{"error": {"code": 400, "message": '
            f'"Invalid field selection {name}"}}'
        ).encode("utf-8"),
        uri="https://www.googleapis.com/drive/v3/comments",
    )


def make_http_error_missing_fields() -> HttpError:
    """The exact error Drive returns when a required ``fields`` is omitted.

    ``comments.list`` and ``comments.create`` both refuse to guess a partial
    response, so this is a 400 the fake has to be able to raise - otherwise
    dropping the selector from the service looks fine in CI and fails against
    Google with ``The 'fields' parameter is required for this method``.
    """
    resp = httplib2.Response({"status": 400})
    return HttpError(
        resp=resp,
        content=(
            '{"error": {"code": 400, "message": '
            '"The \'fields\' parameter is required for this method."}}'
        ).encode("utf-8"),
        uri="https://www.googleapis.com/drive/v3/comments",
    )


class FlakyRequest:
    """Stands in for a googleapiclient ``HttpRequest``.

    ``execute()`` raises the queued exceptions in order, then evaluates
    ``result``. A callable result is evaluated per successful call, so a fake
    that mutates state only mutates it on the attempt that really succeeds.
    """

    def __init__(self, errors, result=None):
        self._errors = list(errors)
        self._result = result if result is not None else {"ok": True}
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._result() if callable(self._result) else self._result


# --------------------------------------------------------------------------- #
# Stateful fakes for the Drive Comments API and the Docs API
# --------------------------------------------------------------------------- #


class _FakeDrive:
    """A stateful stand-in for ``drive.comments()``.

    Comments are really stored, listed, updated and deleted, so the
    idempotency logic is exercised end to end rather than mocked away.

    * ``errors`` - errors raised by the next calls, keyed by operation
      (``"list"``, ``"create"``, ``"update"``, ``"delete"``, ``"get"``). Each
      value is a list consumed in order, so ``{"create": [HttpError]}`` fails
      the first create and lets the retry succeed.
    * ``silent_create`` - create answers 200 but stores nothing, simulating a
      write that "succeeds" without the feedback ever landing.
    """

    def __init__(self, comments=None, errors=None, silent_create=False):
        self.comments: list[dict] = list(comments or [])
        self.errors = {k: list(v) for k, v in (errors or {}).items()}
        self.silent_create = silent_create
        self.calls: dict[str, list[dict]] = {
            name: [] for name in ("list", "create", "update", "delete", "get")
        }
        self._next_id = len(self.comments) + 1

    def api(self):
        return SimpleNamespace(
            list=self._list,
            create=self._create,
            update=self._update,
            delete=self._delete,
            get=self._get,
        )

    def _maybe_fail(self, op: str) -> None:
        queued = self.errors.get(op)
        if queued:
            raise queued.pop(0)

    def _first_attempt_error(self, op: str) -> Any:
        """Pop the queued error for ``op``, raised on the first execute().

        Raising inside ``execute()`` (not when the request is built) is what
        lets the service's retry / fallback logic see it, and failing only the
        first attempt models a transient blip rather than a permanent error.
        """
        queued = self.errors.get(op)
        return queued.pop(0) if queued else None

    @staticmethod
    def _check_fields(
        kwargs: dict, top_level: frozenset[str], required: bool = False
    ) -> Any:
        """Return the 400 Drive would send for a bad or missing ``fields``.

        Mirrors Drive v3 on both counts:

        * every name in a partial-response selector is checked against the
          resource schema, and one unknown name fails the whole call;
        * the methods that will not invent a payload (``comments.list``,
          ``comments.create``, ``comments.update``) reject a request that omits
          ``fields`` with ``The 'fields' parameter is required for this
          method``. A fake that tolerated a missing selector is what let that
          regression reach production in the first place.
        """
        selector = kwargs.get("fields")
        if not selector:
            return make_http_error_missing_fields() if required else None
        try:
            validate_drive_fields(selector, top_level)
        except HttpError as exc:
            return exc
        return None

    @staticmethod
    def _once(error: Any, action: Callable[[], Any]) -> FlakyRequest:
        """A request that fails once with ``error``, then runs ``action``."""
        attempts = {"n": 0}

        def _run():
            attempts["n"] += 1
            if attempts["n"] == 1 and error is not None:
                raise error
            return action()

        return FlakyRequest([], _run)

    def _list(self, **kwargs):
        self.calls["list"].append(kwargs)
        selected = selected_fields(kwargs.get("fields", ""))
        return self._once(
            self._first_attempt_error("list")
            or self._check_fields(kwargs, _DRIVE_COMMENT_LIST_FIELDS, required=True),
            lambda: {
                "comments": [
                    # Drive answers with the selected fields only; returning the
                    # whole comment would hide a read of an unselected field.
                    project_comment(c, selected)
                    for c in self.comments
                ]
            },
        )

    def _create(self, **kwargs):
        self.calls["create"].append(kwargs)
        error = self._first_attempt_error(
            "create"
        ) or self._check_fields(kwargs, _DRIVE_COMMENT_FIELDS, required=True)
        comment_id = f"c{self._next_id}"
        self._next_id += 1

        def _run():
            if not self.silent_create:
                self.comments.append(
                    {"id": comment_id, "content": kwargs["body"]["content"]}
                )
            return {"id": comment_id}

        return self._once(error, _run)

    def _update(self, **kwargs):
        self.calls["update"].append(kwargs)
        error = self._first_attempt_error(
            "update"
        ) or self._check_fields(kwargs, _DRIVE_COMMENT_FIELDS, required=True)

        def _run():
            for comment in self.comments:
                if comment["id"] == kwargs["commentId"]:
                    comment["content"] = kwargs["body"]["content"]
                    break
            return {"id": kwargs["commentId"]}

        return self._once(error, _run)

    def _delete(self, **kwargs):
        self.calls["delete"].append(kwargs)
        error = self._first_attempt_error("delete")

        def _run():
            self.comments = [
                c for c in self.comments if c["id"] != kwargs["commentId"]
            ]
            return {}

        return self._once(error, _run)

    def _get(self, **kwargs):
        self.calls["get"].append(kwargs)
        selected = selected_fields(kwargs.get("fields", ""))
        error = self._first_attempt_error(
            "get"
        ) or self._check_fields(kwargs, _DRIVE_COMMENT_FIELDS, required=True)

        def _run():
            for comment in self.comments:
                if comment["id"] == kwargs["commentId"]:
                    return project_comment(copy.deepcopy(comment), selected)
            raise ClassroomServiceError("comment not found")

        return self._once(error, _run)

    # -- assertion helpers ---------------------------------------------- #
    def marked(self) -> list[dict]:
        return [
            c
            for c in self.comments
            if ClassroomService._is_our_comment(c["content"])
        ]

@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Keep the backoff instant so the retry tests stay fast."""
    monkeypatch.setattr(api_retry, "_wait_before_retry", lambda state: 0.0)


class _FakeDocs:
    """A minimal ``docs.documents()`` used only to place the comment anchor.

    ``batchUpdate`` raises on purpose: the document body must never be
    written to any more.
    """

    def __init__(self, text: str = "print('hello')\n", errors=None):
        self.text = text
        self.errors = list(errors or [])
        self.get_calls = 0

    def documents(self):
        return SimpleNamespace(get=self._get, batchUpdate=self._forbidden)

    def _get(self, **kwargs):
        self.get_calls += 1
        if self.errors:
            raise self.errors.pop(0)
        end = 1 + _u16(self.text)
        return FlakyRequest(
            [],
            {
                "documentId": kwargs["documentId"],
                "revisionId": "rev-1",
                "body": {
                    "content": [
                        {
                            "startIndex": 1,
                            "endIndex": end,
                            "paragraph": {
                                "elements": [
                                    {
                                        "startIndex": 1,
                                        "endIndex": end,
                                        "textRun": {"content": self.text},
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
        )

    def _forbidden(self, **kwargs):
        raise AssertionError("the document body must never be modified")


def _fake_service(comments=None, errors=None, docs=None, silent_create=False):
    """A ClassroomService wired to the fakes above.

    ``service.drive.comments`` and ``service.docs.documents`` are plain
    attributes in the real client library, so the fakes must expose them as
    callables returning the resource namespace.
    """
    service = ClassroomService.__new__(ClassroomService)
    service._profile_cache = {}
    drive = _FakeDrive(comments, errors, silent_create)
    api = drive.api()
    service.drive = SimpleNamespace(comments=lambda: api)
    service.docs = docs if docs is not None else _FakeDocs()
    return service, drive


def _marked_comment(content: str, comment_id: str = "c1") -> dict:
    return {"id": comment_id, "content": content}


@pytest.fixture(autouse=True)
def _clean_notifiers():
    api_retry.clear_retry_notifiers()
    yield
    api_retry.clear_retry_notifiers()


# --------------------------------------------------------------------------- #
# 1. Which failures are worth retrying
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionAbortedError(10053, "A connection attempt was aborted"),
        ConnectionResetError(10054, "An existing connection was forcibly closed"),
        ConnectionRefusedError(10061, "Connection refused"),
        BrokenPipeError(32, "Broken pipe"),
        TimeoutError("timed out"),
        socket.timeout("socket timeout"),
        socket.gaierror("Temporary failure in name resolution"),
        ssl.SSLError("SSL connection broken"),
        httplib2.ServerNotFoundError("no proxy"),
        RemoteDisconnected("remote end closed connection"),
        TransportError("token refresh socket died"),
    ],
)
def test_network_drops_are_transient(exc):
    """Socket/SSL/DNS drops - including Windows 10053 - must be retried."""
    assert api_retry.is_transient_google_error(exc) is True


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_retryable_http_statuses(status):
    assert api_retry.is_transient_google_error(make_http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 412])
def test_client_errors_are_not_retried(status):
    """A 403 (no scope) or 404 (deleted doc) will not fix itself - fail fast."""
    assert api_retry.is_transient_google_error(make_http_error(status)) is False


def test_value_error_is_not_retried():
    assert api_retry.is_transient_google_error(ValueError("bug")) is False


def test_winerror_10053_is_covered_by_errno():
    """WinError 10053 is ECONNABORTED; the errno check is a safety net for
    transports that surface it as a bare OSError instead of the subclass."""
    raw = OSError(10053, "A connection attempt was aborted")
    assert raw.errno == 10053
    assert api_retry.is_transient_google_error(raw) is True


# --------------------------------------------------------------------------- #
# 2. The retry loop itself
# --------------------------------------------------------------------------- #


def test_execute_retries_after_socket_drop():
    """The headline bug: a dropped socket must not reach the caller."""
    request = FlakyRequest([ConnectionAbortedError(10053, "aborted")], {"courses": []})

    result = ClassroomService._execute(request, "list courses")

    assert result == {"courses": []}
    assert request.calls == 2, "should retry exactly once and then succeed"


def test_execute_succeeds_after_several_drops():
    request = FlakyRequest(
        [
            ConnectionAbortedError(10053, "aborted"),
            ConnectionResetError(10054, "reset"),
            make_http_error(503),
        ],
        {"courseWork": []},
    )

    assert ClassroomService._execute(request, "list course work") == {"courseWork": []}
    assert request.calls == 4


def test_execute_notifies_uis_on_each_retry():
    """Streamlit/CLI must see the retry, or a pause looks like a freeze."""
    seen: list[str] = []
    api_retry.add_retry_notifier(seen.append)

    request = FlakyRequest([ConnectionAbortedError(10053, "aborted")], {"ok": True})
    ClassroomService._execute(request, "list courses")

    assert len(seen) == 1
    assert "list courses" in seen[0]
    assert "retrying" in seen[0].lower()


def test_broken_notifier_does_not_break_the_retry():
    """A crashing UI hook must never abort the network call."""
    api_retry.add_retry_notifier(lambda _msg: 1 / 0)

    request = FlakyRequest([ConnectionAbortedError(10053, "aborted")], {"ok": True})
    assert ClassroomService._execute(request, "list courses") == {"ok": True}


def test_execute_gives_up_with_a_clear_error():
    """Exhausted budget -> ClassroomServiceError, never a raw traceback."""
    request = FlakyRequest([ConnectionAbortedError(10053, "aborted")] * 10)

    with pytest.raises(ClassroomServiceError, match="Retried"):
        ClassroomService._execute(request, "list courses")

    assert request.calls == api_retry.MAX_ATTEMPTS


def test_execute_does_not_retry_permission_errors():
    """A 403 must surface immediately - retrying burns time for nothing."""
    request = FlakyRequest([make_http_error(403)])

    with pytest.raises(ClassroomServiceError) as excinfo:
        ClassroomService._execute(request, "update grade for submission 's1'")

    assert "HTTP 403" in str(excinfo.value)
    assert request.calls == 1


# --------------------------------------------------------------------------- #
# 3. Feedback is delivered as a Drive comment, never as body text
# --------------------------------------------------------------------------- #


def test_feedback_is_left_as_a_drive_comment():
    """The core requirement: a comment, not inserted body text."""
    service, drive = _fake_service()

    result = service.publish_feedback_comment("doc-1", "כל הכבוד, הפתרון נכון!", score=92)

    assert result["action"] == "created"
    assert len(drive.calls["create"]) == 1
    assert drive.calls["create"][0]["fileId"] == "doc-1"
    body = drive.calls["create"][0]["body"]
    assert body["content"] == f"{COMMENT_OWNERSHIP_TAG}כל הכבוד, הפתרון נכון!"
    assert "92" not in body["content"], "the grade is not stamped into the bubble"


def test_no_visible_prefix_is_prepended_to_the_feedback():
    """The student reads the feedback text itself - nothing before it.

    No robot emoji, no "automated feedback" banner: the rendered bubble must
    display exactly what the evaluator wrote. Ownership is carried by the
    invisible tag only.
    """
    service, drive = _fake_service()

    service.publish_feedback_comment("doc-1", "כל הכבוד, הפתרון נכון!")

    content = drive.calls["create"][0]["body"]["content"]
    assert "🤖" not in content
    assert "משוב אוטומטי" not in content
    assert "מהמערכת" not in content
    visible = content.lstrip(COMMENT_OWNERSHIP_TAG)
    assert visible.startswith("כל הכבוד"), (
        "after the invisible tag, the feedback text starts immediately"
    )


def test_the_score_is_never_written_into_the_comment():
    """The comment is the tag + feedback text, whatever the score is.

    The grade travels through Classroom's own grade field, so the bubble stays
    clean - and the body is identical for a scored and an unscored run.
    """
    for score in (None, 0, 42, 95, 100):
        service, drive = _fake_service()
        service.publish_feedback_comment("doc-1", "משוב נקי", score=score)
        content = drive.calls["create"][0]["body"]["content"]
        assert content == f"{COMMENT_OWNERSHIP_TAG}משוב נקי"
        assert "ציון" not in content
        assert "/ 100" not in content
        assert "—" not in content, "no score suffix left on the heading"


def test_the_ownership_tag_is_invisible_and_recognised():
    """The tag renders as nothing but still identifies our comments.

    All three characters are zero-advance-width, so the Docs UI shows the
    feedback text starting on the very first line - while ``_is_our_comment``
    still matches, which is what keeps publishing idempotent.
    """
    assert COMMENT_OWNERSHIP_TAG == "\u200b\u2060\u200d"
    import unicodedata

    for char in COMMENT_OWNERSHIP_TAG:
        assert unicodedata.category(char) == "Cf", (
            f"U+{ord(char):04X} must be a zero-width format character, "
            f"got {unicodedata.category(char)}"
        )
    assert ClassroomService._is_our_comment(f"{COMMENT_OWNERSHIP_TAG}משוב")
    assert not ClassroomService._is_our_comment("תודה על ההגשה!")


def test_the_feedback_starts_on_the_first_line():
    """No heading row: the feedback text is the first visible line."""
    service, drive = _fake_service()

    service.publish_feedback_comment("doc-1", "משוב", score=88)

    content = drive.comments[0]["content"]
    visible = content.lstrip(COMMENT_OWNERSHIP_TAG)
    assert visible.splitlines()[0] == "משוב"
    assert ClassroomService._is_our_comment(content)


def test_the_drive_comments_api_is_used():
    """Must go through comments.create, not any Docs write call."""
    service, drive = _fake_service()

    service.publish_feedback_comment("doc-1", "משוב")

    # If the body were written, the fake's _forbidden would have raised.
    assert len(drive.calls["create"]) == 1
    assert "anchor" in drive.calls["create"][0]["body"]


def test_comment_is_anchored_to_the_end_of_the_work():
    """Anchoring is what makes it a bubble next to the student's last line."""
    service, drive = _fake_service(docs=_FakeDocs("print('hello')\n"))

    result = service.publish_feedback_comment("doc-1", "משוב")

    anchor = drive.calls["create"][0]["body"]["anchor"]
    text_range = anchor["textRange"]
    # The body is "print('hello')\n" starting at index 1; the anchor skips the
    # trailing newline and covers the final ")" at document index 14.
    assert text_range["startIndex"] == 14
    assert text_range["endIndex"] == 15
    assert result["anchored"] is True


def test_empty_document_falls_back_to_an_unanchored_comment():
    """No anchor available, but the student still gets the comment."""
    service, drive = _fake_service(docs=_FakeDocs(""))

    result = service.publish_feedback_comment("doc-1", "משוב")

    assert result["anchored"] is False
    assert "anchor" not in drive.calls["create"][0]["body"]
    assert len(drive.marked()) == 1


def test_rejected_anchor_falls_back_to_unanchored():
    """If Google rejects the range, retry without it rather than fail."""
    service, drive = _fake_service(
        docs=_FakeDocs("print('hello')\n"),
        errors={"create": [make_http_error(400)]},
    )

    result = service.publish_feedback_comment("doc-1", "משוב")

    assert result["anchored"] is False
    assert len(drive.calls["create"]) == 2
    assert "anchor" in drive.calls["create"][0]["body"]
    assert "anchor" not in drive.calls["create"][1]["body"]
    assert len(drive.marked()) == 1, "the student still received the feedback"


def test_empty_feedback_is_rejected_before_any_api_call():
    service, drive = _fake_service()

    with pytest.raises(ClassroomServiceError, match="empty"):
        service.publish_feedback_comment("doc-1", "   ")

    assert drive.calls["create"] == [], "no comment for empty feedback"


# --------------------------------------------------------------------------- #
# 4. Idempotency: re-running must not spam the document
# --------------------------------------------------------------------------- #


def test_republishing_updates_the_existing_comment():
    """The whole point: run it twice, still exactly one comment."""
    service, drive = _fake_service()

    first = service.publish_feedback_comment("doc-1", "משוב ראשון", score=70)
    second = service.publish_feedback_comment("doc-1", "משוב שני", score=95)

    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert second["commentId"] == first["commentId"], "same bubble is reused"
    assert len(drive.comments) == 1, "no duplicate comment"
    assert len(drive.calls["create"]) == 1, "no second create call"
    assert len(drive.calls["update"]) == 1
    # The current text won, the old one is gone.
    assert "משוב שני" in drive.comments[0]["content"]
    assert "משוב ראשון" not in drive.comments[0]["content"]
    assert "95" not in drive.comments[0]["content"], "no score in the bubble"


def test_comments_posted_before_the_score_removal_are_still_matched():
    """A comment carrying the old scored heading must not be orphaned.

    :meth:`_is_our_comment` matches the legacy banner, and the old heading
    was ``MARKER + "  —  ציון: 88 / 100"``, so the banner is still a prefix
    of the first line. Re-publishing therefore *rewrites* such a comment
    rather than leaving it behind and adding a second bubble - and the
    rewrite is what strips both the stale score and the stale banner off it.
    """
    service, drive = _fake_service(
        comments=[
            {"id": "c1", "content": f"{FEEDBACK_COMMENT_MARKER}  —  ציון: 88 / 100\n\nישן"}
        ]
    )

    result = service.publish_feedback_comment("doc-1", "משוב חדש", score=95)

    assert result["action"] == "updated", "the old bubble was recognised and reused"
    assert result["commentId"] == "c1"
    assert len(drive.comments) == 1, "no duplicate bubble left behind"
    content = drive.comments[0]["content"]
    assert content == f"{COMMENT_OWNERSHIP_TAG}משוב חדש"
    assert "ציון" not in content, "the stale score line is gone after the rewrite"
    assert "משוב אוטומטי" not in content, "the stale banner is gone too"


def test_republishing_keeps_one_comment_across_many_runs():
    service, drive = _fake_service()

    for score in range(60, 70):
        service.publish_feedback_comment("doc-1", f"משוב {score}", score=score)

    assert len(drive.comments) == 1
    assert "משוב 69" in drive.comments[0]["content"]


def test_comments_carrying_the_legacy_banner_are_migrated_not_duplicated():
    """The first run after the banner removal must rewrite, not stack.

    Documents that already have a visible-banner bubble get it replaced with
    the invisible-tag format on the next publish - one bubble before, one
    bubble after, and no trace of the old banner in the new text.
    """
    service, drive = _fake_service(
        comments=[
            {"id": "c1", "content": f"{LEGACY_FEEDBACK_COMMENT_MARKER}\n\nישן"},
        ]
    )

    result = service.publish_feedback_comment("doc-1", "משוב עדכני", score=90)

    assert result["action"] == "updated", "the legacy bubble was recognised"
    assert result["commentId"] == "c1"
    assert len(drive.comments) == 1, "no second bubble was added"
    content = drive.comments[0]["content"]
    assert content == f"{COMMENT_OWNERSHIP_TAG}משוב עדכני"
    assert "משוב אוטומטי" not in content
    assert "🤖" not in content


def test_extra_marked_duplicates_are_cleaned_up():
    """A double-click or an earlier run must not leave a pile of bubbles."""
    service, drive = _fake_service(
        comments=[
            {"id": "c1", "content": f"{FEEDBACK_COMMENT_MARKER}\n\nישן"},
            {"id": "c2", "content": f"{FEEDBACK_COMMENT_MARKER}\n\nכפול"},
            {"id": "c3", "content": f"{FEEDBACK_COMMENT_MARKER}\n\nישן יותר"},
        ]
    )

    result = service.publish_feedback_comment("doc-1", "משוב מעודכן", score=88)

    assert result["duplicatesRemoved"] == 2
    assert len(drive.comments) == 1
    assert drive.comments[0]["id"] == "c1", "the oldest comment is reused"
    assert "משוב מעודכן" in drive.comments[0]["content"]


def test_teacher_comments_are_never_touched():
    """Only our own marked comments are matched, rewritten or deleted."""
    service, drive = _fake_service(
        comments=[
            {"id": "t1", "content": "תודה על ההגשה!"},
            {"id": "t2", "content": "שאלה: למה השתמשת ב־for?"},
        ]
    )

    result = service.publish_feedback_comment("doc-1", "משוב אוטומטי", score=80)

    assert result["action"] == "created"
    assert result["duplicatesRemoved"] == 0
    assert drive.calls["update"] == [], "a teacher comment was never updated"
    assert drive.calls["delete"] == [], "a teacher comment was never deleted"
    assert len(drive.comments) == 3, "both teacher comments plus our new one"
    assert "תודה על ההגשה!" in drive.comments[0]["content"]
    assert "שאלה: למה השתמשת ב־for?" in drive.comments[1]["content"]
    assert ClassroomService._is_our_comment(drive.comments[2]["content"])


def test_a_locked_comment_is_replaced_rather_than_left_stale():
    """A resolved comment cannot be edited - delete it and create a new one."""
    service, drive = _fake_service(
        comments=[{"id": "c1", "content": f"{FEEDBACK_COMMENT_MARKER}\n\nישן"}],
        errors={"update": [make_http_error(403)]},
    )

    result = service.publish_feedback_comment("doc-1", "משוב חדש", score=99)

    assert result["action"] == "created"
    assert len(drive.comments) == 1
    assert drive.comments[0]["id"] != "c1", "the locked comment was replaced"
    assert "משוב חדש" in drive.comments[0]["content"]
    assert "ישן" not in drive.comments[0]["content"]


def test_comment_survives_a_dropped_socket():
    """Network resilience covers the comment path too."""
    service, drive = _fake_service(
        errors={"create": [ConnectionAbortedError(10053, "aborted")]}
    )

    result = service.publish_feedback_comment("doc-1", "משוב")

    assert result["action"] == "created"
    assert len(drive.comments) == 1
    assert "משוב" in drive.comments[0]["content"]


def test_403_on_create_explains_how_to_fix_the_scope():
    service, _drive = _fake_service(errors={"create": [make_http_error(403)]})

    with pytest.raises(ClassroomServiceError) as excinfo:
        service.publish_feedback_comment("doc-1", "משוב")

    assert "403" in str(excinfo.value)
    assert "src/auth.py" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 5. "Success" must mean the comment is really there
# --------------------------------------------------------------------------- #


def test_a_silent_write_is_reported_not_hidden():
    """A 200 from the API is not proof the student can see the comment."""
    service, drive = _fake_service(silent_create=True)

    with pytest.raises(ClassroomServiceError) as excinfo:
        service.publish_feedback_comment("doc-1", "משוב שלא הגיע")

    assert "not" in str(excinfo.value).lower()
    assert "doc-1" in str(excinfo.value)


def test_mismatched_comment_content_is_reported():
    """The re-read must contain the text we generated, not merely a comment."""
    service, drive = _fake_service(
        silent_create=True,
    )
    # Store something else under the id the create will report.
    original = drive._create

    def _create_other(**kwargs):
        request = original(**kwargs)
        drive.comments.append({"id": "c1", "content": "משוב אחר לגמרי"})
        return request

    drive.api = lambda: SimpleNamespace(
        list=drive._list,
        create=_create_other,
        update=drive._update,
        delete=drive._delete,
        get=drive._get,
    )
    service.drive = SimpleNamespace(comments=lambda: drive.api())

    with pytest.raises(ClassroomServiceError, match="does not match"):
        service.publish_feedback_comment("doc-1", "משוב שהופק")


def test_utf16_anchor_indexing_handles_non_bmp_characters():
    """Hebrew is BMP; the tag is too. Naive len() stays correct here."""
    assert ClassroomService._utf16_len("ab") == 2
    assert ClassroomService._utf16_len("משוב") == 4
    assert ClassroomService._utf16_len("\U0001f916") == 2
    assert ClassroomService._utf16_len(COMMENT_OWNERSHIP_TAG) == len(
        COMMENT_OWNERSHIP_TAG
    )
    # The legacy banner still needs the +1: its robot emoji is non-BMP.
    assert ClassroomService._utf16_len(FEEDBACK_COMMENT_MARKER) == len(
        FEEDBACK_COMMENT_MARKER
    ) + 1


def test_stale_scope_is_detected_on_a_cached_service():
    """``st.cache_resource`` holds old credentials - the grant is re-checked."""
    from src.auth import SCOPES

    service = ClassroomService.__new__(ClassroomService)
    service.credentials = SimpleNamespace(
        scopes=set(SCOPES) - {"https://www.googleapis.com/auth/drive"}
    )
    assert service.missing_scopes == ["https://www.googleapis.com/auth/drive"]


def test_complete_grant_reports_no_missing_scopes():
    from src.auth import SCOPES

    service = ClassroomService.__new__(ClassroomService)
    service.credentials = SimpleNamespace(scopes=set(SCOPES))
    assert service.missing_scopes == []


def test_unknown_scopes_are_treated_as_complete():
    """Some credential types do not report scopes; do not thrash on that."""
    service = ClassroomService.__new__(ClassroomService)
    service.credentials = SimpleNamespace(scopes=None)


# --------------------------------------------------------------------------- #
# 6. The release workflow: the comment survives a refused grade
# --------------------------------------------------------------------------- #


class _StubWidget:
    def __getattr__(self, name):
        return lambda *a, **k: None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubStreamlit:
    """Minimal ``st`` stand-in so ``_run_release_action`` can run headless."""

    CONTAINERS = {"progress", "empty", "dataframe", "columns", "container", "expander", "tabs"}

    def __init__(self):
        self.messages: list[str] = []

    def __getattr__(self, name):
        if name in self.CONTAINERS:
            return lambda *a, **k: _StubWidget()

        def _emit(*args, **kwargs):
            self.messages.append(f"{name}:{args[0] if args else ''}")

        return _emit


@pytest.fixture
def workflow_app(monkeypatch):
    """Import ``app`` with Streamlit replaced by a stub."""
    import app as app_module

    monkeypatch.setattr(app_module, "st", _StubStreamlit())
    return app_module


def _one_student_report():
    from datetime import datetime

    from src.models import (
        EvaluationReport,
        GradingResult,
        ReportEntry,
        TaskEvaluation,
    )

    return EvaluationReport(
        created_at=datetime(2026, 1, 1, 12, 0),
        model="test",
        course_id="c1",
        course_name="Course",
        course_work_id="cw1",
        course_work_title="Assignment",
        assignment_config_id="lesson_1",
        entries=[
            ReportEntry(
                student_id="u1",
                student_name="נועה",
                submission_id="s1",
                doc_id="doc-1",
                result=GradingResult(
                    student_name="נועה",
                    score=88,
                    feedback_hebrew="עבודה טובה, שפרו את השם של המשתנה.",
                    task_evaluations=[
                        TaskEvaluation(
                            task_name="Task 1",
                            student_answer_found="print(1)",
                            status="CORRECT",
                            notes="ok",
                        )
                    ],
                ),
            )
        ],
    )


class _GradeRefusedService:
    """Simulates coursework this OAuth client may not grade (HTTP 403)."""

    def __init__(self):
        self.comments: list[tuple] = []
        self.returned: list[str] = []

    def update_submission_grade(self, *a, **k):
        raise ClassroomServiceError("Failed to update grade (HTTP 403): forbidden")

    def publish_feedback_comment(self, doc_id, feedback_text, score=None):
        self.comments.append((doc_id, feedback_text, score))
        return {"commentId": "c1", "action": "created", "duplicatesRemoved": 0}

    def return_student_submission(self, *a, **k):
        self.returned.append("returned")
        return {}


def test_comment_still_reaches_the_doc_when_grade_publishing_is_refused(
    workflow_app, tmp_path, monkeypatch
):
    """The core requirement: a 403 on the grade must not swallow the comment."""
    monkeypatch.setattr(
        workflow_app, "_delete_report_artifacts", lambda *a, **kwargs: []
    )
    service = _GradeRefusedService()

    workflow_app._run_release_action(
        service,
        tmp_path / "report.json",
        _one_student_report(),
        {
            "publish_grades": True,
            "post_comment": True,
            "return_submissions": True,
        },
    )

    assert service.comments == [
        ("doc-1", "עבודה טובה, שפרו את השם של המשתנה.", 88)
    ], "the Hebrew feedback must be commented even though the grade was refused"
    assert service.returned == [], "never return a submission left ungraded"


# --------------------------------------------------------------------------- #
# 7. Regression: HTTP 400 on the ``fields`` parameter
#
#    Two 400s have bitten this call in production:
#      * "Invalid field selection created"  - a Classroom-API name on a
#        Drive v3 Comment, where the field is ``createdTime``;
#      * "The 'fields' parameter is required for this method" - sending no
#        selector at all, which comments.list rejects just as firmly.
#    The fake below therefore does both jobs: it validates every name against
#    the real v3 Comment schema, and it refuses a request that omits
#    ``fields`` on the methods that require it.
# --------------------------------------------------------------------------- #


def test_the_old_buggy_selector_is_rejected_by_drive():
    """``created`` is not a Drive v3 Comment field - it is ``createdTime``.

    This is the exact selector that shipped and produced
    ``HTTP 400: Invalid field selection created`` in production. Pinned here
    so the mistake is documented and cannot be reintroduced unnoticed.
    """
    with pytest.raises(HttpError) as excinfo:
        validate_drive_fields(
            "nextPageToken,comments(id,content,created)", _DRIVE_COMMENT_LIST_FIELDS
        )
    assert "Invalid field selection created" in str(excinfo.value)


@pytest.mark.parametrize(
    "selector",
    [
        "nextPageToken,comments(id,content,createdTime)",
        "comments(id,content)",
        "comments(id,content,author)",
        "comments(id,content,author(displayName),quotedFileContent(value))",
        "comments(id,content,author/displayName,quotedFileContent/value)",  # slash form
        "comments(id,createdTime,deleted,resolved,htmlContent,fileName)",
        "nextPageToken,comments(*)",
        "*",
    ],
)
def test_valid_drive_selectors_are_accepted(selector):
    """Selectors drawn from the real schema must not raise."""
    validate_drive_fields(selector, _DRIVE_COMMENT_LIST_FIELDS)


@pytest.mark.parametrize(
    "selector",
    [
        "comments(created)",
        "comments(id),bogus",
        "comments(id(foo))",  # sub-selecting inside a leaf
        "nextPageToken,comments(author(displayNameX))",
        "comments(author/displayNameX)",  # the same mistake in slash form
        "comments(id/createdTime)",  # sub-selecting inside a leaf, slash form
        "comments(author/displayName(name))",  # both spellings mixed
    ],
)
def test_nested_bogus_selectors_are_rejected(selector):
    with pytest.raises(HttpError):
        validate_drive_fields(selector, _DRIVE_COMMENT_LIST_FIELDS)


def test_the_shipped_selector_is_valid_against_the_drive_schema():
    """The exact string the service sends must survive Drive's own validation.

    Guards the second half of the original bug: the fix for a missing ``fields``
    is only correct if every name in it really exists on the v3 ``Comment``
    resource.
    """
    assert DRIVE_COMMENT_LIST_FIELDS == (
        "nextPageToken,comments(id,content,author/displayName,"
        "quotedFileContent/value,createdTime)"
    )
    validate_drive_fields(DRIVE_COMMENT_LIST_FIELDS, _DRIVE_COMMENT_LIST_FIELDS)


def test_drive_accepts_both_nesting_spellings():
    """``author/displayName`` and ``author(displayName)`` are equivalent.

    Drive documents both; the service ships the slash form, and the fake has to
    understand it or it would not be checking the string that is really sent.
    """
    paren = "nextPageToken,comments(id,content,author(displayName),createdTime)"
    slash = "nextPageToken,comments(id,content,author/displayName,createdTime)"

    validate_drive_fields(paren, _DRIVE_COMMENT_LIST_FIELDS)
    validate_drive_fields(slash, _DRIVE_COMMENT_LIST_FIELDS)
    assert selected_fields(slash) == selected_fields(paren)


def test_comment_listing_sends_the_required_field_selector():
    """``comments.list`` REQUIRES ``fields`` - it is not an optional optimisation.

    Omitting it is answered with ``HTTP 400: The 'fields' parameter is required
    for this method``, which is exactly what the fake now raises, so a future
    removal of the selector fails here instead of in front of a teacher.
    """
    service, drive = _fake_service()

    service.publish_feedback_comment("doc-1", "משוב")

    assert drive.calls["list"][0]["fields"] == DRIVE_COMMENT_LIST_FIELDS
    # comments.get is no different: it requires a selector too.
    assert drive.calls["get"][0]["fields"] == DRIVE_COMMENT_GET_FIELDS
    # Pagination parameters are still valid and must be preserved.
    assert drive.calls["list"][0]["includeDeleted"] is False
    assert drive.calls["list"][0]["pageSize"] == 100


def test_the_shipped_get_selector_is_valid_against_the_drive_schema():
    """The verification re-read must name real v3 Comment fields.

    ``comments.get`` requires ``fields`` exactly as ``comments.list`` does, so
    a typo here fails every publish *after* the comment was already written.
    """
    assert DRIVE_COMMENT_GET_FIELDS == "id,content,resolved"
    validate_drive_fields(DRIVE_COMMENT_GET_FIELDS, _DRIVE_COMMENT_FIELDS)
    assert selected_fields(DRIVE_COMMENT_GET_FIELDS) == {"id", "content", "resolved"}


def test_verification_reaches_the_api_with_its_selector():
    """A publish that only differs by the verification read must still pass.

    This is the exact shape of the production failure: the comment is written
    fine, and the run still fails on the ``comments.get`` that follows it.
    """
    service, drive = _fake_service()

    result = service.publish_feedback_comment("doc-1", "משוב", score=90)

    assert result["action"] == "created"
    assert len(drive.calls["get"]) == 1, "verification really ran"
    get_call = drive.calls["get"][0]
    assert get_call["fileId"] == "doc-1"
    assert get_call["commentId"] == result["commentId"]
    assert get_call["fields"] == DRIVE_COMMENT_GET_FIELDS


def test_verification_without_a_field_selector_is_rejected_by_drive():
    """The fake enforces the requirement on ``comments.get`` as well."""
    drive = _FakeDrive(comments=[{"id": "c1", "content": "משוב"}])
    api = drive.api()
    api.get = lambda **kw: drive._once(  # deliberately omits ``fields``
        drive._check_fields(kw, _DRIVE_COMMENT_FIELDS, required=True),
        lambda: {"id": "c1", "content": "משוב"},
    )

    with pytest.raises(HttpError) as excinfo:
        api.get(fileId="doc-1", commentId="c1").execute()

    assert "'fields' parameter is required" in str(excinfo.value)


def test_verification_still_catches_a_silent_write():
    """Requiring a selector must not weaken the check it protects."""
    service, drive = _fake_service(silent_create=True)

    with pytest.raises(ClassroomServiceError) as excinfo:
        service.publish_feedback_comment("doc-1", "משוב שלא הגיע")

    assert "not" in str(excinfo.value).lower()
    assert drive.calls["get"][0]["fields"] == DRIVE_COMMENT_GET_FIELDS


def test_listing_without_a_field_selector_is_rejected_by_drive():
    """The fake enforces the requirement, proving it is really required.

    Without this the suite would happily pass a ``comments.list`` that carries
    no ``fields`` at all - the exact call Google answers with a 400.
    """
    service, drive = _fake_service()
    drive.api = lambda: SimpleNamespace(
        list=lambda **kw: drive._once(  # deliberately omits ``fields``
            drive._check_fields(kw, _DRIVE_COMMENT_LIST_FIELDS, required=True),
            lambda: {"comments": []},
        )
    )
    service.drive = SimpleNamespace(comments=lambda: drive.api())

    with pytest.raises(HttpError) as excinfo:
        drive.api().list(fileId="doc-1").execute()

    assert "'fields' parameter is required" in str(excinfo.value)


def test_pagination_still_works_and_repeats_the_selector():
    """Every page must carry the selector too - it is required on each call."""
    service = ClassroomService.__new__(ClassroomService)
    service._profile_cache = {}

    pages = [
        {
            "comments": [{"id": "a", "content": f"{COMMENT_OWNERSHIP_TAG}1"}],
            "nextPageToken": "page-2",
        },
        {"comments": [{"id": "b", "content": f"{COMMENT_OWNERSHIP_TAG}2"}]},
    ]
    seen_tokens: list[Optional[str]] = []
    seen_selectors: list[Optional[str]] = []

    def _list(**kwargs):
        seen_tokens.append(kwargs.get("pageToken"))
        seen_selectors.append(kwargs.get("fields"))
        return FlakyRequest([], pages[len(seen_tokens) - 1])

    drive = _FakeDrive()
    api = drive.api()
    api.list = _list
    service.drive = SimpleNamespace(comments=lambda: api)
    service.docs = _FakeDocs()

    found = service._our_comments("doc-1")
    assert [c["id"] for c in found] == ["a", "b"]
    assert seen_tokens == [None, "page-2"], "the second page was requested"
    assert seen_selectors == [DRIVE_COMMENT_LIST_FIELDS] * 2, (
        "the required selector is sent on every page, not just the first"
    )


def test_create_and_update_keep_a_valid_selector():
    """``fields="id"`` is valid and (per the API) required by comments.create."""
    service, drive = _fake_service()

    service.publish_feedback_comment("doc-1", "משוב ראשון", score=70)
    service.publish_feedback_comment("doc-1", "משוב שני", score=80)

    assert drive.calls["create"][0]["fields"] == "id"
    assert drive.calls["update"][0]["fields"] == "id"
    assert len(drive.comments) == 1


def test_the_whole_comment_lifecycle_runs_clean():
    """list -> create -> update -> get -> delete against a strict fake.

    The fake rejects a missing or malformed ``fields`` on list, create, update
    *and* get, exactly as Drive does, so a clean run here means the shipped
    selectors are genuinely valid - not merely unchecked.
    """
    service, drive = _fake_service(
        comments=[{"id": "c1", "content": f"{COMMENT_OWNERSHIP_TAG}ישן"}]
    )

    result = service.publish_feedback_comment("doc-1", "משוב עדכני", score=95)
    assert result["action"] == "updated"
    assert result["commentId"] == "c1"
    assert service._delete_comment_quietly("doc-1", "c1") is True

    assert drive.comments == []
    assert service._our_comments("doc-1") == []
    # Every comment-bearing call carried a selector, and every one was checked
    # against the real v3 schema (a bad name would have raised above).
    assert drive.calls["list"], "listing really happened"
    for call in drive.calls["list"]:
        assert call["fields"] == DRIVE_COMMENT_LIST_FIELDS
    for op in ("create", "update"):
        for call in drive.calls[op]:
            assert call["fields"] == "id", op
    assert drive.calls["get"], "verification really ran"
    for call in drive.calls["get"]:
        assert call["fields"] == DRIVE_COMMENT_GET_FIELDS
    for op in ("list", "create", "update", "delete", "get"):
        for call in drive.calls[op]:
            # ``createdTime`` is a real v3 field; the Classroom-API ``created``
            # is not, and it is a whole word - not the prefix of ``createdTime``.
            selector = str(call.get("fields", ""))
            assert not re.search(r"\bcreated\b", selector), (op, selector)


