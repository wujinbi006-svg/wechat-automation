"""微信 4.x 会话归档导出器。

把某个会话的全部消息导出为自包含归档：

- 文本/表情/链接等 → Markdown
- 图片 (local_type=3) → 从 msg/attach/<chat_md5>/<YYYY-MM>/Img/<md5>.dat
  按 v1/v2 格式解密为原图（AES-ECB + 单字节 XOR）
- 语音 (local_type=34) → media_0.db VoiceInfo.voice_data 的 SILK 二进制，
  用 ffmpeg 转 WAV
- 视频 (local_type=43) → msg/video/<id>.mp4
- 文件 (local_type=49) → msg/file/<原名>，原名取自 message_resource.db

设计约束：
- 原始数据库只读；解密副本由调用方（DatabaseAdapter）自行管理
- 不向微信 CDN 发起请求；只用本地已落地的 .dat / voice_data / mp4 / file
- 缺失的媒体不伪造，记录到 manifest 里，由调用方决定是否另行获取
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import time
import zstandard
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_HEADER_SZ = 22

IMAGE_MAGIC = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",
}


def sniff_image(data: bytes) -> Optional[str]:
    for magic, ext in IMAGE_MAGIC.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"wxgf":
        return ".wxgf"
    return None


def aligned_aes_block_size(size: int) -> int:
    return size + (16 - size % 16) if size % 16 else size + 16


@dataclass
class MediaStats:
    images_found: int = 0
    images_missing: int = 0
    images_failed: int = 0
    voice_found: int = 0
    voice_missing: int = 0
    voice_converted: int = 0
    video_found: int = 0
    video_missing: int = 0
    file_found: int = 0
    file_missing: int = 0
    errors: list[str] = field(default_factory=list)


class MediaExporter:
    """会话媒体导出器（只读本地已落地文件）。"""

    def __init__(self, adapter, chat_username: str, out_root: Path,
                 image_aes_key: Optional[str] = None,
                 ffmpeg: Optional[str] = None):
        self.adapter = adapter
        self.user = chat_username
        self.out_root = Path(out_root)
        self.images_dir = self.out_root / "attachments" / "images"
        self.voice_dir = self.out_root / "attachments" / "voice"
        self.video_dir = self.out_root / "attachments" / "video"
        self.file_dir = self.out_root / "attachments" / "files"
        for d in (self.images_dir, self.voice_dir, self.video_dir, self.file_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.image_aes_key = image_aes_key
        self.ffmpeg = ffmpeg
        self.chat_md5 = hashlib.md5(self.user.encode("utf-8")).hexdigest()
        self.account_dir = Path(adapter.base_dir) / adapter.current_account()
        self.stats = MediaStats()
        self._image_index: Optional[dict[str, str]] = None

    # ── 图片 AES 密钥 ────────────────────────────────────────────
    def _probe_cipher_block(self) -> bytes:
        """取任意一张 V2 图片的密文首块，用于反测密钥。"""
        base = self.account_dir / "msg" / "attach"
        for dat in base.glob("*/*/Img/*.dat"):
            try:
                head = dat.open("rb").read(32)
            except OSError:
                continue
            if head[:6] == V2_MAGIC and len(head) >= 31:
                return head[15:31]
            if head[:6] == V1_MAGIC and len(head) >= V1_HEADER_SZ + 16:
                return head[V1_HEADER_SZ:V1_HEADER_SZ + 16]
        return b""

    def validate_image_key(self, key: str) -> bool:
        probe = self._probe_cipher_block()
        if not probe or not key:
            return False
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            dec = Cipher(algorithms.AES(key.encode()), modes.ECB()).decryptor()
            return sniff_image(dec.update(probe) + dec.finalize()) is not None
        except Exception:
            return False

    def crack_image_key(self, alphabet: Optional[str] = None,
                        max_len: int = 16) -> Optional[str]:
        """从密文首块反推 16 字节 ASCII AES 密钥。

        微信图片 AES 密钥是 16 位字母数字串。已知首块明文必然是某个图片
        魔数，因此可以逐位爆破：每确定一位，就能用一次 ECB 解密把后续
        block 的约束解锁，最多需要 (字符集大小 × 16) 次 AES 调用。
        """
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        probe = self._probe_cipher_block()
        if not probe:
            return None
        charset = (alphabet or "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
        # 已知明文前缀候选（jpeg / png / gif / riff），按最长公共前缀建约束
        targets = [b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF"]

        def decrypt_first_block(key: bytes) -> bytes:
            try:
                dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
                return dec.update(probe) + dec.finalize()
            except Exception:
                return b""

        def matches(pt: bytes) -> bool:
            return any(t.startswith(pt[:len(t)]) for t in targets if len(pt) >= len(t)) or \
                   any(pt[:min(len(t), len(pt))] == t[:min(len(t), len(pt))] for t in targets)

        found: list[tuple[bytes, bytes]] = []
        for prefix in (b"",):
            candidates = [prefix]
            for pos in range(max_len):
                nxt = []
                for cand in candidates:
                    for ch in charset:
                        trial = cand + ch.encode()
                        pt = decrypt_first_block(trial + b"\x00" * (16 - len(trial)))
                        if matches(pt):
                            nxt.append(trial)
                candidates = nxt
                if not candidates:
                    break
                # 收敛到唯一解
                if len(candidates) == 1 and len(candidates[0]) == max_len:
                    break
            for cand in candidates:
                if len(cand) == max_len:
                    pt = decrypt_first_block(cand)
                    if sniff_image(pt):
                        found.append((cand, pt))
        if found:
            return found[0][0].decode("ascii", "ignore")
        return None

    # ── 图片 ────────────────────────────────────────────────────
    def _build_image_index(self) -> dict[str, str]:
        """md5(小写, 去 _t/_W/_h 后缀) → .dat 路径。

        实际布局是 ``msg/attach/<chat_md5>/<YYYY-MM>/Img/<md5>[_t|_h|_W].dat``，
        月目录与 ``Img`` 层都可能缺失，因此递归遍历而不是固定层级。
        """
        if self._image_index is not None:
            return self._image_index
        index: dict[str, str] = {}
        base = self.account_dir / "msg" / "attach" / self.chat_md5
        if base.is_dir():
            for dat in base.rglob("*.dat"):
                stem = dat.stem.lower()
                # 同一 md5 可能有普通版与 _t/_h 缩略图、_W 原图，原图优先
                for key in {stem, stem.replace("_t", "").replace("_w", "").replace("_h", "")}:
                    if not key:
                        continue
                    existing = index.get(key)
                    if existing is None or stem.endswith("_w"):
                        index[key] = str(dat)
        self._image_index = index
        return index

    def _derive_xor_key(self, dat_path: str, data: bytes) -> int:
        """单字节 XOR：从同图的 _t 缩略图尾部 JPEG 结束标记 FF D9 反推。"""
        # V2 头部已带 xor_size，密钥是单字节；优先用 _t 尾部推算
        stem = Path(dat_path).stem
        base_dir = Path(dat_path).parent
        for cand_name in (f"{stem}_t.dat", f"{stem.replace('_W', '')}_t.dat"):
            cand = base_dir / cand_name
            if cand.exists():
                try:
                    tail = cand.open("rb").read()[-4:]
                    if len(tail) >= 2:
                        return tail[-2] ^ 0xFF if tail[-1] == 0xD9 else tail[0] ^ 0xFF
                except OSError:
                    pass
        # 回退：从密文尾部推（最后一个字节常见 FF D9 的 XOR）
        if data:
            return data[-1] ^ 0xD9
        return 0x00

    def decrypt_image_bytes(self, dat_path: str) -> bytes:
        data = Path(dat_path).read_bytes()
        if not data:
            raise ValueError("空文件")
        magic = data[:6]
        if magic == V2_MAGIC:
            aes_size, xor_size = struct.unpack_from("<LL", data, 6)
            xor_key = self._derive_xor_key(dat_path, data)
            if not self.image_aes_key:
                raise RuntimeError("缺少图片 AES 密钥")
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            aes_blk = aligned_aes_block_size(aes_size)
            off = 15
            aes_data = data[off:off + aes_blk]
            off += aes_blk
            raw = data[off:len(data) - xor_size]
            xor_data = data[len(data) - xor_size:] if xor_size else b""
            dec = Cipher(algorithms.AES(self.image_aes_key.encode()), modes.ECB()).decryptor()
            pt = dec.update(aes_data) + dec.finalize()
            pad = pt[-1] if pt else 0
            if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
                pt = pt[:-pad]
            return pt + raw + bytes(b ^ (xor_key & 0xFF) for b in xor_data)
        if magic == V1_MAGIC:
            xor_key = self._derive_xor_key(dat_path, data)
            body = data[22:]
            return bytes(b ^ (xor_key & 0xFF) for b in body)
        # 早期纯 XOR
        for cand in (0x88, 0x30, 0xFF, 0xE9):
            out = bytes(b ^ cand for b in data)
            if sniff_image(out):
                return out
        raise ValueError("无法识别的图片加密格式")

    def export_image(self, md5: str) -> Optional[Path]:
        index = self._build_image_index()
        dat = index.get(str(md5).lower())
        if not dat:
            self.stats.images_missing += 1
            return None
        try:
            blob = self.decrypt_image_bytes(dat)
        except Exception as exc:
            self.stats.images_failed += 1
            self.stats.errors.append(f"image {md5}: {type(exc).__name__}: {exc}")
            return None
        ext = sniff_image(blob)
        if not ext:
            self.stats.images_failed += 1
            self.stats.errors.append(f"image {md5}: 解密后不是已知图片格式")
            return None
        out = self.images_dir / f"{md5}{ext}"
        out.write_bytes(blob)
        self.stats.images_found += 1
        return out

    # ── 语音 ────────────────────────────────────────────────────
    def _voice_conn(self):
        return self.adapter._open_readonly("message/media_0.db")

    def export_voice(self, server_id: int, local_id: int) -> Optional[Path]:
        try:
            conn = self._voice_conn()
        except Exception as exc:
            self.stats.voice_missing += 1
            self.stats.errors.append(f"voice db: {type(exc).__name__}: {exc}")
            return None
        try:
            row = conn.execute(
                "SELECT chat_name_id FROM Name2Id WHERE user_name=?", (self.user,)
            ).fetchone()
            if not row:
                self.stats.voice_missing += 1
                return None
            chat_name_id = row[0]
            cur = conn.execute(
                "SELECT voice_data FROM VoiceInfo WHERE chat_name_id=? AND svr_id=? "
                "ORDER BY create_time DESC LIMIT 1",
                (chat_name_id, server_id),
            ).fetchone()
        finally:
            conn.close()
        if not cur or not cur[0]:
            self.stats.voice_missing += 1
            return None
        raw = bytes(cur[0])
        self.stats.voice_found += 1
        silk = self.voice_dir / f"{local_id}.silk"
        silk.write_bytes(raw)
        wav = self._silk_to_wav(silk)
        return wav or silk

    def _silk_to_wav(self, silk: Path) -> Optional[Path]:
        if not self.ffmpeg:
            return None
        wav = silk.with_suffix(".wav")
        try:
            proc = subprocess.run(
                [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(silk),
                 "-ar", "24000", "-ac", "1", str(wav)],
                capture_output=True, timeout=60,
            )
            if proc.returncode == 0 and wav.exists() and wav.stat().st_size > 44:
                self.stats.voice_converted += 1
                return wav
            silk.with_suffix(".wav.error").write_bytes(proc.stderr or b"")
        except Exception as exc:
            self.stats.errors.append(f"ffmpeg silk {silk.name}: {exc}")
        return None

    # ── 视频 ────────────────────────────────────────────────────
    def export_video(self, packed_info: bytes) -> Optional[Path]:
        vid = None
        if isinstance(packed_info, (bytes, bytearray)):
            m = re.search(rb"([0-9a-fA-F]{32})", bytes(packed_info))
            vid = m.group(1).decode().lower() if m else None
        if not vid:
            self.stats.video_missing += 1
            return None
        base = self.account_dir / "msg" / "video"
        if base.is_dir():
            for mp4 in base.rglob(f"{vid}.mp4"):
                out = self.video_dir / f"{vid}.mp4"
                shutil.copy2(mp4, out)
                self.stats.video_found += 1
                return out
        self.stats.video_missing += 1
        return None

    # ── 文件 ────────────────────────────────────────────────────
    def _resource_name(self, server_id: int) -> Optional[str]:
        try:
            conn = self.adapter._open_readonly("message/message_resource.db")
        except Exception:
            return None
        try:
            cur = conn.execute(
                "SELECT packed_info FROM MessageResourceDetail WHERE message_id=? LIMIT 1",
                (server_id,),
            ).fetchone()
        except Exception:
            return None
        finally:
            conn.close()
        if not cur or not cur[0]:
            return None
        blob = bytes(cur[0]) if isinstance(cur[0], (bytes, bytearray)) else b""
        # packed_info 里文件名以明文 tail 出现；取常见扩展名结尾的片段
        m = re.search(rb"([^\x00-\x1f/\\:*?\"<>|]{1,120}\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt|md|csv|mp4|mov|mp3|wav|png|jpe?g|gif|apk|exe))", blob)
        if m:
            return m.group(1).decode("utf-8", "replace")
        return None

    def export_file(self, server_id: int, local_id: int) -> Optional[Path]:
        name = self._resource_name(server_id)
        base = self.account_dir / "msg" / "file"
        if name and base.is_dir():
            for f in base.rglob(name):
                out = self.file_dir / f"{local_id}_{name}"
                shutil.copy2(f, out)
                self.stats.file_found += 1
                return out
        self.stats.file_missing += 1
        return None
