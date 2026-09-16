"""WeChat chat archiver: media extraction + voice transcription + Markdown/HTML.

Output layout::

    <root>/
    +-- README.md              archive notes, stats, known gaps
    +-- chat.md                main file, chronological
    +-- chat.html              browsable version
    +-- index.json             machine-readable index (incl. transcripts)
    +-- stats.json             statistics
    +-- attachments/
        +-- voice/             WAV (playable) + original SILK
        +-- images/            decrypted images when cached locally
        +-- sticker/           stickers
        +-- video/             videos
        +-- files/             files

Constraints:
- source databases are read-only, accessed via DatabaseAdapter's decrypted copies
- no requests to WeChat CDN; missing media is labelled honestly, never faked
- failed transcriptions keep a placeholder rather than inventing content
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
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


def _fmt_time(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (ValueError, OSError):
        return ""


def _fmt_day(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except (ValueError, OSError):
        return ""


@dataclass
class ArchiveStats:
    total: int = 0
    by_type: dict = field(default_factory=dict)
    voice_total: int = 0
    voice_decoded: int = 0
    voice_transcribed: int = 0
    voice_failed: int = 0
    image_local: int = 0
    image_cdn_only: int = 0
    sticker_local: int = 0
    video_local: int = 0
    file_local: int = 0
    date_first: str = ""
    date_last: str = ""
    errors: list = field(default_factory=list)


class VoiceTranscriber:
    """SILK -> WAV -> text. Model loads once per batch."""

    def __init__(self, whisper_venv: Optional[str] = None,
                 model_size: str = "base", language: str = "zh",
                 enabled: bool = True):
        self.whisper_python = None
        if whisper_venv:
            cand = Path(whisper_venv) / "Scripts" / "python.exe"
            if cand.exists():
                self.whisper_python = str(cand)
        self.model_size = model_size
        self.language = language
        self.enabled = bool(enabled and self.whisper_python)
        self._import_error: Optional[str] = None

    def silk_to_wav(self, silk_bytes: bytes, wav_path: Path) -> bool:
        try:
            import pysilk
        except ImportError as exc:
            self._import_error = f"pysilk missing: {exc}"
            return False
        body = silk_bytes[1:] if silk_bytes[:1] == b"\x02" else silk_bytes
        buf = io.BytesIO()
        try:
            pysilk.decode(io.BytesIO(body), buf, 24000)
        except Exception as exc:
            self._import_error = f"pysilk decode: {type(exc).__name__}: {exc}"
            return False
        pcm = buf.getvalue()
        if not pcm:
            return False
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        return True

    def transcribe_batch(self, wav_paths: list) -> dict:
        """Transcribe many files in one process so the model loads once."""
        if not self.enabled or not wav_paths:
            return {}
        code = (
            "import sys, json\n"
            "from faster_whisper import WhisperModel\n"
            f"m = WhisperModel({self.model_size!r}, device='cpu', compute_type='int8')\n"
            "paths = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "out = {}\n"
            "for p in paths:\n"
            "    try:\n"
            f"        segs, _ = m.transcribe(p, language={self.language!r}, beam_size=1, vad_filter=True)\n"
            "        out[p] = ''.join(s.text for s in segs).strip()\n"
            "    except Exception:\n"
            "        out[p] = None\n"
            "json.dump(out, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False)\n"
        )
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "in.json"
            outp = Path(td) / "out.json"
            inp.write_text(json.dumps([str(p) for p in wav_paths]), encoding="utf-8")
            try:
                proc = subprocess.run(
                    [self.whisper_python, "-c", code, str(inp), str(outp)],
                    capture_output=True, timeout=max(1800, 30 * len(wav_paths)),
                )
                if proc.returncode == 0 and outp.exists():
                    data = json.loads(outp.read_text(encoding="utf-8"))
                    return {k: v for k, v in data.items() if v}
                self._import_error = proc.stderr.decode("utf-8", "replace")[-400:]
            except Exception as exc:
                self._import_error = f"{type(exc).__name__}: {exc}"
        return {}


class ChatArchiver:
    def __init__(self, adapter, chat_username: str, chat_display: str,
                 out_root: Path, transcriber: Optional[VoiceTranscriber] = None):
        self.adapter = adapter
        self.user = chat_username
        self.display = chat_display
        self.out = Path(out_root)
        self.att = self.out / "attachments"
        self.voice_dir = self.att / "voice"
        self.image_dir = self.att / "images"
        self.sticker_dir = self.att / "sticker"
        self.video_dir = self.att / "video"
        self.file_dir = self.att / "files"
        for d in (self.voice_dir, self.image_dir, self.sticker_dir,
                  self.video_dir, self.file_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.transcriber = transcriber or VoiceTranscriber(enabled=False)
        self.chat_md5 = hashlib.md5(self.user.encode("utf-8")).hexdigest()
        self.account_dir = Path(adapter.base_dir) / adapter.current_account()
        self.stats = ArchiveStats()
        self.messages: list[dict] = []

    # ---- data ----
    def _fetch_rows(self) -> list[dict]:
        table = "Msg_" + hashlib.md5(self.user.encode("utf-8")).hexdigest()
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
                    f'message_content, sort_seq FROM "{table}" ORDER BY sort_seq ASC'
                )
                for row in cur:
                    rows.append({
                        "local_id": row["local_id"],
                        "server_id": row["server_id"],
                        "local_type": row["local_type"],
                        "real_sender_id": row["real_sender_id"],
                        "create_time": row["create_time"],
                        "content": _decode_content(row["message_content"]),
                        "sort_seq": row["sort_seq"],
                        "sender": sender_map.get(int(row["real_sender_id"] or 0)),
                    })
            except Exception as exc:
                self.stats.errors.append(f"{db_name}: {type(exc).__name__}: {exc}")
            finally:
                conn.close()
        rows.sort(key=lambda r: (r.get("sort_seq") or 0, r.get("local_id") or 0))
        return rows

    def _sender_label(self, row: dict) -> str:
        sid = row.get("sender")
        me = self.adapter.current_account().split("_")[0]
        if sid and (sid == me or sid.startswith(me)):
            return "我"
        if sid:
            return self.adapter.get_nickname(sid) or sid
        return "对方"

    # ---- media indexes ----
    def _image_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        base = self.account_dir / "msg" / "attach" / self.chat_md5
        if base.is_dir():
            for dat in base.rglob("*.dat"):
                stem = dat.stem.lower()
                for key in {stem, stem.replace("_t", "").replace("_w", "").replace("_h", "")}:
                    if key:
                        cur = index.get(key)
                        if cur is None or stem.endswith("_w"):
                            index[key] = str(dat)
        return index

    def _sticker_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        base = self.account_dir / "msg" / "attach" / self.chat_md5
        if base.is_dir():
            for f in base.rglob("*"):
                if f.is_file() and f.suffix.lower() in {".dat", ".gif", ".png", ".jpg"}:
                    index[f.stem.lower()] = str(f)
        return index

    # ---- voice ----
    def _voice_wav(self, server_id: int, local_id: int) -> Optional[Path]:
        try:
            conn = self.adapter._open_readonly("message/media_0.db")
        except Exception:
            return None
        try:
            r = conn.execute(
                "SELECT chat_name_id FROM Name2Id WHERE user_name=?", (self.user,)
            ).fetchone()
            if not r:
                return None
            cur = conn.execute(
                "SELECT voice_data FROM VoiceInfo WHERE chat_name_id=? AND svr_id=? "
                "ORDER BY create_time DESC LIMIT 1",
                (r[0], server_id),
            ).fetchone()
        except Exception as exc:
            self.stats.errors.append(f"voice {local_id}: {type(exc).__name__}: {exc}")
            return None
        finally:
            conn.close()
        if not cur or not cur[0]:
            return None
        raw = bytes(cur[0])
        self.stats.voice_total += 1
        (self.voice_dir / f"{local_id}.silk").write_bytes(raw)
        wav = self.voice_dir / f"{local_id}.wav"
        if self.transcriber.silk_to_wav(raw, wav):
            self.stats.voice_decoded += 1
            return wav
        self.stats.voice_failed += 1
        return None

    def _transcribe_all(self) -> None:
        todo = []
        for m in self.messages:
            if m["kind"] != "voice":
                continue
            wav = self.voice_dir / f"{m['local_id']}.wav"
            if wav.exists():
                todo.append(wav)
        if not todo:
            return
        print(f"[transcribe] {len(todo)} voice clips queued", flush=True)
        results = self.transcriber.transcribe_batch(todo)
        for m in self.messages:
            if m["kind"] != "voice":
                continue
            wav = self.voice_dir / f"{m['local_id']}.wav"
            text = results.get(str(wav))
            if text:
                m["transcript"] = text
                self.stats.voice_transcribed += 1
        print(f"[transcribe] done: {self.stats.voice_transcribed}/{len(todo)}", flush=True)

    # ---- video / file ----
    def _export_video(self, content: str, local_id: int) -> Optional[str]:
        m = re.search(r"([0-9a-fA-F]{32})", content or "")
        if not m:
            return None
        vid = m.group(1).lower()
        base = self.account_dir / "msg" / "video"
        if base.is_dir():
            for mp4 in base.rglob(f"{vid}.mp4"):
                out = self.video_dir / f"{local_id}_{vid}.mp4"
                shutil.copy2(mp4, out)
                self.stats.video_local += 1
                return f"attachments/video/{out.name}"
        return None

    def _export_file(self, local_id: int, content: str) -> Optional[str]:
        name = None
        m = re.search(r"<title>(.*?)</title>", content or "")
        if m:
            cand = html.unescape(m.group(1)).strip()
            if cand and len(cand) < 150:
                name = cand
        base = self.account_dir / "msg" / "file"
        if name and base.is_dir():
            for f in base.rglob(name):
                out = self.file_dir / f"{local_id}_{name}"
                shutil.copy2(f, out)
                self.stats.file_local += 1
                return f"attachments/files/{out.name}"
        return None

    # ---- main ----
    def run(self) -> ArchiveStats:
        rows = self._fetch_rows()
        self.stats.total = len(rows)
        if rows:
            self.stats.date_first = _fmt_day(rows[0].get("create_time"))
            self.stats.date_last = _fmt_day(rows[-1].get("create_time"))
        img_index = self._image_index()
        sticker_index = self._sticker_index()

        for row in rows:
            base_type = int(row["local_type"]) & 0xFFFFFFFF
            kind = self.adapter._classify_local_type(row["local_type"], row["content"])
            row["kind"] = kind
            row["sender_label"] = self._sender_label(row)
            row["time_str"] = _fmt_time(row.get("create_time"))
            row["day"] = _fmt_day(row.get("create_time"))
            row["media"] = None
            row["transcript"] = None
            self.stats.by_type[kind] = self.stats.by_type.get(kind, 0) + 1
            content = row["content"] or ""

            if base_type == 3:
                m = re.search(r'md5="([0-9a-fA-F]{32})"', content) or \
                    re.search(r'cdnthumburl="([0-9a-fA-F]{32})"', content)
                md5 = m.group(1).lower() if m else None
                wh = re.search(r'cdnthumbwidth="(\d+)"', content)
                hh = re.search(r'cdnthumbheight="(\d+)"', content)
                row["image_meta"] = {
                    "md5": md5,
                    "w": int(wh.group(1)) if wh else None,
                    "h": int(hh.group(1)) if hh else None,
                }
                local = img_index.get(md5) if md5 else None
                if local:
                    self.stats.image_local += 1
                    row["media"] = os.path.relpath(local, self.out)
                else:
                    self.stats.image_cdn_only += 1
            elif base_type == 34:
                ln = re.search(r'voicelength="(\d+)"', content)
                row["voice_ms"] = int(ln.group(1)) if ln else 0
                wav = self._voice_wav(row["server_id"], row["local_id"])
                if wav:
                    row["media"] = f"attachments/voice/{wav.name}"
            elif base_type == 47:
                m = re.search(r'md5="([0-9a-fA-F]{32})"', content)
                md5 = m.group(1).lower() if m else None
                hit = sticker_index.get(md5) if md5 else None
                if hit:
                    out = self.sticker_dir / (md5 + (Path(hit).suffix or ".dat"))
                    if not out.exists():
                        shutil.copy2(hit, out)
                    self.stats.sticker_local += 1
                    row["media"] = f"attachments/sticker/{out.name}"
            elif base_type == 43:
                row["media"] = self._export_video(content, row["local_id"])
            elif kind in {"file", "link"} or base_type == 49:
                row["media"] = self._export_file(row["local_id"], content)

        self.messages = rows
        self._transcribe_all()
        self._write_markdown()
        self._write_html()
        self._write_index()
        self._write_readme()
        return self.stats

    # ---- output ----
    def _write_markdown(self) -> None:
        L = [f"# 与 {self.display} 的聊天记录", "",
             f"- 会话 ID: `{self.user}`", f"- 消息数: {self.stats.total}",
             f"- 时间范围: {self.stats.date_first} ~ {self.stats.date_last}",
             "", "---", ""]
        last_day = None
        for m in self.messages:
            if m["day"] != last_day:
                L.append(f"\n## {m['day']}\n")
                last_day = m["day"]
            t = m["time_str"][11:19] if m["time_str"] else ""
            p = f"**{m['sender_label']}** `{t}`"
            k = m["kind"]
            c = m["content"] or ""
            if k == "text":
                L.append(f"{p}\n\n{c}\n")
            elif k == "voice":
                secs = (m.get("voice_ms") or 0) / 1000.0
                tr = m.get("transcript")
                if tr:
                    L.append(f"{p} 🎤 `{secs:.1f}s`\n\n> {tr}\n")
                else:
                    L.append(f"{p} 🎤 `{secs:.1f}s` *（语音，未转写）*\n")
                if m.get("media"):
                    L.append(f"  → [{Path(m['media']).name}]({m['media']})\n")
            elif k == "image":
                meta = m.get("image_meta") or {}
                dim = f"{meta.get('w')}×{meta.get('h')}" if meta.get("w") else "尺寸未知"
                if m.get("media"):
                    L.append(f"{p} 🖼️\n\n![图片]({m['media']})\n")
                else:
                    L.append(f"{p} 🖼️ *（图片未本地缓存 · {dim}）*\n")
            elif k == "sticker":
                L.append(f"{p} 😀 " + (f"[表情]({m['media']})" if m.get("media") else "*（表情）*") + "\n")
            elif k == "video":
                L.append(f"{p} 🎬 " + (f"[视频]({m['media']})" if m.get("media") else "*（视频未本地缓存）*") + "\n")
            elif k == "file":
                t2 = re.search(r"<title>(.*?)</title>", c)
                name = html.unescape(t2.group(1)) if t2 else "文件"
                L.append(f"{p} 📎 " + (f"[{name}]({m['media']})" if m.get("media") else f"*（文件: {name}）*") + "\n")
            elif k == "link":
                t2 = re.search(r"<title>(.*?)</title>", c)
                u2 = re.search(r"<url>(.*?)</url>", c)
                name = html.unescape(t2.group(1)) if t2 else "链接"
                u = html.unescape(u2.group(1)) if u2 else ""
                L.append(f"{p} 🔗 [{name}]({u})\n")
            elif k == "voip":
                d = re.search(r"<duration>(\d+)</duration>", c)
                L.append(f"{p} 📞 *通话 {int(d.group(1)) if d else 0}s*\n")
            elif k == "system":
                L.append(f"{p} ⚙️ *{c[:120]}*\n")
            elif k == "redpacket":
                L.append(f"{p} 🧧 *红包*\n")
            elif k == "transfer":
                L.append(f"{p} 💰 *转账*\n")
            else:
                L.append(f"{p} `{k}` {c.replace(chr(10), ' ')[:120]}\n")
        (self.out / "chat.md").write_text("\n".join(L), encoding="utf-8")

    def _write_html(self) -> None:
        css = ("body{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:900px;"
               "margin:24px auto;padding:0 16px;line-height:1.6;background:#f7f7f8;color:#222}"
               ".m{margin:10px 0;padding:8px 12px;border-radius:8px;background:#fff;"
               "box-shadow:0 1px 2px rgba(0,0,0,.06)}.me{background:#d8f5d0}"
               ".who{font-weight:600;font-size:13px;color:#555}"
               ".t{color:#999;font-size:12px;margin-left:6px}"
               ".v{border-left:3px solid #4a9;padding-left:8px;color:#265;margin-top:4px}"
               "h2{margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}"
               "img{max-width:320px;border-radius:6px;display:block;margin-top:6px}"
               ".miss{color:#a60;font-style:italic}")
        P = ["<!doctype html><meta charset='utf-8'>",
             f"<title>与 {html.escape(self.display)} 的聊天记录</title>",
             f"<style>{css}</style>",
             f"<h1>与 {html.escape(self.display)} 的聊天记录</h1>",
             f"<p>{self.stats.total} 条 · {self.stats.date_first} ~ {self.stats.date_last}</p>"]
        last_day = None
        for m in self.messages:
            if m["day"] != last_day:
                P.append(f"<h2>{html.escape(m['day'])}</h2>")
                last_day = m["day"]
            cls = "m me" if m["sender_label"] == "我" else "m"
            who = html.escape(m["sender_label"])
            t = html.escape(m["time_str"][11:19] if m["time_str"] else "")
            k = m["kind"]
            c = m["content"] or ""
            if k == "text":
                b = html.escape(c).replace("\n", "<br>")
            elif k == "voice":
                secs = (m.get("voice_ms") or 0) / 1000.0
                tr = m.get("transcript")
                b = f"🎤 {secs:.1f}s"
                b += f"<div class='v'>{html.escape(tr)}</div>" if tr else " <span class='miss'>（未转写）</span>"
            elif k == "image":
                if m.get("media"):
                    b = f"🖼️<img src='{html.escape(m['media'])}'>"
                else:
                    meta = m.get("image_meta") or {}
                    dim = f"{meta.get('w')}×{meta.get('h')}" if meta.get("w") else ""
                    b = f"🖼️ <span class='miss'>图片未本地缓存 {dim}</span>"
            elif k == "sticker":
                b = "😀 " + (f"<img src='{html.escape(m['media'])}'>" if m.get("media") else "表情")
            elif k == "video":
                b = "🎬 视频" + (f" <a href='{html.escape(m['media'])}'>打开</a>" if m.get("media") else " <span class='miss'>未缓存</span>")
            elif k == "file":
                t2 = re.search(r"<title>(.*?)</title>", c)
                name = html.unescape(t2.group(1)) if t2 else "文件"
                b = f"📎 {html.escape(name)}"
                if m.get("media"):
                    b += f" <a href='{html.escape(m['media'])}'>下载</a>"
            elif k == "link":
                t2 = re.search(r"<title>(.*?)</title>", c)
                u2 = re.search(r"<url>(.*?)</url>", c)
                name = html.unescape(t2.group(1)) if t2 else "链接"
                u = html.unescape(u2.group(1)) if u2 else ""
                b = f"🔗 <a href='{html.escape(u)}'>{html.escape(name)}</a>"
            elif k == "voip":
                d = re.search(r"<duration>(\d+)</duration>", c)
                b = f"📞 通话 {int(d.group(1)) if d else 0}s"
            elif k == "system":
                b = f"⚙️ {html.escape(c[:150])}"
            elif k == "redpacket":
                b = "🧧 红包"
            elif k == "transfer":
                b = "💰 转账"
            else:
                b = f"<code>{html.escape(c[:150])}</code>"
            P.append(f"<div class='{cls}'><span class='who'>{who}</span>"
                     f"<span class='t'>{t}</span><div>{b}</div></div>")
        (self.out / "chat.html").write_text("\n".join(P), encoding="utf-8")

    def _write_index(self) -> None:
        (self.out / "index.json").write_text(json.dumps({
            "chat": self.user, "display": self.display,
            "account": self.adapter.current_account(),
            "count": len(self.messages), "messages": self.messages,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    def _write_readme(self) -> None:
        s = self.stats
        L = [f"# {self.display} 聊天归档", "",
             f"- 微信账号: `{self.adapter.current_account()}`",
             f"- 会话 ID: `{self.user}`",
             f"- 消息总数: **{s.total}**",
             f"- 时间范围: {s.date_first} ~ {s.date_last}", "",
             "## 内容统计", "", "| 类型 | 数量 |", "|---|---|"]
        for k, v in sorted(s.by_type.items(), key=lambda x: -x[1]):
            L.append(f"| {k} | {v} |")
        L += ["", "## 媒体与语音", "",
              f"- 语音: 共 {s.voice_total} 条，解码 {s.voice_decoded}，**成功转写 {s.voice_transcribed}**",
              f"- 图片: 本地 {s.image_local}，仅 CDN（未缓存）{s.image_cdn_only}",
              f"- 表情: 本地 {s.sticker_local}",
              f"- 视频: 本地 {s.video_local}",
              f"- 文件: 本地 {s.file_local}",
              "", "## 文件说明", "",
              "- `chat.md` — 完整记录，按时间正序，语音内嵌转写文本",
              "- `chat.html` — 可浏览版本（浏览器打开）",
              "- `index.json` — 机器可读索引，含每条消息的结构化字段",
              "- `attachments/voice/` — 语音 WAV（可播放）与原始 SILK",
              "- `attachments/` 其余目录 — 本地可取得的图片/表情/视频/文件", ""]
        if s.image_cdn_only:
            L += ["## 已知缺失", "",
                  f"**{s.image_cdn_only} 张图片未包含在本归档中。**", "",
                  "这些图片在数据库里只保留了 CDN 引用（`cdnthumburl`），本地没有缓存文件。",
                  "微信 4.x 的图片 AES 密钥仅在查看大图时临时驻留内存，且 CDN 引用是加密标识",
                  "而非可直接访问的 URL，因此这些原图无法离线取回。每条图片消息的",
                  "时间、发送方、尺寸、md5 仍完整保留在 `chat.md` 与 `index.json` 中。", ""]
        if s.errors:
            L += ["## 处理过程中的错误", "", "```"] + s.errors[:50] + ["```", ""]
        (self.out / "README.md").write_text("\n".join(L), encoding="utf-8")
        (self.out / "stats.json").write_text(
            json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
