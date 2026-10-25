"""Live progress UI primitives for long-running Streamlit batches.

Extracted from :mod:`app` (the former monolithic ``app.py``). This module is
purely presentational: it owns the Streamlit progress bar, status line and
fallback-provider notice used while grading/dry-running many students. It
imports nothing from the rest of the UI, so it can be reused by any tab.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import streamlit as st

from src.seams import st_proxy

logger = logging.getLogger(__name__)


class _LiveProgress:
    """Live progress bar + status line for a long, blocking Streamlit batch.

    Streamlit only repaints the widgets it is told about, and a grading batch
    blocks the script repeatedly: the Google fetch, each Groq call, and the
    paced pause after every student. Two habits keep that from reading as a
    freeze:

    * **paint before the work, not after** - the bar names the student being
      graded *now* instead of trailing one behind, and every phase boundary
      (reading document / scoring / scored) produces a fresh delta; and
    * **tick through the waits** - :func:`src.llm_evaluator.paced_pause` is
      handed :meth:`tick`, so a backoff that stretches to 30s after a 429 shows
      a live countdown and its reason instead of holding one dead frame for the
      whole wait.

    It also surfaces the fallback-provider notice the moment the router leaves
    Groq, so the teacher sees which model is grading while the batch is still
    running rather than only in the summary.
    """

    # Streamlit truncates visually, and a status line is not the place for an
    # essay - the full text is always in the log/report anyway.
    _MAX_LABEL = 140

    def __init__(self, total: int) -> None:
        self.total = max(int(total), 0)
        self._fraction = 0.0
        self._label = "Starting..."
        self._suffix = ""
        self._notice_text: Optional[str] = None
        self._bar = st.progress(0.0, text=self._label)
        self._notice = st.empty()
        self._status = None
        try:
            _status_fn = getattr(st, "status", None)
            if callable(_status_fn):
                self._status = _status_fn("Running dry-run...", expanded=True)
        except Exception:
            self._status = None

    # -- painting ------------------------------------------------------------

    def _text(self) -> str:
        return f"{self._label} {self._suffix}".strip()[: self._MAX_LABEL]

    def _sync_status(self, text: str) -> None:
        status = getattr(self, "_status", None)
        if status is None:
            return
        try:
            update = getattr(status, "update", None)
            if callable(update):
                update(label=text, expanded=True)
        except Exception:
            logger.debug("status sync failed", exc_info=True)

    def _paint(self) -> None:
        value = min(max(self._fraction, 0.0), 1.0)
        text = self._text()
        self._sync_status(text)
        try:
            self._bar.progress(value, text=text)
        except Exception:
            # A dead widget must never abort a batch that is otherwise fine.
            logger.debug("progress paint failed", exc_info=True)

    def label(self, text: str) -> None:
        """Point the bar at a new phase, keeping the completed fraction."""
        self._label = text
        self._suffix = ""
        self._paint()

    def fraction(self, value: float) -> None:
        """Set the completed fraction directly (used by the dry-run callback)."""
        try:
            self._fraction = float(value)
        except (TypeError, ValueError):
            return
        self._paint()

    def begin(self, index: int, name: str, action: str) -> None:
        """Start student ``index``: bar moves to the leading edge of that slice."""
        self._fraction = (index - 1) / self.total if self.total else 0.0
        self.label(f"[{index}/{self.total}] {name} - {action}")

    def complete(self, index: int, note: str) -> None:
        """Close student ``index``: bar advances and the outcome is stated."""
        self._fraction = index / self.total if self.total else 1.0
        self.label(f"[{index}/{self.total}] {note}")

    def pacing(self) -> None:
        """Rewrite the label's tail so the wait does not read as more scoring.

        ``"... - scoring"`` becomes ``"... - pacing"``; without this the screen
        would keep claiming it is scoring for the whole pause that follows the
        score.
        """
        head = self._label.split(" - ", 1)[0]
        self.label(f"{head} - pacing")

    def tick(self, remaining: float, total: float, reason: str) -> None:
        """Recount a paced pause. Called by ``paced_pause(on_tick=...)``.

        The countdown rides on the *current* label rather than replacing it, so
        the screen keeps saying which student is being waited on while also
        saying why (``pacing`` vs ``rate-limit backoff``).
        """
        try:
            left = float(remaining)
        except (TypeError, ValueError):
            return
        if left <= 0:
            self._suffix = ""
        else:
            self._suffix = f"waiting {math.ceil(left)}s ({reason})"
        self._paint()

    def notice(self, text: Optional[str]) -> None:
        """Show the fallback-provider warning once, and only when it changes."""
        if not text or text == self._notice_text:
            return
        self._notice_text = text
        try:
            self._notice.markdown(f"⚡ {text}")
        except Exception:
            logger.debug("notice paint failed", exc_info=True)

    def clear(self) -> None:
        """Drop the transient widgets so the summary below owns the screen."""
        status = getattr(self, "_status", None)
        if status is not None:
            try:
                update = getattr(status, "update", None)
                if callable(update):
                    update(label="Dry-run finished.", expanded=False)
            except Exception:
                logger.debug("status finish failed", exc_info=True)
        for widget in (self._bar, self._notice):
            try:
                widget.empty()
            except Exception:
                logger.debug("progress cleanup failed", exc_info=True)


# ---------------------------------------------------------------------------
# Seam wiring
# ---------------------------------------------------------------------------
# Tests monkeypatch ``st`` on the ``app`` facade (``monkeypatch.setattr(app,
# "st", stub)``); resolve it at call time so the widgets land in the stub.
st = st_proxy("app", st)