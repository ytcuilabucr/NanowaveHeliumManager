"""main.py viewer: shows the service's page, never starts acquisition."""

import socket
import sys
import types

import pytest

from heliumrecovery import __main__ as cli
from heliumrecovery.viewer import service_up


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_service_up() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        assert service_up("127.0.0.1", srv.getsockname()[1])
    assert not service_up("127.0.0.1", _free_port(), timeout=0.2)


def _fake_webview(monkeypatch) -> list:
    windows = []
    mod = types.SimpleNamespace(create_window=lambda *a, **k: windows.append((a, k)), start=lambda: None)
    monkeypatch.setitem(sys.modules, "webview", mod)
    return windows


def test_view_opens_url_or_waiting_page_and_no_core(monkeypatch) -> None:
    monkeypatch.setattr(cli, "Core", lambda *a, **k: pytest.fail("viewer must not start acquisition"))
    windows = _fake_webview(monkeypatch)
    port = _free_port()
    cli.main(["view", "--port", str(port)])
    assert "Waiting for the Helium Recovery service" in windows[-1][1]["html"]
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        up = srv.getsockname()[1]
        cli.main(["view", "--port", str(up)])
    assert windows[-1][0][1] == f"http://127.0.0.1:{up}/"


def test_desktop_refuses_when_service_already_serves(monkeypatch) -> None:
    monkeypatch.setattr(cli, "Core", lambda *a, **k: pytest.fail("must not start a second acquisition"))
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        with pytest.raises(SystemExit, match="already serves"):
            cli.main(["desktop", "--port", str(srv.getsockname()[1])])
