"""wxwal injection daemon.

Keeps the hook present in the WeChat process across restarts:

  - waits for the WeChat main process to appear
  - checks whether wxwal is already loaded in that process (module enumeration,
    not a proxy such as a log line or a file marker)
  - injects only when it is actually absent
  - repeats forever, so a WeChat restart is picked up automatically

LoadLibraryW on an already-loaded module is a no-op that returns the existing
handle, but relying on that is not enough: a NEW process (after a WeChat
restart) has no hook at all, which is the case this daemon exists to cover.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * MAX_PATH),
    ]


k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
k32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
k32.CloseHandle.argtypes = [ctypes.c_void_p]

HOOK_DLL_NAME = "wxwal10.dll"


def find_wechat_pid() -> int | None:
    """Main WeChat process: the Weixin.exe with no --type= on its command line."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | "
             "Where-Object { $_.CommandLine -notmatch '--type=' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    for token in out.stdout.split():
        if token.strip().isdigit():
            return int(token.strip())
    return None


def modules_of(pid: int) -> list[str]:
    """Names of modules loaded in the target process."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return []
    names: list[str] = []
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not k32.Module32FirstW(snap, ctypes.byref(entry)):
            return []
        while True:
            names.append(entry.szModule)
            if not k32.Module32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)
    return names


def is_injected(pid: int) -> bool:
    """True only when the hook module is actually present in that process."""
    return any(n.lower() == HOOK_DLL_NAME.lower() for n in modules_of(pid))


def inject(pid: int, dll_path: Path) -> bool:
    from inject import inject as _inject
    return _inject(pid, dll_path)


def log(msg: str, log_path: Path) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main() -> int:
    base = Path(__file__).resolve().parent
    dll = base / "build" / HOOK_DLL_NAME
    log_path = base / "build" / "daemon.log"
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    if not dll.exists():
        log(f"FATAL: {dll} not found", log_path)
        return 2

    log(f"daemon starting; watching for WeChat every {interval}s", log_path)
    last_pid: int | None = None
    last_state: bool | None = None

    while True:
        try:
            pid = find_wechat_pid()
            if pid is None:
                if last_pid is not None:
                    log("WeChat gone; waiting", log_path)
                    last_pid = None
                    last_state = None
                time.sleep(interval)
                continue

            injected = is_injected(pid)
            if pid != last_pid or injected != last_state:
                log(f"pid={pid} injected={injected}", log_path)
                last_pid, last_state = pid, injected

            if not injected:
                ok = inject(pid, dll)
                # verify against the module list rather than trusting the call
                time.sleep(2)
                verified = is_injected(pid)
                log(f"inject pid={pid} returned={ok} verified={verified}", log_path)
                last_state = verified
            time.sleep(interval)
        except KeyboardInterrupt:
            log("daemon stopped", log_path)
            return 0
        except Exception as exc:
            log(f"loop error: {type(exc).__name__}: {exc}", log_path)
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
