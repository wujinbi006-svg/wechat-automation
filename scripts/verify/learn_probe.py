import json, urllib.request

BASE = "http://127.0.0.1:8010"


def call(name, **args):
    body = json.dumps({"name": name, "arguments": args}).encode()
    req = urllib.request.Request(BASE + "/tools/call", data=body,
                                 headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    if not r.get("success"):
        raise RuntimeError(r.get("error"))
    return r["data"]


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=30).read().decode())


st = get("/status")
d = st.get("data", st)
print("=== active_event_source ===")
print(d.get("active_event_source"))
print("=== event_sources ===")
print(json.dumps(d.get("event_sources"), ensure_ascii=False, indent=1)[:1200])

print("\n=== conversation.list ===")
for c in call("wechat.conversation.list"):
    print(" ", c)

print("\n=== history: 好友B (last 5) ===")
for m in call("wechat.message.history", conversation_id="好友B", limit=5):
    print(" ", m["timestamp"], m["sender_name"], "|", str(m["content"])[:60])

print("\n=== history: filehelper (last 3) ===")
for m in call("wechat.message.history", conversation_id="filehelper", limit=3):
    print(" ", m["timestamp"], m["sender_name"], "|", str(m["content"])[:60])
