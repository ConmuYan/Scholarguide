"""Bounded per-source ordering, cooldown and retry decisions.

A deliberately small policy object shared by ``AcademicSource`` and
``SourceStore``: which sources run, in which order, and how long a failing
source rests before it may be tried again. It holds no credentials and makes
no network calls, so it is trivially testable offline.
"""
from __future__ import annotations

import threading
import time

#: Error codes that deserve a cooldown rather than an immediate retry.
_COOLDOWN_CODES = frozenset({'rate_limited', 'bot_challenge', 'server_error',
                             'timeout', 'transport'})


class SourceScheduler:
    """Order enabled sources by priority and rest failing ones briefly."""

    def __init__(self, *, cooldown_seconds: float = 45.0):
        self._lock = threading.RLock()
        self.cooldown_seconds = float(cooldown_seconds)
        self.cooldown: dict[str, float] = {}

    # -- ordering ---------------------------------------------------------
    def order(self, names: list[str], *, enabled: dict[str, bool] | None = None,
              priorities: dict[str, int] | None = None) -> list[str]:
        """Keep only enabled names, ordered by priority then by caller order."""
        enabled = enabled or {}
        priorities = priorities or {}
        ordered = [name for name in names if enabled.get(name, True)]
        return sorted(dict.fromkeys(ordered),
                      key=lambda name: (priorities.get(name, 100), names.index(name)))

    # -- health -----------------------------------------------------------
    def blocked(self, name: str, *, now: float | None = None) -> float:
        """Seconds remaining on cooldown; 0 when the source may run."""
        with self._lock:
            until = self.cooldown.get(name, 0.0)
        if not until:
            return 0.0
        now = time.monotonic() if now is None else now
        return max(0.0, until - now)

    def note_failure(self, name: str, code: str) -> bool:
        """Put a retryable failure on cooldown. Returns whether it rested."""
        if code not in _COOLDOWN_CODES:
            return False
        with self._lock:
            previous = self.cooldown.get(name, 0.0)
            until = time.monotonic() + self.cooldown_seconds
            self.cooldown[name] = max(previous, until)
        return True

    def note_success(self, name: str) -> None:
        with self._lock:
            self.cooldown.pop(name, None)

    def wait_seconds(self, name: str, *, now: float | None = None) -> float:
        return self.blocked(name, now=now)
