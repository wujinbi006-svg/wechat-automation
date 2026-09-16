"""Verify every Gateway tool and record its exact response shape.

Run this before and after any change: it is the ground truth for the manual,
and it catches a tool that silently regressed.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

BASE = "http://127.0.0.1:8010"

TOOLS = [
    ("wechat.state", {}),
    ("wechat.account.list", {}),
    ("wechat.account.current", {}),
    ("wechat.conversation.list", {}),
    ("wechat.conversation.search", {"name": "文件"}),
    ("wechat.contact.search", {"name": "文件"}),
    ("wechat.message.read", {"limit": 3}),
    ("wechat.message.latest", {}),
    ("wechat.message.history", {"conversation_id": "filehelper", "limit": 2}),
    ("wechat.database.inventory", {}),
    ("wechat.database.xinfo", {}),
    ("wechat.group.search", {"name": "群"}),
]


def call(name, args, timeout=120):
    body = json.dumps({"name": name, "arguments": args}).encode()
    req = urllib.request.Request(BASE + "/tools/call", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except Exception as exc:
        return {"ok": False, "err": f"{type(exc).__name__}: {exc}", "ms": int((time.time()-t0)*1000)}
    return {"ok": bool(r.get("success")), "data": r.get("data"),
            "err": (r.get("error") or {}).get("code"), "ms": int((time.time()-t0)*1000)}


def shape(d):
    if isinstance(d, list):
        return f"list[{len(d)}]"
    if isinstance(d, dict):
        return "dict{" + ",".join(list(d.keys())[:5]) + "}"
    return repr(d)[:40]


print(f"{'tool':34s} {'ok':4s} {'ms':>7s}  shape / error")
print("-" * 90)
fails = 0
for name, args in TOOLS:
    r = call(name, args)
    if r["ok"]:
        print(f"{name:34s} YES  {r['ms']:>7d}  {shape(r['data'])}")
    else:
        fails += 1
        print(f"{name:34s} NO   {r['ms']:>7d}  ERROR={r.get('err')} {str(r.get('err'))[:40]}")
print("-" * 90)
print(f"{len(TOOLS)-fails}/{len(TOOLS)} tools OK")
