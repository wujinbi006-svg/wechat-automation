import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from src.uia_service import ReplicaUIADriver, WeChatUIAService
from scripts.diagnostics.foreground_monitor_60s import foreground_window_changed


class _FakeWindow:
    pass


class _FakeImpl:
    def __init__(self):
        self._win = None
        self.activated = False

    def _find_main(self):
        return _FakeWindow()

    @staticmethod
    def _foreground_snapshot():
        return {
            "available": True, "hwnd": 1, "pid": 2,
            "gui_info_available": True, "cursor_available": True,
            "z_order_available": True, "active_hwnd": 1,
            "focus_hwnd": 1, "capture_hwnd": None, "z_order_prev": None,
        }

    def ensure_window(self, *args, **kwargs):
        self.activated = True
        raise AssertionError("passive path called activating ensure_window")


def test_passive_replica_probe_does_not_call_activating_backend(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    driver._impl = _FakeImpl()
    monkeypatch.setattr(ReplicaUIADriver, "_foreground_snapshot", staticmethod(_FakeImpl._foreground_snapshot))

    assert driver.ensure_window() is True
    assert driver._impl.activated is False
    assert driver._impl._win is not None


def test_read_only_probe_rejects_foreground_change(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    driver._impl = type("Impl", (), {"_find_main": lambda self: _FakeWindow()})()
    snapshots = iter([
        {"available": True, "hwnd": 10, "pid": 1, "title": "Editor", "gui_info_available": True, "cursor_available": True, "z_order_available": True},
        {"available": True, "hwnd": 20, "pid": 2, "title": "微信", "gui_info_available": True, "cursor_available": True, "z_order_available": True},
    ])
    monkeypatch.setattr(ReplicaUIADriver, "_foreground_snapshot", staticmethod(lambda: next(snapshots)))
    assert driver.read_only_probe() is False
    assert driver._last_probe["mode"] == "PASSIVE_READ_ONLY"


def test_read_only_probe_rejects_focus_or_cursor_change(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    driver._impl = type("Impl", (), {"_find_main": lambda self: _FakeWindow()})()
    snapshots = iter([
        {"available": True, "hwnd": 10, "pid": 1, "active_hwnd": 10, "focus_hwnd": 10,
         "gui_info_available": True, "cursor_available": True, "z_order_available": True,
         "capture_hwnd": None, "cursor": [100, 100], "z_order_prev": 9},
        {"available": True, "hwnd": 10, "pid": 1, "active_hwnd": 10, "focus_hwnd": 11,
         "gui_info_available": True, "cursor_available": True, "z_order_available": True,
         "capture_hwnd": None, "cursor": [100, 101], "z_order_prev": 9},
    ])
    monkeypatch.setattr(ReplicaUIADriver, "_foreground_snapshot", staticmethod(lambda: next(snapshots)))
    assert driver.read_only_probe() is False
    assert driver._last_probe["foreground_before"]["focus_hwnd"] == 10


def test_foreground_invariant_ignores_user_cursor_movement():
    """A cursor move alone is not a focus/activation change by the monitor."""
    before = {
        "available": True,
        "hwnd": 10,
        "active_hwnd": 10,
        "focus_hwnd": 10,
        "capture_hwnd": None,
        "cursor": [100, 100],
        "z_order_prev": 9,
        "gui_info_available": True, "cursor_available": True, "z_order_available": True,
    }
    after = dict(before, cursor=[310, 240])
    assert ReplicaUIADriver._foreground_snapshot_changed(before, after) is False
    assert WeChatUIAService._foreground_snapshot_changed(before, after) is False


def test_snapshot_change_ignores_z_order_only_telemetry():
    before = {"available": True, "hwnd": 10, "pid": 1, "z_order_prev": 9, "gui_info_available": True, "cursor_available": True, "z_order_available": True}
    after = {"available": True, "hwnd": 10, "pid": 1, "z_order_prev": 12, "gui_info_available": True, "cursor_available": True, "z_order_available": True}
    assert ReplicaUIADriver._foreground_snapshot_changed(before, after) is False
    assert WeChatUIAService._foreground_snapshot_changed(before, after) is False


def test_passive_safety_does_not_blame_a_user_switch_between_other_apps(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    before = {"available": True, "hwnd": 10, "pid": 1, "title": "Editor"}
    after = {"available": True, "hwnd": 11, "pid": 2, "title": "Browser"}
    monkeypatch.setattr(ReplicaUIADriver, "_snapshot_is_wechat", staticmethod(lambda _snapshot: False))
    assert ReplicaUIADriver._foreground_snapshot_changed(before, after) is True
    assert driver.foreground_safety_violation(before, after) is False


def test_passive_safety_blocks_a_non_wechat_to_wechat_transition(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    before = {"available": True, "hwnd": 10, "pid": 1, "title": "Editor"}
    after = {"available": True, "hwnd": 11, "pid": 2, "title": "WeChat"}
    monkeypatch.setattr(
        ReplicaUIADriver,
        "_snapshot_is_wechat",
        staticmethod(lambda snapshot: snapshot.get("title") == "WeChat"),
    )
    assert driver.foreground_safety_violation(before, after) is True


def test_long_running_monitor_does_not_treat_z_order_only_as_window_change():
    before = {"available": True, "hwnd": 10, "pid": 1, "z_order_prev": 9}
    after = dict(before, z_order_prev=12)
    # Neither the strict guard nor the soak report treats z-order telemetry as
    # foreground theft when the foreground window remains unchanged.
    assert ReplicaUIADriver._foreground_snapshot_changed(before, after) is False
    assert foreground_window_changed(before, after) is False


def test_long_running_monitor_detects_none_and_window_transitions():
    baseline = {"available": True, "hwnd": None, "pid": None}
    window = {"available": True, "hwnd": 10, "pid": 1}
    other_window = {"available": True, "hwnd": 11, "pid": 2}
    assert foreground_window_changed(baseline, window) is True
    assert foreground_window_changed(window, baseline) is True
    assert foreground_window_changed(window, other_window) is True
    assert foreground_window_changed(window, dict(window)) is False


def test_snapshot_change_detects_focus_appearing_from_none():
    before = {
        "available": True, "hwnd": 10, "pid": 1, "active_hwnd": None,
        "focus_hwnd": None, "capture_hwnd": None, "z_order_prev": 9,
        "gui_info_available": True, "cursor_available": True,
        "z_order_available": True,
    }
    after = dict(before, focus_hwnd=11)
    assert ReplicaUIADriver._foreground_snapshot_changed(before, after) is True
    assert WeChatUIAService._foreground_snapshot_changed(before, after) is True


def test_read_only_probe_records_after_snapshot_when_backend_raises(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    driver._impl = type("Impl", (), {
        "_find_main": lambda self: (_ for _ in ()).throw(RuntimeError("backend")),
    })()
    snapshots = iter([
        {"available": True, "hwnd": 10, "pid": 1, "gui_info_available": True,
         "cursor_available": True, "z_order_available": True},
        {"available": True, "hwnd": 20, "pid": 2, "gui_info_available": True,
         "cursor_available": True, "z_order_available": True},
    ])
    monkeypatch.setattr(ReplicaUIADriver, "_foreground_snapshot", staticmethod(lambda: next(snapshots)))
    import pytest
    with pytest.raises(RuntimeError, match="backend"):
        driver.read_only_probe()
    assert driver._last_probe["foreground_changed"] is True
    assert driver._last_probe["foreground_after"]["hwnd"] == 20


def test_unverifiable_snapshot_fails_closed():
    incomplete = {"available": True, "hwnd": 10, "pid": 1}
    assert ReplicaUIADriver._foreground_snapshot_verifiable(incomplete, incomplete) is False
    assert WeChatUIAService._foreground_snapshot_verifiable(incomplete, incomplete) is False


def test_read_only_probe_blocks_when_foreground_evidence_is_unavailable(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    driver._impl = type("Impl", (), {"_find_main": lambda self: _FakeWindow()})()
    monkeypatch.setattr(
        ReplicaUIADriver,
        "_foreground_snapshot",
        staticmethod(lambda: {"available": False, "error": "query failed"}),
    )
    assert driver.read_only_probe() is False
    assert driver._last_probe["foreground_verifiable"] is False


def test_recovery_blocks_when_foreground_evidence_is_unavailable(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    driver._impl = type("Impl", (), {"_win": None, "_find_main": lambda self: _FakeWindow()})()
    monkeypatch.setattr(
        ReplicaUIADriver,
        "_foreground_snapshot",
        staticmethod(lambda: {"available": False, "error": "query failed"}),
    )
    assert driver.recover_accessibility_tree(timeout=0.1) is False
    assert driver._last_accessibility_recovery["result"] == "FOREGROUND_UNVERIFIABLE"


def test_recovery_rejects_foreground_change(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    driver._impl = type("Impl", (), {"_win": None, "_find_main": lambda self: _FakeWindow()})()
    snapshots = iter([
        {"available": True, "hwnd": 10, "pid": 1, "title": "Editor", "gui_info_available": True, "cursor_available": True, "z_order_available": True},
        {"available": True, "hwnd": 20, "pid": 2, "title": "微信", "gui_info_available": True, "cursor_available": True, "z_order_available": True},
    ])
    monkeypatch.setattr(ReplicaUIADriver, "_foreground_snapshot", staticmethod(lambda: next(snapshots)))
    assert driver.recover_accessibility_tree() is False
    # The operation finds a tree, but the safety record must reject it.
    assert driver._last_accessibility_recovery["result"] == "FOREGROUND_CHANGED"
    assert driver._last_accessibility_recovery["tree_ready"] is False


def test_service_connect_requires_explicit_read_only_driver_contract():
    class Driver:
        def __init__(self):
            self.called = 0

        def ensure_window(self):
            self.called += 1
            return True

    driver = Driver()
    service = WeChatUIAService(driver=driver)
    import pytest
    with pytest.raises(Exception, match="read_only_probe"):
        service.connect()
    assert driver.called == 0
    service.close()


def test_read_only_call_checks_foreground_even_when_backend_raises(monkeypatch):
    class Driver:
        @staticmethod
        def _foreground_snapshot():
            return next(snapshots)

    snapshots = iter([
        {"available": True, "hwnd": 10, "pid": 1, "title": "Editor", "gui_info_available": True, "cursor_available": True, "z_order_available": True},
        {"available": True, "hwnd": 20, "pid": 2, "title": "微信", "gui_info_available": True, "cursor_available": True, "z_order_available": True},
    ])
    service = WeChatUIAService(driver=Driver())
    try:
        import pytest
        with pytest.raises(Exception, match="改变了前台窗口"):
            service._read_only_call("probe", lambda: (_ for _ in ()).throw(RuntimeError("backend")))
    finally:
        service.close()


def test_service_health_probe_rejects_foreground_change(monkeypatch):
    class Driver:
        @staticmethod
        def _foreground_snapshot():
            return next(snapshots)

        def ensure_window(self):
            return True

    snapshots = iter([
        {"available": True, "hwnd": 10, "pid": 1, "title": "Editor", "gui_info_available": True, "cursor_available": True, "z_order_available": True},
        {"available": True, "hwnd": 20, "pid": 2, "title": "微信", "gui_info_available": True, "cursor_available": True, "z_order_available": True},
    ])
    service = WeChatUIAService(driver=Driver())
    try:
        with __import__("pytest").raises(Exception, match="改变了前台窗口"):
            service._read_only_call("health_probe", service.driver.ensure_window)
    finally:
        service.close()


def test_search_chat_uses_passive_session_enumeration():
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    calls = []

    def get_chats():
        calls.append("get_chats")
        return [
            {"id": "session_item_文件传输助手", "name": "文件传输助手", "preview": "已置顶", "time": "今天"},
            {"id": "session_item_好友B", "name": "好友B", "preview": "你好", "time": "昨天"},
            {"id": "session_item_测试群", "name": "测试群", "preview": "文件传输助手", "time": "周一"},
        ]

    driver.get_chats = get_chats
    driver._impl = type(
        "Impl",
        (),
        {"_collect_results": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("search must not use interactive search results")
        )},
    )()

    exact = driver.search_chat("文件传输助手")
    assert [row["name"] for row in exact] == ["文件传输助手"]
    assert calls == ["get_chats"]

    by_preview = driver.search_chat("文件传输")
    assert [row["name"] for row in by_preview] == ["文件传输助手", "测试群"]

    assert driver.search_chat("") == []


def test_recover_accessibility_tree_uses_gate_without_activating_backend(monkeypatch):
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)

    class Impl:
        def __init__(self):
            self._win = None
            self.calls = []
            self._found = False

        def _find_main(self):
            self.calls.append("find_main")
            if len(self.calls) > 1:
                return _FakeWindow()
            return None

        def ensure_window(self, *args, **kwargs):
            raise AssertionError("must not call activating ensure_window")

    driver._impl = Impl()
    driver._last_accessibility_recovery = None
    stable = {"available": True, "hwnd": 1, "pid": 2, "title": "Explorer", "gui_info_available": True, "cursor_available": True, "z_order_available": True, "active_hwnd": 1, "focus_hwnd": 1, "capture_hwnd": None, "z_order_prev": None}
    monkeypatch.setattr(ReplicaUIADriver, "_foreground_snapshot", staticmethod(lambda: dict(stable)))

    assert driver.recover_accessibility_tree(timeout=0.2) is True
    assert driver._last_accessibility_recovery["foreground_changed"] is False
    assert driver._last_accessibility_recovery["result"] == "RECOVERED"


def test_direct_open_chat_does_not_fall_back_to_interactive_search_click():
    driver = ReplicaUIADriver.__new__(ReplicaUIADriver)
    driver._navigation_records = []

    class Rect:
        left = top = right = bottom = 0

    class NullPattern:
        def Select(self):
            raise AssertionError("unexpected select")

        def Invoke(self):
            raise AssertionError("unexpected invoke")

        def DoDefaultAction(self):
            raise AssertionError("unexpected default action")

        def ScrollIntoView(self):
            raise AssertionError("unexpected scroll")

    class Cell:
        ClassName = "mmui::ChatSessionCell"
        Name = "好友B\n你好"
        BoundingRectangle = Rect()

        def GetScrollItemPattern(self):
            return None

        def GetSelectionItemPattern(self):
            return None

        def GetInvokePattern(self):
            return None

        def GetLegacyIAccessiblePattern(self):
            return None

    class ListControl:
        def GetChildren(self):
            return [Cell()]

    class Window:
        def ListControl(self, **kwargs):
            return ListControl()

    class Impl:
        def __init__(self):
            self._win = Window()

        def _find_main(self):
            return Window()

        def current_chat(self):
            return "文件传输助手"

        def open_chat(self, *args, **kwargs):
            raise AssertionError("interactive fallback must stay explicit")

    driver._impl = Impl()

    assert driver.direct_open_chat("好友B") is False
    assert driver._navigation_records[-1]["result"] == "TARGET_OPEN_FAILED"
