from src.uia_service import ReplicaUIADriver, WeChatUIAService


class FakeDriver:
    @staticmethod
    def _foreground_snapshot():
        return {
            "available": True, "hwnd": 1, "pid": 2,
            "gui_info_available": True, "cursor_available": True,
            "z_order_available": True, "active_hwnd": 1,
            "focus_hwnd": 1, "capture_hwnd": None, "z_order_prev": None,
        }

    def read_only_probe(self):
        return True

    def ensure_window(self):
        return True


class OpenChatFallbackDriver(FakeDriver):
    def __init__(self):
        self.direct_called = 0
        self.interactive_called = 0

    def direct_open_chat(self, name):
        self.direct_called += 1
        return False

    def interactive_open_chat(self, name):
        self.interactive_called += 1
        return {"opened": True, "current_chat": name, "mode": "INTERACTIVE_MODE"}


class AlreadyActiveDriver(OpenChatFallbackDriver):
    def current_chat(self):
        return "好友B"


def test_uia_worker_thread_is_stable():
    service = WeChatUIAService(FakeDriver())
    try:
        first = service._call("one", lambda: __import__("threading").get_ident())
        second = service._call("two", lambda: __import__("threading").get_ident())
        assert first == second
        assert service.worker_status()["dedicated"] is True
    finally:
        service.close()


def test_execute_command_returns_structured_error():
    service = WeChatUIAService(FakeDriver())
    try:
        result = service.execute_command("probe", lambda: (_ for _ in ()).throw(RuntimeError("stale element")))
        assert result["ok"] is False
        assert result["error"]["code"] == "UIA_ELEMENT_STALE"
        assert result["request_id"]
    finally:
        service.close()


def test_call_accepts_operation_specific_timeout():
    service = WeChatUIAService(FakeDriver(), timeout=0.01)
    try:
        result = service._call("slow", lambda: "ok", timeout=0.5)
        assert result == "ok"
    finally:
        service.close()


def test_open_chat_never_implicitly_enters_interactive_mode():
    service = WeChatUIAService(OpenChatFallbackDriver())
    try:
        assert service.open_chat("好友B") is False
        assert service.driver.interactive_called == 0
        assert service.driver.direct_called == 1
    finally:
        service.close()


def test_open_chat_skips_when_already_active():
    service = WeChatUIAService(AlreadyActiveDriver())
    try:
        assert service.open_chat("好友B") is True
        assert service.driver.interactive_called == 0
        assert service.driver.direct_called == 0
    finally:
        service.close()


class NullComPointer:
    def __bool__(self):
        raise ValueError("NULL COM pointer access")


class CurrentAttributeRaises:
    def __bool__(self):
        return True

    @property
    def CurrentName(self):
        raise ValueError("NULL COM pointer access")


def test_native_diagnostic_guards_null_com_pointer():
    """COM 空包装对象不能被 ``is not None`` 误判为可访问元素。"""
    assert ReplicaUIADriver._com_pointer_valid(None) is False
    assert ReplicaUIADriver._com_pointer_valid(NullComPointer()) is False
    assert ReplicaUIADriver._safe_current(CurrentAttributeRaises(), "CurrentName", "fallback") == "fallback"


def test_uia_monitoring_does_not_steal_foreground():
    """The monitor contract cannot use an activating fallback when disconnected."""
    class PassiveDriver:
        def __init__(self):
            self.probes = 0

        @staticmethod
        def _foreground_snapshot():
            return {
                "available": True, "hwnd": 1, "pid": 2,
                "gui_info_available": True, "cursor_available": True,
                "z_order_available": True, "active_hwnd": 1,
                "focus_hwnd": 1, "capture_hwnd": None, "z_order_prev": None,
            }

        def read_only_probe(self):
            self.probes += 1
            return False

    driver = PassiveDriver()
    service = WeChatUIAService(driver)
    try:
        try:
            service.connect()
        except Exception:
            pass
        assert driver.probes == 1
        assert service.connected is False
    finally:
        service.close()


def test_status_query_never_initiates_connection():
    """A status read must not bypass monitor retry/backoff policy."""
    import threading
    from interactive_agent import InteractiveAgent

    class Driver:
        def is_running(self):
            return True

    class Service:
        connected = False
        driver = Driver()

        def get_status(self):
            return {"connected": False, "available": True, "provider": "UIA"}

        def worker_status(self):
            return {"dedicated": True}

    agent = InteractiveAgent.__new__(InteractiveAgent)
    agent.service = Service()
    agent._service_lock = threading.RLock()
    agent.lifecycle = "DEGRADED"
    agent.last_error = "UIA unavailable"
    agent.last_success = None
    agent.reconnect_count = 3
    agent._foreground_safety_blocked = False

    def unexpected_connect():
        raise AssertionError("status must not initiate connect")

    agent._connect_once = unexpected_connect
    status = agent.dispatch("status", {})
    assert status["uia_ready"] is False
    assert status["message_listener"] is False
    assert status["foreground_policy"] == "NEVER_STEAL"


def test_diagnostics_uses_read_only_guard():
    """Tree diagnostics must not bypass foreground safety accounting."""
    import threading
    from interactive_agent import InteractiveAgent

    calls = []

    class Driver:
        def diagnostics(self):
            return {"mode": "PASSIVE_READ_ONLY"}

    class Service:
        connected = True
        driver = Driver()

        def _read_only_call(self, operation, callback, timeout=None):
            calls.append((operation, timeout))
            return callback()

    agent = InteractiveAgent.__new__(InteractiveAgent)
    agent.service = Service()
    agent._service_lock = threading.RLock()
    assert agent.dispatch("diagnostics", {}) == {"mode": "PASSIVE_READ_ONLY"}
    assert calls == [("diagnostics", 30.0)]
