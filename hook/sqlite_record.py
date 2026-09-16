"""SQLite record (row payload) decoder.

A row payload is:
    varint header_size
    header_size-1 bytes of serial types (varints)
    values, in order, sized by their serial type

This turns the raw leaf-page payloads into named columns so captured WAL frames
yield actual message rows rather than opaque bytes.
"""
from __future__ import annotations

import struct


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    v = 0
    for _ in range(9):
        if pos >= len(buf):
            return v, pos
        b = buf[pos]; pos += 1
        v = (v << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return v, pos


def _serial_len(t: int) -> int:
    if t == 0: return 0
    if t == 1: return 1
    if t == 2: return 2
    if t == 3: return 3
    if t == 4: return 4
    if t == 5: return 6
    if t == 6: return 8
    if t == 7: return 8
    if t == 8: return 0
    if t == 9: return 0
    if t >= 12:
        return (t - 12) // 2 if t % 2 == 0 else (t - 13) // 2
    return 0


def _read_value(buf: bytes, pos: int, t: int):
    n = _serial_len(t)
    chunk = buf[pos:pos + n]
    if t == 0: return None
    if t == 1: return int.from_bytes(chunk, "big", signed=True)
    if t == 2: return int.from_bytes(chunk, "big", signed=True)
    if t == 3: return int.from_bytes(chunk, "big", signed=True)
    if t == 4: return int.from_bytes(chunk, "big", signed=True)
    if t == 5: return int.from_bytes(chunk, "big", signed=True)
    if t == 6: return int.from_bytes(chunk, "big", signed=True)
    if t == 7: return struct.unpack(">d", chunk)[0] if len(chunk) == 8 else None
    if t == 8: return 0
    if t == 9: return 1
    if t == 10 or t == 11: return None
    if t >= 12:
        if t % 2 == 0:
            try: return chunk.decode("utf-8", "replace")
            except Exception: return None
        return chunk  # blob
    return None


def decode_record(payload: bytes):
    """Return the list of column values in a SQLite row payload."""
    pos = 0
    hdr_size, pos = _varint(payload, pos)
    if hdr_size <= 0 or hdr_size > len(payload):
        return None
    types = []
    tp = pos
    while tp < hdr_size:
        t, tp = _varint(payload, tp)
        types.append(t)
    vals = []
    vp = hdr_size
    for t in types:
        vals.append(_read_value(payload, vp, t))
        vp += _serial_len(t)
    return vals


def as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return f"<blob {len(v)}>"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)
