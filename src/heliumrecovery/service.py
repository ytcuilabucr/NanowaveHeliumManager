"""Running under the "HeliumRecovery" Windows service (NSSM)?

The Internal-tab "Restart service" button only stops the program: NSSM
(``AppExit Default Restart``, scripts/install_service.ps1) starts it again after
``AppRestartDelay`` (10 s). That needs no administrator rights, but it only
works when NSSM is the parent process, so the button is offered only then.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

RESTART_DELAY_S = 10  # AppRestartDelay in install_service.ps1


class _Entry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _processes() -> dict[int, tuple[int, str]]:
    """{pid: (parent pid, exe name)} of all processes (Windows only)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        return {}
    out: dict[int, tuple[int, str]] = {}
    try:
        e = _Entry()
        e.dwSize = ctypes.sizeof(_Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def under_nssm() -> bool:
    """True if nssm.exe started this process (the venv python.exe may sit in between)."""
    if os.environ.get("HR_SERVICE") == "1":
        return True
    if sys.platform != "win32":
        return False
    try:
        procs = _processes()
    except OSError:
        return False
    pid = os.getpid()
    for _ in range(3):  # this python -> (venv launcher python) -> nssm
        parent = procs.get(pid)
        if parent is None:
            return False
        pid = parent[0]
        name = procs.get(pid, (0, ""))[1].lower()
        if name == "nssm.exe":
            return True
    return False
