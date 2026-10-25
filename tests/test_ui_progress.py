"""Tests for the live-progress plumbing that keeps a batch from looking frozen.

A grading batch blocks the script repeatedly - the Google fetch, every Groq
call, and the paced pause after each student - so a screen that receives no
update for 30 seconds is indistinguishable from a crash. This file pins the two
halves of the fix:

* :func:`src.llm_router.ticked_sleep` / :func:`src.llm_evaluator.paced_pause`
  narrate a wait through an ``on_tick`` callback (and stay one silent sleep
  when nobody is listening, so the CLI is unchanged); and
* ``app._LiveProgress`` paints *before* the work, counts a wait down, and
  surfaces the fallback-provider notice while the batch is still running.

Run from the project root::

    python -m pytest tests/test_ui_progress.py -q

Pure unit tests: no network, no credentials, no real sleeping.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.llm_evaluator as evaluator  # noqa: E402
import src.llm_router as llm_router  # noqa: E402
from src.llm_router import (  # noqa: E402
    PAUSE_TICK_SECONDS,
    AdaptivePacer,
    ticked_sleep,
)

# --------------------------------------------------------------------------- #
# 1. ticked_sleep - a wait that explains itself
# --------------------------------------------------------------------------- #


def test_without_a_callback_the_wait_is_one_silent_sleep(monkeypatch) -> None:
    """The CLI path must be byte-for-byte the sleep it always was."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm_router.time, "sleep", sleeps.append)

    ticked_sleep(7.5)

    assert sleeps == [7.5]


def test_with_a_callback_the_wait_is_sliced_and_ends_at_zero(monkeypatch) -> None:
    """Ticks are emitted per slice and the last one always reports 0.0."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm_router.time, "sleep", sleeps.append)
    ticks: list[tuple[float, float, str]] = []

    ticked_sleep(1.0, lambda left, total, reason: ticks.append((left, total, reason)))

    # One tick per slice, plus the terminal "we are done" tick.
    assert len(ticks) == len(sleeps) + 1
    assert len(sleeps) == -(-1.0 // PAUSE_TICK_SECONDS)  # ceil division
    assert ticks[0][0] == pytest.approx(1.0, abs=0.05)
    assert ticks[-1] == (0.0, 1.0, "pacing")
    assert all(total == 1.0 and reason == "pacing" for _, total, reason in ticks)


def test_a_broken_tick_callback_cannot_abort_the_wait(monkeypatch) -> None:
    """A dead widget must never take a grading batch down with it."""
    slept: list[float] = []
    monkeypatch.setattr(llm_router.time, "sleep", slept.append)

    def _explode(*_args) -> None:
        raise RuntimeError("the streamlit session went away")

    ticked_sleep(0.5, _explode)

    assert slept, "the wait must still have happened"


def test_a_zero_length_wait_ticks_nothing(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(llm_router.time, "sleep", sleeps.append)
    ticks: list[float] = []

    ticked_sleep(0, lambda left, _total, _reason: ticks.append(left))

    assert sleeps == []
    assert ticks == []


def test_the_slice_is_never_larger_than_the_wait(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(llm_router.time, "sleep", sleeps.append)

    ticked_sleep(0.1, lambda *_args: None)

    assert sleeps
    assert max(sleeps) <= 0.1


# --------------------------------------------------------------------------- #
# 2. AdaptivePacer / paced_pause - the between-student wait
# --------------------------------------------------------------------------- #


def test_a_stretched_pause_calls_itself_a_rate_limit_backoff(monkeypatch) -> None:
    """After a 429 the countdown must say *why* it is waiting, not just *that*."""
    monkeypatch.setattr(llm_router.time, "sleep", lambda _seconds: None)
    pacer = AdaptivePacer(base_seconds=2.0, max_seconds=30.0)

    pacing: set[str] = set()
    pacer.pause(lambda _left, _total, reason: pacing.add(reason))
    assert pacing == {"pacing"}

    pacer.report_rate_limited()
    backoff: set[str] = set()
    pacer.pause(lambda _left, _total, reason: backoff.add(reason))
    assert backoff == {"rate-limit backoff"}


def test_paced_pause_forwards_the_tick_to_the_shared_pacer(monkeypatch) -> None:
    """The UI callback has to reach the real pacer, not be swallowed here."""
    seen: list[object] = []

    class _FakePacer:
        def pause(self, on_tick=None) -> None:
            seen.append(on_tick)

    monkeypatch.setattr(evaluator, "_pacer", lambda: _FakePacer())

    def _callback(*_args) -> None:
        return None

    evaluator.paced_pause(on_tick=_callback)

    assert seen == [_callback]


def test_paced_pause_without_a_pacer_still_ticks_the_fixed_delay(monkeypatch) -> None:
    """The no-router fallback must be just as visible as the adaptive path."""
    monkeypatch.setattr(evaluator, "_pacer", lambda: None)
    monkeypatch.setattr(evaluator, "EVAL_PAUSE_SECONDS", 0.5)
    monkeypatch.setattr(llm_router.time, "sleep", lambda _seconds: None)
    ticks: list[tuple[float, float, str]] = []

    evaluator.paced_pause(
        on_tick=lambda left, total, reason: ticks.append((left, total, reason))
    )

    assert ticks
    assert ticks[-1] == (0.0, 0.5, "pacing")


def test_paced_pause_without_a_callback_is_unchanged(monkeypatch) -> None:
    """Default callers (the CLI, older tests) get the original single sleep."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm_router.time, "sleep", sleeps.append)

    class _FakePacer:
        def pause(self, on_tick=None) -> None:
            assert on_tick is None

    monkeypatch.setattr(evaluator, "_pacer", lambda: _FakePacer())

    evaluator.paced_pause()

    assert sleeps == []


# --------------------------------------------------------------------------- #
# 3. app._LiveProgress - paint before the work
# --------------------------------------------------------------------------- #


class _Bar:
    def __init__(self, log: list) -> None:
        self._log = log

    def progress(self, value=None, text=None, **_kwargs) -> None:
        self._log.append(("bar", value, text))

    def empty(self) -> None:
        self._log.append(("bar-empty",))


class _Slot:
    def __init__(self, log: list) -> None:
        self._log = log

    def markdown(self, message: str) -> None:
        self._log.append(("notice", message))

    def empty(self) -> None:
        self._log.append(("notice-empty",))


class _StStub:
    """Minimal ``st``: records every paint so assertions can read the screen."""

    def __init__(self) -> None:
        self.log: list = []

    def progress(self, value=0.0, text=None, **_kwargs) -> _Bar:
        self.log.append(("bar-init", value, text))
        return _Bar(self.log)

    def empty(self) -> _Slot:
        self.log.append(("slot-init",))
        return _Slot(self.log)


@pytest.fixture
def live(monkeypatch):
    """A ``_LiveProgress`` wired to a recording Streamlit stub."""
    import app as app_module

    stub = _StStub()
    monkeypatch.setattr(app_module, "st", stub)
    return app_module._LiveProgress, stub


def _last_bar(stub):
    for entry in reversed(stub.log):
        if entry[0] == "bar":
            return entry[1], entry[2]
    return None, None


def test_the_bar_points_at_the_student_being_started(live) -> None:
    """Painting *before* the call is what stops the batch reading as a stall."""
    cls, stub = live
    progress = cls(4)

    progress.begin(3, "Noa", "reading document")

    value, text = _last_bar(stub)
    assert value == pytest.approx(0.5)  # two of four done, third under way
    assert "[3/4] Noa" in text
    assert "reading document" in text


def test_completing_a_student_advances_the_bar(live) -> None:
    cls, stub = live
    progress = cls(4)

    progress.begin(1, "Noa", "reading document")
    progress.complete(1, "Noa - scored 92")

    value, text = _last_bar(stub)
    assert value == pytest.approx(0.25)
    assert "scored 92" in text


def test_the_countdown_rides_on_the_current_label(live) -> None:
    """A backoff must show *who* is being waited on and *how long is left*."""
    cls, stub = live
    progress = cls(2)
    progress.begin(1, "Noa", "reading document")

    progress.tick(12.4, 15.0, "rate-limit backoff")
    _value, during = _last_bar(stub)
    assert "Noa" in during
    assert "13s" in during
    assert "rate-limit backoff" in during

    progress.tick(0.0, 15.0, "rate-limit backoff")
    _value, after = _last_bar(stub)
    assert "waiting" not in after
    assert "Noa" in after


def test_pacing_rewrites_the_tail_so_scoring_does_not_linger(live) -> None:
    cls, stub = live
    progress = cls(2)
    progress.label("[1/2] Yael - scoring")

    progress.pacing()

    _value, text = _last_bar(stub)
    assert text.endswith("- pacing")
    assert "scoring" not in text


def test_the_fallback_notice_is_shown_once_and_only_when_it_changes(live) -> None:
    cls, stub = live
    progress = cls(1)

    progress.notice(None)
    progress.notice("switched to gemini")
    progress.notice("switched to gemini")

    notices = [entry for entry in stub.log if entry[0] == "notice"]
    assert len(notices) == 1
    assert "gemini" in notices[0][1]


def test_clear_drops_the_transient_widgets(live) -> None:
    cls, stub = live
    progress = cls(1)

    progress.clear()

    kinds = [entry[0] for entry in stub.log]
    assert "bar-empty" in kinds
    assert "notice-empty" in kinds


def test_the_fraction_is_clamped_and_a_bad_value_is_ignored(live) -> None:
    cls, stub = live
    progress = cls(10)

    progress.fraction(5.0)
    assert _last_bar(stub)[0] == 1.0

    progress.fraction(-3.0)
    assert _last_bar(stub)[0] == 0.0

    before = len(stub.log)
    progress.fraction("not a number")  # type: ignore[arg-type]
    assert len(stub.log) == before


def test_a_widget_that_explodes_does_not_break_the_batch(live) -> None:
    """The bar is decoration; losing it must not fail the grading run."""

    class _Exploding:
        def progress(self, *_args, **_kwargs) -> None:
            raise RuntimeError("session gone")

        def empty(self) -> None:
            raise RuntimeError("session gone")

    cls, _stub = live
    progress = cls(2)
    progress._bar = _Exploding()  # noqa: SLF001 - deliberate fault injection

    progress.begin(1, "Noa", "reading document")
    progress.tick(3.0, 5.0, "pacing")
    progress.clear()  # must not raise
