"""从解密缓存库里直接导出与某联系人的全部聊天记录（绕过网关 limit/超时）。

为什么不用网关：wechat.message.history 在大 limit 下会 15s 超时，
实测最多只能取回约 300 条；而库里实际有 383 条。直读解密缓存可拿全量。

缓存目录里的同一个会话表会有很多历史快照（每次解密写一份），
这里取「行数最多」的那一份，保证最完整。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
CACHE = r"__ROOT__\work\decrypted_db"

LOCAL_TYPE = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情",
    48: "位置", 49: "链接/文件/卡片", 50: "通话", 10000: "系统消息",
}


def decode_content(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raw = bytes(value)
    if not raw:
        return ""
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(raw).decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def pick_snapshot(table: str):
    best = None
    for p in glob.glob(os.path.join(CACHE, "*.db")):
        try:
            if os.path.getsize(p) < 8192:
                continue
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (table,)).fetchone():
                con.close()
                continue
            n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            con.close()
            if best is None or n > best[1]:
                best = (p, n)
        except Exception:
            pass
    return best


def render(mtype: str, content: str) -> str:
    text = content or ""
    if text.lstrip().startswith("<"):
        title = re.search(r"<title>(.*?)</title>", text, re.S)
        if title and title.group(1).strip():
            return f"[文件] {title.group(1).strip()}" if "<appmsg" in text else f"[链接] {title.group(1).strip()}"
        if "img aeskey" in text or "<img " in text:
            return "[图片]"
        if "<videomsg" in text:
            return "[视频]"
        if "<voipmsg" in text or "VoIPBubbleMsg" in text:
            d = re.search(r"通话时长\s*([0-9:]+)", text)
            return f"[通话] {d.group(1)}" if d else "[通话]"
        if "<emoji" in text:
            return "[表情]"
        if "<appmsg" in text:
            return "[链接/卡片]"
        if "<sysmsg" in text:
            return "[系统消息]"
        if "转账" in text or "<wcpayinfo" in text:
            return "[转账]"
        return f"[{mtype}]"
    if mtype in ("文本",):
        return text
    if mtype == "图片":
        return "[图片]"
    if mtype == "语音":
        return "[语音]"
    if mtype == "视频":
        return "[视频]"
    if mtype == "表情":
        return "[表情]"
    if mtype == "通话":
        return "[通话]"
    return f"[{mtype}] {text[:60]}"


def collect_all(table: str):
    """扫描所有缓存快照，按 (local_id, sort_seq) 去重合并。

    微信 4.x 会按时间把消息分片到 message_0 / message_1 / …；
    同一个会话的早期消息可能留在旧分片里，只取单个文件会漏。
    缓存目录里同一分片还有多个时间点的快照，合并即得并集。

    注意：Name2Id（real_sender_id → wxid）是**每个库各自一张**，
    必须在各自库内解析完发送者再合并，否则 rowid 会串号。
    """
    merged: dict[tuple, dict] = {}
    sources: list[tuple[str, int]] = []
    for p in sorted(glob.glob(os.path.join(CACHE, "*.db"))):
        try:
            if os.path.getsize(p) < 8192:
                continue
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (table,)).fetchone():
                con.close()
                continue
            try:
                local_map = {int(r[0]): str(r[1])
                             for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
            except sqlite3.DatabaseError:
                local_map = {}
            rows = con.execute(
                f'SELECT local_id, real_sender_id, create_time, local_type, '
                f'message_content, sort_seq FROM "{table}"'
            ).fetchall()
            new = 0
            for r in rows:
                key = (r["local_id"], r["sort_seq"])
                if key in merged:
                    continue
                merged[key] = {
                    "local_id": r["local_id"],
                    "sort_seq": r["sort_seq"],
                    "create_time": r["create_time"],
                    "local_type": r["local_type"],
                    "message_content": r["message_content"],
                    "sender_id": local_map.get(int(r["real_sender_id"] or 0), ""),
                }
                new += 1
            con.close()
            if new:
                sources.append((os.path.basename(p), new))
        except Exception:
            pass
    ordered = sorted(merged.values(),
                     key=lambda r: (r["sort_seq"] or 0, r["local_id"] or 0))
    return ordered, sources


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-wxid", default="wxid_EXAMPLE_FRIEND_A")
    ap.add_argument("--chat-name", default="好友A")
    ap.add_argument("--me-wxid", default=None, help="自己的 wxid（默认自动判定为除对方外出现最多者）")
    ap.add_argument("--out", default=r"__USERPROFILE__\Desktop\与好友A_聊天记录.txt")
    args = ap.parse_args()

    table = "Msg_" + hashlib.md5(args.chat_wxid.encode("utf-8")).hexdigest()
    print("table:", table)
    rows, sources = collect_all(table)
    if not rows:
        print("[abort] 缓存里找不到该会话表")
        return 2
    print(f"合并了 {len(sources)} 个快照，去重后 {len(rows)} 条")
    for name, n in sorted(sources, key=lambda x: -x[1])[:5]:
        print(f"   + {n:5d}  {name}")
    path = sources[0][0] if sources else "-"

    # 统计发送者出现次数，判定「我」
    counts: dict[str, int] = {}
    for r in rows:
        sid = r["sender_id"] or "?"
        counts[sid] = counts.get(sid, 0) + 1
    me = args.me_wxid
    if me is None:
        others = {k: v for k, v in counts.items() if k != args.chat_wxid and k != "?"}
        me = max(others, key=others.get) if others else None
    print("sender distribution:", sorted(counts.items(), key=lambda x: -x[1])[:6])
    print("me =", me)

    lines = [
        f"# 与 {args.chat_name} 的聊天记录（全量·数据库直读）",
        f"# 导出时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}",
        f"# 会话 wxid：{args.chat_wxid}",
        f"# 数据来源：解密缓存快照 {os.path.basename(path)}",
        f"# 总条数：{len(rows)}",
    ]
    if rows:
        t0 = datetime.fromtimestamp(int(rows[0]["create_time"]), tz=timezone.utc).astimezone(CST)
        t1 = datetime.fromtimestamp(int(rows[-1]["create_time"]), tz=timezone.utc).astimezone(CST)
        lines.append(f"# 时间范围：{t0:%Y-%m-%d %H:%M:%S} ~ {t1:%Y-%m-%d %H:%M:%S}")
    lines += ["=" * 78, ""]

    cur_day = None
    for r in rows:
        ct = int(r["create_time"] or 0)
        dt = datetime.fromtimestamp(ct, tz=timezone.utc).astimezone(CST)
        day = dt.strftime("%Y-%m-%d")
        if day != cur_day:
            lines += ["", f"───── {day} ─────"]
            cur_day = day
        sid = r["sender_id"] or ""
        if sid == args.chat_wxid:
            who = args.chat_name
        elif sid == me:
            who = "我"
        else:
            who = sid or "未知"
        ltype = int(r["local_type"] or 0)
        mtype = LOCAL_TYPE.get(ltype, f"type{ltype}")
        body = render(mtype, decode_content(r["message_content"]))
        body = body.replace("\r", "").replace("\n", " ⏎ ")
        lines.append(f"[{dt:%H:%M:%S}] {who}: {body}")

    out = Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已保存: {out}  ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
