"""System operation mode (header indicator) and per-instrument activation.

Operation modes:

    OBSERVER  the program reads everything and changes nothing by itself
              (operators still use the write controls as before)
    TRANSFER  a helium transfer is in progress: entered when Transfer Step 1 is
              opened, left when Step 3 finishes. ATL "not responding" alerts are
              ignored meanwhile (ATLs are unplugged for the transfer)
    AGENT     the program may change settings by itself (the former "Guardian
              mode", docs/slack_alerts_and_guardian.md §3)

TRANSFER remembers the mode it interrupted and returns to it. AGENT never
survives a restart: the program always comes back as OBSERVER.

Activation: an instrument switched off (e.g. under engineering) gets no alerts
and its controls are greyed out. Both are kept in ``state/modes.json``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path

log = logging.getLogger(__name__)


class OperationMode(StrEnum):
    OBSERVER = "observer"
    TRANSFER = "transfer"
    AGENT = "agent"

    @property
    def title(self) -> str:
        return self.value.capitalize()


# instruments that can be switched off (keys as in services.alerts.DEVICE_NAMES)
DEVICES = ("pump", "atl1", "atl2", "atl3", "janis", "cmi", "wei", "valve")


class ModeController:
    def __init__(self, path: Path | None = None, inactive: Iterable[str] = ()) -> None:
        self.path = path
        self.mode = OperationMode.OBSERVER
        self.before_transfer = OperationMode.OBSERVER
        self.inactive: set[str] = set(inactive)
        # (old, new, reason) on every mode change; the alert manager posts it
        self.on_change: Callable[[OperationMode, OperationMode, str], None] | None = None
        self._load()

    # ------------------------------------------------------------------ modes
    def set(self, mode: OperationMode, reason: str = "set from the header") -> None:
        old = self.mode
        if mode == old:
            return
        if mode == OperationMode.TRANSFER:
            self.before_transfer = old
        self.mode = mode
        log.warning("operation mode %s -> %s (%s)", old.value, mode.value, reason)
        self._save()
        if self.on_change is not None:
            try:
                self.on_change(old, mode, reason)
            except Exception:
                log.exception("mode change callback failed")

    def start_transfer(self) -> None:
        self.set(OperationMode.TRANSFER, "Transfer Step 1 opened")

    def end_transfer(self, reason: str = "Transfer Step 3 finished") -> None:
        if self.mode == OperationMode.TRANSFER:
            self.set(self.before_transfer, reason)

    @property
    def transfer(self) -> bool:
        return self.mode == OperationMode.TRANSFER

    @property
    def agent(self) -> bool:
        return self.mode == OperationMode.AGENT

    # ------------------------------------------------------------- activation
    def active(self, device: str) -> bool:
        return device not in self.inactive

    def set_active(self, device: str, on: bool) -> None:
        if on == self.active(device):
            return
        (self.inactive.discard if on else self.inactive.add)(device)
        log.warning("instrument %s %s", device, "activated" if on else "deactivated")
        self._save()

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        if self.path is None:
            return
        data = {"mode": self.mode.value, "before_transfer": self.before_transfer.value,
                "inactive": sorted(self.inactive)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("mode state save failed: %s", exc)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            mode = OperationMode(data.get("mode", "observer"))
            before = OperationMode(data.get("before_transfer", "observer"))
        except (OSError, ValueError) as exc:
            log.warning("mode state unreadable, starting as observer: %s", exc)
            return
        agent_off = OperationMode.OBSERVER  # Agent never comes back on by itself
        self.mode = agent_off if mode == OperationMode.AGENT else mode
        self.before_transfer = agent_off if before == OperationMode.AGENT else before
        if "inactive" in data:
            self.inactive = {d for d in data["inactive"] if isinstance(d, str)}
