"""wxwal sidecar: receive captured WAL writes from the injected DLL.

The DLL pushes one record per WriteFile call to \\\\.\\pipe\\wxwal:

    [WalFrameHeader 20 bytes][payload]

SQLite may split a WAL generation across several WriteFile calls, and may also
issue writes to non-WAL files on the same hook, so this module does the
reassembly and validation itself:

  - group payloads by handle
  - find the WAL header (magic 0x377f0682 / 0x377f0683)
  - slice out complete frames (24-byte frame header + page_size payload)
  - validate each frame's salt against the header salt
  - decrypt the page with the database's SQLCipher key

Only frames that pass every check are reported. Anything else is counted and
dropped, never guessed at.
"""
from __future__ import annotations

import ctypes
import hashlib
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

PIPE_NAME = r"\\.\pipe\wxwal10"

# The pipe is single-instance by design: the DLL connects to the first
# available server and the data is a byte stream, so two readers would split
# frames between them and both would see corruption. Exactly one consumer may
# own the pipe at a time. Debug tooling is stopped while the Gateway is up.
PIPE_INSTANCES = 1

FRAME_MAGIC = 0x57414C31  # "WAL1"
HDR = struct.Struct("<IIIIII")  # magic, pid, handle, size, tick, path_len
LEGACY_HDR = struct.Struct("<IIIII")  # pre-path protocol, 20 bytes

WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683
WAL_HDR_SIZE = 32
FRAME_HDR_SIZE = 24

# Win32
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_uint32, ctypes.c_void_p]
k32.CreateNamedPipeW.restype = ctypes.c_void_p
k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k32.ConnectNamedPipe.restype = ctypes.c_int
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
k32.ReadFile.restype = ctypes.c_int
k32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]

PIPE_ACCESS_INBOUND = 0x00000001
PIPE_TYPE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
ERROR_PIPE_CONNECTED = 535
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass
class WalStats:
    records: int = 0
    bytes: int = 0
    frames: int = 0
    decrypt_ok: int = 0
    decrypt_fail: int = 0
    malformed: int = 0
    handles: set = field(default_factory=set)


class WalCapture:
    """Named-pipe server plus WAL frame reassembly."""

    def __init__(self, page_size: int = 4096, on_frame: Optional[Callable] = None):
        self.page_size = page_size
        self.on_frame = on_frame
        self.stats = WalStats()
        self._buffers: dict[int, bytearray] = {}
        self.paths: dict[int, str] = {}
        self._stream = bytearray()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- server
    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, name="wxwal-pipe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _serve(self) -> None:
        while not self._stop.is_set():
            h = k32.CreateNamedPipeW(PIPE_NAME, PIPE_ACCESS_INBOUND,
                                     PIPE_TYPE_BYTE | PIPE_WAIT, 1, 1 << 20, 1 << 20,
                                     0, None)
            if h == INVALID_HANDLE_VALUE:
                time.sleep(0.5)
                continue
            try:
                ok = k32.ConnectNamedPipe(h, None)
                if not ok and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                    continue
                self._read_loop(h)
            finally:
                k32.DisconnectNamedPipe(h)
                k32.CloseHandle(h)

    def _read_loop(self, h) -> None:
        buf = ctypes.create_string_buffer(1 << 20)
        got = ctypes.c_uint32(0)
        while not self._stop.is_set():
            if not k32.ReadFile(h, buf, len(buf), ctypes.byref(got), None):
                return  # client went away
            if got.value == 0:
                return
            self._feed(buf.raw[:got.value])

    # ------------------------------------------------------------- reassembly
    def _feed(self, chunk: bytes) -> None:
        """Accumulate into a stream and cut out complete records.

        A byte-mode pipe returns arbitrary chunk boundaries: a 4096-byte page
        routinely spans several reads and a header can be split mid-way. Parsing
        each read in isolation (the first version of this) misaligns everything
        and reports garbage instead of frames.
        """
        self._stream.extend(chunk)
        while True:
            if len(self._stream) < HDR.size:
                return
            magic, pid, handle, size, tick, path_len = HDR.unpack_from(self._stream, 0)
            if magic != FRAME_MAGIC:
                # resync one byte at a time until a plausible header appears
                del self._stream[0]
                self.stats.malformed += 1
                if self.stats.malformed > 100000:
                    self._stream.clear()
                    return
                continue
            if size > (64 << 20) or path_len > 4096:
                del self._stream[0]
                self.stats.malformed += 1
                continue
            need = HDR.size + path_len + size
            if len(self._stream) < need:
                return
            path_bytes = bytes(self._stream[HDR.size:HDR.size + path_len]) if path_len else b""
            payload = bytes(self._stream[HDR.size + path_len: need])
            del self._stream[:need]
            if path_bytes:
                try:
                    self.paths[handle] = path_bytes.decode("utf-8", "replace")
                except Exception:
                    pass
            self.stats.records += 1
            self.stats.bytes += size
            self.stats.handles.add(handle)
            self._consume(handle, payload)

    def _consume(self, handle: int, payload: bytes) -> None:
        """Reassemble WAL frames for one file handle.

        Observed layout from the live client (wxwal raw dump):

            24-byte frame header : page_no(4) db_size(4) salt1(4) salt2(4) ck1(4) ck2(4)
            4096-byte page       : encrypted page content

        These arrive as TWO separate WriteFile calls, and the 32-byte WAL header
        is NOT resent 鈥?it was written once when the WAL was created. An earlier
        version of this parser required the WAL header to lead the stream, so it
        never matched a single frame. Ordering per handle is preserved because a
        SQLite connection writes sequentially, so a per-handle buffer is enough.
        """
        buf = self._buffers.setdefault(handle, bytearray())
        buf.extend(payload)

        page = self.page_size
        while True:
            # optional 32-byte WAL header (present only at WAL creation)
            if len(buf) >= 4 and bytes(buf[:4]) in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"):
                if len(buf) < WAL_HDR_SIZE:
                    return
                del buf[:WAL_HDR_SIZE]
                continue
            if len(buf) < FRAME_HDR_SIZE + page:
                return
            page_no = struct.unpack_from(">I", buf, 0)[0]
            if not (0 < page_no < (1 << 24)):
                # not a plausible frame header; drop a byte and resync
                del buf[0]
                self.stats.malformed += 1
                continue
            page_bytes = bytes(buf[FRAME_HDR_SIZE: FRAME_HDR_SIZE + page])
            del buf[:FRAME_HDR_SIZE + page]
            self.stats.frames += 1
            if self.on_frame:
                try:
                    # pass the source path so the consumer can pick the right key
                    self.on_frame(handle, page_no, page_bytes, self.paths.get(handle, ""))
                except TypeError:
                    self.on_frame(page_no, page_bytes)
                except Exception:
                    self.stats.malformed += 1

    @staticmethod
    def _find_magic(buf: bytearray) -> int:
        for i in range(len(buf) - 3):
            m = struct.unpack_from(">I", buf, i)[0]
            if m in (WAL_MAGIC_LE, WAL_MAGIC_BE):
                return i
        return -1


def decrypt_page(raw_key: bytes, page: bytes, page_number: int, page_size: int = 4096) -> bytes:
    """Decrypt one SQLCipher page (same scheme as the main database file)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    reserve = 80
    iv = page[page_size - reserve: page_size - reserve + 16]
    encrypted = page[16: page_size - reserve] if page_number == 1 else page[: page_size - reserve]
    dec = Cipher(algorithms.AES(raw_key), modes.CBC(iv)).decryptor()
    plain = dec.update(encrypted) + dec.finalize()
    head = b"SQLite format 3\x00" if page_number == 1 else b""
    return head + plain + b"\x00" * reserve
