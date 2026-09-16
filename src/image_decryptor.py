"""WeChat 4.x image (.dat) decryptor.

三种格式共存于同一目录：

1. ``07 08 56 32 08 07`` (v2)：AES-ECB 头部 + 明文段 + 单字节 XOR 尾部
   结构 [6B sig][4B aes_size LE][4B xor_size LE][1B pad][aes][raw][xor]
2. ``07 08 05 56 02 05`` (v1)：单字节 XOR，密钥在头部
3. 无签名：整体单字节 XOR（``e0 c7 e0 xx`` ^ 0x1F -> ``ff d8 ff xx`` JPEG）

密钥来自 wx_key.get_image_key()（本地文件推导，无需 hook）。
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"

IMAGE_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
    (b"wxgf", ".wxgf"),
)


def sniff(data: bytes) -> Optional[str]:
    for magic, ext in IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    return None


def _aligned(size: int) -> int:
    """与参考实现一致：即 16 对齐时也要再补一个整块。"""
    return size + (16 - size % 16) if size % 16 else size + 16


def decrypt_dat(path: str | Path, aes_key: str, xor_key: int) -> bytes:
    """解密单个 .dat，返回明文图片字节。

    依次尝试 v2 / v1 / 无签名 XOR；任何一种通过图片魔数校验即返回。
    全部失败抛 ValueError，绝不返回猜测内容。
    """
    data = Path(path).read_bytes()
    if not data:
        raise ValueError("empty file")
    magic = data[:6]

    if magic == V2_MAGIC and len(data) >= 15:
        try:
            blob = _decrypt_v2(data, aes_key, xor_key)
            if sniff(blob):
                return blob
        except Exception:
            pass

    if magic == V1_MAGIC and len(data) >= 22:
        blob = bytes(b ^ (xor_key & 0xFF) for b in data[22:])
        if sniff(blob):
            return blob

    # 无签名：整体单字节 XOR
    for key in (xor_key & 0xFF, 0x1F, 0x88, 0x30, 0xFF, 0xE9):
        blob = bytes(b ^ key for b in data)
        if sniff(blob):
            return blob

    raise ValueError(f"unrecognized format, magic={magic.hex()}")


def _decrypt_v2(data: bytes, aes_key: str, xor_key: int) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    blk = _aligned(aes_size)
    off = 15
    aes_data = data[off:off + blk]
    off += blk
    raw = data[off:len(data) - xor_size] if xor_size else data[off:]
    xor_data = data[len(data) - xor_size:] if xor_size else b""
    dec = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).decryptor()
    pt = dec.update(aes_data) + dec.finalize()
    pad = pt[-1] if pt else 0
    if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
        pt = pt[:-pad]
    return pt + raw + bytes(b ^ (xor_key & 0xFF) for b in xor_data)
