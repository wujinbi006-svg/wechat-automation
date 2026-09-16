import sys, time, threading, pickle
sys.path.insert(0, r"__ROOT__\hook")
sys.path.insert(0, r"__ROOT__")
from wal_capture import WalCapture
import trigger_safe

frames = []
lock = threading.Lock()
def on_frame(pno, data):
    with lock:
        frames.append((pno, bytes(data)))

cap = WalCapture(page_size=4096, on_frame=on_frame)
cap.start()
time.sleep(1.5)
print("listener up; triggering (文件传输助手 only)", flush=True)

try:
    trigger_safe.open_safe()
    trigger_safe.send_safe("wal capture test " + time.strftime("%H:%M:%S"))
    print("  send ok", flush=True)
except Exception as e:
    print("  trigger failed:", type(e).__name__, e, flush=True)

for _ in range(20):
    time.sleep(1)
    if cap.stats.frames:
        break

s = cap.stats
print()
print(f"records   = {s.records}")
print(f"bytes     = {s.bytes}")
print(f"frames    = {s.frames}")
print(f"handles   = {len(s.handles)}")
print(f"malformed = {s.malformed}")
with lock:
    if frames:
        pickle.dump(frames, open(r"__ROOT__\hook\build\frames.pkl","wb"))
        print(f"saved {len(frames)} frames")
        print("first:", [(p, len(d)) for p, d in frames[:8]])
cap.stop()
