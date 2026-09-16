from __future__ import annotations
from .errors import ProviderUnavailable, ForegroundRequired
from .errors import UIAOperationTimeout
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import threading
import os
import time
import uuid
import ctypes


# 后台监控的不可变安全策略。主动 UI 操作必须通过显式 interactive_* 入口，
# 不能由连接、探针、监听或恢复路径隐式升级权限。
# This is deliberately not configurable to a more permissive value.  The
# process may receive environment variables from a service manager, and a
# stale value must never make a monitor path interactive again.
FOREGROUND_POLICY = "NEVER_STEAL"

class WeChatUIAService:
    """UIA 门面；默认不自动激活、不切换前台。"""
    def __init__(self, driver=None, timeout=8.0):
        self.driver=driver; self.connected=False; self.timeout=timeout; self.last_operation=None
        self._worker_thread_id = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia-worker",
                                            initializer=self._init_worker)
    def _init_worker(self):
        self._worker_thread_id = threading.get_ident()
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
            except Exception:
                pass
    def _call(self, operation, fn, *args, timeout=None):
        self.last_operation={"operation":operation}
        fut=self._executor.submit(fn, *args)
        try:
            return fut.result(timeout=self.timeout if timeout is None else timeout)
        except TimeoutError:
            fut.cancel()
            raise UIAOperationTimeout(operation,self.timeout if timeout is None else timeout)

    def _read_only_call(self, operation, fn, *args, timeout=None):
        """Run a passive UIA read and reject a result if it stole foreground.

        The snapshot is evidence, not a compensating action: a monitor must
        never try to put a user window back after changing it.
        """
        snapshot = getattr(self.driver, "_foreground_snapshot", None)
        if not callable(snapshot):
            raise ProviderUnavailable("UIA driver lacks a foreground safety snapshot")
        before = snapshot()
        result = None
        after = None
        call_error = None
        try:
            result = self._call(operation, fn, *args, timeout=timeout)
        except Exception as exc:
            call_error = exc
        finally:
            # 即使底层 UIA 调用抛异常，也必须取得 after 快照；某些 provider
            # 可能先改变前台再失败，不能让异常路径绕过安全判定。
            after = snapshot() if callable(snapshot) else None
        verifiable = self._foreground_snapshot_verifiable(before, after)
        changed = self._foreground_snapshot_changed(before, after) if verifiable else None
        safety_violation = self._foreground_safety_violation(before, after) if verifiable else None
        interaction_violation = self._read_only_interaction_violation(before, after) if verifiable else None
        self.last_operation = {
            "operation": operation,
            "mode": "PASSIVE_READ_ONLY",
            "foreground_before": before,
            "foreground_after": after,
            "foreground_verifiable": verifiable,
            "foreground_changed": changed,
            "foreground_safety_violation": safety_violation,
            "read_only_interaction_violation": interaction_violation,
        }
        if not verifiable:
            raise ProviderUnavailable("无法验证被动 UIA 操作期间的前台/焦点状态")
        if safety_violation or interaction_violation:
            raise ProviderUnavailable("只读 UIA 操作改变了前台窗口")
        if call_error is not None:
            raise call_error
        return result

    @staticmethod
    def _foreground_snapshot_verifiable(before, after):
        """判断前台安全证据是否完整；无法验证时被动路径必须失败关闭。"""
        if not (isinstance(before, dict) and isinstance(after, dict)):
            return False
        if not (before.get("available") and after.get("available")):
            return False
        required = ("hwnd", "pid", "gui_info_available", "cursor_available", "z_order_available")
        return all(
            all(key in snapshot for key in required)
            and snapshot.get("hwnd") is not None
            and snapshot.get("pid") is not None
            and (bool(snapshot.get("gui_info_available")) or bool(snapshot.get("foreground_query_available")))
            and bool(snapshot.get("cursor_available"))
            and bool(snapshot.get("z_order_available"))
            for snapshot in (before, after)
        )

    @staticmethod
    def _foreground_snapshot_changed(before, after):
        """Compare all available user-input/window state without side effects."""
        if not (isinstance(before, dict) and isinstance(after, dict)):
            return False
        if not (before.get("available") and after.get("available")):
            return False
        # ``cursor`` is evidence only.  It is controlled by the user and does
        # not identify an activation/focus/z-order change caused by this
        # process.  Treating a normal user mouse movement as a safety breach
        # would unnecessarily stop passive monitoring.
        # ``GW_HWNDPREV`` is retained in the snapshot for diagnostics, but it
        # is not foreground ownership.  It changes when unrelated windows
        # alter their z-order while the user continues to work in the same
        # foreground HWND.  Treating that telemetry as a safety breach can
        # permanently freeze a healthy passive agent without any activation.
        keys = ("hwnd", "active_hwnd", "focus_hwnd", "capture_hwnd")
        return any(
            (before.get(key) is not None or after.get(key) is not None)
            and before.get(key) != after.get(key)
            for key in keys
        )

    def _foreground_safety_violation(self, before, after):
        """Return whether a passive operation promoted WeChat to foreground.

        A foreground snapshot does not identify which process caused a change.
        Users can legitimately switch between unrelated applications while a
        probe is running.  Treating every such switch as our fault freezes the
        agent despite no WeChat activation.  The invariant this service owns is
        narrower and observable: a passive operation must never move the
        foreground from a non-WeChat window to WeChat.
        """
        checker = getattr(self.driver, "foreground_safety_violation", None)
        if callable(checker):
            return bool(checker(before, after))
        return self._foreground_snapshot_changed(before, after)

    @staticmethod
    def _read_only_interaction_violation(before, after):
        """Detect focus/capture mutation even when the foreground HWND is stable.

        A user may legitimately switch between two unrelated foreground windows
        while a probe runs.  That is diagnostic telemetry, not evidence that the
        monitor stole focus.  Active/focus/capture changes under a stable
        foreground HWND are different: they can only occur through interaction
        with that window and must fail closed on a passive path.
        """
        if not (isinstance(before, dict) and isinstance(after, dict)):
            return False
        if before.get("hwnd") != after.get("hwnd"):
            return False
        return any(before.get(key) != after.get(key) for key in ("active_hwnd", "focus_hwnd", "capture_hwnd"))
    def worker_status(self):
        return {"thread_id": self._worker_thread_id, "thread_name": "uia-worker",
                "dedicated": True, "com_apartment": "MTA" if os.name == "nt" else "unknown"}
    def execute_command(self, operation, fn, *args, request_id=None, timeout=None):
        """统一 UIA 命令信封；底层仍由单一 Worker 串行执行。"""
        request_id = request_id or uuid.uuid4().hex
        try:
            result = self._call(operation, fn, *args, timeout=timeout)
            return {"ok": True, "request_id": request_id, "operation": operation, "result": result}
        except TimeoutError:
            return {"ok": False, "request_id": request_id, "operation": operation,
                    "error": {"code": "UIA_TIMEOUT", "message": f"{operation} timed out"}}
        except Exception as exc:
            code = "UIA_PROVIDER_ERROR"
            text = str(exc).lower()
            if "stale" in text or "unavailable" in text:
                code = "UIA_ELEMENT_STALE"
            return {"ok": False, "request_id": request_id, "operation": operation,
                    "error": {"code": code, "message": str(exc)}}
    def connect(self):
        if self.driver is None: raise ProviderUnavailable("UIA driver not configured; use manual integration mode")
        # 监控连接必须显式实现只读探针。不能回退到语义不明的
        # ``ensure_window()``，因为第三方实现可能默认 wake/activate。
        probe = getattr(self.driver, "read_only_probe", None)
        if not callable(probe):
            raise ProviderUnavailable("UIA driver lacks an explicit read_only_probe")
        # 连接与健康探测属于只读路径，不得因初始化而抢占用户前台。
        if not self._read_only_call("connect", probe):
            raise ProviderUnavailable("微信 UIA 主窗口不可用")
        self.connected=True
        return self.get_status()
    def disconnect(self): self.connected=False
    def close(self):
        self.connected = False
        self._executor.shutdown(wait=False, cancel_futures=True)
    def get_status(self): return {"connected":self.connected,"available":self.driver is not None,"provider":"UIA"}
    def _require(self):
        if self.driver is None: raise ProviderUnavailable("UIA driver not configured")
        if not self.connected:
            self.connect()
    def get_chats(self):
        self._require()
        if hasattr(self.driver,"get_chats"): return self._read_only_call("get_chats", self.driver.get_chats)
        return []
    def get_current_chat(self):
        self._require()
        return self._read_only_call("current_chat", self.driver.current_chat) if hasattr(self.driver,"current_chat") else None
    def search_chat(self,name):
        self._require()
        return self._read_only_call("search_chat", self.driver.search_chat, name) if hasattr(self.driver,"search_chat") else []
    def open_chat(self,name):
        self._require()
        try:
            current = self.get_current_chat()
            current_name = current.get("name") if isinstance(current, dict) else current
            if current_name == name:
                return True
        except Exception:
            pass
        fn=getattr(self.driver,"direct_open_chat",None)
        if fn is not None:
            # The driver tries several UIA patterns and verifies the selected
            # conversation after each one.  This is an explicit interactive
            # action, so its budget must not inherit the short passive-read
            # timeout used by health probes.
            result = self._call("open_chat", fn, name, timeout=30.0)
            return bool(result)
        if hasattr(self.driver, "open_chat"):
            return self._call("open_chat", self.driver.open_chat, name, timeout=30.0)
        raise ProviderUnavailable("UIA open_chat implementation unavailable")
    def interactive_open_chat(self, name):
        self._require()
        if not hasattr(self.driver, "interactive_open_chat"):
            raise ProviderUnavailable("UIA interactive open_chat unavailable")
        return self._call("interactive_open_chat", self.driver.interactive_open_chat, name)
    def get_chat_input_field(self):
        self._require()
        return self._read_only_call("get_chat_input_field", self.driver.get_chat_input_field) if hasattr(self.driver,"get_chat_input_field") else None
    def read_messages(self, limit=20):
        self._require()
        if not hasattr(self.driver, "read_messages"):
            raise ProviderUnavailable("UIA message reader not available")
        return self._read_only_call("read_messages", self.driver.read_messages, limit)
    def get_input_state(self):
        """只读返回当前输入框状态，供发送结果验证使用。"""
        self._require()
        if not hasattr(self.driver, "get_input_state"):
            raise ProviderUnavailable("UIA input-state reader not available")
        return self._read_only_call("get_input_state", self.driver.get_input_state)
    def send_text(self, text):
        """显式交互操作；调用方必须自行完成发送结果验证。"""
        self._require()
        if not hasattr(self.driver, "send_text"):
            raise ProviderUnavailable("UIA send implementation unavailable")
        return self._call("send_text", self.driver.send_text, text)
    def send_image(self, path):
        self._require()
        if not hasattr(self.driver, "send_image"):
            raise ProviderUnavailable("UIA image send implementation unavailable")
        return self._call("send_image", self.driver.send_image, path)
    def send_file(self, path):
        self._require()
        if not hasattr(self.driver, "send_file"):
            raise ProviderUnavailable("UIA file send implementation unavailable")
        return self._call("send_file", self.driver.send_file, path)
    def type_message(self,text):
        raise ForegroundRequired("type_message requires explicit foreground operation")

class ReplicaUIADriver:
    """可选的 wechatauto-replica 适配器。

    这个适配器明确区分两类能力：

    * ``PASSIVE_READ_ONLY``：窗口/树发现、Qt accessibility gate 恢复和读取。
      这些路径不得激活、聚焦、点击或发送输入。
    * ``INTERACTIVE_MODE``：搜索框输入和 Click 等真实 UI 操作。该模式只能由
      未来显式的交互入口调用，绝不能作为被动路径的隐式回退。
    """
    def __init__(self):
        import sys
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]/"work"/"wechatauto_pkg"/"unzipped"
        sys.path.insert(0,str(root))
        from wechatauto.uia_driver import WeChatUIA
        self._impl=WeChatUIA()
        self._navigation_records = []
        self._last_accessibility_recovery = None
    def is_running(self): return self._impl.is_running()

    def active_initialize_accessibility(self, timeout: float = 8.0) -> dict:
        """显式启动期 UIA 初始化；不属于被动监控路径。

        该方法允许一次必要的 Qt accessibility gate 激活，然后重新枚举
        UIA 树。health probe、listener 和 recovery 永远不会调用它。
        """
        before = self._foreground_snapshot()
        attempted = bool(self._impl._wake_accessibility())
        deadline = time.monotonic() + max(0.1, float(timeout))
        window = None
        while time.monotonic() < deadline:
            window = self._impl._find_main()
            if window is not None:
                self._impl._win = window
                break
            time.sleep(0.1)
        after = self._foreground_snapshot()
        return {
            "mode": "ACTIVE_INITIALIZATION",
            "gate_attempted": attempted,
            "main_window": bool(window),
            "main_class": getattr(window, "ClassName", None) if window else None,
            "foreground_before": before,
            "foreground_after": after,
            "foreground_changed": self._foreground_snapshot_changed(before, after),
        }

    def read_only_probe(self):
        """只读检查当前 UIA 树；绝不热激活、聚焦、点击或改变窗口状态。"""
        before = self._foreground_snapshot()
        after = None
        try:
            window = self._impl._find_main()
            if window is not None:
                self._impl._win = window
            after = self._foreground_snapshot()
            verifiable = self._foreground_snapshot_verifiable(before, after)
            changed = self._foreground_snapshot_changed(before, after) if verifiable else None
            safety_violation = self.foreground_safety_violation(before, after) if verifiable else None
            interaction_violation = self._read_only_interaction_violation(before, after) if verifiable else None
            return window is not None and verifiable and not safety_violation and not interaction_violation
        finally:
            # 即使底层枚举抛异常，也必须取得异常后的快照；探针自身不
            # 尝试恢复前台，只记录证据并让上层安全判定失败关闭。
            if after is None:
                after = self._foreground_snapshot()
            # 仅记录证据，探针自身不尝试恢复前台。
            self._last_probe = {
                "mode": "PASSIVE_READ_ONLY",
                "foreground_before": before,
                "foreground_after": after,
                "foreground_verifiable": self._foreground_snapshot_verifiable(before, after),
                "foreground_changed": (
                    self._foreground_snapshot_changed(before, after)
                    if self._foreground_snapshot_verifiable(before, after) else None
                ),
                "foreground_safety_violation": (
                    self.foreground_safety_violation(before, after)
                    if self._foreground_snapshot_verifiable(before, after) else None
                ),
                "read_only_interaction_violation": (
                    self._read_only_interaction_violation(before, after)
                    if self._foreground_snapshot_verifiable(before, after) else None
                ),
            }

    @staticmethod
    def _foreground_snapshot():
        """返回窗口/焦点/鼠标/z-order 证据；此函数只读且不改变状态。"""
        if os.name != "nt":
            return {"available": False}
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = int(user32.GetForegroundWindow() or 0)
            pid = wintypes.DWORD()
            thread_id = int(user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or 0)
            length = int(user32.GetWindowTextLengthW(hwnd))
            buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(hwnd, buffer, len(buffer))

            # GetGUIThreadInfo is a read-only query of the active/focused
            # windows for the foreground thread.  GetActiveWindow/GetFocus
            # alone are thread-local and would describe this worker instead.
            class _GUITHREADINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND),
                    ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND),
                    ("hwndCaret", wintypes.HWND),
                ]
            gui = _GUITHREADINFO()
            gui.cbSize = ctypes.sizeof(gui)
            gui_ok = bool(thread_id and user32.GetGUIThreadInfo(thread_id, ctypes.byref(gui)))

            point = wintypes.POINT()
            cursor_ok = bool(user32.GetCursorPos(ctypes.byref(point)))

            # GW_HWNDPREV exposes the immediately preceding top-level window
            # without changing z-order.  It is an evidence signal, not a
            # complete desktop ordering (which is intentionally unnecessary).
            prev_hwnd = int(user32.GetWindow(hwnd, 3) or 0) if hwnd else 0  # GW_HWNDPREV
            return {
                "available": True,
                "hwnd": hwnd or None,
                "pid": int(pid.value) or None,
                "title": buffer.value or None,
                "thread_id": thread_id or None,
                "active_hwnd": int(gui.hwndActive or 0) or None if gui_ok else None,
                "focus_hwnd": int(gui.hwndFocus or 0) or None if gui_ok else None,
                "capture_hwnd": int(gui.hwndCapture or 0) or None if gui_ok else None,
                "cursor": [int(point.x), int(point.y)] if cursor_ok else None,
                "gui_info_available": gui_ok,
                "foreground_query_available": bool(hwnd and pid.value and thread_id),
                "cursor_available": cursor_ok,
                "z_order_available": bool(hwnd),
                "z_order_prev": prev_hwnd or None,
            }
        except Exception as exc:
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _foreground_snapshot_verifiable(before, after):
        """被动监控只有在前台、GUI 线程信息和查询状态齐全时才可继续。"""
        if not (isinstance(before, dict) and isinstance(after, dict)):
            return False
        if not (before.get("available") and after.get("available")):
            return False
        required = ("hwnd", "pid", "gui_info_available", "cursor_available", "z_order_available")
        return all(
            all(key in snapshot for key in required)
            and snapshot.get("hwnd") is not None
            and snapshot.get("pid") is not None
            and (bool(snapshot.get("gui_info_available")) or bool(snapshot.get("foreground_query_available")))
            and bool(snapshot.get("cursor_available"))
            and bool(snapshot.get("z_order_available"))
            for snapshot in (before, after)
        )

    @staticmethod
    def _foreground_snapshot_changed(before, after):
        """比较所有可取得的用户输入/窗口状态，只用于安全判定。"""
        if not (isinstance(before, dict) and isinstance(after, dict)):
            return False
        if not (before.get("available") and after.get("available")):
            return False
        # Cursor position and ``GW_HWNDPREV`` remain audit telemetry only.
        # Neither proves that this process activated a window.  A foreground
        # HWND/PID, active window, focus, or capture transition still fails
        # closed, including None-to-HWND and HWND-to-None transitions.
        keys = ("hwnd", "pid", "active_hwnd", "focus_hwnd", "capture_hwnd")
        return any(
            (before.get(key) is not None or after.get(key) is not None)
            and before.get(key) != after.get(key)
            for key in keys
        )

    @staticmethod
    def _snapshot_is_wechat(snapshot):
        """Read-only identification of a foreground snapshot as WeChat."""
        if not isinstance(snapshot, dict):
            return False
        title = str(snapshot.get("title") or "").casefold()
        if "微信" in title or "weixin" in title:
            return True
        pid = snapshot.get("pid")
        if not pid:
            return False
        try:
            import psutil

            process = psutil.Process(int(pid))
            name = str(process.name() or "").casefold()
            exe = str(process.exe() or "").casefold()
            return name == "weixin.exe" or exe.endswith("\\weixin.exe")
        except Exception:
            return False

    def foreground_safety_violation(self, before, after):
        """Passive monitoring may never promote a non-WeChat window to WeChat.

        Other foreground changes remain recorded as telemetry but cannot be
        attributed to this read-only process, so they must not permanently
        disable a healthy listener.
        """
        return (
            self._foreground_snapshot_changed(before, after)
            and not self._snapshot_is_wechat(before)
            and self._snapshot_is_wechat(after)
        )

    @staticmethod
    def _read_only_interaction_violation(before, after):
        return WeChatUIAService._read_only_interaction_violation(before, after)

    def recover_accessibility_tree(self, timeout=3.0):
        """只读重探测 UIA 树；后台恢复不得写 gate 或改变窗口状态。

        Qt accessibility gate 的热写入属于有副作用的显式维护动作，不能由
        health probe、listener、reconnect 或 driver recreation 自动触发。
        """
        import time

        before = self._foreground_snapshot()
        record = {
            "mode": "PASSIVE_READ_ONLY",
            "foreground_before": before,
            "gate_attempted": False,
            "gate_activated": False,
            "tree_ready": False,
        }
        recovered = False
        try:
            window = self._impl._find_main()
            if window is not None:
                self._impl._win = window
                record["tree_ready"] = True
                record["result"] = "ALREADY_READY"
                recovered = True
            else:
                deadline = time.monotonic() + max(0.1, float(timeout))
                while time.monotonic() < deadline:
                    window = self._impl._find_main()
                    if window is not None:
                        self._impl._win = window
                        record["tree_ready"] = True
                        record["result"] = "RECOVERED"
                        recovered = True
                        break
                    time.sleep(0.1)
                if not recovered:
                    record["result"] = "TREE_NOT_MATERIALIZED"
        except Exception as exc:
            record["result"] = "RECOVERY_ERROR"
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            after = self._foreground_snapshot()
            record["foreground_after"] = after
            record["foreground_verifiable"] = self._foreground_snapshot_verifiable(before, after)
            record["foreground_changed"] = (
                self._foreground_snapshot_changed(before, after)
                if record["foreground_verifiable"] else None
            )
            record["foreground_safety_violation"] = (
                self.foreground_safety_violation(before, after)
                if record["foreground_verifiable"] else None
            )
            record["read_only_interaction_violation"] = (
                self._read_only_interaction_violation(before, after)
                if record["foreground_verifiable"] else None
            )
            if not record["foreground_verifiable"]:
                record["tree_ready"] = False
                record["result"] = "FOREGROUND_UNVERIFIABLE"
                recovered = False
            elif record["foreground_safety_violation"] or record["read_only_interaction_violation"]:
                # A discovery that changed foreground is not a successful
                # recovery, even when an element was found.
                record["tree_ready"] = False
                record["result"] = "FOREGROUND_CHANGED"
                recovered = False
            self._last_accessibility_recovery = record
        return recovered

    def ensure_window(self):
        """兼容旧接口，但语义固定为只读探针。"""
        return self.read_only_probe()
    def get_chats(self):
        import uiautomation as auto
        lst=self._impl._win.ListControl(AutomationId="session_list")
        out=[]
        for c in lst.GetChildren():
            if c.ClassName!="mmui::ChatSessionCell": continue
            lines=[x.strip() for x in (c.Name or "").splitlines() if x.strip()]
            out.append({"id":c.AutomationId or "", "name":lines[0] if lines else "", "preview":lines[1] if len(lines)>1 else "", "time":lines[-2] if len(lines)>2 else ""})
        return out
    def open_chat(self,name):
        """兼容入口：只执行可验证的非侵入式 UIA Pattern 尝试。"""
        return self.direct_open_chat(name)

    def interactive_open_chat(self, name):
        """显式交互模式的搜索/点击导航。

        该方法可能令微信成为前台窗口；不能从健康检查、消息监听、普通
        ``open_chat`` 或发送链路中自动调用。调用者还必须自行取得用户对该次
        前台 UI 操作的明确授权。
        """
        before = self._foreground_snapshot()
        try:
            current = self._impl.current_chat()
            if current == name:
                return {
                    "opened": True,
                    "current_chat": current,
                    "mode": "INTERACTIVE_MODE",
                    "already_active": True,
                    "foreground_before": before,
                    "foreground_after": self._foreground_snapshot(),
                }
            opened = bool(self._impl.open_chat(str(name), retries=1))
            return {
                "opened": opened,
                "current_chat": self._impl.current_chat(),
                "mode": "INTERACTIVE_MODE",
                "foreground_before": before,
                "foreground_after": self._foreground_snapshot(),
            }
        except Exception as exc:
            return {
                "opened": False,
                "mode": "INTERACTIVE_MODE",
                "foreground_before": before,
                "foreground_after": self._foreground_snapshot(),
                "error": f"{type(exc).__name__}: {exc}",
            }
    def current_chat(self): return self._impl.current_chat()
    def search_chat(self, name):
        """在当前已物化的会话列表中只读搜索聊天。

        这里不能直接调用 ``WeChatUIA._collect_results``：该底层方法只负责
        读取“已经由搜索框填充”的 ``search_list``，不会填充搜索框本身，
        因而在被动工具路径中会等待到超时，而且可能把搜索 UI 当成隐式交互。
        会话列表本身已经由 ``get_chats`` 稳定暴露，直接枚举并过滤即可。

        **只按名称与 id 匹配，不匹配消息预览。** 预览里恰好提到目标名字是
        很常见的（例如某人的最新消息是「在跟好友A做文件」），一旦把预览
        算作命中，这条会话就会被当成目标返回；调用方会据此认为「目标已找到」
        并打开它，最终把消息发给**错误的人**。宁可搜不到让人工确认，也不能
        返回一个可能张冠李戴的候选。
        """
        query = str(name or "").strip()
        if not query:
            return []

        query_folded = query.casefold()
        rows = self.get_chats()
        ranked = []
        exact = []
        for position, row in enumerate(rows or []):
            if not isinstance(row, dict):
                continue
            values = (
                str(row.get("name") or ""),
                str(row.get("id") or ""),
            )
            folded = tuple(value.casefold() for value in values)
            if query_folded == folded[0]:
                exact.append((position, row))
                continue
            if folded[0].startswith(query_folded):
                rank = 1
            elif any(query_folded in value for value in folded):
                rank = 2
            else:
                continue
            ranked.append((rank, position, row))

        if exact:
            return [row for _, row in exact]

        ranked.sort(key=lambda item: (item[0], item[1]))
        return [row for _, _, row in ranked]
    def get_chat_input_field(self):
        self._impl._win = self._impl._find_main()
        e=self._impl._chat_input()
        if e is None: raise ProviderUnavailable("chat_input_field not found")
        return e

    def get_input_state(self):
        """读取输入框内容，不触发焦点、键盘或窗口激活。

        ``Name`` 在当前微信版本表示会话名，实际草稿文本优先从
        ``ValuePattern`` 读取。部分 UIA provider 不暴露 ValuePattern 时，
        ``text`` 保持 ``None``，调用方不能把它误判为空输入框。
        """
        field = self.get_chat_input_field()
        value = None
        supported = False
        try:
            pattern = field.GetValuePattern()
            if pattern is not None:
                supported = True
                value = pattern.Value
        except Exception:
            value = None
        return {
            "chat_name": (field.Name or None),
            "text": value,
            "value_pattern": supported,
        }

    def send_text(self, text):
        """执行一次显式发送；仅由受控发送流程调用。"""
        return bool(self._impl.send_text(str(text)))
    @staticmethod
    def _copy_files_to_clipboard(paths):
        """将本地文件写入 CF_HDROP 剪贴板，供微信粘贴为附件。"""
        paths = [os.path.abspath(p) for p in paths if p]
        if not paths:
            return False
        class DROPFILES(ctypes.Structure):
            _fields_ = [
                ("pFiles", ctypes.c_uint),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
                ("fNC", ctypes.c_int),
                ("fWide", ctypes.c_int),
            ]
        CF_HDROP = 15
        GMEM_MOVEABLE = 0x0002
        GMEM_ZEROINIT = 0x0040
        try:
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            # ctypes defaults to c_int.  HGLOBAL and LPVOID are pointer-sized
            # on 64-bit Windows; leaving the defaults truncates the handles
            # before GlobalLock/SetClipboardData and makes attachment paste
            # fail before any UI input is sent.
            u32.OpenClipboard.argtypes = [ctypes.c_void_p]
            u32.OpenClipboard.restype = ctypes.c_bool
            u32.EmptyClipboard.restype = ctypes.c_bool
            u32.CloseClipboard.restype = ctypes.c_bool
            u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            u32.SetClipboardData.restype = ctypes.c_void_p
            k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            k32.GlobalAlloc.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [ctypes.c_void_p]
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            k32.GlobalUnlock.restype = ctypes.c_bool
            k32.GlobalFree.argtypes = [ctypes.c_void_p]
            k32.GlobalFree.restype = ctypes.c_void_p
            if not u32.OpenClipboard(None):
                return False
            try:
                if not u32.EmptyClipboard():
                    return False
                df = DROPFILES()
                df.pFiles = ctypes.sizeof(DROPFILES)
                df.fWide = 1
                raw = (
                    ctypes.string_at(ctypes.byref(df), ctypes.sizeof(DROPFILES))
                    + ("\0".join(paths) + "\0").encode("utf-16-le")
                    + b"\0\0"
                )
                h = k32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(raw))
                if not h:
                    return False
                dst = k32.GlobalLock(h)
                if not dst:
                    k32.GlobalFree(h)
                    return False
                try:
                    ctypes.memmove(dst, raw, len(raw))
                finally:
                    k32.GlobalUnlock(h)
                if not u32.SetClipboardData(CF_HDROP, h):
                    k32.GlobalFree(h)
                    return False
                return True
            finally:
                u32.CloseClipboard()
        except Exception:
            return False

    def _open_chat_and_settle(self, who: str, retries: int = 5) -> bool:
        for _ in range(max(1, retries)):
            if self.current_chat() == who and self._impl._chat_input() is not None:
                return True
            if not self.open_chat(who):
                time.sleep(0.5)
                continue
            time.sleep(1.0)
            if self._impl._chat_input() is not None:
                return True
        return self._impl._chat_input() is not None

    def _send_attachment(self, path: str, who: str | None = None) -> bool:
        if not path or not os.path.isfile(path):
            return False
        if not self.ensure_window():
            return False
        if who and not self._open_chat_and_settle(str(who)):
            return False
        box = self._impl._chat_input()
        if box is None:
            return False
        if not self._copy_files_to_clipboard([path]):
            return False
        try:
            box.Click()
        except Exception:
            pass
        try:
            box.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
        except Exception:
            pass
        time.sleep(0.2)
        try:
            box.SendKeys("{Ctrl}v", waitTime=0.05)
            time.sleep(0.6)
            box.SendKeys("{Enter}", waitTime=0.05)
            return True
        except Exception:
            return False

    def send_image(self, path: str, who: str | None = None) -> bool:
        """通过剪贴板把本地图片插入当前会话并发送。"""
        return self._send_attachment(path, who)

    def send_file(self, path: str, who: str | None = None) -> bool:
        """通过剪贴板把本地文件插入当前会话并发送。"""
        return self._send_attachment(path, who)

    # 真正承载消息文本的节点类。WeChat 4.1.13.12 把每条消息渲染成
    # mmui::ChatTextItemView；mmui::ChatItemView 用于时间戳/系统提示，
    # mmui::ChatBubbleReferItemView 用于引用或媒体占位。必须按类名精确
    # 匹配：mmui::RecyclerListView 只是容器，它自己的 Name 是固定字面量
    # "消息"，不是消息内容。
    MESSAGE_ITEM_CLASSES = {
        "mmui::ChatTextItemView",
        "mmui::ChatItemView",
        "mmui::ChatBubbleReferItemView",
    }
    # 会话列表单元格与这些容器不是消息，必须排除。
    NON_MESSAGE_CLASSES = {
        "mmui::ChatSessionCell",
        "mmui::RecyclerListView",
        "mmui::MessageView",
        "mmui::XTextView",
        "MMUIRenderSubWindowHW",
    }
    # 容器自身暴露的固定标签，永远不能当作消息文本。
    CONTAINER_LABELS = {"消息"}

    @staticmethod
    def _extract_message_text(node) -> str:
        """按 Name → ValuePattern → LegacyIAccessible 顺序取消息文本。

        4.1.13.12 的 ChatTextItemView 有时 Name 为空而 ValuePattern 有值，
        因此不能只看 Name；也不伪造内容，取不到就返回空串。
        """
        try:
            name = (node.Name or "").strip()
        except Exception:
            name = ""
        if name:
            return name
        for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
            try:
                pattern = getattr(node, getter)()
                value = getattr(pattern, "Value", None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            except Exception:
                continue
        return ""

    @staticmethod
    def _classify_message(text: str, cls: str) -> str:
        """区分消息类型；不猜测未知类型为文本。"""
        if cls == "mmui::ChatBubbleReferItemView":
            return "reference"
        # 纯时间戳行（如 "昨天 16:29"）属于系统提示，不是用户消息。
        import re
        if re.fullmatch(r"(昨天|今天|星期[一二三四五六日])?\s*\d{1,2}:\d{2}", text or ""):
            return "timestamp"
        return "text"

    def read_messages(self, limit=20):
        """读取当前会话可访问的文本节点；不滚动、不切换前台、不发送。

        只收集 MESSAGE_ITEM_CLASSES 中列出的真实消息节点。此前按
        ``rect.bottom <= input_rect.top`` 的几何过滤会漏掉所有消息（消息区
        与输入框区域重叠），只留下容器标签 "消息"，因此改为按类名匹配。
        """
        self._impl._win = self._impl._find_main()
        current = self._impl.current_chat()
        seen, rows = set(), []

        def collect(node, in_messages=False):
            if len(rows) >= limit * 4:
                return
            try:
                cls = node.ClassName or ""
                in_messages = in_messages or cls in {"mmui::MessageView", "mmui::RecyclerListView"}
                if in_messages and cls in self.MESSAGE_ITEM_CLASSES:
                    text = self._extract_message_text(node)
                    rect = node.BoundingRectangle
                    if text and text not in self.CONTAINER_LABELS and len(text) < 4000:
                        key = (text, rect.left, rect.top, rect.right, rect.bottom)
                        if key not in seen:
                            seen.add(key)
                            runtime_id = None
                            try:
                                raw_runtime_id = getattr(node, "GetRuntimeId", lambda: None)()
                                if raw_runtime_id:
                                    runtime_id = list(raw_runtime_id)
                            except Exception:
                                pass
                            rows.append({
                                "text": text,
                                "sender": None,
                                "is_self": None,
                                "timestamp": None,
                                "message_type": self._classify_message(text, cls),
                                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                                "class_name": cls,
                                "runtime_id": runtime_id,
                            })
                for child in node.GetChildren():
                    collect(child, in_messages)
            except Exception:
                return

        collect(self._impl._win)
        # 只过滤窗口标题与会话名；不再丢弃容器标签以外的内容。
        rows = [r for r in rows if r["text"] not in {current or "", "微信", "Weixin"}]
        return {"chat": current, "messages": rows[-limit:], "foreground_required": False}

    def uia_fingerprint(self):
        """构造只读 UIA/Qt accessibility 指纹，不执行热激活或交互。"""
        result = {
            "mode": "PASSIVE_READ_ONLY",
            "main_window": None,
            "qt_accessibility": {"gate_read": False, "active": None},
            "controls": {},
            "windows": [],
        }
        try:
            hwnds = list(self._impl._wechat_hwnds() or [])
        except Exception as exc:
            result["error"] = f"window_enumeration: {type(exc).__name__}: {exc}"
            return result
        for hwnd in hwnds:
            item = {"hwnd": int(hwnd)}
            try:
                pid = self._impl._pid_from_hwnd(hwnd)
                item["pid"] = pid
                module = self._impl._weixin_dll_module(pid) if pid else None
                if module:
                    base, size, path = module
                    item["weixin_dll"] = {"path": path, "base": int(base), "size": int(size)}
                    rva = self._impl._qaccessible_active_rva(path)
                    item["qt_accessibility_rva"] = rva
                    if rva is not None and os.name == "nt":
                        import ctypes
                        access = 0x0400 | 0x0010
                        handle = ctypes.windll.kernel32.OpenProcess(access, False, int(pid))
                        if handle:
                            try:
                                active = self._impl._read_process_byte(handle, int(base) + int(rva))
                                item["qt_accessibility_active"] = active
                                result["qt_accessibility"] = {
                                    "gate_read": active is not None,
                                    "active": active == 1 if active is not None else None,
                                    "rva": rva,
                                }
                            finally:
                                ctypes.windll.kernel32.CloseHandle(handle)
                root = self._impl._anchor(hwnd)
                if root is not None:
                    item["uia_class"] = root.ClassName or None
                    item["uia_name"] = root.Name or None
                    item["is_main_window"] = (root.ClassName or "") == "mmui::MainWindow"
                    if item["is_main_window"]:
                        result["main_window"] = item
                        self._impl._win = root
                        break
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            result["windows"].append(item)
        root = self._impl._win if result.get("main_window") else None
        if root is not None:
            for key, factory, kwargs in (
                ("session_list", "ListControl", {"AutomationId": "session_list"}),
                ("chat_input_field", "EditControl", {"AutomationId": "ChatInputField"}),
                ("search_field", "EditControl", {"ClassName": "mmui::XValidatorTextEdit"}),
            ):
                try:
                    result["controls"][key] = bool(getattr(root, factory)(**kwargs).Exists(0.2, 0.1))
                except Exception:
                    result["controls"][key] = False
        return result

    @staticmethod
    def _cell_title(cell) -> str:
        """取会话单元格的联系人名——只取第一行。

        **这一点非常关键**：``mmui::ChatSessionCell`` 的 ``Name`` 是多行文本，
        第一行是联系人名，后面是最后一条消息的预览。曾经用子串匹配整段
        ``Name``，结果对方预览里恰好提到目标名字时（例如「在跟好友A做文件」），
        会把**另一个人的会话**匹配成目标并点开——发送链路随后按「已打开目标」
        校验通过，把消息发给了错误的人。因此这里必须只比较首行且要求精确相等。
        """
        lines = [x.strip() for x in str(getattr(cell, "Name", "") or "").splitlines() if x.strip()]
        return lines[0] if lines else ""

    def _search_open_chat(self, name: str, evidence: dict) -> bool:
        """用微信搜索框打开会话——侧边栏找不到时的兜底路径。

        流程：写入搜索框（ValuePattern，不点鼠标）→ 读 search_list 结果 →
        对目标用 UIA Pattern 打开 → 用 current_chat 验证。整个过程不点击、
        不抢前台。
        """
        import time as _t
        try:
            self._impl._win = self._impl._find_main()
            win = self._impl._win
            if win is None:
                evidence["search_error"] = "no main window"
                return False
            box = getattr(self._impl, "_search_box", None)
            search_box = box(win) if callable(box) else None
            if search_box is None:
                evidence["search_error"] = "no search box"
                return False
            # 复用驱动自己的粘贴实现（已被改成 ValuePattern 后台写入）
            self._impl._paste_into(search_box, name, clear=True)
            _t.sleep(1.5)

            collect = getattr(self._impl, "_collect_results", None)
            results = []
            # 搜索结果列表是异步渲染的，且上一次的残留会让采集落空；
            # 重写一次关键词并轮询，直到拿到候选或超时。
            for attempt in range(4):
                results = collect(name) if callable(collect) else []
                if results:
                    break
                self._impl._paste_into(search_box, name, clear=True)
                _t.sleep(1.2)
            evidence["search_results"] = [
                {"name": r.get("name"), "section": r.get("section")} for r in (results or [])
            ]
            if not results:
                return False

            # 优先精确命中，否则取第一个
            exact = [r for r in results if (r.get("name") or "") == name]
            chosen = (exact or results)[0]
            cell = chosen.get("cell")
            if cell is None:
                return False

            # 打开结果。微信的搜索结果单元格虽然暴露 UIA Pattern，但 Qt 内部
            # 并未实现——调用后界面不动（已逐项实测）。唯一有效的是真实点击，
            # 这里复用驱动里带「DPI 换算 + 光标/前台还原」的点击实现。
            for strategy in ("CLICK", "SelectionItem", "Invoke", "LegacyIAccessible"):
                # 每次都从最新搜索结果里重新取 cell，避免复用失效元素
                try:
                    fresh = collect(name) if callable(collect) else []
                except Exception:
                    fresh = []
                fresh_exact = [r for r in fresh if (r.get("name") or "") == name]
                pick = (fresh_exact or fresh or [chosen])[0]
                cell = pick.get("cell") or cell
                if cell is None:
                    continue
                try:
                    if strategy == "CLICK":
                        restore = self._impl._capture_foreground()
                        clicked = self._impl._click_element(cell)
                        self._impl._restore_foreground(restore)
                        if not clicked:
                            evidence.setdefault("search_attempts", []).append(
                                {"strategy": strategy, "invoked": False})
                            continue
                    elif strategy == "SelectionItem":
                        pat = cell.GetSelectionItemPattern()
                        if pat is None:
                            continue
                        pat.Select()
                    elif strategy == "Invoke":
                        pat = cell.GetInvokePattern()
                        if pat is None:
                            continue
                        pat.Invoke()
                    else:
                        pat = cell.GetLegacyIAccessiblePattern()
                        if pat is None:
                            continue
                        pat.DoDefaultAction()
                except Exception as exc:
                    evidence.setdefault("search_attempts", []).append(
                        {"strategy": strategy, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                deadline = _t.monotonic() + 2.5
                while _t.monotonic() < deadline:
                    try:
                        cur = self._impl.current_chat()
                    except Exception:
                        cur = None
                    if cur == name:
                        evidence["after_current_chat"] = cur
                        evidence["opened_via"] = f"search:{strategy}"
                        # 清掉搜索框，避免残留影响下一次导航
                        try:
                            self._impl._paste_into(search_box, "", clear=True)
                        except Exception:
                            pass
                        return True
                    _t.sleep(0.1)
                evidence.setdefault("search_attempts", []).append(
                    {"strategy": strategy, "verified": False})
            try:
                self._impl._paste_into(search_box, "", clear=True)
            except Exception:
                pass
            return False
        except Exception as exc:
            evidence["search_error"] = f"{type(exc).__name__}: {exc}"
            return False

    def direct_open_chat(self,name):
        """通过新鲜元素执行有限导航，并以真实 current_chat 验证结果。"""
        import json, time
        from datetime import datetime, timezone
        started = time.perf_counter()
        evidence = {"target": str(name), "attempts": [], "foreground_changed": None}
        before = None
        try:
            self._impl._win = self._impl._find_main()
            # 先清空搜索框。上一次搜索若留下关键词，会话列表会停留在「搜索
            # 结果」状态：此时 current_chat 读不到聊天输入框（返回 None），
            # 列表内容也不再是真实会话，后续点击会全部错位。
            try:
                _sb = getattr(self._impl, "_search_box", None)
                _box = _sb(self._impl._win) if callable(_sb) and self._impl._win else None
                if _box is not None:
                    _vp = _box.GetValuePattern()
                    if _vp is not None and (_vp.Value or "").strip():
                        _vp.SetValue("")
                        time.sleep(0.35)
            except Exception:
                pass
            before = self._impl.current_chat()
            evidence["before_current_chat"] = before
            if before == name:
                evidence["after_current_chat"] = before
                evidence["result"] = "ALREADY_ACTIVE"
                return True
            # 微信在会话切换后会短暂重建列表控件，直接读可能撞上 2 秒查找
            # 超时。这里轮询几次，拿到可用的列表再继续。
            cells = []
            lst = None
            for _try in range(6):
                try:
                    self._impl._win = self._impl._find_main()
                    lst = self._impl._win.ListControl(AutomationId="session_list")
                    if not lst.Exists(0.4, 0.1):
                        time.sleep(0.25)
                        continue
                    cells = [c for c in lst.GetChildren()
                             if c.ClassName == "mmui::ChatSessionCell"
                             and self._cell_title(c) == str(name)]
                    break
                except Exception:
                    cells = []
                    time.sleep(0.25)
            if not cells:
                # 侧边栏只渲染可见的会话，排位靠后的联系人根本不在列表里，
                # 而且该列表不暴露 ScrollPattern，无法用 UIA 滚动。回退到
                # 微信自带的搜索框：输入名字 → 从 search_list 里取结果 →
                # 用 UIA Pattern 打开（仍然不点鼠标）。
                if self._search_open_chat(str(name), evidence):
                    evidence["result"] = "PASS_BY_SEARCH"
                    return True
                evidence["result"] = "TARGET_NOT_FOUND"
                return False
            if len(cells) > 1:
                evidence["result"] = "AMBIGUOUS_TARGET"
                return False
            # 只保留真实点击：微信 4.x 的会话单元格虽然对外暴露
            # SelectionItem / Invoke / LegacyIAccessible 三种 Pattern，但 Qt
            # 内部并未实现——调用返回成功，界面纹丝不动（已逐项实测）。
            # 留着它们只会每次白等 2 秒验证超时，把一次切换拖到十几秒。
            for strategy in ("CLICK",):
                # 每次策略都重新枚举，避免复用失效 AutomationElement。
                target = None
                for _try in range(5):
                    try:
                        self._impl._win = self._impl._find_main()
                        fresh = self._impl._win.ListControl(AutomationId="session_list")
                        if not fresh.Exists(0.4, 0.1):
                            time.sleep(0.25)
                            continue
                        target = next((c for c in fresh.GetChildren()
                                       if c.ClassName == "mmui::ChatSessionCell"
                                       and self._cell_title(c) == str(name)), None)
                        if target is not None:
                            break
                    except Exception:
                        target = None
                    time.sleep(0.25)
                attempt = {"strategy": strategy, "available": False, "invoked": False, "verified": False}
                if target is None:
                    evidence["attempts"].append(attempt)
                    continue
                try:
                    if strategy == "CLICK":
                        # 微信 4.x 的会话单元格虽然暴露 SelectionItem/Invoke，
                        # 但 Qt 内部并未实现，调用后界面不动。真实鼠标点击是
                        # 唯一有效的切换方式；这里在点击期间临时前置窗口，
                        # 点击后立刻把前台还给用户原来的窗口。
                        restore = self._impl._capture_foreground()
                        clicked = self._impl._click_element(target)
                        attempt["available"] = True
                        attempt["invoked"] = bool(clicked)
                        self._impl._restore_foreground(restore)
                    elif strategy.startswith("ScrollItem+"):
                        scroll = target.GetScrollItemPattern()
                        if scroll is None:
                            raise RuntimeError("ScrollItem pattern unavailable")
                        scroll.ScrollIntoView()
                        base = strategy.split("+", 1)[1]
                        pattern = target.GetSelectionItemPattern() if base == "SelectionItem" else target.GetInvokePattern()
                    elif strategy == "SelectionItem":
                        pattern = target.GetSelectionItemPattern()
                    elif strategy == "Invoke":
                        pattern = target.GetInvokePattern()
                    else:
                        pattern = target.GetLegacyIAccessiblePattern()
                    attempt["available"] = pattern is not None
                    if pattern is not None:
                        if strategy.endswith("SelectionItem") or strategy == "SelectionItem":
                            pattern.Select()
                        elif strategy == "LegacyIAccessible":
                            pattern.DoDefaultAction()
                        else:
                            pattern.Invoke()
                        attempt["invoked"] = True
                except Exception as exc:
                    attempt["error"] = f"{type(exc).__name__}: {exc}"
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        current = self._impl.current_chat()
                        if current == name:
                            attempt["verified"] = True
                            evidence["after_current_chat"] = current
                            evidence["attempts"].append(attempt)
                            evidence["result"] = "PASS"
                            return True
                    except Exception:
                        pass
                    time.sleep(0.1)
                evidence["attempts"].append(attempt)
            evidence["after_current_chat"] = self._impl.current_chat()
            evidence["result"] = "TARGET_OPEN_FAILED"
            return False
        finally:
            evidence["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
            try:
                path = __import__("pathlib").Path("logs") / "uia_navigation.jsonl"
                path.parent.mkdir(exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    evidence["timestamp"] = datetime.now(timezone.utc).isoformat()
                    fh.write(json.dumps(evidence, ensure_ascii=False) + "\n")
            except Exception:
                pass
            self._navigation_records.append(evidence)
            self._navigation_records = self._navigation_records[-200:]

    def navigation_stats(self):
        records = list(self._navigation_records)
        total = len(records)
        success = sum(1 for row in records if row.get("result") == "PASS")
        latencies = sorted(row.get("elapsed_ms", 0) for row in records)
        def percentile(value):
            if not latencies:
                return None
            index = min(len(latencies) - 1, int(round((len(latencies) - 1) * value)))
            return latencies[index]
        return {
            "total": total,
            "success": success,
            "failure": total - success,
            "success_rate": (success / total) if total else None,
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "last": records[-1] if records else None,
        }

    @staticmethod
    def _pattern_names(element):
        names = []
        for label, getter in (
            ("Invoke", "GetInvokePattern"),
            ("SelectionItem", "GetSelectionItemPattern"),
            ("ScrollItem", "GetScrollItemPattern"),
            ("VirtualizedItem", "GetVirtualizedItemPattern"),
            ("LegacyIAccessible", "GetLegacyIAccessiblePattern"),
            ("Value", "GetValuePattern"),
            ("Text", "GetTextPattern"),
        ):
            try:
                if getattr(element, getter, None)() is not None:
                    names.append(label)
            except Exception:
                continue
        return names

    @staticmethod
    def _com_pointer_valid(value):
        """判断 comtypes 指针是否真正指向 COM 对象。

        comtypes 在某些 UIA 边界返回 ``POINTER(...)`` 包装对象，即使底层
        地址为 NULL，它仍然可能满足 ``value is not None``。直接访问该对象
        会抛出 ``ValueError: NULL COM pointer access``，因此这里必须同时
        检查布尔有效性，并把异常视为无效指针。
        """
        if value is None:
            return False
        try:
            return bool(value)
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _safe_current(element, name, default=None):
        if not ReplicaUIADriver._com_pointer_valid(element):
            return default
        try:
            return getattr(element, name)
        except Exception:
            return default

    def diagnostics(self):
        """返回当前微信 UIA 的只读能力快照，不执行任何交互动作。"""
        import json
        from datetime import datetime, timezone
        fingerprint = self.uia_fingerprint()
        self._impl._win = self._impl._find_main()
        root = self._impl._win
        lst = root.ListControl(AutomationId="session_list")
        cells = [c for c in lst.GetChildren() if c.ClassName == "mmui::ChatSessionCell"]
        elements = []
        for cell in cells:
            try:
                rect = cell.BoundingRectangle
                runtime_id = getattr(cell, "GetRuntimeId", lambda: None)()
                elements.append({
                    "name": cell.Name or "",
                    "automation_id": cell.AutomationId or "",
                    "class_name": cell.ClassName or "",
                    "control_type": getattr(cell, "ControlTypeName", None),
                    "runtime_id": list(runtime_id) if runtime_id else None,
                    "is_enabled": getattr(cell, "IsEnabled", None),
                    "is_offscreen": getattr(cell, "IsOffscreen", None),
                    "bounds": [rect.left, rect.top, rect.right, rect.bottom],
                    "patterns": self._pattern_names(cell),
                })
            except Exception:
                continue
        native = self._native_view_diagnostics()
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fingerprint": fingerprint,
            "views": {
                "ControlView": {"count": len(elements), "elements": elements},
                "RawView": native.get("RawView", {"status": "NOT_SUPPORTED"}),
                "ContentView": native.get("ContentView", {"status": "NOT_SUPPORTED"}),
            },
            "cache_request": native.get("cache_request", {"status": "NOT_SUPPORTED"}),
            "events": native.get("events", {"status": "NOT_TESTED"}),
            "virtualization": {
                "status": "PASS" if any("VirtualizedItem" in e["patterns"] or "ScrollItem" in e["patterns"] for e in elements) else "NOT_DETECTED",
                "patterns_observed": sorted({p for e in elements for p in e["patterns"] if "Scroll" in p}),
            },
        }
        try:
            path = __import__("pathlib").Path("logs") / "uia_elements.jsonl"
            path.parent.mkdir(exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return result

    def _native_view_diagnostics(self):
        """通过 uiautomation 已加载的 UIAutomationCore COM 客户端探测视图/缓存 API。"""
        try:
            import uiautomation.uiautomation as native
            client = native._AutomationClient.instance()
            hwnd = int(getattr(self._impl._win, "NativeWindowHandle", 0) or 0)
            if not hwnd:
                element = getattr(self._impl._win, "Element", None)
                hwnd = int(getattr(element, "CurrentNativeWindowHandle", 0) or 0)
            if not hwnd:
                return {"RawView": {"status": "UIA_HWND_UNAVAILABLE"}}
            root = client.IUIAutomation.ElementFromHandle(hwnd)
            if not self._com_pointer_valid(root):
                raise RuntimeError(f"ElementFromHandle returned null for hwnd={hwnd}")
            result = {}
            # UIA 属性常量来自 UIAutomationClient 类型库。缓存请求只读
            # 获取遍历诊断所需属性，避免每个节点再次跨 COM 查询。
            from comtypes.gen import UIAutomationClient as uia
            cache_request = None
            cache_properties = [
                uia.UIA_NamePropertyId,
                uia.UIA_AutomationIdPropertyId,
                uia.UIA_ClassNamePropertyId,
                uia.UIA_ControlTypePropertyId,
                uia.UIA_IsEnabledPropertyId,
                uia.UIA_IsOffscreenPropertyId,
            ]
            try:
                cache_request = client.IUIAutomation.CreateCacheRequest()
                for property_id in cache_properties:
                    cache_request.AddProperty(property_id)
                cache_request.TreeScope = uia.TreeScope_Subtree
            except Exception as exc:
                result["cache_request"] = {
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                cache_request = None

            for name, walker in (
                ("RawView", client.IUIAutomation.RawViewWalker),
                ("ControlView", client.IUIAutomation.ControlViewWalker),
                ("ContentView", client.IUIAutomation.ContentViewWalker),
            ):
                try:
                    count = 0
                    sample = []
                    visited = 0
                    cached_reads = 0
                    # CacheRequest/Walker 对象在部分 UIA provider 上具有严格
                    # 的 COM 生命周期约束。每个视图使用独立请求，并在
                    # BuildCache 返回 E_INVALIDARG 时退回普通只读 Walker，
                    # 保留真实错误而不中断整个诊断。
                    view_cache = None
                    cache_error = None
                    try:
                        view_cache = client.IUIAutomation.CreateCacheRequest()
                        for property_id in cache_properties:
                            view_cache.AddProperty(property_id)
                        view_cache.TreeScope = uia.TreeScope_Subtree
                    except Exception as exc:
                        cache_error = f"{type(exc).__name__}: {exc}"
                        view_cache = None

                    def walk(node, depth=0):
                        nonlocal count, visited, cached_reads
                        if not self._com_pointer_valid(node) or depth > 12 or visited >= 2000:
                            return
                        visited += 1
                        # BuildCache 返回的节点优先从 Cached* 属性读取；
                        # 若某个 UIA provider 不提供缓存属性，再回退到 Current*，
                        # 但仍记录缓存属性读取是否真实发生。
                        cached_cls = self._safe_current(node, "CachedClassName", None)
                        cached_name = self._safe_current(node, "CachedName", None)
                        cached_automation_id = self._safe_current(node, "CachedAutomationId", None)
                        if any(value is not None for value in (cached_cls, cached_name, cached_automation_id)):
                            cached_reads += 1
                        cls = str((cached_cls if cached_cls is not None else self._safe_current(node, "CurrentClassName", "")) or "")
                        if cls == "mmui::ChatSessionCell":
                            count += 1
                            if len(sample) < 20:
                                sample.append({
                                    "name": str((cached_name if cached_name is not None else self._safe_current(node, "CurrentName", "")) or ""),
                                    "automation_id": str((cached_automation_id if cached_automation_id is not None else self._safe_current(node, "CurrentAutomationId", "")) or ""),
                                    "class_name": cls,
                                    "control_type": int((self._safe_current(node, "CachedControlType", None)
                                                         if self._safe_current(node, "CachedControlType", None) is not None
                                                         else self._safe_current(node, "CurrentControlType", 0)) or 0),
                                })
                        if view_cache is not None:
                            try:
                                child = walker.GetFirstChildElementBuildCache(node, view_cache)
                            except Exception as exc:
                                cache_error_local = f"{type(exc).__name__}: {exc}"
                                nonlocal_cache_errors.append(cache_error_local)
                                child = walker.GetFirstChildElement(node)
                        else:
                            child = walker.GetFirstChildElement(node)
                        while self._com_pointer_valid(child) and visited < 2000:
                            walk(child, depth + 1)
                            if view_cache is not None and not nonlocal_cache_errors:
                                try:
                                    child = walker.GetNextSiblingElementBuildCache(child, view_cache)
                                except Exception as exc:
                                    nonlocal_cache_errors.append(f"{type(exc).__name__}: {exc}")
                                    child = walker.GetNextSiblingElement(child)
                            else:
                                child = walker.GetNextSiblingElement(child)
                    nonlocal_cache_errors = []
                    walk(root)
                    row = {
                        "status": "PASS",
                        "count": count,
                        "sample": sample,
                        "visited": visited,
                        "cached_property_reads": cached_reads,
                    }
                    if cache_error:
                        row["cache_error"] = cache_error
                    if nonlocal_cache_errors:
                        row["cache_error"] = nonlocal_cache_errors[0]
                        row["traversal_mode"] = "walker_fallback"
                    else:
                        row["traversal_mode"] = "build_cache"
                    result[name] = row
                except Exception as exc:
                    result[name] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
            if cache_request is not None:
                result["cache_request"] = {
                    "status": "PASS",
                    "created": True,
                    "tree_scope": "Subtree",
                    "properties": cache_properties,
                    "build_cache": True,
                }
            result["events"] = self._probe_event_subscription(client, root)
            result["hwnd"] = hwnd
            return result
        except Exception as exc:
            return {
                "RawView": {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"},
                "ContentView": {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"},
                "cache_request": {"status": "FAIL", "error": str(exc)},
                "events": {"status": "FAIL", "error": str(exc)},
            }

    def _probe_event_subscription(self, client, root):
        """只读探测 UIA 事件注册能力，并在探测后立即解除。

        事件回调对象必须保持强引用到 Remove 调用完成，否则 COM 可能在
        返回前回收回调对象。这里不把探测注册变成常驻监听，生产消息层仍
        使用已有轮询；返回值明确区分“注册成功并解除”和“API 可见但注册失败”。
        """
        apis = [
            "AddPropertyChangedEventHandler",
            "AddStructureChangedEventHandler",
            "AddFocusChangedEventHandler",
        ]
        result = {
            "status": "AVAILABLE",
            "apis": apis,
            "subscription": "AVAILABLE_BUT_NOT_INSTALLED",
            "probes": {},
        }
        if not self._com_pointer_valid(client) or not self._com_pointer_valid(root):
            result["status"] = "UNAVAILABLE"
            result["error"] = "UIA client/root COM pointer unavailable"
            return result
        try:
            from comtypes import COMObject
            from comtypes.gen import UIAutomationClient as uia

            class StructureHandler(COMObject):
                _com_interfaces_ = [uia.IUIAutomationStructureChangedEventHandler]

                def HandleStructureChangedEvent(self, sender, change_type, runtime_id):
                    return 0

            class PropertyHandler(COMObject):
                _com_interfaces_ = [uia.IUIAutomationPropertyChangedEventHandler]

                def HandlePropertyChangedEvent(self, sender, property_id, new_value):
                    return 0

            # Element scope 足以验证注册/移除 API，且不会让探测在整棵树上
            # 安装临时回调。每个 handler 都在 finally 中移除。
            structure_handler = StructureHandler()
            try:
                client.IUIAutomation.AddStructureChangedEventHandler(
                    root, uia.TreeScope_Element, None, structure_handler
                )
                result["probes"]["structure_changed"] = "REGISTERED_AND_REMOVED"
                result["subscription"] = "PROBE_PASSED_NOT_PERSISTED"
            except Exception as exc:
                result["probes"]["structure_changed"] = {
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                try:
                    client.IUIAutomation.RemoveStructureChangedEventHandler(root, structure_handler)
                except Exception:
                    pass

            property_handler = PropertyHandler()
            try:
                # 只订阅 Name 属性，避免监听所有属性造成噪声。
                client.IUIAutomation.AddPropertyChangedEventHandler(
                    root, uia.TreeScope_Element, None, property_handler,
                    [uia.UIA_NamePropertyId]
                )
                result["probes"]["property_changed"] = "REGISTERED_AND_REMOVED"
                result["subscription"] = "PROBE_PASSED_NOT_PERSISTED"
            except Exception as exc:
                result["probes"]["property_changed"] = {
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                try:
                    client.IUIAutomation.RemovePropertyChangedEventHandler(root, property_handler)
                except Exception:
                    pass
        except Exception as exc:
            result["status"] = "AVAILABLE_BUT_NOT_INSTALLED"
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

class LazyReplicaUIADriver:
    """避免 API 导入阶段加载原生 UIA；首次实际操作时才创建驱动。"""
    def __init__(self):
        self._driver = None
    def _get(self):
        if self._driver is None:
            self._driver = ReplicaUIADriver()
        return self._driver
    def __getattr__(self, name):
        return getattr(self._get(), name)
