"""Decrypt captured WAL frames using the correct per-database key.

The passphrase alone is not enough: every database derives its own raw key from
its own salt, so the frame's source path must be resolved to a .db file first.

数据库密钥不写在本文件里 —— 每台机器、每个账号都不同。按以下顺序解析：

  1) 环境变量 WECHAT_DB_KEY
  2) 环境变量 WECHAT_DB_KEY_FILE 指向的文件
  3) 工程根目录下的 work/db.key

获取方法见工程根的 SECRETS.md。
"""
from __future__ import annotations

import hashlib
import os
import pickle
import struct
import sys
import zstandard
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hook.wal_capture import decrypt_page  # noqa: E402


def load_passphrase() -> bytes:
    """按环境变量 → 密钥文件 → work/db.key 的顺序取密钥。"""
    raw = os.environ.get("WECHAT_DB_KEY", "").strip()
    if not raw:
        key_file = os.environ.get("WECHAT_DB_KEY_FILE", "").strip()
        path = Path(key_file) if key_file else (ROOT / "work" / "db.key")
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not raw:
        raise SystemExit(
            "未找到数据库密钥。请设置 WECHAT_DB_KEY 或把密钥写入 work/db.key"
            "（获取方法见 SECRETS.md）"
        )
    return bytes.fromhex(raw)


PASSPHRASE = load_passphrase()
PAGE = 4096


def db_from_wal(wal_path: str) -> Path | None:
    p = wal_path.replace("\\\\?\\", "")
    if p.endswith("-wal"):
        p = p[:-4]
    cand = Path(p)
    return cand if cand.exists() else None


def raw_key(db: Path) -> bytes | None:
    try:
        with db.open("rb") as f:
            salt = f.read(16)
    except OSError:
        return None
    if len(salt) < 16:
        return None
    return hashlib.pbkdf2_hmac("sha512", PASSPHRASE, salt, 256000, dklen=32)


def page_type(page: bytes) -> int:
    base = 100 if page[:16].startswith(b"SQLite format 3") else 0
    return page[base] if base < len(page) else -1


def parse_leaf(page: bytes, max_rows: int = 6):
    """Return [(rowid, payload)] from a leaf table page."""
    base = 100 if page[:16].startswith(b"SQLite format 3") else 0
    if base + 8 > len(page) or page[base] != 0x0D:
        return []
    ncell = struct.unpack_from(">H", page, base + 3)[0]
    out = []
    for i in range(min(ncell, max_rows)):
        off = base + 8 + i * 2
        if off + 2 > len(page):
            break
        cell = struct.unpack_from(">H", page, off)[0]
        if cell >= len(page):
            continue
        pos = cell
        plen = 0
        for _ in range(9):
            if pos >= len(page):
                break
            b = page[pos]; pos += 1
            plen = (plen << 7) | (b & 0x7F)
            if not (b & 0x80):
                break
        rowid = 0
        for _ in range(9):
            if pos >= len(page):
                break
            b = page[pos]; pos += 1
            rowid = (rowid << 7) | (b & 0x7F)
            if not (b & 0x80):
                break
        if 0 < plen and pos + plen <= len(page):
            out.append((rowid, page[pos:pos + plen]))
    return out


def decode(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    raw = bytes(v)
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        try:
            return zstandard.ZstdDecompressor().decompress(raw).decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


if __name__ == "__main__":
    frames = pickle.load(open(
        r"__ROOT__\hook\build\frames2.pkl", "rb"))
    print(f"loaded {len(frames)} frames\n")

    key_cache: dict[str, bytes] = {}
    good = 0
    for f in frames:
        db = db_from_wal(f["path"])
        if db is None:
            print(f"  skip (db missing): {f['path']}")
            continue
        k = key_cache.get(str(db))
        if k is None:
            k = raw_key(db)
            key_cache[str(db)] = k
        if not k:
            continue
        plain = decrypt_page(k, f["data"], f["page_no"], PAGE)
        t = page_type(plain)
        rows = parse_leaf(plain, max_rows=3)
        name = db.name
        if t == 0x0D:
            good += 1
        print(f"  {name:22s} page {f['page_no']:5d}  type=0x{t:02x}  rows={len(rows)}")
        for rowid, payload in rows:
            txt = decode(payload)
            if txt:
                print(f"       rowid={rowid}: {txt[:100]!r}")

    print(f"\nleaf pages decrypted: {good}/{len(frames)}")
