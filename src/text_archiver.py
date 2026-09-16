"""Text-only chat archive: fast, database-only, no filesystem scans.

Reads only the decrypted message databases and writes:

    <root>/
    +-- README.md      notes and stats
    +-- chat.md        full transcript, chronological
    +-- chat.txt       plain-text version
    +-- index.json     structured records

No media lookup, no directory walking, no speech-to-text. Media messages are
represented as one-line markers so nothing silently disappears from the log.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import zstandard


def _decode_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raw = bytes(value)
    if not raw:
        return ""
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        try:
            return zstandard.ZstdDecompressor().decompress(raw).decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _ts(v: Optional[int]) -> str:
    if not v:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(v)))
    except (ValueError, OSError):
        return ""


def _day(v: Optional[int]) -> str:
    if not v:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(v)))
    except (ValueError, OSError):
        return ""


@dataclass
class TextStats:
    total: int = 0
    by_type: dict = field(default_factory=dict)
    date_first: str = ""
    date_last: str = ""
    errors: list = field(default_factory=list)


class TextArchiver:
    """Database-only transcript writer. One pass, no media resolution."""

    def __init__(self, adapter, chat_username: str, chat_display: str, out_root: Path):
        self.adapter = adapter
        self.user = chat_username
        self.display = chat_display
        self.out = Path(out_root)
        self.out.mkdir(parents=True, exist_ok=True)
        self.stats = TextStats()
        self.rows: list[dict] = []

    def _self_wxid(self) -> str:
        """本机登录账号的 wxid。

        账号目录形如 ``wxid_EXAMPLE_ACCOUNT``：真正的 wxid 在最后一个
        ``_`` 之前。用 ``split('_')[0]`` 只会得到 ``wxid``，导致自己的消息
        无法被识别。
        """
        raw = str(self.adapter.current_account() or "")
        if raw.startswith("wxid_"):
            head, sep, tail = raw.rpartition("_")
            if sep and head and tail and len(tail) <= 8:
                return head
        return raw

    def _name_map(self) -> dict:
        """username -> 显示名。一次性载入，避免每条消息都开一次联系人库。

        contact 表可能有几千行；逐条 get_nickname() 会重复打开并解密同一个
        数据库，是归档变慢的主要来源。
        """
        if getattr(self, "_names", None) is not None:
            return self._names
        names: dict[str, str] = {}
        try:
            conn = self.adapter._open_readonly("db_storage/contact/contact.db")
        except Exception:
            self._names = names
            return names
        try:
            for r in conn.execute(
                "SELECT username, remark, nick_name FROM contact"
            ):
                u = str(r["username"] or "")
                if u:
                    names[u] = str(r["remark"] or r["nick_name"] or u)
        except Exception as exc:
            self.stats.errors.append(f"contact map: {type(exc).__name__}: {exc}")
        finally:
            conn.close()
        self._names = names
        return names

    def load(self) -> None:
        table = "Msg_" + hashlib.md5(self.user.encode("utf-8")).hexdigest()
        me = self._self_wxid()
        names = self._name_map()
        rows: list[dict] = []
        for db_name in self.adapter._message_db_names():
            try:
                conn = self.adapter._open_readonly(db_name)
            except Exception:
                continue
            try:
                if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    continue
                sender_map = self.adapter._sender_map(conn)
                cur = conn.execute(
                    f'SELECT local_id, server_id, local_type, real_sender_id, create_time, '
                    f'message_content, sort_seq FROM "{table}"'
                )
                for r in cur:
                    sid = sender_map.get(int(r["real_sender_id"] or 0)) or ""
                    if sid and sid == me:
                        who = "我"
                    elif sid and sid == self.user:
                        who = self.display
                    else:
                        who = "我" if not sid else (names.get(sid) or sid)
                    rows.append({
                        "local_id": r["local_id"],
                        "local_type": r["local_type"],
                        "create_time": r["create_time"],
                        "sort_seq": r["sort_seq"],
                        "sender_id": sid or None,
                        "sender": who,
                        "content": _decode_content(r["message_content"]),
                    })
            except Exception as exc:
                self.stats.errors.append(f"{db_name}: {type(exc).__name__}: {exc}")
            finally:
                conn.close()
        rows.sort(key=lambda x: (x.get("sort_seq") or 0, x.get("local_id") or 0))
        self.rows = rows
        self.stats.total = len(rows)
        if rows:
            self.stats.date_first = _day(rows[0]["create_time"])
            self.stats.date_last = _day(rows[-1]["create_time"])

    @staticmethod
    def _render(row: dict) -> Optional[str]:
        """Return a one-line representation, or None to skip entirely."""
        base = int(row["local_type"]) & 0xFFFFFFFF
        c = row["content"] or ""
        if base == 1:
            return c
        if base == 3:
            return "[图片]"
        if base == 34:
            m = re.search(r'voicelength="(\d+)"', c)
            return f"[语音 {int(m.group(1))/1000:.1f}s]" if m else "[语音]"
        if base == 43:
            return "[视频]"
        if base == 47:
            return "[表情]"
        if base == 48:
            return "[位置]"
        if base == 50:
            m = re.search(r"<duration>(\d+)</duration>", c)
            return f"[通话 {int(m.group(1)) if m else 0}s]"
        if base == 10000:
            return f"[系统] {c[:80]}" if c else "[系统]"
        if base in (49,):
            sub = re.search(r"<type>(\d+)</type>", c)
            st = sub.group(1) if sub else ""
            LABELS = {"6": "文件", "5": "链接", "57": "引用", "33": "小程序",
                      "36": "小程序", "44": "红包", "2000": "转账", "2001": "红包",
                      "19": "聊天记录", "24": "笔记", "17": "转发", "87": "公告"}
            label = LABELS.get(st)
            # 转账/红包的 title 里常是 <![CDATA[...]]>，必须剥掉再取文本。
            raw_title = re.search(r"<title>(.*?)</title>", c, re.S)
            title = html.unescape(raw_title.group(1)) if raw_title else ""
            title = re.sub(r"<!\[CDATA\[|\]\]>", "", title).strip()
            title = title.replace("\n", " ")[:60]
            if label is None:
                if "<wcpayinfo>" in c or "转账" in title:
                    label = "转账"
                elif "红包" in title or "<hongbao" in c.lower():
                    label = "红包"
                else:
                    label = "应用消息"
            return f"[{label}] {title}" if title and title != label else f"[{label}]"
        # high-bit flagged variants route through the adapter's classifier
        kind = row.get("_kind") or ""
        if kind and kind != "text":
            return f"[{kind}]"
        return f"[未知类型 {base}]"

    def write(self) -> TextStats:
        for row in self.rows:
            row["_kind"] = self.adapter._classify_local_type(row["local_type"], row["content"])

        md = [f"# 与 {self.display} 的聊天记录", "",
              f"- 会话 ID: `{self.user}`",
              f"- 消息数: {self.stats.total}",
              f"- 时间范围: {self.stats.date_first} ~ {self.stats.date_last}",
              "", "---", ""]
        txt = [f"与 {self.display} 的聊天记录",
               f"消息数: {self.stats.total}",
               f"时间范围: {self.stats.date_first} ~ {self.stats.date_last}",
               "=" * 60, ""]
        last_day = None
        for row in self.rows:
            line = self._render(row)
            if line is None:
                continue
            d = _day(row["create_time"])
            t = _ts(row["create_time"])[11:19]
            base = int(row["local_type"]) & 0xFFFFFFFF
            kind = row["_kind"]
            self.stats.by_type[kind] = self.stats.by_type.get(kind, 0) + 1
            if d != last_day:
                md.append(f"\n## {d}\n")
                txt.append(f"\n----- {d} -----\n")
                last_day = d
            md.append(f"**{row['sender']}** `{t}`  {line}\n")
            txt.append(f"[{t}] {row['sender']}: {line}")
            row["time_str"] = _ts(row["create_time"])
            row["day"] = d
            row["kind"] = kind
            row["text"] = line

        (self.out / "chat.md").write_text("\n".join(md), encoding="utf-8")
        (self.out / "chat.txt").write_text("\n".join(txt), encoding="utf-8")

        index = [{
            "local_id": r["local_id"],
            "time": r.get("time_str"),
            "day": r.get("day"),
            "sender": r["sender"],
            "sender_id": r.get("sender_id"),
            "kind": r.get("kind"),
            "text": r.get("text"),
        } for r in self.rows]
        (self.out / "index.json").write_text(
            json.dumps({"chat": self.user, "display": self.display,
                        "count": len(index), "messages": index},
                       ensure_ascii=False, indent=1), encoding="utf-8")

        s = self.stats
        L = [f"# {self.display} 聊天归档（纯文本）", "",
             f"- 微信账号: `{self.adapter.current_account()}`",
             f"- 会话 ID: `{self.user}`",
             f"- 消息总数: **{s.total}**",
             f"- 时间范围: {s.date_first} ~ {s.date_last}", "",
             "## 内容统计", "", "| 类型 | 数量 |", "|---|---|"]
        for k, v in sorted(s.by_type.items(), key=lambda x: -x[1]):
            L.append(f"| {k} | {v} |")
        L += ["", "## 文件", "",
              "- `chat.md` — Markdown 全文，按日期分组",
              "- `chat.txt` — 纯文本全文，便于直接阅读或检索",
              "- `index.json` — 结构化记录（时间/发送方/类型/文本）", "",
              "## 说明", "",
              "本归档只包含文本内容。图片、语音、视频、文件等媒体以一行标记表示",
              "（如 `[图片]`、`[语音 4.8s]`），媒体原件未包含在内。", ""]
        if s.errors:
            L += ["## 处理过程中的错误", "", "```"] + s.errors[:50] + ["```", ""]
        (self.out / "README.md").write_text("\n".join(L), encoding="utf-8")
        (self.out / "stats.json").write_text(
            json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
        return self.stats
