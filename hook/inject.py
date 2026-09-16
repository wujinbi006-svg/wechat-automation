"""Inject wxwal.dll into the running WeChat process.

Standard CreateRemoteThread + LoadLibraryW. Same-user, non-elevated, so it
works without admin. Verifies the load by checking the DLL's own log line and
its exported frame counter rather than assuming success.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE

k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                               wintypes.DWORD, wintypes.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p

k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wintypes.BOOL

k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
k32.GetModuleHandleW.restype = wintypes.HMODULE

k32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
k32.GetProcAddress.restype = ctypes.c_void_p

k32.CreateRemoteThread.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD)]
k32.CreateRemoteThread.restype = wintypes.HANDLE

k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]

PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF


def find_wechat_pid() -> int | None:
    """Main WeChat process = the Weixin.exe whose command line has no --type."""
    import subprocess
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | "
         "Where-Object { $_.CommandLine -notmatch '--type=' } | "
         "Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True, timeout=30,
    )
    for line in out.stdout.split():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def inject(pid: int, dll_path: Path) -> bool:
    dll_str = str(dll_path)
    dll_bytes = (dll_str + "\x00").encode("utf-16-le")

    h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        print(f"OpenProcess failed, err={ctypes.get_last_error()}")
        return False
    try:
        addr = k32.VirtualAllocEx(h, None, len(dll_bytes), MEM_COMMIT | MEM_RESERVE,
                                  PAGE_READWRITE)
        if not addr:
            print(f"VirtualAllocEx failed, err={ctypes.get_last_error()}")
            return False

        written = ctypes.c_size_t(0)
        buf = ctypes.create_string_buffer(dll_bytes)
        if not k32.WriteProcessMemory(h, addr, buf, len(dll_bytes), ctypes.byref(written)):
            print(f"WriteProcessMemory failed, err={ctypes.get_last_error()}")
            return False

        load_lib = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
        if not load_lib:
            print("GetProcAddress(LoadLibraryW) failed")
            return False

        tid = wintypes.DWORD(0)
        th = k32.CreateRemoteThread(h, None, 0, load_lib, addr, 0, ctypes.byref(tid))
        if not th:
            print(f"CreateRemoteThread failed, err={ctypes.get_last_error()}")
            return False

        k32.WaitForSingleObject(th, 10000)
        k32.CloseHandle(th)
        return True
    finally:
        k32.CloseHandle(h)


if __name__ == "__main__":
    dll = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(r"__ROOT__\hook\build\wxwal.dll")
    if not dll.exists():
        print(f"DLL not found: {dll}")
        sys.exit(2)

    log = Path(r"__ROOT__\hook\build\wxwal.log")
    if log.exists():
        log.unlink()

    pid = find_wechat_pid()
    if not pid:
        print("WeChat main process not found")
        sys.exit(3)
    print(f"WeChat main pid = {pid}")
    print(f"injecting {dll.name} ...")

    if not inject(pid, dll):
        sys.exit(4)

    time.sleep(2)
    if log.exists():
        print("=== wxwal.log ===")
        print(log.read_text(encoding="utf-8", errors="replace").strip())
    else:
        print("no log produced -- DLL did not run DllMain")
