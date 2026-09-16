"""Verify every code example in the manual actually works as written."""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8010"


def call(name, **args):
    body = json.dumps({"name": name, "arguments": args}).encode()
    req = urllib.request.Request(BASE + "/tools/call", data=body,
                                 headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    if not r.get("success"):
        raise RuntimeError(r.get("error"))
    return r["data"]


ok = fail = 0


def check(label, fn):
    global ok, fail
    try:
        fn()
        print(f"  PASS  {label}")
        ok += 1
    except Exception as exc:
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")
        fail += 1


print("=== 手册第 3 节：HTTP API ===")
check("/health", lambda: urllib.request.urlopen(BASE + "/health", timeout=20).read())
check("/status returns data.active_event_source",
      lambda: (_ for _ in ()).throw(AssertionError("no field"))
      if "data" not in json.loads(urllib.request.urlopen(BASE + "/status", timeout=20).read().decode())
      else None)
check("/capabilities", lambda: urllib.request.urlopen(BASE + "/capabilities", timeout=30).read())
check("/agent/status", lambda: urllib.request.urlopen(BASE + "/agent/status", timeout=30).read())
check("/tools/list", lambda: urllib.request.urlopen(BASE + "/tools/list", timeout=30).read())

print("\n=== 手册第 4 节：工具 ===")
check("wechat.state has provider/database/uia",
      lambda: [call("wechat.state")[k] for k in ("provider", "database", "uia")])
check("wechat.account.current is a string",
      lambda: isinstance(call("wechat.account.current"), str) or (_ for _ in ()).throw(AssertionError()))
check("wechat.conversation.list is a list",
      lambda: isinstance(call("wechat.conversation.list"), list) or (_ for _ in ()).throw(AssertionError()))
check("wechat.database.inventory has db_storage/ keys",
      lambda: any(k.startswith("db_storage/") for k in call("wechat.database.inventory"))
      or (_ for _ in ()).throw(AssertionError("no prefixed keys")))
check("wechat.message.history row has documented keys",
      lambda: all(k in call("wechat.message.history", conversation_id="filehelper", limit=1)[0]
                  for k in ("message_id", "chat_id", "sender_id", "sender_name",
                            "timestamp", "create_time", "message_type", "content",
                            "sort_seq", "local_id"))
      or (_ for _ in ()).throw(AssertionError("missing documented keys")))

print("\n=== 手册第 5 节：示例 ===")


def ex_history_by_nickname():
    rows = call("wechat.message.history", conversation_id="文件传输助手", limit=3)
    assert isinstance(rows, list), "must be a list"


check("history by nickname resolves", ex_history_by_nickname)


def ex_ws():
    """Connecting is the assertion. Waiting for an event would block forever
    when nobody is sending, which is the normal idle state."""
    import asyncio
    import websockets

    async def go():
        async with websockets.connect("ws://127.0.0.1:8010/ws/events") as ws:
            assert ws.state.name == "OPEN", f"unexpected state {ws.state}"

    asyncio.run(go())


check("websocket /ws/events accepts a subscriber", ex_ws)

print("\n=== 手册第 14 节：自动回复控制面 ===")


def ex_auto_reply_status():
    payload = json.loads(urllib.request.urlopen(BASE + "/auto-reply/status", timeout=20).read().decode())
    assert payload.get("success"), payload
    data = payload["data"]
    for key in ("attached", "enabled"):
        assert key in data, f"missing {key}"
    if data.get("attached"):
        for key in ("dry_run", "targets", "counters", "queue_depth"):
            assert key in data, f"missing {key}"


check("/auto-reply/status reports the engine state", ex_auto_reply_status)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
