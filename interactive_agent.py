"""Session 1 中运行的 Interactive Agent。

负责在交互会话中执行 UIA 读取与显式控制动作；真实发送由服务总线统一编排与验证。
"""
from __future__ import annotations

import json
import os
import socketserver
import threading
import time
import ctypes
import logging

from src.interactive_ipc import IPC_HOST, IPC_PORT, IPC_TOKEN
from src.uia_service import ReplicaUIADriver, WeChatUIAService


def _real_send_enabled() -> bool:
    """Keep IPC write operations default-off even if callers bypass Gateway."""
    return os.getenv("WECHAT_ENABLE_SEND", "").strip().lower() in {"1", "true", "yes", "on"}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline().decode("utf-8"))
            if req.get("token") != IPC_TOKEN:
                raise PermissionError("invalid local agent token")
            result = self.server.agent.dispatch(req.get("method"), req.get("params") or {})
            out = {"success": True, "data": result}
        except Exception as exc:
            out = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))


class AgentServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, agent):
        super().__init__((IPC_HOST, IPC_PORT), Handler)
        self.agent = agent


class InteractiveAgent:
    def __init__(self):
        self.service = WeChatUIAService(ReplicaUIADriver())
        self.started_at = time.time()
        self.last_error = None
        self.lifecycle = "STARTING"
        self.reconnect_count = 0
        self.last_success = None
        # A passive UIA operation that changes foreground is a safety incident,
        # not a transient connectivity fault.  Do not keep recreating drivers
        # and probing after one is observed; that could repeatedly interrupt
        # the user's active application.
        self._foreground_safety_blocked = False
        self._service_lock = threading.RLock()
        self._stop = threading.Event()
        self._logger = logging.getLogger("wechat.interactive_agent")
        threading.Thread(target=self._bootstrap, name="interactive-agent-bootstrap", daemon=True).start()
        threading.Thread(target=self._health_loop, name="interactive-agent-health", daemon=True).start()

    def _replace_service(self):
        """丢弃旧 UIA/COM 根对象并创建全新实例。"""
        with self._service_lock:
            old = self.service
            try:
                old.close()
            except Exception:
                pass
            self.service = WeChatUIAService(ReplicaUIADriver())

    def _connect_once(self) -> bool:
        if self._foreground_safety_blocked:
            raise RuntimeError("FOREGROUND_SAFETY_BLOCKED: passive UIA monitoring is paused")
        with self._service_lock:
            self.lifecycle = "CONNECTING_UIA"
            self.service.connect()
            self.last_error = None
            self.last_success = time.time()
            self.lifecycle = "READY"
            return self.service.get_status()

    def _active_initialize_once(self) -> None:
        """在启动期执行一次显式 UIA gate 初始化，随后仅走被动探针。"""
        initializer = getattr(self.service.driver, "active_initialize_accessibility", None)
        if not callable(initializer):
            return
        result = initializer(timeout=8.0)
        self._logger.info("active UIA initialization result=%s", result)

    def _is_foreground_safety_error(self, exc: Exception) -> bool:
        return "改变了前台窗口" in str(exc) or "FOREGROUND_CHANGED" in str(exc)

    def _block_foreground_safety(self, exc: Exception) -> None:
        self._foreground_safety_blocked = True
        self.lifecycle = "FOREGROUND_SAFETY_BLOCKED"
        self.last_error = f"FOREGROUND_SAFETY_BLOCKED: {exc}"
        self._logger.error("passive UIA changed foreground; automatic recovery paused: %s", exc)

    def _bootstrap(self):
        for delay in (0, 2, 5, 10, 20, 30, 30, 30):
            if delay:
                time.sleep(delay)
            try:
                if delay == 0:
                    self._active_initialize_once()
                self._connect_once()
                self._logger.info("UIA bootstrap successful")
                return
            except Exception as exc:
                if self._is_foreground_safety_error(exc):
                    self._block_foreground_safety(exc)
                    return
                self.last_error = str(exc)
                self.lifecycle = "WAITING_FOR_WECHAT"
                self._logger.warning("UIA bootstrap failed: %s", exc)
                # UIA 树可能在微信启动过程中被替换；每次重试重建 driver，
                # 避免复用失效的 COM/UIA 根对象。
                try:
                    self._replace_service()
                except Exception as rebuild_exc:
                    self._logger.warning("UIA driver rebuild failed: %s", rebuild_exc)
        self.lifecycle = "DEGRADED"

    def _health_loop(self):
        """周期探测 UIA；失败时保持 Agent 存活并重建连接。"""
        failure_streak = 0
        next_reconnect_at = 0.0
        while not self._stop.wait(3.0):
            if self._foreground_safety_blocked:
                continue
            try:
                with self._service_lock:
                    service = self.service
                    connected = bool(service.connected)
                    running = bool(service.driver and service.driver.is_running())
                if not running:
                    self.lifecycle = "WAITING_FOR_WECHAT"
                    self.last_error = "微信进程未运行"
                    continue
                probe_ok = False
                if connected:
                    # Health probing is part of the passive monitor contract.
                    # It must use the same foreground invariant as connection,
                    # discovery, and message reads; never call the worker
                    # directly because that would bypass the evidence check.
                    # Require the explicit read-only contract.  ``ensure_window``
                    # is intentionally not accepted here because third-party
                    # wrappers may use it as an activation/wake operation.
                    probe = getattr(service.driver, "read_only_probe", None)
                    probe_ok = bool(service._read_only_call("health_probe", probe)) if callable(probe) else False
                if not connected or not probe_ok:
                    now = time.monotonic()
                    if now < next_reconnect_at:
                        continue
                    self.lifecycle = "DEGRADED"
                    self.reconnect_count += 1
                    self._logger.info("UIA connection lost; reconnect attempt=%s", self.reconnect_count)
                    self._replace_service()
                    self._connect_once()
                    failure_streak = 0
                    next_reconnect_at = 0.0
                    self._logger.info("UIA reconnect successful")
            except Exception as exc:
                if self._is_foreground_safety_error(exc):
                    self._block_foreground_safety(exc)
                    continue
                self.lifecycle = "DEGRADED"
                self.last_error = str(exc)
                self._logger.warning("UIA health probe failed: %s", exc)
                failure_streak += 1
                delay = min(60.0, 3.0 * (2 ** min(failure_streak - 1, 4)))
                next_reconnect_at = time.monotonic() + delay
                self._logger.info("UIA reconnect delayed seconds=%s", delay)
                try:
                    self._replace_service()
                except Exception:
                    self._logger.exception("UIA recovery rebuild failed")

    def dispatch(self, method, params):
        if method == "status":
            with self._service_lock:
                connected = self.service.connected
            with self._service_lock:
                status = self.service.get_status()
            session_id = ctypes.c_uint32()
            ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id))
            return {
                **status,
                "worker": self.service.worker_status(),
                "navigation_stats": getattr(self.service.driver, "navigation_stats", lambda: {})(),
                "lifecycle": self.lifecycle,
                "session_id": int(session_id.value),
                "username": os.getenv("USERNAME", ""),
                "agent_pid": os.getpid(),
                "heartbeat": time.time(),
                "uia_ready": bool(status.get("connected")),
                "wechat_detected": bool(self.service.driver and self.service.driver.is_running()),
                # The agent only has a usable passive listener after the UIA
                # tree is actually connected.  Do not report a configured
                # monitor as live when bootstrap failed.
                "message_listener": bool(status.get("connected")),
                "last_error": self.last_error,
                "last_success": self.last_success,
                "reconnect_count": self.reconnect_count,
                "foreground_policy": "NEVER_STEAL",
                "foreground_safety_blocked": self._foreground_safety_blocked,
                # 让上层无需实际发送即可判断写操作是否被环境放行。
                "send_enabled": _real_send_enabled(),
            }
        if method == "connect":
            return self._connect_once()
        if method == "disconnect":
            with self._service_lock:
                self.service.disconnect()
                return self.service.get_status()
        if method == "get_chats":
            return self.service.get_chats()
        if method == "get_current_chat":
            return self.service.get_current_chat()
        if method == "search_chat":
            return self.service.search_chat(params["name"])
        if method == "open_chat":
            return self.service.open_chat(params["name"])
        if method == "interactive_open_chat":
            # Explicit, user-authorized UI navigation.  This is deliberately
            # separate from the passive ``open_chat`` monitor path.
            return self.service.interactive_open_chat(params["name"])
        if method == "read_messages":
            return self.service.read_messages(int(params.get("limit", 20)))
        if method == "get_input_state":
            return self.service.get_input_state()
        if method == "diagnostics":
            with self._service_lock:
                if not self.service.connected:
                    self._connect_once()
                diagnostics = getattr(self.service.driver, "diagnostics", None)
                if diagnostics is None:
                    raise ValueError("UIA diagnostics unavailable")
                # 完整 Raw/Control/Content View 诊断可能跨越数千个 COM 节点，
                # 使用独立上限，避免正常诊断被普通 UIA 操作的 8 秒预算误判。
                # Diagnostics enumerates UIA trees and must obey the same
                # foreground invariant as polling, bootstrap, and recovery.
                return self.service._read_only_call("diagnostics", diagnostics, timeout=30.0)
        if method == "send_text":
            if not _real_send_enabled():
                raise PermissionError("SEND_DISABLED: WECHAT_ENABLE_SEND is not enabled")
            with self._service_lock:
                if not self.service.connected:
                    self._connect_once()
                return self.service.send_text(params["text"])
        if method == "send_image":
            if not _real_send_enabled():
                raise PermissionError("SEND_DISABLED: WECHAT_ENABLE_SEND is not enabled")
            with self._service_lock:
                if not self.service.connected:
                    self._connect_once()
                return self.service.send_image(**params)
        if method == "send_file":
            if not _real_send_enabled():
                raise PermissionError("SEND_DISABLED: WECHAT_ENABLE_SEND is not enabled")
            with self._service_lock:
                if not self.service.connected:
                    self._connect_once()
                return self.service.send_file(**params)
        raise ValueError(f"unsupported method: {method}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler("logs/interactive_agent.log", encoding="utf-8"),
                  logging.StreamHandler()],
    )
    agent = InteractiveAgent()
    server = AgentServer(agent)
    print(f"interactive agent listening on {IPC_HOST}:{IPC_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        agent._stop.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
