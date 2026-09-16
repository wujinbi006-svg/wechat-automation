"""WeChat voice export: SILK -> playable WAV, plus Markdown/HTML linkage.

Reads VoiceInfo from the decrypted media_0.db and writes:

    <root>/
    +-- voice/                 <local_id>.wav  (playable)  + <local_id>.silk (original)
    +-- index.json             updated with media paths
    +-- chat.md / chat.txt     regenerated with audio links
    +-- voice.html             audio player page, grouped by date

No speech-to-text: the deliverable is the audio itself.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import time
import wave
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
class VoiceStats:
    messages: int = 0
    voice_messages: int = 0
    silk_found: int = 0
    wav_written: int = 0
    decode_failed: int = 0
    no_audio: int = 0
    total_seconds: float = 0.0
    errors: list = field(default_factory=list)


class VoiceExporter:
    """Attach playable audio to an existing text archive."""

    def __init__(self, adapter, chat_username: str, chat_display: str, out_root: Path):
        self.adapter = adapter
        self.user = chat_username
        self.display = chat_display
        self.out = Path(out_root)
        self.voice_dir = self.out / "voice"
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.stats = VoiceStats()

    def _self_wxid(self) -> str:
        raw = str(self.adapter.current_account() or "")
        if raw.startswith("wxid_"):
            head, sep, tail = raw.rpartition("_")
            if sep and head and tail and len(tail) <= 8:
                return head
        return raw

    def _name_map(self) -> dict:
        names: dict[str, str] = {}
        try:
            conn = self.adapter._open_readonly("db_storage/contact/contact.db")
        except Exception:
            return names
        try:
            for r in conn.execute("SELECT username, remark, nick_name FROM contact"):
                u = str(r["username"] or "")
                if u:
                    names[u] = str(r["remark"] or r["nick_name"] or u)
        except Exception:
            pass
        finally:
            conn.close()
        return names

    def _contact_db(self) -> Optional[str]:
        for n in self.adapter.discover_databases():
            if n.replace("\\", "/").endswith("contact/contact.db"):
                return n
        return None

    def _media_db(self) -> Optional[str]:
        for n in self.adapter.discover_databases():
            if n.replace("\\", "/").endswith("message/media_0.db"):
                return n
        return None

    # ---- message rows ----
    def _load_voice_rows(self) -> list[dict]:
        table = "Msg_" + hashlib.md5(self.user.encode("utf-8")).hexdigest()
        me = self._self_wxid()
        names = self._name_map()
        rows = []
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
                sm = self.adapter._sender_map(conn)
                for r in conn.execute(
                    f'SELECT local_id, server_id, local_type, real_sender_id, create_time, '
                    f'message_content, sort_seq FROM "{table}"'
                ):
                    if (int(r["local_type"]) & 0xFFFFFFFF) != 34:
                        continue
                    sid = sm.get(int(r["real_sender_id"] or 0)) or ""
                    who = "我" if sid == me else (names.get(sid) or sid or "对方")
                    content = _decode_content(r["message_content"])
                    m = re.search(r'voicelength="(\d+)"', content)
                    rows.append({
                        "local_id": r["local_id"],
                        "server_id": r["server_id"],
                        "create_time": r["create_time"],
                        "sort_seq": r["sort_seq"],
                        "sender": who,
                        "voice_ms": int(m.group(1)) if m else 0,
                    })
            finally:
                conn.close()
        rows.sort(key=lambda x: (x.get("sort_seq") or 0, x.get("local_id") or 0))
        return rows

    # ---- audio ----
    @staticmethod
    def _silk_to_wav(silk: bytes, target: Path) -> bool:
        try:
            import pysilk
        except ImportError:
            return False
        body = silk[1:] if silk[:1] == b"\x02" else silk
        buf = io.BytesIO()
        try:
            pysilk.decode(io.BytesIO(body), buf, 24000)
        except Exception:
            return False
        pcm = buf.getvalue()
        if not pcm:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        return True

    def run(self) -> VoiceStats:
        rows = self._load_voice_rows()
        self.stats.voice_messages = len(rows)
        print(f"[voice] {len(rows)} voice messages", flush=True)

        media_db = self._media_db()
        if media_db is None:
            self.stats.errors.append("media_0.db not found")
            return self.stats
        conn = self.adapter._open_readonly(media_db)
        try:
            # media_0.db 的 Name2Id 只有 user_name 一列；chat_name_id 对应
            # 它的 rowid，不是名为 chat_name_id 的列。
            cid_row = conn.execute(
                "SELECT rowid FROM Name2Id WHERE user_name=?", (self.user,)
            ).fetchone()
            chat_id = cid_row[0] if cid_row else None
            if chat_id is None:
                self.stats.errors.append("chat_name_id not found")
                return self.stats

            # 一次性把所有 svr_id -> voice_data 读进内存，避免逐条查询
            blob_map: dict[int, bytes] = {}
            for r in conn.execute(
                "SELECT svr_id, voice_data FROM VoiceInfo WHERE chat_name_id=?", (chat_id,)
            ):
                if r[1]:
                    blob_map[int(r["svr_id"])] = bytes(r[1])
            print(f"[voice] {len(blob_map)} audio blobs loaded", flush=True)
        finally:
            conn.close()

        done = 0
        for row in rows:
            raw = blob_map.get(int(row["server_id"]) if row["server_id"] else 0)
            if not raw:
                self.stats.no_audio += 1
                continue
            self.stats.silk_found += 1
            lid = row["local_id"]
            (self.voice_dir / f"{lid}.silk").write_bytes(raw)
            wav = self.voice_dir / f"{lid}.wav"
            if self._silk_to_wav(raw, wav):
                self.stats.wav_written += 1
                row["wav"] = f"voice/{lid}.wav"
                row["seconds"] = round((row["voice_ms"] or 0) / 1000.0, 1)
                row["day"] = _day(row["create_time"])
                row["time"] = _ts(row["create_time"])
                self.stats.total_seconds += row["seconds"]
            else:
                self.stats.decode_failed += 1
            done += 1
            if done % 200 == 0:
                print(f"[voice] {done}/{len(rows)}", flush=True)

        self._write_index(rows)
        self._write_player(rows)
        self._patch_transcripts(rows)
        print(f"[voice] wav written {self.stats.wav_written}/{len(rows)}", flush=True)
        return self.stats

    # ---- outputs ----
    def _write_index(self, rows: list[dict]) -> None:
        (self.out / "voice_index.json").write_text(
            json.dumps({"chat": self.user, "count": len(rows), "voice": rows},
                       ensure_ascii=False, indent=1), encoding="utf-8")

    def _write_player(self, rows: list[dict]) -> None:
        css = ("body{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:860px;"
               "margin:24px auto;padding:0 16px;line-height:1.7;background:#f7f7f8;color:#222}"
               ".v{background:#fff;border-radius:8px;padding:8px 12px;margin:8px 0;"
               "box-shadow:0 1px 2px rgba(0,0,0,.06)}.me{background:#d8f5d0}"
               ".who{font-weight:600;font-size:13px;color:#555}"
               ".t{color:#999;font-size:12px;margin-left:6px}"
               "h2{margin-top:26px;border-bottom:1px solid #ddd;padding-bottom:4px}"
               "audio{width:100%;margin-top:6px}")
        P = ["<!doctype html><meta charset='utf-8'>",
             f"<title>{html.escape(self.display)} 语音记录</title>",
             f"<style>{css}</style>",
             f"<h1>{html.escape(self.display)} 语音记录</h1>",
             f"<p>{len(rows)} 条 · 合计 {self.stats.total_seconds/60:.1f} 分钟</p>"]
        last_day = None
        for r in rows:
            d = _day(r["create_time"])
            if d != last_day:
                P.append(f"<h2>{html.escape(d)}</h2>")
                last_day = d
            cls = "v me" if r["sender"] == "我" else "v"
            t = _ts(r["create_time"])[11:19]
            body = "（音频未找到）"
            if r.get("wav"):
                body = f"<audio controls preload='none' src='{html.escape(r['wav'])}'></audio>"
            P.append(f"<div class='{cls}'><span class='who'>{html.escape(r['sender'])}</span>"
                     f"<span class='t'>{t} · {r.get('seconds', 0)}s</span><div>{body}</div></div>")
        (self.out / "voice.html").write_text("\n".join(P), encoding="utf-8")

    def _patch_transcripts(self, rows: list[dict]) -> None:
        """Regenerate chat.md / chat.txt with per-message audio links.

        必须逐行替换：``[语音 3.5s]`` 这类标记会在几十行里重复出现，全局
        str.replace 会把同一条链接写进所有时长相同的消息。
        """
        by_key = {}
        for r in rows:
            if r.get("wav"):
                by_key[(r.get("day"), r["sender"], r.get("seconds"))] = r["wav"]

        # 从 voice_index.json 无法直接定位行，因此按 (日期, 发送方, 时长) 逐行匹配，
        # 并在同一组合内按出现顺序消费，保证一行一条链接。
        pools: dict[tuple, list[str]] = {}
        for r in rows:
            if not r.get("wav"):
                continue
            key = (r.get("day"), r["sender"], r.get("seconds"))
            pools.setdefault(key, []).append(r["wav"])

        for name, link_tpl in (
            ("chat.txt", "[语音 {s}s] {w}"),
            ("chat.md", "[语音 {s}s]({w})"),
        ):
            f = self.out / name
            if not f.exists():
                continue
            # 每个文件用独立副本：chat.txt 先跑会清空共享队列，导致 chat.md
            # 再也取不到任何 wav。
            file_pools = {k: list(v) for k, v in pools.items()}
            out_lines = []
            cur_day = None
            for line in f.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^(?:-{5}\s+|##\s+)(\d{4}-\d{2}-\d{2})", line.strip())
                if m:
                    cur_day = m.group(1)
                new = line
                # 已带链接的行跳过，重复运行不会叠加第二个链接。
                if "语音" in line and not re.search(r"voice/\d+\.wav", line):
                    who = self._sender_of(line)
                    sm = re.search(r"语音 (\d+(?:\.\d+)?)s", line)
                    if sm and who:
                        pool = file_pools.get((cur_day, who, float(sm.group(1))))
                        if pool:
                            wav = pool.pop(0)
                            new = line.replace(
                                f"[语音 {sm.group(1)}s]",
                                link_tpl.format(s=sm.group(1), w=wav), 1)
                out_lines.append(new)
            f.write_text("\n".join(out_lines), encoding="utf-8")

    # chat.txt: "[17:41:13] 好友B: ..."   chat.md: "**好友B** `17:41:13` ..."
    _SENDER_MD = re.compile(r"\*\*(.+?)\*\*")
    _SENDER_TXT = re.compile(r"\]\s*([^:：]+)[:：]")

    @classmethod
    def _sender_of(cls, line: str):
        m = cls._SENDER_MD.search(line)
        if m:
            return m.group(1).strip()
        m = cls._SENDER_TXT.search(line)
        if m:
            return m.group(1).strip()
        return None

    def update_stats(self) -> None:
        f = self.out / "stats.json"
        data = {}
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        data["voice"] = asdict(self.stats)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
