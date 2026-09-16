"""Send ONE text message to a verified target: pre-flight, send, DB read-back.

Pre-flight  : current chat MUST equal the target (manual section 6).
Send        : gateway /control/execute -> wechat.message.send_text
Post-verify : poll the database until the exact text lands. The WAL hook
              reaches the DB several seconds AFTER the UIA send returns, so a
              single immediate read is a false negative.

Usage:
    python scripts\\send_verified.py --text "你好"
    python scripts\\send_verified.py --text-file msg.txt
    python scripts\\send_verified.py --chat 好友A --wxid wxid_xxx --text "hi"
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


def post(path: str, body: dict, timeout: float = 150.0) -> dict:
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
    ap.add_argument("--text", default=None)
    ap.add_argument("--text-file", default=None)
    args = ap.parse_args()

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    elif args.text is not None:
        text = args.text
    else:
        print("[abort] 需要 --text 或 --text-file")
        return 2
    if not text:
        print("[abort] 内容为空")
        return 2

    # --- 1. pre-flight ----------------------------------------------------
    current = rpc("get_current_chat", timeout=30.0)
    print(f"[pre-flight] current_chat = {current!r}")
    if current not in {args.chat, args.wxid}:
        print(f"[abort] 当前会话是 {current!r}，目标 {args.chat!r}，不切会话、不发送")
        return 3

    before = history(args.chat, 8)
    before_ids = ids(before)
    print(f"[pre-flight] last = {before[0].get('timestamp') if before else None} "
          f"{str(before[0].get('content'))[:40] if before else ''}")
    print(f"[send] text =\n{text}\n" + "-" * 50)

    # --- 2. send ----------------------------------------------------------
    req_id = f"one-{int(time.time())}"
    resp = post("/control/execute", {
        "request_id": req_id,
        "action": "wechat.message.send_text",
        "target": {"conversation_id": args.chat},
        "payload": {"text": text},
        "timeout_ms": 60000,
    })
    result = (resp or {}).get("result") or {}
    print(f"[send] ok={resp.get('ok')} state={result.get('result_state')} "
          f"executed={result.get('executed')} sent={result.get('sent')} "
          f"recipient={result.get('recipient')}")

    # --- 3. post-verify ---------------------------------------------------
    hit, new = False, []
    t0 = time.time()
    while time.time() - t0 < 60:
        time.sleep(3)
        rows = history(args.chat, 10)
        new = [m for m in rows if str(m.get("message_id") or m.get("local_id")) not in before_ids]
        print(f"[verify] +{int(time.time() - t0):2d}s  new rows: {len(new)}")
        for m in new:
            print(f"   {m.get('message_id')} {m.get('timestamp')} "
                  f"{m.get('sender_name')} | {str(m.get('content'))[:60]!r}")
        hit = any(str(m.get("content", "")).strip() == text.strip() for m in new)
        if hit:
            break

    print(f"\n[result] DB 中确认落库: {hit}")
    return 0 if hit else 4


if __name__ == "__main__":
    raise SystemExit(main())
