import json, urllib.request

BASE = "http://127.0.0.1:8010"


def call(tool, **args):
    body = json.dumps({"name": tool, "arguments": args}).encode()
    req = urllib.request.Request(BASE + "/tools/call", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())


for cid in ["好友A", "session_item_好友A"]:
    print("=" * 70)
    print("conversation_id =", repr(cid))
    r = call("wechat.message.history", conversation_id=cid, limit=6)
    if not r.get("success"):
        print("ERR", r.get("error"))
        continue
    for m in r["data"]:
        print("  chat_id=%-32s sender_id=%-28s sender_name=%-10s type=%s" % (
            m.get("chat_id"), m.get("sender_id"), m.get("sender_name"), m.get("message_type")))
        print("     ", str(m.get("content"))[:80].replace("\n", " "))
