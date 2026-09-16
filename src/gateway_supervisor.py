from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventHub:
    def __init__(self, max_events: int = 500):
        self._events = deque(maxlen=max_events)
        self._clients: dict[asyncio.Queue, asyncio.AbstractEventLoop | None] = {}
        self._lock = threading.Lock()

    def publish(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)
            clients = list(self._clients.items())
        for queue, loop in clients:
            if loop is None or loop.is_closed():
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass
                continue
            try:
                loop.call_soon_threadsafe(self._enqueue, queue, event)
            except RuntimeError:
                # 连接所属的事件循环已经结束；清理由 websocket finally
                # 完成，这里不能让后台监控线程因为一个失效客户端退出。
                continue

    @staticmethod
    def _enqueue(queue: asyncio.Queue, event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock:
            self._clients[queue] = loop
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._clients.pop(queue, None)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


class GatewaySupervisor:
    def __init__(self, service, interval: float = 3.0):
        self.service = service
        self.interval = interval
        self.events = EventHub()
        # WAL-hook event source: real database write events, so new messages do
        # not have to wait for the next poll. UIA polling stays as a fallback and
        # the dedupe below stops the two sources double-publishing.
        self.wal_source = None
        self.started_at = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reconnect_lock = threading.Lock()
        self._dedupe_lock = threading.RLock()
        self._last_fingerprint: str | None = None
        self._seen_fingerprints: deque[str] = deque(maxlen=2000)
        self._last_success: str | None = None
        self._last_message_event: str | None = None
        self._last_error: str | None = None
        self._reconnect_count = 0
        self._next_reconnect_at = 0.0
        self._reconnect_backoff = 3.0
        self._dedupe_dropped = 0
        self._event_count = 0
        # 事件监听器在去重之后、广播之前同步调用。它们必须廉价且自行入队，
        # 否则会拖慢 WAL 事件线程。
        self._listeners: list[Callable[[dict], None]] = []
        # 自动回复引擎（可选）：挂上后由 /status 一起汇报。
        self.auto_reply: Any = None
        self._logger = logging.getLogger("wechat.gateway.supervisor")

    def add_event_listener(self, listener: Callable[[dict], None]) -> None:
        """注册一个事件监听器（同步调用，必须快速返回）。"""
        if listener not in self._listeners:
            self._listeners.append(listener)
            self._logger.info("event listener attached: %s",
                              getattr(listener, "__qualname__", repr(listener)))

    def remove_event_listener(self, listener: Callable[[dict], None]) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _notify_listeners(self, event: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                # 监听器故障绝不能影响 Gateway 的事件广播。
                self._logger.exception("event listener failed: %s",
                                       getattr(listener, "__qualname__", repr(listener)))

    @staticmethod
    def normalize_message_event(event: dict[str, Any], *, source: str = "uia") -> dict:
        """把 UIA/测试输入统一为 Gateway 的稳定事件合同。

        ``message_id`` 有真实值时优先使用；没有稳定 ID 时保留
        ``dedupe=fingerprint_fallback``，绝不伪造平台消息 ID。
        """
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        event_name = event.get("event") or event.get("type") or "message.new"
        if event_name != "message.new":
            raise ValueError(f"unsupported event: {event_name}")
        conversation_id = (
            event.get("conversation_id")
            or event.get("chat_id")
            or event.get("conversationId")
        )
        content = event.get("content")
        if content is None:
            content = event.get("text")
        if not conversation_id:
            raise ValueError("missing event fields: conversation_id")
        if content is None:
            raise ValueError("missing event fields: content")
        if not isinstance(content, str):
            content = str(content)
        message_id = event.get("message_id") or event.get("messageId")
        chat_type = str(event.get("chat_type") or event.get("chatType") or "direct").lower()
        if chat_type not in {"direct", "group"}:
            chat_type = "direct"
        # 缺少时间戳时必须保留 null，不能在每次轮询时生成新时间戳，否则
        # 同一条 UIA 消息会被误认为是不同事件。真实时间戳由 provider 提供；
        # fallback 指纹依赖下方的稳定 UIA 元数据。
        timestamp = event["timestamp"] if "timestamp" in event else None
        chat_name = event.get("chat_name") or event.get("chatName") or conversation_id
        sender_id = event.get("sender_id") or event.get("senderId")
        sender_name = event.get("sender_name") or event.get("senderName")
        runtime_id = event.get("runtime_id") or event.get("runtimeId")
        rect = event.get("rect") or event.get("bounding_rectangle") or event.get("bounds")
        class_name = event.get("class_name") or event.get("className")
        index = event.get("index")
        normalized = {
            "event": "message.new",
            "type": "message.new",
            "source": source,
            "timestamp": timestamp,
            "chat_id": str(conversation_id),
            "conversation_id": str(conversation_id),
            "chat_name": str(chat_name),
            "chat_type": chat_type,
            "sender_id": str(sender_id) if sender_id is not None else None,
            "sender_name": str(sender_name) if sender_name is not None else None,
            "message_id": str(message_id) if message_id is not None else None,
            "message_type": str(event.get("message_type") or event.get("messageType") or "text"),
            "content": content,
            "runtime_id": (
                list(runtime_id) if isinstance(runtime_id, (tuple, list))
                else (str(runtime_id) if runtime_id is not None else None)
            ),
            "rect": list(rect) if isinstance(rect, (tuple, list)) else rect,
            "class_name": str(class_name) if class_name is not None else None,
            "index": index,
        }
        if normalized["message_id"]:
            normalized["dedupe"] = "message_id"
            normalized["dedupe_key"] = f"id:{normalized['message_id']}"
        else:
            normalized["dedupe"] = "fingerprint_fallback"
            normalized["dedupe_key"] = GatewaySupervisor.fingerprint_message(normalized)
        return normalized

    @staticmethod
    def fingerprint_message(event: dict[str, Any]) -> str:
        """生成没有稳定微信消息 ID 时使用的可解释 fallback 指纹。"""
        parts = (
            event.get("conversation_id") or event.get("chat_id") or "",
            event.get("timestamp") if event.get("timestamp") is not None else "",
            event.get("sender_id") or event.get("sender") or "",
            event.get("message_type") or "text",
            event.get("content") or event.get("text") or "",
            event.get("runtime_id") or event.get("runtimeId") or "",
            event.get("rect") or event.get("bounding_rectangle") or event.get("bounds") or "",
            event.get("class_name") or event.get("className") or "",
            event.get("index") if event.get("index") is not None else "",
        )
        encoded_parts = [
            json.dumps(part, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if isinstance(part, (dict, list, tuple))
            else str(part)
            for part in parts
        ]
        digest = hashlib.sha256("\x1f".join(encoded_parts).encode("utf-8")).hexdigest()
        return f"fp:{digest}"

    def publish_event(self, event: dict, *, source: str = "uia") -> dict | None:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        event_name = event.get("event") or event.get("type") or "message.new"
        normalized = (
            self.normalize_message_event(event, source=source)
            if event_name == "message.new"
            else event
        )
        if normalized.get("event") == "message.new":
            key = normalized.get("dedupe_key") or self.fingerprint_message(normalized)
            with self._dedupe_lock:
                if key in self._seen_fingerprints:
                    self._dedupe_dropped += 1
                    self._logger.info("duplicate message event dropped key=%s", key)
                    return None
                self._seen_fingerprints.append(key)
                self._last_fingerprint = key
                self._last_message_event = utc_now()
                self._event_count += 1
        self._notify_listeners(normalized)
        self.events.publish(normalized)
        return normalized

    def inject_event(self, event: dict) -> dict:
        """仅供显式测试模式使用的事件入口；复用生产 EventHub 广播。"""
        if not isinstance(event, dict):
            raise ValueError("invalid synthetic event")
        normalized = self.publish_event(event, source="synthetic")
        if normalized is None:
            return {
                **self.normalize_message_event(event, source="synthetic"),
                "duplicate": True,
            }
        self._logger.info("synthetic event injected chat=%s message_id=%s",
                          normalized["chat_id"], normalized["message_id"])
        return normalized

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        database = getattr(self.service, "provider", None)
        adapter = getattr(database, "adapter", None)
        starter = getattr(adapter, "start_key_discovery", None)
        if callable(starter):
            starter()
        self._start_wal_source(database, adapter)
        self.started_at = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wechat-supervisor", daemon=True)
        self._thread.start()
        self._logger.info("supervisor started")

    def _start_wal_source(self, database, adapter) -> None:
        """Attach the WAL-hook event source when an adapter is available.

        Failure here must not stop the Gateway: the UIA poller is still running,
        so the supervisor degrades to timer-based reads instead of going down.
        """
        if adapter is None:
            self._logger.info("wal source skipped: no database adapter")
            return
        try:
            from .wal_event_source import WalEventSource

            # The Gateway cannot own the hook pipe: it runs in Session 0 and the
            # pipe lives in Session 1 (cross-session connect is denied). It reads
            # from the Session-1 sidecar over a local socket instead.
            self.wal_source = WalEventSource(adapter, self.publish_event)
            self.wal_source.start()
            self._logger.info("wal source attached (sidecar tcp://127.0.0.1:18011)")
        except Exception as exc:
            self.wal_source = None
            self._logger.warning("wal source failed to start: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self.wal_source is not None:
            try:
                self.wal_source.stop()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        database = getattr(self.service, "provider", None)
        adapter = getattr(database, "adapter", None)
        stopper = getattr(adapter, "stop_key_discovery", None)
        if callable(stopper):
            stopper()
        self._logger.info("supervisor stopped")

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._poll()
            except Exception:
                self._last_error = "supervisor poll failed"
                self._logger.exception("supervisor poll failed")

    def _wal_live(self) -> bool:
        """True when the WAL source is the authoritative event feed."""
        if self.wal_source is None:
            return False
        try:
            return self.wal_source.health().get("state") == "READY"
        except Exception:
            return False

    def _poll(self) -> None:
        process = self._wechat_process_running()
        if not process:
            self._last_error = "wechat process not running"
            return
        try:
            if not self._uia_connected():
                if time.monotonic() >= self._next_reconnect_at:
                    self._reconnect()
                return

            if self._wal_live():
                # The WAL hook is delivering real database writes, so the UIA
                # reader must NOT re-read the last N messages here. Doing that
                # raced the hook: the poller would publish a just-arrived message
                # first and the dedupe then dropped the authoritative WAL copy,
                # so every event was attributed to the fallback source.
                self._last_success = utc_now()
                self._last_error = None
                return

            messages = self.service.read_messages(limit=20)
            self._emit_new_messages(messages)
            self._last_success = utc_now()
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            self._logger.warning("wechat poll failed: %s", exc)
            if time.monotonic() >= self._next_reconnect_at:
                self._reconnect()

    def _reconnect(self) -> None:
        if not self._reconnect_lock.acquire(blocking=False):
            return
        try:
            self._reconnect_count += 1
            self._logger.info("reconnect attempt=%s", self._reconnect_count)
            if self.service.uia:
                self.service.uia.disconnect()
                result = self.service.uia.connect()
                if not isinstance(result, dict) or not result.get("connected"):
                    raise RuntimeError("interactive agent did not establish a UIA connection")
            self.events.publish({"event": "connection.changed", "timestamp": utc_now(), "connected": True})
            self._last_success = utc_now()
            self._last_error = None
            self._reconnect_backoff = 3.0
            self._next_reconnect_at = 0.0
            self._logger.info("reconnect successful")
        except Exception as exc:
            self._last_error = str(exc)
            self._next_reconnect_at = time.monotonic() + self._reconnect_backoff
            self._reconnect_backoff = min(self._reconnect_backoff * 2.0, 60.0)
            self._logger.warning("reconnect failed: %s", exc)
        finally:
            self._reconnect_lock.release()

    def _emit_new_messages(self, result) -> None:
        if not isinstance(result, dict):
            return
        chat = result.get("conversation_id") or result.get("chat")
        chat_name = result.get("chat_name") or chat
        if not chat:
            return
        for index, message in enumerate(result.get("messages") or []):
            if not isinstance(message, dict):
                continue
            content = message.get("text")
            if content is None:
                content = message.get("content")
            if content is None or str(content) == "":
                continue
            normalized = self.publish_event(
                {
                    "event": "message.new",
                    "timestamp": message.get("timestamp"),
                    "conversation_id": chat,
                    "chat_name": chat_name,
                    "message_id": message.get("message_id") or message.get("id"),
                    "message_type": message.get("message_type") or message.get("type") or "text",
                    "sender_id": message.get("sender_id") or message.get("sender"),
                    "sender_name": message.get("sender_name"),
                    "content": str(content),
                    "runtime_id": message.get("runtime_id") or message.get("runtimeId"),
                    "rect": message.get("rect") or message.get("bounding_rectangle"),
                    "class_name": message.get("class_name") or message.get("className"),
                    "index": index,
                },
                source="uia",
            )
            if normalized is not None:
                normalized["index"] = index
                self._logger.info("message event chat=%s message_id=%s dedupe=%s",
                                  normalized["chat_id"], normalized["message_id"],
                                  normalized["dedupe"])

    @staticmethod
    def _wechat_process_running() -> bool:
        try:
            import psutil
            return any((p.info.get("name") or "").lower() in {"weixin.exe", "wechat.exe"} for p in psutil.process_iter(["name"]))
        except Exception:
            return True

    def _uia_connected(self) -> bool:
        """Read the authoritative UIA state instead of a proxy-local cache.

        ``InteractiveAgentProxy.connected`` only reflects the last connect
        attempt made by this process.  The Session 1 agent can lose its UIA
        tree later, so supervisor health and recovery must query IPC status.
        """
        try:
            status = self.service.ui_status()
        except Exception as exc:
            self._last_error = f"UIA status unavailable: {exc}"
            return False
        return bool(status.get("connected")) if isinstance(status, dict) else False

    def status(self) -> dict:
        with self._dedupe_lock:
            event_count = self._event_count
            dedupe_dropped = self._dedupe_dropped
        uia_connected = self._uia_connected()
        wal = self.wal_source.health() if self.wal_source is not None else None
        # A message listener is live if EITHER source can deliver: the WAL hook
        # works without UIA, and UIA polling works without the hook.
        wal_live = bool(wal and wal.get("state") == "READY")
        poller_live = bool(self._thread and self._thread.is_alive() and uia_connected)
        return {
            "gateway": "running",
            "wechat_process": self._wechat_process_running(),
            "wechat_connected": uia_connected,
            "automation_ready": uia_connected,
            "message_listener": bool(wal_live or poller_live),
            "event_sources": {
                "wal_hook": wal,
                "uia_poll": {
                    "state": "READY" if poller_live else "DEGRADED",
                    "interval_seconds": self.interval,
                    "role": "fallback",
                },
            },
            "active_event_source": "wal_hook" if wal_live else ("uia_poll" if poller_live else "none"),
            "connected_clients": self.events.client_count,
            "last_success": self._last_success,
            "last_message_event": self._last_message_event,
            "last_error": self._last_error,
            "reconnect_count": self._reconnect_count,
            "event_count": event_count,
            "dedupe_dropped": dedupe_dropped,
            "auto_reply": (
                self.auto_reply.status() if self.auto_reply is not None else {"enabled": False}
            ),
            "uptime_seconds": int(time.monotonic() - self.started_at),
        }
