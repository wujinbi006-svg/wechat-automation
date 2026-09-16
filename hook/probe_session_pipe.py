"""Diagnose the Session 0 <-> Session 1 named-pipe boundary.

A named pipe created by a Session 0 service is not connectable from Session 1
(Win32 error 5, access denied) under the default security descriptor. This
probe establishes which direction works, so the WAL transport can be chosen
based on measurement rather than assumption.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

k32.CreateNamedPipeW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.c_void_p]
k32.CreateNamedPipeW.restype = ctypes.c_void_p
k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k32.ConnectNamedPipe.restype = wintypes.BOOL
k32.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p]
k32.CreateFileW.restype = ctypes.c_void_p
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]

INVALID = ctypes.c_void_p(-1).value
PIPE_ACCESS_INBOUND = 0x00000001
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
ERROR_PIPE_CONNECTED = 535


def session_id() -> int:
    pid = os.getpid()
    sid = wintypes.DWORD(0)
    k32.ProcessIdToSessionId(pid, ctypes.byref(sid))
    return int(sid.value)


def probe(name: str) -> dict:
    """Create a server pipe, then connect to it as a client from this session."""
    result = {"pipe": name, "server_created": False, "client_connect": None,
              "error": None, "session": session_id()}
    h = k32.CreateNamedPipeW(name, PIPE_ACCESS_DUPLEX,
                             PIPE_TYPE_BYTE | PIPE_WAIT, 1, 1 << 16, 1 << 16, 0, None)
    if h == INVALID:
        result["error"] = f"CreateNamedPipe err={ctypes.get_last_error()}"
        return result
    result["server_created"] = True

    connected = threading.Event()
    got = {"err": None}

    def accept():
        ok = k32.ConnectNamedPipe(h, None)
        if not ok:
            got["err"] = ctypes.get_last_error()
        connected.set()

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    time.sleep(0.3)

    c = k32.CreateFileW(name, GENERIC_WRITE | GENERIC_READ, 0, None,
                        OPEN_EXISTING, 0, None)
    if c == INVALID:
        result["client_connect"] = False
        result["error"] = f"client err={ctypes.get_last_error()}"
    else:
        result["client_connect"] = True
        k32.CloseHandle(c)
    connected.wait(timeout=2)
    if got["err"] and got["err"] != ERROR_PIPE_CONNECTED:
        result.setdefault("server_note", f"ConnectNamedPipe err={got['err']}")
    k32.CloseHandle(h)
    return result


if __name__ == "__main__":
    print("current session:", session_id())
    for candidate in (r"\\.\pipe\wxd_probe_a", r"\\.\pipe\wxd_probe_b"):
        print(probe(candidate))
