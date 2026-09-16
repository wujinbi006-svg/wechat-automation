"""Safe activity trigger for hook testing.

Hard rules learned the hard way:
  - the ONLY target used here is 文件传输助手 (filehelper): the user's own
    device, no third party can ever see it
  - WeChat's send targets the *currently open* conversation, NOT a named one,
    so opening a chat and then sending silently redirects the message. This
    module therefore re-opens and re-verifies the target immediately before
    every send, and aborts if the current chat is not filehelper.
  - no real contact or group is ever opened from this script
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, r"__ROOT__")

from src.interactive_ipc import rpc  # noqa: E402

SAFE_TARGET = "文件传输助手"
FORBIDDEN_HINTS = ("群", "chatroom")


def assert_safe(name: str) -> None:
    """Refuse anything that is not the file-transfer helper."""
    if name != SAFE_TARGET:
        raise RuntimeError(f"refusing to target {name!r}: only {SAFE_TARGET!r} is allowed")
    for hint in FORBIDDEN_HINTS:
        if hint in name:
            raise RuntimeError(f"refusing: {name!r} looks like a group/contact")


def open_safe() -> None:
    assert_safe(SAFE_TARGET)
    rpc("interactive_open_chat", {"name": SAFE_TARGET}, timeout=45)
    time.sleep(1.5)
    cur = rpc("get_current_chat", timeout=15)
    # get_current_chat returns a bare name string in this build
    name = cur if isinstance(cur, str) else (cur or {}).get("name")
    if name != SAFE_TARGET:
        raise RuntimeError(f"current chat is {name!r}, expected {SAFE_TARGET!r}; aborting send")


def send_safe(text: str) -> bool:
    """Open the safe target, re-verify it, only then send."""
    open_safe()
    rpc("send_text", {"text": text}, timeout=45)
    return True


if __name__ == "__main__":
    print(f"triggering activity against {SAFE_TARGET} only", flush=True)
    for i in range(3):
        try:
            open_safe()
            print(f"  open+verify ok ({i+1}/3)", flush=True)
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}", flush=True)
            sys.exit(1)
        time.sleep(2)

    try:
        send_safe("wal hook probe " + time.strftime("%H:%M:%S"))
        print("  send ok", flush=True)
    except Exception as exc:
        print(f"  send FAILED: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
