"""Live capture that records the source database path with every frame."""
from __future__ import annotations

import pickle
import sys
import threading
import time

sys.path.insert(0, r"__ROOT__\hook")
sys.path.insert(0, r"__ROOT__")

from wal_capture import WalCapture  # noqa: E402
import trigger_safe  # noqa: E402

OUT = r"__ROOT__\hook\build\frames2.pkl"

frames = []
lock = threading.Lock()


def on_frame(handle: int, page_no: int, data: bytes, path: str) -> None:
    with lock:
        frames.append({
            "handle": handle,
            "path": path,
            "page_no": page_no,
            "data": bytes(data),
        })


cap = WalCapture(page_size=4096, on_frame=on_frame)
cap.start()
time.sleep(1.5)
print("listener up; triggering (文件传输助手 only)", flush=True)

try:
    trigger_safe.open_safe()
    trigger_safe.send_safe("wal capture " + time.strftime("%H:%M:%S"))
    print("  send ok", flush=True)
except Exception as exc:
    print(f"  trigger failed: {type(exc).__name__}: {exc}", flush=True)

for _ in range(4):
    time.sleep(2)

s = cap.stats
print()
print(f"records   = {s.records}")
print(f"frames    = {s.frames}")
print(f"handles   = {len(s.handles)}")
print(f"malformed = {s.malformed}")
print()
print("=== frames per database ===")
per = {}
with lock:
    for f in frames:
        per[f["path"]] = per.get(f["path"], 0) + 1
for path, n in sorted(per.items(), key=lambda x: -x[1]):
    print(f"   {n:4d}  {path}")

with lock:
    if frames:
        pickle.dump(frames, open(OUT, "wb"))
        print(f"\nsaved {len(frames)} frames -> {OUT}")
cap.stop()
