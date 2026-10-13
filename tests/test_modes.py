"""Operation mode and instrument activation (core/modes.py)."""

import json

from heliumrecovery.core.modes import ModeController, OperationMode


def test_transfer_returns_to_the_previous_mode() -> None:
    seen = []
    m = ModeController(inactive=("atl2",))
    m.on_change = lambda old, new, why: seen.append((old, new))
    m.set(OperationMode.AGENT)
    m.start_transfer()
    assert m.transfer and m.before_transfer == OperationMode.AGENT
    m.end_transfer()
    assert m.mode == OperationMode.AGENT
    m.end_transfer()  # not in transfer: nothing happens
    assert seen == [(OperationMode.OBSERVER, OperationMode.AGENT), (OperationMode.AGENT, OperationMode.TRANSFER),
                    (OperationMode.TRANSFER, OperationMode.AGENT)]


def test_activation_and_persistence(tmp_path) -> None:
    path = tmp_path / "modes.json"
    m = ModeController(path, inactive=("atl2", "wei", "janis"))
    assert not m.active("janis") and m.active("atl1")
    m.set_active("janis", True)
    m.set_active("cmi", False)
    m.set(OperationMode.AGENT)
    m.start_transfer()
    again = ModeController(path, inactive=("atl2",))  # the saved toggles win over the default
    assert again.inactive == {"atl2", "wei", "cmi"}
    assert again.mode == OperationMode.TRANSFER and again.before_transfer == OperationMode.OBSERVER  # never Agent
    path.write_text(json.dumps({"mode": "agent"}), encoding="utf-8")
    assert ModeController(path).mode == OperationMode.OBSERVER


def test_unreadable_state_starts_as_observer(tmp_path) -> None:
    path = tmp_path / "modes.json"
    path.write_text("{not json", encoding="utf-8")
    m = ModeController(path, inactive=("wei",))
    assert m.mode == OperationMode.OBSERVER and m.inactive == {"wei"}
