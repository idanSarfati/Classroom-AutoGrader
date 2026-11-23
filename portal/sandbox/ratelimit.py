"""Per-student rate limit for sandbox runs.

The portal runs as one process, so an in-memory sliding window is enough.
Like the login throttle, a multi-worker deployment should move this to the
database or a shared cache.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

DEFAULT_MAX_RUNS = 20
DEFAULT_WINDOW_SECONDS = 60.0


class RunLimiter:
    def __init__(
        self,
        *,
        max_runs: int = DEFAULT_MAX_RUNS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_runs <= 0:
            raise ValueError("max_runs must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_runs = int(max_runs)
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, list[float]] = {}

    def allow(self, key: str) -> tuple[bool, float]:
        """Record one run and return ``(allowed, retry_after_seconds)``."""
        now = self._clock()
        with self._lock:
            recent = [
                stamp
                for stamp in self._runs.get(key, [])
                if now - stamp < self.window_seconds
            ]
            if len(recent) >= self.max_runs:
                self._runs[key] = recent
                retry_after = max(
                    0.0, self.window_seconds - (now - recent[0])
                )
                return False, retry_after
            recent.append(now)
            self._runs[key] = recent
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._runs.clear()
