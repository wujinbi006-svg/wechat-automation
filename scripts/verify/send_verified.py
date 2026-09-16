"""Send one text message to a verified target, with pre-flight and post-verify.

Pre-flight  : current chat MUST equal the target (manual section 6).
Send        : gateway /control/execute -> wechat.message.send_text
Post-verify : read the message back from the database (send receipt is not
              observable, so the DB is the only ground truth).
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, r"__ROOT__")
from src.interactive_ipc import rpc  # noqa: E402

BASE = "http://127.0.0.1:8010"
TARGET = "好友A"
TEXT = "我喜欢你呀"


def post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode("utf-8"))


def history(cid, limit):
    r = post("/tools/call", {"name": "wechat.message.history",
                             "arguments": {"conversation_id": cid, "limit": limit}})
    return r.get("data") or []


# --- 1. pre-flight -------------------------------------------------------
current = rpc("get_current_chat", timeout=30.0)
print(f"[pre-flight] current_chat = {current!r}")
if current != TARGET:
    print(f"[ABORT] current chat is {current!r}, expected {TARGET!r}")
    sys.exit(2)

before = history(TARGET, 5)
before_ids = {str(m.get("message_id") or m.get("local_id")) for m in before}
print(f"[pre-flight] last message before send: "
      f"{before[0].get('timestamp') if before else None} "
      f"{str(before[0].get('content'))[:40] if before else ''}")

# --- 2. send -------------------------------------------------------------
req_id = f"send-friendA-{int(time.time())}"
print(f"[send] request_id={req_id} text={TEXT!r}")
resp = post("/control/execute", {
    "request_id": req_id,
    "action": "wechat.message.send_text",
    "target": {"conversation_id": TARGET},
    "payload": {"text": TEXT},
    "timeout_ms": 60000,
})
print("[send] response:")
print(json.dumps(resp, ensure_ascii=False, indent=1)[:2000])

# --- 3. post-verify ------------------------------------------------------
# The WAL hook reaches the database a few seconds AFTER the UIA send returns,
# so a single immediate read is a false negative. Poll instead.
hit = False
deadline = time.time() + 60
new = []
while time.time() < deadline:
    time.sleep(3)
    after = history(TARGET, 8)
    after_ids = {str(m.get("message_id") or m.get("local_id")) for m in after}
    new = [m for m in after
           if str(m.get("message_id") or m.get("local_id")) not in before_ids]
    print(f"[verify] +{int(time.time() - (deadline - 60))}s  new rows in DB: {len(new)}")
    for m in new:
        print(f"   {m.get('timestamp')} | {m.get('sender_name')} | "
              f"{m.get('message_type')} | {str(m.get('content'))[:50]!r}")
    hit = any(str(m.get("content", "")).strip() == TEXT for m in new)
    if hit:
        break

print(f"\n[verify] DB 中确认落库 {TEXT!r} : {hit}")
if not hit:
    print("[warn] gateway reported SENT_VERIFIED via UIA, but the database row "
          "did not appear within 60s -- check the WAL hook / 18011 sidecar.")
sys.exit(0 if hit else 3)
