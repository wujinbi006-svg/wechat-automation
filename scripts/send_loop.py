"""定时发送脚本：按固定间隔向一个已锁定目标重复发送文本。

设计约束（来自 WECHAT_AUTOMATION_MANUAL.md 第 6 节 / 第 13 节）：
  1. 每次发送前必须重新校验「当前打开的会话 == 目标」，不一致就中止 —— 绝不盲发。
  2. 目标同时用 备注名 与 wxid 双向确认，避免昵称撞车。
  3. 每次发送用唯一 request_id（网关按 request_id 幂等，重复 id 只执行一次）。
  4. 落库回执有时差：UIA 返回 SENT_VERIFIED 后 DB 要过几秒才出现，所以只做轻量
     计数，不在每轮里轮询数据库（太慢）；需要严格核对时用 --verify-every N。
  5. 支持 STOP 文件与 Ctrl+C 优雅停止，退出时打印汇总。

用法：
    python scripts\\send_loop.py                      # 默认：好友A / 我喜欢你 / 10s / 10 条
    python scripts\\send_loop.py --interval 10 --max 20
    python scripts\\send_loop.py --dry-run            # 只打印，不真发
    python scripts\\send_loop.py --wxid wxid_xxx --chat 某人
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.interactive_ipc import rpc  # noqa: E402

BASE = "http://127.0.0.1:8010"
STOP_FILE = REPO / "work" / "STOP_SEND"

DEFAULT_CHAT = "好友A"
DEFAULT_WXID = "wxid_EXAMPLE_FRIEND_A"   # 备注「好友A」/ 微信号 example_alias_a
DEFAULT_TEXT = "我喜欢你"


def post(path: str, body: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def history_ids(chat: str, limit: int = 6) -> set:
    r = post("/tools/call", {"name": "wechat.message.history",
                             "arguments": {"conversation_id": chat, "limit": limit}})
    return {str(m.get("message_id") or m.get("local_id")) for m in (r.get("data") or [])}


def current_chat() -> str | None:
    try:
        return rpc("get_current_chat", timeout=30.0)
    except Exception:
        return None


def open_and_confirm(chat: str, wxid: str) -> str | None:
    """开聊并确认成功；返回确认后的会话名，失败返回 None。"""
    try:
        rpc("open_chat", {"name": chat}, timeout=30.0)
    except Exception:
        pass  # 目标本来就开着时，导航可能「失败」，下面用当前会话判定
    cur = current_chat()
    if cur in {chat, wxid}:
        return cur
    return None


def send_once(chat: str, wxid: str, text: str, seq: int) -> dict:
    req_id = f"loop-{int(time.time())}-{seq}"
    resp = post("/control/execute", {
        "request_id": req_id,
        "action": "wechat.message.send_text",
        "target": {"conversation_id": chat},
        "payload": {"text": text},
        "timeout_ms": 60000,
    }, timeout=150.0)
    result = (resp or {}).get("result") or resp or {}
    return {
        "request_id": req_id,
        "ok": bool(resp.get("ok")),
        "state": result.get("result_state") or (resp.get("error") or {}).get("code"),
        "executed": result.get("executed"),
        "sent": result.get("sent"),
        "recipient": result.get("recipient"),
        "error": result.get("error") or resp.get("error"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="定时间隔发送脚本")
    ap.add_argument("--chat", default=DEFAULT_CHAT, help="会话备注名")
    ap.add_argument("--wxid", default=DEFAULT_WXID, help="该会话的 wxid（双重确认用）")
    ap.add_argument("--text", default=DEFAULT_TEXT, help="要发送的文本")
    ap.add_argument("--interval", type=float, default=10.0, help="间隔秒数（默认 10）")
    ap.add_argument("--max", type=int, default=10, help="最多发多少条；0 = 不限")
    ap.add_argument("--dry-run", action="store_true", help="只打印不真发")
    args = ap.parse_args()

    if args.interval < 1:
        print("[abort] interval 必须 >= 1 秒")
        return 2

    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    print("=" * 64)
    print(f"目标会话   : {args.chat}")
    print(f"目标 wxid  : {args.wxid}")
    print(f"发送内容   : {args.text!r}")
    print(f"间隔       : {args.interval}s")
    print(f"条数上限   : {'不限' if args.max == 0 else args.max}")
    print(f"dry-run    : {args.dry_run}")
    print(f"停止方式   : Ctrl+C  或  创建文件 {STOP_FILE}")
    print("=" * 64)

    cur = open_and_confirm(args.chat, args.wxid)
    if cur is None:
        print(f"[abort] 无法把会话切到 {args.chat!r}，当前是 {current_chat()!r}")
        return 3
    print(f"[pre-flight] current_chat = {cur!r}  ✅")

    seq = 0
    sent_ok = failed = 0
    started = time.time()

    try:
        while True:
            if args.max and seq >= args.max:
                print(f"[done] 已达上限 {args.max} 条")
                break
            if STOP_FILE.exists():
                print(f"[done] 检测到 {STOP_FILE.name}")
                break

            seq += 1
            tick = time.time()

            # —— 每轮都重新校验，绝不盲发 ——
            cur = current_chat()
            if cur not in {args.chat, args.wxid}:
                print(f"[guard] 当前会话变成 {cur!r}，尝试切回 {args.chat!r} …")
                cur = open_and_confirm(args.chat, args.wxid)
                if cur is None:
                    print(f"[abort] 会话已被切走且切不回（当前 {current_chat()!r}），"
                          f"已发 {sent_ok} 条后停止")
                    break

            if args.dry_run:
                print(f"[{seq:03d}] DRY-RUN  {args.text!r} -> {args.chat}")
                sent_ok += 1
            else:
                before = history_ids(args.chat)
                r = send_once(args.chat, args.wxid, args.text, seq)
                if r["sent"]:
                    sent_ok += 1
                    print(f"[{seq:03d}] {r['state']:22s} ok  id={r['request_id']}")
                else:
                    failed += 1
                    print(f"[{seq:03d}] {str(r['state']):22s} FAIL "
                          f"executed={r['executed']} error={r['error']}")

            # 从本轮开始算间隔，避免发送耗时叠加到周期上
            nxt = tick + args.interval
            remain = nxt - time.time()
            while remain > 0:
                if STOP_FILE.exists():
                    raise KeyboardInterrupt
                time.sleep(min(0.5, remain))
                remain = nxt - time.time()

    except KeyboardInterrupt:
        print("\n[stop] 已中断")

    dur = time.time() - started
    print("=" * 64)
    print(f"共尝试 {seq} 次 | 成功 {sent_ok} | 失败 {failed} | 耗时 {dur:.1f}s")
    print("提示：网关返回 SENT_VERIFIED 只代表 UIA 侧观测到消息；"
          "落库还要等 WAL hook 几秒。要严格核对请用 send_verified.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
