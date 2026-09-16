"""发送一个文件到已锁定目标：前置校验 → 发送 → 数据库落库验证。

用法:
    python scripts\\send_file_verified.py --path "C:\\...\\x.docx"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.interactive_ipc import rpc  # noqa: E402

BASE = "http://127.0.0.1:8010"
DEFAULT_CHAT = "好友A"
DEFAULT_WXID = "wxid_EXAMPLE_FRIEND_A"


def post(path: str, body: dict, timeout: float = 180.0) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def history(chat: str, limit: int) -> list:
    r = post("/tools/call", {"name": "wechat.message.history",
                             "arguments": {"conversation_id": chat, "limit": limit}})
    return r.get("data") or []


def ids(rows) -> set:
    return {str(m.get("message_id") or m.get("local_id")) for m in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default=DEFAULT_CHAT)
    ap.add_argument("--wxid", default=DEFAULT_WXID)
    ap.add_argument("--path", required=True)
    ap.add_argument("--kind", choices=["file", "image"], default="file",
                    help="file = 以文件发送；image = 以图片发送（聊天里直接显示）")
    args = ap.parse_args()

    p = Path(args.path)
    if not p.is_file():
        print(f"[abort] 文件不存在: {p}")
        return 2
    print(f"[file] {p}  ({p.stat().st_size} bytes)")

    current = rpc("get_current_chat", timeout=30.0)
    print(f"[pre-flight] current_chat = {current!r}")
    if current not in {args.chat, args.wxid}:
        print(f"[abort] 当前会话 {current!r} != 目标 {args.chat!r}，不发送")
        return 3

    before = ids(history(args.chat, 8))
    req_id = f"{args.kind}-{int(time.time())}"
    action = "wechat.message.send_image" if args.kind == "image" else "wechat.message.send_file"
    resp = post("/control/execute", {
        "request_id": req_id,
        "action": action,
        "target": {"conversation_id": args.chat},
        "payload": {"path": str(p)},
        "timeout_ms": 90000,
    })
    result = (resp or {}).get("result") or {}
    print(f"[send] ok={resp.get('ok')} state={result.get('result_state')} "
          f"executed={result.get('executed')} sent={result.get('sent')} "
          f"recipient={result.get('recipient')}")
    if result.get("error"):
        print(f"[send] error={result['error']}")

    hit = False
    t0 = time.time()
    while time.time() - t0 < 60:
        time.sleep(3)
        rows = history(args.chat, 12)
        new = [m for m in rows if str(m.get("message_id") or m.get("local_id")) not in before]
        print(f"[verify] +{int(time.time() - t0):2d}s  new rows: {len(new)}")
        for m in new:
            print(f"   {m.get('message_id')} {m.get('timestamp')} "
                  f"{m.get('sender_name')} | {str(m.get('content'))[:70]!r}")
        hit = False
        for m in new:
            content = str(m.get("content", ""))
            mtype = str(m.get("message_type", ""))
            if args.kind == "image":
                hit = hit or ("img aeskey" in content) or ("图片" in mtype)
            else:
                hit = hit or (p.name in content) or (p.stem in content)
        if hit:
            break

    print(f"\n[result] 数据库确认文件已送达: {hit}")
    return 0 if hit else 4


if __name__ == "__main__":
    raise SystemExit(main())
