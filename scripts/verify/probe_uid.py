import json, urllib.request

BASE = "http://127.0.0.1:8010"


def call(tool, **args):
    body = json.dumps({"name": tool, "arguments": args}).encode()
    req = urllib.request.Request(BASE + "/tools/call", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())


for cid in ["example_alias_a", "好友A"]:
    print("=" * 70)
    print("resolve:", repr(cid))
    r = call("wechat.message.history", conversation_id=cid, limit=4)
    if not r.get("success"):
        print("  ERR", r.get("error"))
        continue
    for m in r["data"]:
        print("  chat_id=%-30s sender=%-8s name=%-8s %s" % (
            m.get("chat_id"), m.get("sender_id"), m.get("sender_name"),
            str(m.get("content"))[:50].replace("\n", " ")))

print("\n=== conversation.search example_alias_a ===")
print(json.dumps(call("wechat.conversation.search", **{"name": "example_alias_a"}),
                 ensure_ascii=False)[:600])
print("\n=== contact.search example_alias_a ===")
print(json.dumps(call("wechat.contact.search", **{"name": "example_alias_a"}),
                 ensure_ascii=False)[:600])
