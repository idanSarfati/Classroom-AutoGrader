"""Service layer over the Google Classroom, Drive, and Docs APIs.

Built on top of :func:`src.auth.get_google_credentials`. Provides course /
coursework listings, submission retrieval with name resolution, and Google
Docs text extraction for the AutoGrader pipeline.

Every Google API round-trip goes through :meth:`ClassroomService._execute`, so
a dropped socket (Windows ``WinError 10053``) is retried transparently instead
of crashing the run - see :mod:`src.api_retry`.

Feedback reaches students as a Google Drive **comment** on their document
(:meth:`ClassroomService.publish_feedback_comment`), anchored to the end of
their work so it appears as a bubble. The student's own code and text are
never modified. This path works even when Classroom refuses to publish grades
for an assignment (e.g. coursework owned by a different Developer Console
project), and re-publishing updates the existing comment rather than adding
another one.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timezone
from typing import Any, Optional

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.api_retry import MAX_ATTEMPTS as MAX_API_ATTEMPTS
from src.api_retry import call_with_retry, is_transient_google_error
from src.auth import SCOPES, get_google_credentials
from src.models import Course, CourseWork, StudentSubmission

logger = logging.getLogger(__name__)

GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
UNKNOWN_STUDENT = "Unknown student"

# Socket timeout for every Google API round-trip.
#
# google-api-python-client builds its transport as ``httplib2.Http(timeout=None)``
# and ``None`` means "wait forever". A half-open connection - a Wi-Fi roam, a
# VPN reconnect, a middlebox silently dropping an idle keep-alive - therefore
# leaves ``request.execute()`` blocked indefinitely: Streamlit's spinner never
# stops and, because no exception is ever raised, the retry policy in
# :mod:`src.api_retry` never even gets the chance to run. Bounding it turns
# that silent hang into a socket timeout, which
# :func:`src.api_retry.is_transient_google_error` already classifies as
# transient - so it is retried, and if the network stays down the caller gets
# an actionable error instead of an infinite spinner.
GOOGLE_HTTP_TIMEOUT_SECONDS = 30.0

# Partial-response selectors for the dashboard scan.
#
# Without ``fields`` the Classroom API returns each item whole: a coursework
# payload carries its materials, description and grading settings, and a
# student submission carries the full Drive metadata of every attachment. The
# scan reads six fields per assignment and six per submission, and would pay
# for the rest in bytes on each of the hundreds of calls a scan makes.
#
# Field names are pinned against the Classroom v1 discovery schema by
# ``tests/test_dashboard_dry_run.py``: a name Classroom does not know is
# rejected outright with ``HTTP 400: Invalid field selection``, taking the
# whole request with it.
COURSE_WORK_LIST_FIELDS = (
    "nextPageToken,"
    "courseWork(id,title,state,workType,dueDate,dueTime)"
)

# ``lateState`` and ``assigneeSubmissionTime`` are deliberately absent: neither
# exists in the Classroom v1 ``StudentSubmission`` schema, so naming them
# would fail every submission call with HTTP 400.
STUDENT_SUBMISSION_LIST_FIELDS = (
    "nextPageToken,"
    "studentSubmissions(id,userId,state,late,updateTime,assignmentSubmission)"
)

# Classroom's own verdict for "handed in, not yet graded/returned".
PENDING_SUBMISSION_STATE = "TURNED_IN"

# Every comment this app leaves carries a tag at the very start, and that tag is
# what makes publishing idempotent: a re-run recognises and rewrites its own
# comment instead of stacking a new bubble, and it is what keeps us from ever
# touching a comment the teacher wrote themselves.
#
# The tag is deliberately *invisible*. Drive's Comment resource exposes no
# author we can trust for this - the app posts with the teacher's own
# credentials, so ``author`` says "teacher" for our comments and for the
# teacher's alike - which leaves the comment text as the only thing that can
# tell them apart. The visible Hebrew banner that used to sit on the first line
# was that tag, and it was the only ownership marker a re-run had, so removing
# it outright would have made every run post a second bubble and made duplicate
# cleanup useless. These zero-width characters carry the same identity to
# ``content.startswith`` while rendering as nothing in the Docs UI.
#
# U+200B ZERO WIDTH SPACE, U+2060 WORD JOINER, U+200D ZERO WIDTH JOINER: all
# zero-advance-width, all preserved verbatim in the stored ``content`` string.
COMMENT_OWNERSHIP_TAG = "\u200b\u2060\u200d"

# The banner this tag replaced. Comments published by earlier runs still start
# with it, so :meth:`ClassroomService._is_our_comment` must keep recognising them
# - otherwise the first run after this change would add a second bubble to every
# document that already had feedback instead of rewriting it. The next publish
# rewrites such a comment in the new format, which migrates it automatically.
LEGACY_FEEDBACK_COMMENT_MARKER = "🤖 משוב אוטומטי מהמערכת"

# Backwards-compatible alias: the old public name for the visible banner.
# New comments no longer carry it - they carry COMMENT_OWNERSHIP_TAG instead -
# but external code (and these tests) may still reference the legacy banner.
FEEDBACK_COMMENT_MARKER = LEGACY_FEEDBACK_COMMENT_MARKER

# Partial-response selector for ``drive.comments().list``.
#
# Drive v3 does **not** default this endpoint: called without ``fields`` it
# answers ``HTTP 400: The 'fields' parameter is required for this method``.
# (The same is true of ``comments.create``, which sends ``fields="id"``.)
# So the selector is mandatory, and because Drive validates every name in it
# against the resource schema, it is also a liability: one unknown name fails
# the entire call with ``HTTP 400: Invalid field selection <name>``. This one
# names only fields the v3 ``Comment`` resource actually defines:
#
#   * ``nextPageToken``      - the pagination loop in
#                               :meth:`ClassroomService._our_comments` is driven
#                               by it, and it sits beside the comment array
#                               rather than inside it, so naming ``fields``
#                               means naming it explicitly.
#   * ``id`` / ``content``   - the id is reused to update our previous comment,
#                               and ``content`` is what the feedback marker is
#                               matched on. These two are the reason we list.
#   * ``author/displayName`` - who wrote a comment; makes a listed thread
#     ``quotedFileContent/value`` readable without a second round-trip.
#   * ``createdTime``        - the v3 spelling. The Classroom API's ``created``
#                               is NOT a Drive Comment field, and that mistake
#                               shipped once, breaking listing outright.
#
# Nesting uses the slash form (``author/displayName``); Drive documents
# parentheses (``author(displayName)``) and the slash form as equivalents, and
# both are accepted. The test suite validates this string against the real v3
# Comment schema so a bad name fails in CI instead of in front of a teacher.
DRIVE_COMMENT_LIST_FIELDS = (
    "nextPageToken,comments(id,content,author/displayName,"
    "quotedFileContent/value,createdTime)"
)

# Partial-response selector for ``drive.comments().get``, the call that verifies
# a comment really landed before we report success. It has the same
# requirement as ``comments.list``: no ``fields`` means
# ``HTTP 400: The 'fields' parameter is required for this method``.
#
#   * ``id``       - confirms we read back the comment we think we did, so a
#                    stale or wrong id cannot pass verification;
#   * ``content``  - the field the generated feedback is compared against;
#   * ``resolved`` - if the thread was already resolved (the teacher got there
#                    first), that is worth seeing when a verification looks odd.
DRIVE_COMMENT_GET_FIELDS = "id,content,resolved"


# Drive ids appear in alternateLinks such as
# https://drive.google.com/open?id=<id> or .../document/d/<id>/edit
_DRIVE_ID_PATTERNS = (
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
)


class ClassroomServiceError(Exception):
    """A Google API call failed in a reportable, non-fatal way."""


def _parse_rfc3339(value: Any) -> Optional[datetime]:
    """Parse a Google RFC 3339 timestamp into an aware UTC ``datetime``.

    Classroom returns timestamps such as ``2026-03-01T14:30:00Z`` or with a
    fractional part and/or a numeric offset. Anything unparseable yields
    ``None`` rather than raising, because a missing timestamp must degrade the
    late-submission check, never abort a grading run.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Ignoring unparseable Classroom timestamp %r.", value)
        return None
    # A timestamp without an offset is documented as UTC by Classroom; make
    # that explicit so it can be compared against the due instant safely.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _classroom_due_instant(raw: dict[str, Any]) -> tuple[Optional[date], Optional[datetime]]:
    """Due ``date`` and exact due ``datetime`` (UTC) of a coursework item.

    Classroom splits the deadline across ``dueDate`` (a ``Date``) and
    ``dueTime`` (a ``TimeOfDay``), and requires both together. Only the date is
    returned when the time is missing, because a date alone cannot decide
    whether work handed in late that same evening was late.
    """
    due = raw.get("dueDate") or {}
    if not (due.get("year") and due.get("month") and due.get("day")):
        return None, None
    due_date = date(int(due["year"]), int(due["month"]), int(due["day"]))
    time_of_day = raw.get("dueTime") or {}
    if not (time_of_day.get("hours") is not None
            and time_of_day.get("minutes") is not None):
        return due_date, None
    due_datetime = datetime(
        due_date.year,
        due_date.month,
        due_date.day,
        int(time_of_day.get("hours") or 0),
        int(time_of_day.get("minutes") or 0),
        int(time_of_day.get("seconds") or 0),
        tzinfo=timezone.utc,
    )
    return due_date, due_datetime


def _drive_file_id_from_link(url: str) -> str:
    """Extract a Drive file id from a Drive/Docs URL, or '' if not found."""
    for pattern in _DRIVE_ID_PATTERNS:
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return ""


class ClassroomService:
    """Thin, testable wrapper around the Google API clients.

    Instantiating the service builds the Classroom, Drive, and Docs API
    clients using :func:`src.auth.get_google_credentials`.
    """

    def __init__(self, credentials: Any = None) -> None:
        creds = credentials if credentials is not None else get_google_credentials()
        # Kept so callers can verify the OAuth grant actually covers every
        # scope we need. Streamlit caches this object (and therefore these
        # credentials) for the life of the server process, so a scope added
        # to SCOPES later is otherwise never picked up - see
        # app.get_classroom_service.
        self.credentials = creds
        # cache_discovery=False: avoid stale on-disk discovery caches.
        self.classroom = self._build_api("classroom", "v1", creds)
        self.drive = self._build_api("drive", "v3", creds)
        self.docs = self._build_api("docs", "v1", creds)
        # userProfiles().get() calls are cached per student id.
        self._profile_cache: dict[str, str] = {}
        # Drive mimeType lookups, cached per file id for the same reason: the
        # same document can appear on several assignments (a re-submission, a
        # second course), and a failed lookup would otherwise be retried.
        self._mime_cache: dict[str, Optional[str]] = {}

    @staticmethod
    def _build_api(service: str, version: str, credentials: Any) -> Any:
        """Build one API client whose sockets cannot block forever.

        ``build()`` refuses to take both ``http`` and ``credentials``, so the
        authorized transport is constructed here and handed over on its own.
        It wraps the very same credentials the credentials-only path would
        use, so token refresh behaves identically - the only difference is the
        bounded ``httplib2`` timeout (see
        :data:`GOOGLE_HTTP_TIMEOUT_SECONDS`).

        If anything in the bounded path fails, construction falls back to the
        plain credentials-only call, so hardening the transport can never
        become the reason the app refuses to start.
        """
        try:
            http = AuthorizedHttp(
                credentials,
                http=httplib2.Http(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS),
            )
            return build(service, version, http=http, cache_discovery=False)
        except Exception:  # noqa: BLE001 - startup must not fail on transport setup
            logger.warning(
                "Could not build a timeout-bounded transport for the %s API; "
                "falling back to the default, which can block indefinitely on "
                "a stalled connection.",
                service,
                exc_info=True,
            )
            return build(
                service, version, credentials=credentials, cache_discovery=False
            )

    @property
    def missing_scopes(self) -> list[str]:
        """Scopes this credential was NOT granted, according to Google.

        An empty list means either the grant is complete or the scopes are
        unknown (some credential types do not report them); both cases are
        indistinguishable here, and the API will surface a 403 if it matters.
        """
        granted = set(getattr(self.credentials, "scopes", None) or ())
        if not granted:
            return []
        return sorted(set(SCOPES) - granted)

    # ------------------------------------------------------------------ #
    # Error helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _api_error(action: str, exc: HttpError) -> ClassroomServiceError:
        """Turn an :class:`HttpError` into a human-readable service error."""
        status = exc.resp.status if exc.resp is not None else "?"
        message = str(exc)
        try:
            payload = json.loads(exc.content.decode("utf-8"))
            message = payload.get("error", {}).get("message") or message
        except Exception:  # noqa: BLE001 - keep the raw error as a fallback
            pass
        return ClassroomServiceError(f"Failed to {action} (HTTP {status}): {message}")

    @staticmethod
    def _execute(request: Any, action: str) -> Any:
        """Run an ``HttpRequest.execute()`` with transient-failure retries.

        A single choke point for every Classroom / Drive / Docs call, so a
        dropped socket (Windows ``WinError 10053`` surfaces as
        ``ConnectionAbortedError``), a DNS hiccup, or a 5xx/429 from Google is
        paused and retried transparently rather than escaping as a raw
        traceback in the Streamlit console.

        Non-transient failures (403, 404, 400, ...) are raised immediately -
        the caller turns them into a :class:`ClassroomServiceError` that
        Streamlit renders as a normal error message.
        """
        try:
            return call_with_retry(request.execute, action=action)
        except HttpError as exc:
            raise ClassroomService._api_error(action, exc) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            # Only claim a retry count when retries actually happened, so the
            # message does not send the user hunting for a network problem
            # that was never a factor.
            retried = (
                f" Retried {MAX_API_ATTEMPTS} times without success - check "
                f"the network connection and try again."
                if is_transient_google_error(exc)
                else ""
            )
            raise ClassroomServiceError(
                f"Failed to {action}: {type(exc).__name__}: {exc}.{retried}"
            ) from exc



    # ------------------------------------------------------------------ #
    # Courses
    # ------------------------------------------------------------------ #
    def list_courses(self) -> list[Course]:
        """Return all ACTIVE courses visible to the authenticated user.

        Handles ``nextPageToken`` pagination.
        """
        courses: list[Course] = []
        page_token: Optional[str] = None
        while True:
            response = self._execute(
                self.classroom.courses().list(
                    courseStates=["ACTIVE"],
                    pageToken=page_token,
                    pageSize=100,
                ),
                "list courses",
            )
            for raw in response.get("courses", []):
                courses.append(
                    Course(
                        id=raw.get("id", ""),
                        name=raw.get("name", ""),
                        section=raw.get("section"),
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return courses

    # ------------------------------------------------------------------ #
    # Coursework
    # ------------------------------------------------------------------ #
    def list_course_work(self, course_id: str) -> list[CourseWork]:
        """Return published assignments (id, title, dueDate) for a course.

        ``courseWork.list`` returns only PUBLISHED items unless states are
        requested otherwise; pagination via ``nextPageToken`` is handled.
        """
        items: list[CourseWork] = []
        page_token: Optional[str] = None
        while True:
            response = self._execute(
                self.classroom.courses().courseWork().list(
                    courseId=course_id,
                    courseWorkStates=["PUBLISHED"],
                    pageToken=page_token,
                    pageSize=100,
                    fields=COURSE_WORK_LIST_FIELDS,
                ),
                f"list course work for '{course_id}'",
            )
            for raw in response.get("courseWork", []):
                due_date, due_datetime = _classroom_due_instant(raw)
                items.append(
                    CourseWork(
                        id=raw.get("id", ""),
                        title=raw.get("title", "Untitled assignment"),
                        due_date=due_date,
                        due_datetime=due_datetime,
                        state=raw.get("state"),
                        work_type=raw.get("workType"),
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return items

    # ------------------------------------------------------------------ #
    # Coursework migration helpers (pilot: Developer-owned clones)
    #
    # Classroom only lets a Developer Console project modify course work and
    # student submissions that the *same* OAuth client created, so the pilot
    # migration needs to read the raw coursework payloads (drafts included)
    # and re-create them with this project as the owner.
    # ------------------------------------------------------------------ #
    def list_course_work_items(
        self,
        course_id: str,
        states: Sequence[str] = ("PUBLISHED", "DRAFT"),
    ) -> list[dict[str, Any]]:
        """Return raw coursework resources, drafts included.

        Unlike :meth:`list_course_work` - a trimmed :class:`CourseWork` view
        of PUBLISHED items only - this returns the untouched API payload, so
        callers can round-trip fields such as ``materials``, ``topicId``,
        ``maxPoints`` and read ``associatedWithDeveloper`` (whether the item
        is owned by the requesting Developer Console project).

        Pagination via ``nextPageToken`` is handled.
        """
        items: list[dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            response = self._execute(
                self.classroom.courses().courseWork().list(
                    courseId=course_id,
                    courseWorkStates=list(states),
                    pageToken=page_token,
                    pageSize=100,
                ),
                f"list course work items for '{course_id}'",
            )
            items.extend(response.get("courseWork", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return items

    def list_topics(self, course_id: str) -> dict[str, str]:
        """Return ``topicId -> topic name`` for one course (paginated).

        Used to report which topic a cloned draft lands in. Raises
        :class:`ClassroomServiceError` on API failure so callers that only
        need topic ids can fall back to an empty mapping.
        """
        topics: dict[str, str] = {}
        page_token: Optional[str] = None
        while True:
            response = self._execute(
                self.classroom.courses().topics().list(
                    courseId=course_id,
                    pageToken=page_token,
                    pageSize=100,
                ),
                f"list topics for '{course_id}'",
            )
            for raw in response.get("topic", []):
                topics[raw.get("topicId", "")] = raw.get("name", "")
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return topics

    def create_course_work_draft(
        self, course_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Create coursework from ``body`` as a DRAFT owned by this project.

        ``state`` is always forced to ``DRAFT`` so a pilot clone can never be
        published (and handed to students) by accident; publishing stays a
        deliberate action in the Classroom UI.

        Per the Classroom API, the created item - and its student submissions
        - become associated with the Developer Console project of the OAuth
        client id used for this request. That association is what later allows
        the auto-grader to push grades and return submissions for this
        coursework. Requires the ``classroom.coursework.students`` scope.
        """
        payload = {**body, "state": "DRAFT"}
        title = payload.get("title", "?")
        return self._execute(
            self.classroom.courses().courseWork().create(
                courseId=course_id, body=payload
            ),
            f"create draft course work '{title}'",
        )

    # ------------------------------------------------------------------ #
    # Submissions
    # ------------------------------------------------------------------ #
    def _raw_submissions(
        self, course_id: str, course_work_id: str
    ) -> list[dict[str, Any]]:
        """Paginated ``studentSubmissions.list`` with a tight selector.

        One HTTP round trip per page and **no per-student work** - resolving a
        display name costs its own ``userProfiles.get`` and resolving an
        attachment costs a Drive lookup, so both are deliberately left to
        :meth:`_submission_from_raw` and only performed for the submissions a
        caller actually needs.
        """
        raw_submissions: list[dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            response = self._execute(
                self.classroom.courses()
                .courseWork()
                .studentSubmissions()
                .list(
                    courseId=course_id,
                    courseWorkId=course_work_id,
                    pageToken=page_token,
                    pageSize=100,
                    fields=STUDENT_SUBMISSION_LIST_FIELDS,
                ),
                "list student submissions",
            )
            raw_submissions.extend(response.get("studentSubmissions", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return raw_submissions

    def _submission_from_raw(self, raw: dict[str, Any]) -> StudentSubmission:
        """Build the full model - the per-student API calls live in here.

        ``_display_name`` costs one ``userProfiles.get`` per student not yet in
        ``_profile_cache`` and ``_resolve_google_doc`` costs one Drive lookup
        per attachment, so this is the expensive half of a scan. Callers that
        only want some of the rows must filter on the raw state *before*
        getting here - see :meth:`get_pending_submissions`.
        """
        # Lateness is read from every signal Classroom offers, so the
        # policy can pick the strongest one. ``late`` is a bool and
        # ``lateState`` a string depending on the API version; ``None``
        # means "not reported" and is deliberately distinct from False.
        late_state = (raw.get("lateState") or "").strip() or None
        is_late_flag = raw.get("late")
        if is_late_flag is not None:
            is_late_flag = bool(is_late_flag)
        return StudentSubmission(
            student_id=raw.get("userId", ""),
            student_name=self._display_name(raw.get("userId", "")),
            submission_id=raw.get("id", ""),
            state=raw.get("state", "SUBMISSION_STATE_UNSPECIFIED"),
            doc_id=self._resolve_google_doc(raw),
            raw_code="",
            late_state=late_state,
            is_late=is_late_flag,
            submitted_at=_parse_rfc3339(
                raw.get("assigneeSubmissionTime")
            ) or _parse_rfc3339(raw.get("updateTime")),
        )

    def get_submissions(
        self, course_id: str, course_work_id: str
    ) -> list[StudentSubmission]:
        """Return every submission for an assignment with names/doc ids.

        * Retrieves all pages (``nextPageToken``).
        * Captures each submission's ``state`` (e.g. TURNED_IN, NEW) -
          callers filter as needed.
        * Resolves the student's display name via ``userProfiles().get``
          (cached per student).
        * Sets ``doc_id`` when the submission attaches a Google Doc;
          links, videos, forms, and non-Doc files leave ``doc_id`` as None.

        Costs one ``userProfiles.get`` per distinct student and one Drive
        lookup per attachment; a dashboard-wide scan should use
        :meth:`get_pending_submissions` instead.
        """
        return [
            self._submission_from_raw(raw)
            for raw in self._raw_submissions(course_id, course_work_id)
        ]

    def get_pending_submissions(
        self, course_id: str, course_work_id: str
    ) -> list[StudentSubmission]:
        """Only the submissions that are ``TURNED_IN`` - the scan's hot path.

        Identical network cost to :meth:`get_submissions` (the same single
        paginated call), but the state filter is applied to the **raw** rows
        *before* the expensive per-student work. An assignment whose work has
        already been handed back therefore costs one round trip instead of one
        ``userProfiles.get`` per student plus a Drive lookup per attachment -
        which is what turns a scan of a whole school from thousands of calls
        into a couple of hundred.
        """
        pending = [
            raw
            for raw in self._raw_submissions(course_id, course_work_id)
            if (raw.get("state") or "").strip().upper()
            == PENDING_SUBMISSION_STATE
        ]
        return [self._submission_from_raw(raw) for raw in pending]

    def _display_name(self, user_id: str) -> str:
        """Resolve a user's display name via the Classroom user profile."""
        if not user_id:
            return UNKNOWN_STUDENT
        if user_id in self._profile_cache:
            return self._profile_cache[user_id]

        name = UNKNOWN_STUDENT
        try:
            profile = self._execute(
                self.classroom.userProfiles().get(userId=user_id),
                f"fetch profile for student '{user_id}'",
            )
            name_obj = profile.get("name") or {}
            name = (
                name_obj.get("fullName")
                or " ".join(
                    part
                    for part in (name_obj.get("givenName"), name_obj.get("familyName"))
                    if part
                )
                or UNKNOWN_STUDENT
            )
        except ClassroomServiceError as exc:
            # Missing profile scope or deleted user - degrade gracefully,
            # but remember the fallback so we don't retry on every page.
            logger.warning("Could not resolve profile for %s: %s", user_id, exc)

        self._profile_cache[user_id] = name
        return name

    def _resolve_google_doc(self, submission: dict[str, Any]) -> Optional[str]:
        """Return the doc id of the first Google Doc attachment, if any.

        Links / YouTube videos / Forms / non-Doc Drive files are not
        extractable via the Docs API and yield ``None`` (the CLI then flags
        the submission as an unsupported attachment type).
        """
        attachments = (
            (submission.get("assignmentSubmission") or {}).get("attachments") or []
        )
        for attachment in attachments:
            drive_file = attachment.get("driveFile")
            if not drive_file:
                continue  # link / youTubeVideo / form attachment
            file_id = drive_file.get("id") or _drive_file_id_from_link(
                drive_file.get("alternateLink", "")
            )
            if not file_id:
                continue
            if self._drive_mime_type(file_id) == GOOGLE_DOC_MIME_TYPE:
                return file_id
        return None

    # ------------------------------------------------------------------ #
    # Drive file metadata
    # ------------------------------------------------------------------ #
    def get_drive_file_info(self, file_id: str) -> Optional[dict[str, Any]]:
        """Return Drive metadata for one file, or ``None`` when unavailable.

        Answers ``{"id", "name", "mimeType", "webViewLink"}`` - enough to tell
        a Google Doc from a Slides deck or a Sheet, and to print a link a human
        can click. Classroom's ``DriveFile`` carries no mimeType, so the Drive
        API is the authoritative source (it also verifies we can read it).

        A 403/404 (not shared with us, deleted, or outside our domain) returns
        ``None`` instead of raising: an attachment we cannot inspect must not
        abort a listing of the whole assignment.
        """
        if not file_id:
            return None
        try:
            return self._execute(
                self.drive.files().get(
                    fileId=file_id,
                    fields="id,name,mimeType,webViewLink",
                    supportsAllDrives=True,
                ),
                f"inspect Drive file '{file_id}'",
            )
        except ClassroomServiceError as exc:
            # 403/404: not accessible or deleted - treat as unsupported
            # rather than aborting the whole listing.
            logger.warning("Could not inspect Drive file %s: %s", file_id, exc)
            return None

    def _drive_mime_type(self, file_id: str) -> Optional[str]:
        """Look up a Drive file's mimeType; None when unavailable.

        Thin wrapper over :meth:`get_drive_file_info`, so submission
        attachment filtering and assignment-material listings share one Drive
        call shape (and one set of ``fields``).

        Memoised per file id - including failures, so a file that was deleted
        or not shared with us is not re-requested on every assignment it was
        ever attached to.
        """
        if file_id in self._mime_cache:
            return self._mime_cache[file_id]
        info = self.get_drive_file_info(file_id)
        mime_type = info.get("mimeType") if info else None
        self._mime_cache[file_id] = mime_type
        return mime_type

    # ------------------------------------------------------------------ #
    # Docs text extraction
    # ------------------------------------------------------------------ #
    def extract_doc_text(self, doc_id: str) -> str:
        """Return the document body as one clean multi-line string.

        Walks ``body.content`` structural elements (paragraphs, tables,
        tables of contents), concatenating every ``textRun`` in document
        order. Table cell contents are included; non-text elements (page
        breaks, equations, rich links, etc.) are skipped.
        """
        document = self._execute(
            self.docs.documents().get(documentId=doc_id),
            f"fetch document '{doc_id}'",
        )

        content = (document.get("body") or {}).get("content") or []
        text = "".join(self._walk_segments(content))
        # Trim surrounding blank lines only; keep internal whitespace intact.
        return text.strip("\n")

    @classmethod
    def _walk_segments(cls, segments: list[dict[str, Any]]) -> Iterator[str]:
        """Recursively yield raw textRun contents of structural elements."""
        for segment in segments:
            paragraph = segment.get("paragraph")
            if paragraph is not None:
                for element in paragraph.get("elements", []):
                    text_run = element.get("textRun")
                    # Non-textRun elements (page breaks, rich links,
                    # equations, auto text...) carry no plain text runs.
                    if text_run is not None:
                        yield text_run.get("content", "")

            table = segment.get("table")
            if table is not None:
                for row in table.get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        cell_text = "".join(
                            cls._walk_segments(cell.get("content") or [])
                        )
                        yield cell_text
                        # Keep cells on separate lines when the cell's own
                        # text does not already end with a newline.
                        if cell_text and not cell_text.endswith("\n"):
                            yield "\n"

            toc = segment.get("tableOfContents")
            if toc is not None:
                yield from cls._walk_segments(toc.get("content") or [])

            # sectionBreak segments contain no extractable text runs.

    # ------------------------------------------------------------------ #
    # Grading writes (require the classroom.coursework.students scope)
    # ------------------------------------------------------------------ #
    def update_submission_grade(
        self,
        course_id: str,
        course_work_id: str,
        submission_id: str,
        grade: float,
        draft: bool = False,
    ) -> dict[str, Any]:
        """Set a student's grade for one submission.

        With ``draft=False`` both ``assignedGrade`` and ``draftGrade`` are
        updated (``updateMask='assignedGrade,draftGrade'``). With
        ``draft=True`` only ``draftGrade`` is touched, so the grade stays
        private until the teacher publishes and returns the submission.

        Raises :class:`ClassroomServiceError` when the request fails - e.g.
        an OAuth scope or permission problem surfaces Google's
        "insufficient scopes" message in the error text.
        """
        if draft:
            update_mask = "draftGrade"
            body: dict[str, Any] = {"draftGrade": grade}
        else:
            update_mask = "assignedGrade,draftGrade"
            body = {"assignedGrade": grade, "draftGrade": grade}
        return self._execute(
            self.classroom.courses()
            .courseWork()
            .studentSubmissions()
            .patch(
                courseId=course_id,
                courseWorkId=course_work_id,
                id=submission_id,
                updateMask=update_mask,
                body=body,
            ),
            f"update grade for submission '{submission_id}'",
        )

    def return_student_submission(
        self, course_id: str, course_work_id: str, submission_id: str
    ) -> dict[str, Any]:
        """Return (release) a graded submission to the student.

        Raises :class:`ClassroomServiceError` when the request fails (see
        :meth:`update_submission_grade` for scope-related failures).
        """
        return self._execute(
            self.classroom.courses()
            .courseWork()
            .studentSubmissions()
            .return_(
                courseId=course_id,
                courseWorkId=course_work_id,
                id=submission_id,
                body={},
            ),
            f"return submission '{submission_id}'",
        )

    # ------------------------------------------------------------------ #
    # Feedback as a Google Drive comment (Drive Comments API)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _utf16_len(text: str) -> int:
        """Length of ``text`` in UTF-16 code units (the Docs API index unit).

        Python string indices count code points, but Google indexes documents
        in UTF-16 - every non-BMP character counts as two.
        """
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def _format_feedback_comment(feedback_text: str) -> str:
        """Render the comment body: the feedback text, nothing else visible.

        The student reads exactly what the evaluator wrote - no banner, no
        robot emoji, no Hebrew "automated feedback" prefix. Ownership lives in
        the invisible :data:`COMMENT_OWNERSHIP_TAG` prepended to the text: it
        is what makes publishing idempotent, because
        :meth:`_is_our_comment` recognises our comments by it and a re-run
        rewrites its own comment instead of adding another bubble.

        Comments carrying the legacy visible banner are still recognised (and
        rewritten in this format) via :meth:`_is_our_comment`, so the first
        run after this change migrates old bubbles instead of duplicating
        them.

        The numeric score is deliberately **not** rendered. The grade already
        reaches the student through Classroom's own grade field and the
        exported report, so stamping it into the bubble was noise - and it
        made the heading differ between scored and unscored runs, for no gain.
        """
        return f"{COMMENT_OWNERSHIP_TAG}{feedback_text.strip()}"

    def publish_feedback_comment(
        self,
        doc_id: str,
        feedback_text: str,
        score: Optional[float] = None,
    ) -> dict[str, Any]:
        """Leave the Hebrew feedback as a comment on the student's document.

        ``score`` is accepted for call-site compatibility (``app.py`` and
        ``main.py`` pass the grade) but is **not** written into the comment.
        The bubble carries the feedback text only (plus an invisible ownership
        tag); the grade itself reaches the student through Classroom's grade
        field. See :meth:`_format_feedback_comment`.

        The Classroom API has no submission-comment endpoint
        (studentSubmissions supports only get/list/modifyAttachments/patch/
        reclaim/return/turnIn), so feedback is delivered through the Drive
        Comments API on the attached file - visible to the document's
        collaborators (student and teacher) only, and without touching the
        student's own code or text. Requires the
        ``https://www.googleapis.com/auth/drive`` OAuth scope (see
        ``src/auth.py``).

        Publishing is idempotent. Every comment this app leaves carries the
        invisible :data:`COMMENT_OWNERSHIP_TAG`, so a re-run:

        * rewrites that comment with the current text instead of adding a
          second bubble;
        * deletes any extra marked comments left by an earlier run or by a
          double-click, so the document never collects duplicates;
        * replaces (delete + create) a comment Google refuses to edit, e.g.
          one the teacher already resolved.

        Comments the teacher wrote themselves are never matched or touched.
        The new comment is anchored to the end of the document so it appears
        as a bubble next to the student's work, falling back to an unanchored
        comment if Google rejects the range.

        Returns a dict with ``commentId``, ``action`` (``created`` or
        ``updated``), ``anchored`` and ``duplicatesRemoved``. The result is
        re-read before returning so a silent write is never reported as
        success.
        """
        text = (feedback_text or "").strip()
        if not text:
            raise ClassroomServiceError(
                f"Comment text is empty; nothing to post for '{doc_id}'."
            )
        content = self._format_feedback_comment(text)

        # 1. Reuse our own previous comment rather than spamming the document.
        previous = self._our_comments(doc_id)
        removed = 0
        if previous:
            keep = previous[0]["id"]
            for extra in previous[1:]:
                if self._delete_comment_quietly(doc_id, extra["id"]):
                    removed += 1
            try:
                self._execute(
                    self.drive.comments().update(
                        fileId=doc_id,
                        commentId=keep,
                        body={"content": content},
                        fields="id",
                    ),
                    f"update comment on document '{doc_id}'",
                )
                self._verify_comment(doc_id, keep, content)
                return {
                    "commentId": keep,
                    "action": "updated",
                    "anchored": True,
                    "duplicatesRemoved": removed,
                }
            except ClassroomServiceError as exc:
                # A resolved or otherwise locked comment cannot be edited.
                # Replace it rather than leaving stale feedback behind.
                logger.info(
                    "Could not update comment %s on %s (%s); recreating it.",
                    keep,
                    doc_id,
                    exc,
                )
                if not self._delete_comment_quietly(doc_id, keep):
                    raise

        # 2. First time (or replacing a locked comment): create a new one.
        try:
            created = self._create_anchored_comment(doc_id, content)
        except ClassroomServiceError as exc:
            raise self._with_scope_hint(exc, doc_id) from exc

        comment_id = str((created or {}).get("id", ""))
        self._verify_comment(doc_id, comment_id, content)
        return {
            "commentId": comment_id,
            "action": "created",
            "anchored": bool((created or {}).get("_anchored")),
            "duplicatesRemoved": removed,
        }

    @staticmethod
    def _is_our_comment(content: str) -> bool:
        """Whether a comment body was written by this app.

        Matches the invisible :data:`COMMENT_OWNERSHIP_TAG` on new comments
        and the legacy visible banner (:data:`LEGACY_FEEDBACK_COMMENT_MARKER`)
        on comments published by earlier runs. Matching both is what lets the
        first run after the banner removal *rewrite* the old bubble in the
        new format instead of adding a second one beside it.
        """
        return content.startswith(COMMENT_OWNERSHIP_TAG) or content.startswith(
            LEGACY_FEEDBACK_COMMENT_MARKER
        )

    def _our_comments(self, doc_id: str) -> list[dict[str, Any]]:
        """Comments on ``doc_id`` that this app wrote, oldest first.

        Only comments recognised by :meth:`_is_our_comment` are returned, so
        comments written by the teacher are never matched, rewritten, or
        deleted.

        ``fields`` is not optional here. ``comments.list`` is one of the Drive
        v3 methods that refuses to guess a partial response, and answers
        ``HTTP 400: The 'fields' parameter is required for this method`` when
        it is missing. The selector sent is
        :data:`DRIVE_COMMENT_LIST_FIELDS`, which names only fields the v3
        ``Comment`` resource really has - ``createdTime``, never ``created`` -
        and includes ``nextPageToken``, since pagination stops working the
        moment ``fields`` narrows the payload.
        """
        found: list[dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            response = self._execute(
                self.drive.comments().list(
                    fileId=doc_id,
                    includeDeleted=False,
                    pageSize=100,
                    pageToken=page_token,
                    # Required by comments.list; see DRIVE_COMMENT_LIST_FIELDS.
                    fields=DRIVE_COMMENT_LIST_FIELDS,
                ),
                f"list comments on document '{doc_id}'",
            )
            for comment in response.get("comments", []):
                content = (comment.get("content") or "").strip()
                if self._is_our_comment(content):
                    found.append(comment)
            page_token = response.get("nextPageToken")
            if not page_token:
                return found

    def _delete_comment_quietly(self, doc_id: str, comment_id: str) -> bool:
        """Delete one of our own comments; never raises.

        A comment the teacher already removed in the Docs UI simply makes this
        a no-op, so callers can clean up duplicates unconditionally.
        """
        try:
            self._execute(
                self.drive.comments().delete(
                    fileId=doc_id, commentId=comment_id
                ),
                f"delete comment '{comment_id}' on document '{doc_id}'",
            )
            return True
        except ClassroomServiceError as exc:
            logger.warning("Could not delete comment %s: %s", comment_id, exc)
            return False

    @classmethod
    def _comment_anchor(cls, document: dict[str, Any]) -> Optional[dict[str, Any]]:
        """A ``textRange`` anchor on the last line of the document.

        Anchoring is what makes the feedback a bubble next to the student's
        work rather than a floating note. ``None`` means "post it
        unanchored", which is also the fallback if Google rejects the range.
        """
        runs = cls._text_runs_with_index(
            (document.get("body") or {}).get("content") or []
        )
        for start, _end, text, _in_table in reversed(runs):
            stripped = text.rstrip("\r\n")
            if not stripped.strip():
                continue
            end = start + cls._utf16_len(stripped)
            return {
                "textRange": {"startIndex": max(end - 1, start), "endIndex": end}
            }
        return None

    def _create_anchored_comment(self, doc_id: str, content: str) -> dict[str, Any]:
        """Create a Drive comment, anchored when the document allows it.

        Falls back to an unanchored (general) comment if Google rejects the
        anchor - an empty document, a table-only body, or a range that is no
        longer valid. The student still gets the comment either way.
        """
        anchor: Optional[dict[str, Any]] = None
        try:
            document = self._execute(
                self.docs.documents().get(documentId=doc_id),
                f"fetch document '{doc_id}'",
            )
            anchor = self._comment_anchor(document)
        except ClassroomServiceError as exc:
            # Reading the document is only needed for the anchor.
            logger.info("Could not read %s for anchoring (%s).", doc_id, exc)

        try:
            created = self._create_comment(doc_id, content, anchor)
        except ClassroomServiceError as exc:
            # Only an invalid *range* is worth retrying unanchored. A 403/404
            # means the comment cannot be written at all, and retrying it with
            # no anchor would only hide the real, actionable error.
            if anchor is None or "HTTP 400" not in str(exc):
                raise
            logger.info(
                "Anchored comment rejected for %s; posting it unanchored.", doc_id
            )
            created = self._create_comment(doc_id, content, None)
            anchor = None

        result = dict(created or {})
        result["_anchored"] = anchor is not None
        return result

    def _create_comment(
        self, doc_id: str, content: str, anchor: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if anchor is not None:
            body["anchor"] = anchor
        return self._execute(
            self.drive.comments().create(
                fileId=doc_id,
                body=body,
                # comments.create REQUIRES an explicit fields param
                # (otherwise: HTTP 400 "'fields' parameter is required").
                fields="id",
            ),
            f"post comment on document '{doc_id}'",
        )

    def _verify_comment(self, doc_id: str, comment_id: str, content: str) -> None:
        """Re-read the comment and confirm the feedback is really there.

        A 200 from the Comments API is not proof that the student can see the
        text, so the comment is fetched back and compared. Reporting success
        without this check is how feedback silently goes missing.

        ``comments.get`` requires ``fields`` just as ``comments.list`` does, so
        the re-read sends :data:`DRIVE_COMMENT_GET_FIELDS`. Skipping it here
        turned every publish into a hard failure *after* the comment had
        already been written - the student got the feedback, the run reported
        an error, and a re-run then tried to update a comment that was fine.
        """
        if not comment_id:
            raise ClassroomServiceError(
                f"Google accepted the comment on document '{doc_id}' but "
                "returned no comment id, so the feedback could not be verified."
            )
        comment = self._execute(
            self.drive.comments().get(
                fileId=doc_id,
                commentId=comment_id,
                # Required by comments.get; see DRIVE_COMMENT_GET_FIELDS.
                fields=DRIVE_COMMENT_GET_FIELDS,
            ),
            f"verify comment on document '{doc_id}'",
        )
        actual = ((comment or {}).get("content") or "").strip()
        if actual != content.strip():
            raise ClassroomServiceError(
                f"The comment on document '{doc_id}' does not match the "
                "generated feedback. Please publish again."
            )

    @staticmethod
    def _with_scope_hint(
        exc: ClassroomServiceError, doc_id: str
    ) -> ClassroomServiceError:
        """Add a re-authorization hint to a 403 from the Drive API.

        A 403 on ``comments.create`` almost always means the saved token
        predates the ``drive`` scope, so point the teacher at the one command
        that fixes it instead of a bare permission error.
        """
        if "HTTP 403" in str(exc):
            return ClassroomServiceError(
                f"{exc} Hint: run 'python src/auth.py' to re-authorize with "
                "the drive scope, then reconnect the app, and publish again."
            )
        return exc

    @classmethod
    def _text_runs_with_index(
        cls,
        segments: list[dict[str, Any]],
        cursor: int = 1,
        in_table: bool = False,
    ) -> list[tuple[int, int, str, bool]]:
        """Absolute ``(start, end, content, in_table)`` for every textRun.

        ``documents.get`` stamps ``startIndex``/``endIndex`` on every
        structural element and paragraph element, so those are read directly
        rather than re-derived by summing string lengths (which drifts as soon
        as a table sits between the start of the document and the text of
        interest). Used to place the comment anchor on the last real line.
        """
        runs: list[tuple[int, int, str, bool]] = []
        position = cursor

        def descend(children: list[dict[str, Any]], inside_table: bool) -> None:
            nonlocal position
            for child in children:
                start = child.get("startIndex")
                if isinstance(start, int):
                    position = int(start)

                paragraph = child.get("paragraph")
                if paragraph is not None:
                    for element in paragraph.get("elements", []):
                        run = element.get("textRun")
                        if run is None:
                            continue
                        text = run.get("content", "")
                        element_start = element.get("startIndex")
                        if isinstance(element_start, int):
                            position = int(element_start)
                        length = cls._utf16_len(text)
                        runs.append((position, position + length, text, inside_table))
                        position += length

                table = child.get("table")
                if table is not None:
                    for row in table.get("tableRows", []):
                        for cell in row.get("tableCells", []):
                            descend(cell.get("content") or [], True)

                toc = child.get("tableOfContents")
                if toc is not None:
                    descend(toc.get("content") or [], inside_table)

                end = child.get("endIndex")
                if isinstance(end, int):
                    position = int(end)

        descend(segments, in_table)
        return runs


