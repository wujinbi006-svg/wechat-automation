"""WAL-driven event source for the Gateway.

Session boundary (measured, not assumed)
    The hook DLL lives inside WeChat, which runs in Session 1. A named pipe
    created by the Session 0 Gateway service cannot be connected from Session 1
    (Win32 error 5, access denied - probed directly), so the Gateway cannot own
    the pipe. A Session-1 sidecar does, and the Gateway reads events from it
    over 127.0.0.1:18011, an ordinary local socket that crosses sessions fine.

        Gateway (Session 0) --tcp 18011--> sidecar (Session 1) --pipe--> WeChat

This module is the Gateway-side client: it polls the sidecar and republishes
events into the supervisor. The UIA poller stays as a fallback and the
supervisor's dedupe prevents double-publishing.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

SIDECAR_HOST = "127.0.0.1"
SIDECAR_PORT = 18011
POLL_INTERVAL = 0.5


@dataclass
class WalEventStats:
    polls: int = 0
    messages: int = 0
    errors: int = 0
    sidecar_restarts: int = 0
    last_error: str = ""
    last_event_at: float = 0.0
    state: str = "STARTING"          # STARTING | READY | DEGRADED | FAILED
    sidecar: dict = field(default_factory=dict)
    databases: dict = field(default_factory=dict)


class WalEventSource:
    """Polls the Session-1 sidecar and republishes events to the Gateway."""

    def __init__(self, adapter=None, publish: Optional[Callable] = None,
                 host: str = SIDECAR_HOST, port: int = SIDECAR_PORT):
        self.adapter = adapter
        self.publish = publish
        self.host = host
        self.port = port
        self.stats = WalEventStats()
        self._cursor = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="wal-event-poll",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def health(self) -> dict:
        s = self.stats
        return {
            "state": s.state,
            "transport": f"tcp://{self.host}:{self.port}",
            "polls": s.polls,
            "messages": s.messages,
            "errors": s.errors,
            "sidecar_restarts": s.sidecar_restarts,
            "last_error": s.last_error,
            "last_event_at": s.last_event_at,
            "databases": dict(s.databases),
            "sidecar": dict(s.sidecar),
            "wired_into": "gateway_supervisor",
        }

    # -------------------------------------------------------------------- io
    def _rpc(self, method: str, since: int = 0, timeout: float = 5.0) -> Optional[dict]:
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout) as c:
                c.sendall((json.dumps({"method": method, "since": since}) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = c.recv(1 << 20)
                    if not chunk:
                        break
                    buf += chunk
            if not buf:
                return None
            return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def _loop(self) -> None:
        """Poll the sidecar; an outage degrades rather than fails the Gateway.

        Wrapped in a top-level try/except because an unhandled exception here
        kills the thread silently: the stats object still exists (so /status
        keeps reporting the source) but polls stops advancing, which looks like
        a stall rather than a crash.
        """
        while not self._stop.is_set():
            try:
                self.stats.polls += 1
                resp = self._rpc("wal_drain", self._cursor)
                if not resp or not resp.get("ok"):
                    self.stats.state = "DEGRADED"
                    self._stop.wait(2.0)
                    continue
                data = resp.get("data") or {}
                total = int(data.get("total") or 0)
                # The sidecar can restart (its cursor resets to 0) while this
                # client keeps polling. Without this guard the client would keep
                # requesting an offset that no longer exists and silently miss
                # every event produced after the restart.
                if total < self._cursor:
                    self.stats.errors += 1
                    self.stats.last_error = (
                        f"sidecar restarted (total {total} < cursor {self._cursor}); resyncing")
                    self.stats.sidecar_restarts += 1
                    self._cursor = 0
                    continue
                for ev in data.get("events") or []:
                    self._publish(ev)
                    self.stats.messages += 1
                    self.stats.last_event_at = time.time()
                self._cursor = total
                side = data.get("stats") or {}
                self.stats.sidecar = side
                self.stats.databases = side.get("databases") or {}
                self.stats.state = "READY"
            except Exception as exc:
                self.stats.errors += 1
                self.stats.last_error = f"loop: {type(exc).__name__}: {exc}"
                self.stats.state = "DEGRADED"
            self._stop.wait(POLL_INTERVAL)

    def _publish(self, ev: dict) -> None:
        """Hand one event to the supervisor's publisher.

        publish_event's signature is (event, *, source=...) - source is
        KEYWORD-ONLY, so passing it positionally raises TypeError. The old code
        swallowed that TypeError and retried, which worked but made every event
        cost two calls and turned any real failure into a silent drop. Call the
        keyword form directly and surface genuine failures in the stats.
        """
        if not self.publish:
            return
        payload = {
            "event": "message.new",
            "source": "wal_hook",
            "conversation_id": ev.get("conversation_id"),
            "chat_id": ev.get("conversation_id"),
            "chat_name": ev.get("chat_name") or ev.get("conversation_id"),
            "chat_type": ev.get("chat_type") or "direct",
            "message_id": ev.get("message_id"),
            "sender_id": ev.get("sender_id"),
            "sender_name": ev.get("sender_name"),
            "content": ev.get("content"),
            "timestamp": ev.get("timestamp"),
            "message_type": ev.get("message_type"),
            "sort_seq": ev.get("sort_seq"),
        }
        try:
            self.publish(payload, source="wal_hook")
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = f"publish: {type(exc).__name__}: {exc}"
