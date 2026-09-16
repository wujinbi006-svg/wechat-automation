"""导出与某个联系人的全部聊天文本为 .txt。

数据源：Gateway wechat.message.history（数据库直读，非 UIA 可见部分）。
分页：用 before = 上一页最早一条的 sort_seq 向前翻，直到取空。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "http://127.0.0.1:8010"
CST = timezone(timedelta(hours=8))

TYPE_LABEL = {
    "text": None,           # 直接输出内容
    "文本": None,
    "图片": "[图片]",
    "语音": "[语音]",
    "视频": "[视频]",
    "文件": "[文件]",
    "文件/链接/卡片": "[文件/链接/卡片]",
    "表情": "[表情]",
    "系统": "[系统消息]",
}


def post(path: str, body: dict, timeout: float = 180.0) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def fetch_page(chat: str, limit: int, before=None) -> list:
    args = {"conversation_id": chat, "limit": limit}
    if before is not None:
        args["before"] = before
    r = post("/tools/call", {"name": "wechat.message.history", "arguments": args})
    if not r.get("success"):
        raise RuntimeError(r.get("error"))
    return r.get("data") or []


def readable(msg: dict) -> str:
    """把一条消息渲染成可读文本。"""
    mtype = str(msg.get("message_type") or "")
    content = msg.get("content")
    text = "" if content is None else str(content)

    # 原生类型映射
    if mtype in ("text", "文本"):
        return text

    # XML 类（图片/视频/文件/引用/通话/转账…）→ 提取标题或归类
    if text.lstrip().startswith("<"):
        title = re.search(r"<title>(.*?)</title>", text, re.S)
        if title and title.group(1).strip():
            tag = "[链接]" if "<url>" in text else "[文件]"
            return f"{tag} {title.group(1).strip()}"
        if "<img " in text or "img aeskey" in text:
            return "[图片]"
        if "<videomsg" in text:
            return "[视频]"
        if "<voipmsg" in text or "VoIPBubbleMsg" in text:
            dur = re.search(r"通话时长\s*([0-9:]+)", text)
            return f"[通话] {dur.group(1)}" if dur else "[通话]"
        if "<emoji" in text:
            return "[表情]"
        if "<appmsg" in text:
            return "[链接/小程序]"
        if "<sysmsg" in text:
            return "[系统消息]"
        if "转账" in text or "<wcpayinfo" in text:
            return "[转账]"
        return f"[{mtype or '其他'}]"

    label = TYPE_LABEL.get(mtype)
    if label:
        return label
    return f"[{mtype}] {text[:60]}" if mtype else text


def norm_time(msg: dict) -> str:
    ts = msg.get("timestamp") or msg.get("create_time")
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return ts
    elif isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default="好友A")
    ap.add_argument("--out", default=r"__USERPROFILE__\Desktop\与好友A_聊天记录.txt")
    ap.add_argument("--page", type=int, default=300)
    args = ap.parse_args()

    seen = {}
    before = None
    page_no = 0
    while True:
        page_no += 1
        batch = fetch_page(args.chat, args.page, before)
        if not batch:
            break
        for m in batch:
            key = str(m.get("message_id") or m.get("local_id"))
            seen[key] = m
        oldest = min((m.get("sort_seq") or 0) for m in batch)
        print(f"page {page_no}: +{len(batch)}  (total {len(seen)}, oldest sort_seq={oldest})")
        if before is not None and oldest >= before:
            break  # 没有前进了，防止死循环
        before = oldest
        if len(batch) < args.page:
            break
        time.sleep(0.2)

    rows = sorted(seen.values(), key=lambda m: (m.get("sort_seq") or 0,
                                                int(m.get("local_id") or 0)))

    lines = []
    lines.append(f"# 与 {args.chat} 的聊天记录（数据库直读全量）")
    lines.append(f"# 导出时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"# 来源：WeChatGateway wechat.message.history")
    lines.append(f"# 总条数：{len(rows)}")
    if rows:
        lines.append(f"# 时间范围：{norm_time(rows[0])} ~ {norm_time(rows[-1])}")
    lines.append("=" * 78)
    lines.append("")

    cur_day = None
    for m in rows:
        t = norm_time(m)
        day = t[:10]
        if day != cur_day:
            lines.append("")
            lines.append(f"───── {day} ─────")
            cur_day = day
        sender = m.get("sender_name") or ""
        who = "我" if sender in ("10000@", "我", "") else sender
        body = readable(m).replace("\r", "")
        if "\n" in body:
            body = body.replace("\n", " ⏎ ")
        lines.append(f"[{t[11:]}] {who}: {body}")

    out = Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已保存: {out}  ({out.stat().st_size} bytes, {len(rows)} 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
