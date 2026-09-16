"""定时间隔发送 · 同义轮换版。

每 10 秒发一条，内容从同义池里轮换 —— 意思是同一个意思（我喜欢你），
但每条字面都不同，避开「完全相同重复消息」这个最典型的风控特征。

安全约束与 send_loop.py 一致：
  1. 每轮重新校验「当前打开的会话 == 目标」，被切走就切回，切不回即中止。
  2. 目标用 备注名 + wxid 双重确认。
  3. 每次唯一 request_id（网关闭等，重试不会重复发）。
  4. 间隔从每轮开始计时，不含发送耗时。
  5. Ctrl+C 或 work/STOP_SEND 停止；每轮结果落 JSONL 日志。

用法：
    python scripts\\send_loop_varied.py                       # 10s 一条，不限条数
    python scripts\\send_loop_varied.py --max 30              # 只发 30 条
    python scripts\\send_loop_varied.py --interval 30         # 换 30 秒
    python scripts\\send_loop_varied.py --pool-file my.txt    # 自定义同义池（一行一条）
    python scripts\\send_loop_varied.py --random              # 随机取，不按顺序
    python scripts\\send_loop_varied.py --dry-run             # 只打印不真发
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.interactive_ipc import rpc  # noqa: E402

BASE = "http://127.0.0.1:8010"
STOP_FILE = REPO / "work" / "STOP_SEND"
LOG_FILE = REPO / "work" / "send_loop_log.jsonl"

DEFAULT_CHAT = "好友A"
DEFAULT_WXID = "wxid_EXAMPLE_FRIEND_A"   # 备注「好友A」/ 微信号 example_alias_a

# 同义池：全部是「我喜欢你」的不同说法，不夹带别的意思
DEFAULT_POOL = [
    "我喜欢你",
    "我喜欢你呀",
    "我好喜欢你",
    "喜欢你",
    "我特别喜欢你",
    "我真的很喜欢你",
    "我是真的喜欢你",
    "我可太喜欢你了",
    "我老喜欢你了",
    "我就喜欢你",
    "我喜欢你哦",
    "我喜欢你欸",
    "我喜欢你，超喜欢",
    "我喜欢你，特别喜欢",
    "我喜欢你，一直喜欢",
    "我喜欢你，认真的",
    "我喜欢你，不是开玩笑",
    "我喜欢你，藏不住的那种",
    "我喜欢你，忍不住想说",
    "我喜欢你，说多少遍都不够",
    "我喜欢你，今天也是",
    "我喜欢你，每天都喜欢",
    "我喜欢你，从很久以前开始",
    "我喜欢你，没理由",
    "我喜欢你，你懂的",
    "我喜欢你，宝",
    "喜欢你，喜欢你",
    "喜欢你，就是喜欢你",
    "好喜欢你啊",
    "好喜欢好喜欢你",
    "我超级喜欢你",
    "我果然还是喜欢你",
    "我喜欢你，反正就是喜欢",
    "我喜欢你，这句话是真的",
    "我喜欢你，一直都会",
]


def post(path: str, body: dict, timeout: float = 150.0) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def current_chat() -> str | None:
    try:
        return rpc("get_current_chat", timeout=30.0)
    except Exception:
        return None


def open_and_confirm(chat: str, wxid: str) -> str | None:
    try:
        rpc("open_chat", {"name": chat}, timeout=30.0)
    except Exception:
        pass  # 目标本来就开着时导航可能「失败」，下面用当前会话判定
    cur = current_chat()
    return cur if cur in {chat, wxid} else None


def send_once(chat: str, text: str, seq: int) -> dict:
    req_id = f"loopv-{int(time.time())}-{seq}"
    resp = post("/control/execute", {
        "request_id": req_id,
        "action": "wechat.message.send_text",
        "target": {"conversation_id": chat},
        "payload": {"text": text},
        "timeout_ms": 60000,
    })
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


def load_pool(path: str | None) -> list[str]:
    if not path:
        return list(DEFAULT_POOL)
    lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines()]
    pool = [ln for ln in lines if ln and not ln.startswith("#")]
    if not pool:
        raise SystemExit(f"[abort] 同义池为空: {path}")
    return pool


def main() -> int:
    ap = argparse.ArgumentParser(description="同义轮换 · 定时间隔发送")
    ap.add_argument("--chat", default=DEFAULT_CHAT)
    ap.add_argument("--wxid", default=DEFAULT_WXID)
    ap.add_argument("--pool-file", default=None, help="同义池文件，一行一条（# 开头为注释）")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--max", type=int, default=0, help="条数上限，0 = 不限（默认）")
    ap.add_argument("--random", action="store_true", help="随机取，不按顺序轮换")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.interval < 1:
        print("[abort] interval 必须 >= 1 秒")
        return 2

    pool = load_pool(args.pool_file)
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    print("=" * 66)
    print(f"目标会话   : {args.chat}   (wxid {args.wxid})")
    print(f"同义池     : {len(pool)} 条，{'随机' if args.random else '顺序轮换'}")
    print(f"间隔       : {args.interval}s")
    print(f"条数上限   : {'不限' if args.max == 0 else args.max}")
    print(f"dry-run    : {args.dry_run}")
    print(f"日志       : {LOG_FILE}")
    print(f"停止       : Ctrl+C  或  New-Item -ItemType File {STOP_FILE}")
    print("=" * 66)
    for i, t in enumerate(pool, 1):
        print(f"  {i:02d}. {t}")

    cur = open_and_confirm(args.chat, args.wxid)
    if cur is None:
        print(f"[abort] 无法把会话切到 {args.chat!r}，当前是 {current_chat()!r}")
        return 3
    print(f"[pre-flight] current_chat = {cur!r}  ✅\n")

    seq = sent_ok = failed = 0
    last_text = None
    rng = random.Random()
    started = time.time()

    try:
        while True:
            if args.max and seq >= args.max:
                print(f"[done] 已达上限 {args.max} 条")
                break
            if STOP_FILE.exists():
                print(f"[done] 检测到 {STOP_FILE.name}")
                break

            # 取一条与上一条不同的
            if args.random:
                cand = [t for t in pool if t != last_text] or pool
                text = rng.choice(cand)
            else:
                text = pool[seq % len(pool)]

            seq += 1
            last_text = text
            tick = time.time()

            cur = current_chat()
            if cur not in {args.chat, args.wxid}:
                print(f"[guard] 当前会话变成 {cur!r}，尝试切回 {args.chat!r} …")
                cur = open_and_confirm(args.chat, args.wxid)
                if cur is None:
                    print(f"[abort] 切不回目标（当前 {current_chat()!r}），已发 {sent_ok} 条后停止")
                    break

            if args.dry_run:
                print(f"[{seq:03d}] DRY-RUN  {text}")
                sent_ok += 1
            else:
                r = send_once(args.chat, text, seq)
                ok = bool(r["sent"])
                sent_ok += ok
                failed += (not ok)
                print(f"[{seq:03d}] {str(r['state']):22s} {'ok' if ok else 'FAIL'}  {text}")
                if not ok:
                    print(f"       error={r['error']} executed={r['executed']}")
                with LOG_FILE.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "seq": seq, "text": text, **r,
                    }, ensure_ascii=False) + "\n")

            nxt = tick + args.interval
            remain = nxt - time.time()
            while remain > 0:
                if STOP_FILE.exists():
                    raise KeyboardInterrupt
                time.sleep(min(0.5, remain))
                remain = nxt - time.time()

    except KeyboardInterrupt:
        print("\n[stop] 已中断")

    print("=" * 66)
    print(f"共尝试 {seq} 次 | 成功 {sent_ok} | 失败 {failed} | "
          f"耗时 {time.time() - started:.1f}s")
    print(f"日志: {LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
