"""Error tracking.

Per-kind failure counters plus the recent-error log shown in the Status tab's
"Error List". Counts are consecutive failures: a successful read clears them.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ..drivers.base import now


class ErrorKind(StrEnum):
    """Kinds of failure, each with its own consecutive-failure counter."""

    TCP_READ = "TCP read error"
    HTTP_READ = "HTTP read error"
    HTTP_POST = "HTTP post error"
    JANIS_READ = "Janis read error"
    SHEET_READ = "Sheet read error"
    VALVE_READ = "Valve read error"


@dataclass(frozen=True)
class ErrorState:
    kind: ErrorKind
    count: int = 0
    time: datetime | None = None
    message: str | None = None


@dataclass(frozen=True)
class LogEntry:
    time: datetime
    source: str
    message: str


class ErrorHandler:
    """Per-kind consecutive-error counters plus a bounded message log."""

    def __init__(self, log_size: int = 500) -> None:
        self._states: dict[ErrorKind, ErrorState] = {}
        self._log: deque[LogEntry] = deque(maxlen=log_size)
        self.init()

    def init(self) -> None:
        self._states = {k: ErrorState(k) for k in ErrorKind}
        self._log.clear()

    def set(self, kind: ErrorKind, message: str = "", source: str = "") -> ErrorState:
        """Record one failure."""
        old = self._states[kind]
        state = ErrorState(kind, old.count + 1, now(), message or old.message)
        self._states[kind] = state
        self._log.append(LogEntry(state.time, source or kind.value, message or kind.value))
        return state

    def clear(self, kind: ErrorKind) -> None:
        """Reset after a successful read."""
        if self._states[kind].count:
            self._states[kind] = ErrorState(kind, 0, now())

    def get(self, kind: ErrorKind) -> ErrorState:
        return self._states[kind]

    def list(self) -> list[ErrorState]:
        return list(self._states.values())

    def check(self, kind: ErrorKind, threshold: int = 0) -> bool:
        """True when more than `threshold` consecutive failures ."""
        return self._states[kind].count > threshold

    def log(self) -> list[LogEntry]:
        """Error List, oldest first."""
        return list(self._log)
