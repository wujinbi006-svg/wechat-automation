"""wxwal Session-1 sidecar: owned pipe + delta reader, served over IPC.

Why this process exists
    A named pipe created by the Session 0 Gateway service cannot be connected
    from Session 1 (Win32 error 5, access denied - probed directly). The hook
    DLL lives inside WeChat, which runs in Session 1, so the pipe must be owned
    by Session 1. This sidecar does that and answers the Session 0 Gateway over
    127.0.0.1:18011.

Design: frame as TRIGGER, not as payload
    An earlier version tried to reconstruct every message straight out of the
    WAL page, which requires attributing each page to a table (Msg_<md5(user)>)
    by walking that table's b-tree. That mapping is incomplete in practice and
    repeatedly mislabelled rows (FTS index pages surfacing as chat messages).

    The page is now used only as a signal: "this database was written". The
    delta itself is then read through the already-verified database reader,
    which knows every table, the zstd compression, and the sender mapping. The
    trigger still makes this event-driven; it just cannot invent a message.
"""
from __future__ import annotations

import hashlib
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
SERVICE_ROOT = BASE.parent
for p in (str(SERVICE_ROOT), str(BASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from wal_capture import WalCapture, decrypt_page  # noqa: E402

IPC_HOST = "127.0.0.1"
IPC_PORT = int(os.getenv("WXWAL_IPC_PORT", "18011"))
PAGE = 4096
DEBOUNCE = 0.3


class WalSidecar:
    """Owns the pipe; converts write events into message deltas."""

    def __init__(self, adapter=None):
        self.adapter = adapter
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.stats = {
            "state": "STARTING", "frames": 0, "decrypted": 0, "messages": 0,
            "errors": 0, "databases": {}, "last_error": "",
            "started_at": time.time(), "chats_seen": 0,
        }
        self._keys: dict[str, bytes] = {}
        self._seen: set = set()
        self._capture: WalCapture | None = None
        self._dirty = threading.Event()
        self._last_drain = 0.0
        self._last_seq: dict[str, int] = {}
        self._last_session: dict[str, int] = {}
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ startup
    def start(self) -> None:
        self._prime()
        self._capture = WalCapture(page_size=PAGE, on_frame=self._on_frame)
        self._capture.start()
        self._worker = threading.Thread(target=self._drain_loop, name="wal-drain", daemon=True)
        self._worker.start()
        self.stats["state"] = "READY"

    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()
        if self._capture:
            self._capture.stop()

    def _prime(self) -> None:
        """Record current session stamps; only later changes count as new."""
        if self.adapter is None:
            return
        try:
            for c in self.adapter.get_chats() or []:
                u = c.get("id") or c.get("chat_id")
                if u:
                    self._last_session[str(u)] = c.get("time") or 0
        except Exception as exc:
            self.stats["last_error"] = f"prime: {exc}"

    # --------------------------------------------------------------------- pipe
    def _raw_key(self, db_path: Path):
        cached = self._keys.get(str(db_path))
        if cached:
            return cached
        if self.adapter is None:
            return None
        try:
            for _n, info in self.adapter.discover_databases().items():
                if Path(info.path) == db_path:
                    k = self.adapter._key_for(info)
                    if k:
                        self._keys[str(db_path)] = k
                        return k
        except Exception:
            pass
        return None

    @staticmethod
    def _db_path(wal_path: str):
        p = wal_path.replace("\\\\?\\", "")
        if p.endswith("-wal"):
            p = p[:-4]
        cand = Path(p)
        return cand if cand.exists() else None

    def _on_frame(self, handle, page_no, data, path) -> None:
        s = self.stats
        s["frames"] += 1
        try:
            db = self._db_path(path)
            if db is None:
                return
            name = db.name
            s["databases"][name] = s["databases"].get(name, 0) + 1
            # Only the message databases carry chat traffic.
            if not (name.startswith("message_") and name.endswith(".db")):
                return
            if name.endswith("_fts.db"):
                return
            key = self._raw_key(db)
            if not key:
                return
            decrypt_page(key, data, page_no, PAGE)
            s["decrypted"] += 1
            now = time.monotonic()
            if now - self._last_drain >= DEBOUNCE:
                self._last_drain = now
                self._dirty.set()
        except Exception as exc:
            s["errors"] += 1
            s["last_error"] = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------- drain
    def _drain_loop(self) -> None:
        while not self._stop.is_set():
            self._dirty.wait(timeout=1.0)
            self._dirty.clear()
            if self._stop.is_set():
                break
            try:
                self._drain()
            except Exception as exc:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"drain: {type(exc).__name__}: {exc}"

    def _drain(self) -> None:
        if self.adapter is None:
            return
        sessions = self.adapter.get_chats() or []
        self.stats["chats_seen"] = len(sessions)
        changed = []
        for meta in sessions:
            u = meta.get("id") or meta.get("chat_id")
            if not u:
                continue
            u = str(u)
            stamp = meta.get("time") or 0
            if stamp > self._last_session.get(u, 0):
                changed.append((u, meta))
            elif stamp > 0:
                self._last_session[u] = max(self._last_session.get(u, 0), stamp)

        for user, meta in changed:
            self._last_session[user] = meta.get("time") or 0
            try:
                rows = self.adapter.get_messages(user, limit=20)
            except Exception:
                continue
            for row in rows:
                seq = row.get("sort_seq") or 0
                if seq <= self._last_seq.get(user, 0):
                    continue
                self._last_seq[user] = seq
                ev = self._row_event(user, row, meta)
                if ev:
                    with self.lock:
                        self.events.append(ev)
                        if len(self.events) > 5000:
                            del self.events[:2000]
                    self.stats["messages"] += 1

    def _row_event(self, user: str, row: dict, meta: dict) -> dict | None:
        mid = row.get("message_id") or row.get("local_id")
        seq = row.get("sort_seq") or 0
        key = (user, mid, seq)
        if key in self._seen:
            return None
        self._seen.add(key)
        if len(self._seen) > 20000:
            self._seen.clear()
        return {
            "event": "message.new",
            "source": "wal_hook",
            "conversation_id": user,
            "chat_id": user,
            "chat_name": meta.get("name") or user,
            "chat_type": "group" if user.endswith("@chatroom") else "direct",
            "message_id": str(mid) if mid is not None else None,
            "sender_id": row.get("sender_id"),
            "sender_name": row.get("sender_name") or row.get("sender_id"),
            "message_type": row.get("message_type") or "text",
            "content": row.get("content") or row.get("text") or "",
            "timestamp": row.get("create_time"),
            "sort_seq": seq,
        }

    # -------------------------------------------------------------------- read
    def drain(self, since: int = 0) -> dict:
        with self.lock:
            batch = self.events[since:since + 200]
            total = len(self.events)
        safe = []
        for ev in batch:
            safe.append({k: (v if isinstance(v, (str, int, float, bool)) or v is None
                             else str(v)) for k, v in ev.items()})
        return {"events": safe, "total": total, "stats": dict(self.stats)}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline().decode("utf-8"))
            m = req.get("method")
            sc = self.server.sc
            if m == "wal_status":
                out = {"ok": True, "data": sc.drain(req.get("since", 0))["stats"]}
            elif m == "wal_drain":
                out = {"ok": True, "data": sc.drain(req.get("since", 0))}
            elif m == "wal_ping":
                out = {"ok": True, "data": {"alive": True}}
            else:
                out = {"ok": False, "error": f"unsupported method: {m}"}
            payload = json.dumps(out, ensure_ascii=False, default=str)
        except Exception as exc:
            payload = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        self.wfile.write((payload + "\n").encode("utf-8"))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, sc):
        super().__init__((IPC_HOST, IPC_PORT), Handler)
        self.sc = sc


def main() -> int:
    try:
        from src.database_adapter import DatabaseAdapter
        adapter = DatabaseAdapter()
    except Exception as exc:
        print(f"adapter unavailable ({exc})", flush=True)
        adapter = None
    sc = WalSidecar(adapter)
    sc.start()
    server = Server(sc)
    print(f"wxwal sidecar listening on {IPC_HOST}:{IPC_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sc.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
