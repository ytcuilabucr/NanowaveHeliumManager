"""Detection of the NSSM service, which the Internal-tab "Restart service" button relies on."""

import os
import sys

import pytest

from heliumrecovery import service


def test_not_under_nssm_in_tests(monkeypatch) -> None:
    monkeypatch.delenv("HR_SERVICE", raising=False)
    assert service.under_nssm() is False


def test_env_flag_set_by_install_script(monkeypatch) -> None:
    monkeypatch.setenv("HR_SERVICE", "1")
    assert service.under_nssm() is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process list")
def test_process_list_has_this_process() -> None:
    procs = service._processes()
    assert os.getpid() in procs and procs[os.getpid()][1].lower().startswith("python")


def test_nssm_grandparent_detected(monkeypatch) -> None:
    monkeypatch.delenv("HR_SERVICE", raising=False)
    monkeypatch.setattr(service.sys, "platform", "win32")
    me = os.getpid()
    monkeypatch.setattr(service, "_processes", lambda: {me: (2, "python.exe"), 2: (3, "python.exe"),
                                                        3: (4, "nssm.exe"), 4: (0, "services.exe")})
    assert service.under_nssm() is True
    monkeypatch.setattr(service, "_processes", lambda: {me: (2, "python.exe"), 2: (0, "explorer.exe")})
    assert service.under_nssm() is False
