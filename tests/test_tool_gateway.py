from fastapi.testclient import TestClient
from api import app


def test_tool_list_and_send_validation():
    client = TestClient(app)
    response = client.get("/tools/list")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["data"]}
    assert "wechat.list_chats" in names
    assert "wechat.send_message" in names
    assert "wechat.message.send_image" in names
    assert "wechat.message.send_file" in names

    response = client.post("/tools/call", json={
        "name": "wechat.send_message", "arguments": {}
    })
    assert response.json()["success"] is True
    assert response.json()["data"]["success"] is False
    assert response.json()["data"]["error"]["code"] == "TARGET_REQUIRED"


def test_tool_validation_and_unknown_tool():
    client = TestClient(app)
    response = client.post("/tools/call", json={"name": "wechat.type_message", "arguments": {}})
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    response = client.post("/tools/call", json={"name": "wechat.nope", "arguments": {}})
    assert response.json()["error"]["code"] == "TOOL_NOT_FOUND"

def test_send_state_machine_and_target_resolution(monkeypatch):
    from src.providers import MockDataProvider
    from src.service_api import WeChatService
    monkeypatch.setenv("WECHAT_ENABLE_SEND", "true")
    class FakeUIA:
        connected = True
        def __init__(self):
            self.sent = False
            self.last_text = ""
        def search_chat(self, name):
            return [{"id": "yang-1", "name": "好友B"}] if name == "好友B" else []
        def open_chat(self, name): return True
        def get_current_chat(self): return "好友B"
        def get_input_state(self):
            return {"text": self.last_text if not self.sent else ""}
        def read_messages(self, limit):
            return {"messages": [] if not self.sent else [{"text": self.last_text}]}
        def send_text(self, text):
            self.sent = True
            self.last_text = text
            return True
    service = WeChatService(MockDataProvider(), FakeUIA())
    draft = service.draft_message("好友B", "自动化测试")
    confirmed = service.confirm_message(draft["draft_id"])
    mismatch = service.send_message(recipient="好友B", confirmation_id=confirmed["confirmation_id"], content="篡改")
    assert mismatch["error"]["code"] == "SEND_CONFIRMATION_MISMATCH"
    legacy_ready = service.send_message(recipient="好友B", confirmation_id=confirmed["confirmation_id"], content="自动化测试")
    assert legacy_ready["success"] is True
    assert legacy_ready["result_state"] == "SENT_VERIFIED"
    ready = service.send_message(recipient="好友B", content="自动化测试2")
    assert ready["success"] is True
    assert ready["result_state"] == "SENT_VERIFIED"
    assert service.send_message(recipient="好友B", confirmation_id=confirmed["confirmation_id"], content="自动化测试")["error"]["code"] == "SEND_ALREADY_CONSUMED"


def test_send_enabled_defaults_off(monkeypatch):
    from src.service_api import WeChatService
    monkeypatch.delenv("WECHAT_ENABLE_SEND", raising=False)
    assert WeChatService.send_enabled() is False


def test_send_enabled_explicit_false(monkeypatch):
    from src.service_api import WeChatService
    monkeypatch.setenv("WECHAT_ENABLE_SEND", "false")
    assert WeChatService.send_enabled() is False


def test_tool_list_does_not_claim_database_history_or_writes_are_executable(monkeypatch):
    from src.providers import MockDataProvider
    from src.service_api import WeChatService

    class DisconnectedUIA:
        connected = False

        @staticmethod
        def get_status():
            return {"connected": False}

    monkeypatch.delenv("WECHAT_ENABLE_SEND", raising=False)
    service = WeChatService(MockDataProvider(), DisconnectedUIA())
    tools = {item["name"]: item for item in service.tool_list()}
    assert tools["wechat.message.history"]["enabled"] is False
    assert tools["wechat.message.history"]["status"] == "DATABASE_KEY_UNAVAILABLE"
    assert tools["wechat.message.send_text"]["enabled"] is False
    assert tools["wechat.type_message"]["enabled"] is False
    assert tools["wechat.type_message"]["foreground_required"] is True


def test_all_real_write_paths_default_to_disabled(monkeypatch, tmp_path):
    from src.providers import MockDataProvider
    from src.service_api import WeChatService

    monkeypatch.delenv("WECHAT_ENABLE_SEND", raising=False)
    monkeypatch.delenv("WECHAT_ACTUATOR_MODE", raising=False)

    class RejectingUIA:
        connected = True
        def search_chat(self, _name):
            raise AssertionError("disabled send must not resolve or open a chat")
        def get_chat_input_field(self):
            raise AssertionError("disabled type_message must not touch UIA")

    service = WeChatService(MockDataProvider(), RejectingUIA())
    image = tmp_path / "blocked.png"
    file = tmp_path / "blocked.txt"
    image.write_bytes(b"x")
    file.write_text("x", encoding="utf-8")
    assert service.type_message("must not type")["error"]["code"] == "SEND_DISABLED"
    assert service.send_image(recipient="任何人", path=str(image))["error"]["code"] == "SEND_DISABLED"
    assert service.send_file(recipient="任何人", path=str(file))["error"]["code"] == "SEND_DISABLED"


def test_send_image_and_file_are_verified(tmp_path, monkeypatch):
    from src.providers import MockDataProvider, Message
    from src.service_api import WeChatService
    monkeypatch.setenv("WECHAT_ENABLE_SEND", "true")

    class AttachmentProvider(MockDataProvider):
        def __init__(self, before_rows, after_rows):
            super().__init__()
            self.before_rows = before_rows
            self.after_rows = after_rows
            self.sent = False

        def get_messages(self, chat_id, limit=20, before=None):
            if chat_id not in {"文件传输助手", "filehelper"}:
                return []
            rows = self.after_rows if self.sent else self.before_rows
            return rows[:limit]

    class AttachmentUIA:
        connected = True

        def __init__(self, provider):
            self.provider = provider
            self.current = "文件传输助手"

        def search_chat(self, name):
            return [{"id": "filehelper", "name": "文件传输助手"}] if name in {"文件传输助手", "filehelper"} else []

        def open_chat(self, name):
            self.current = "文件传输助手"
            return True

        def get_current_chat(self):
            return {"name": self.current}

        def send_image(self, path):
            self.provider.sent = True
            return True

        def send_file(self, path):
            self.provider.sent = True
            return True

    image_path = tmp_path / "test.png"
    image_path.write_bytes(b"fake-png")
    file_path = tmp_path / "test.txt"
    file_path.write_text("fake-file", encoding="utf-8")

    before_rows = [Message("1", "filehelper", "other", "对方", "2026-09-04", "文本", "旧消息").__dict__]
    image_after = before_rows + [{"message_id": "2", "chat_id": "filehelper", "sender_id": 2, "sender_name": "我", "timestamp": "2026-09-04", "message_type": "图片", "content": "test.png"}]
    file_after = before_rows + [{"message_id": "3", "chat_id": "filehelper", "sender_id": 2, "sender_name": "我", "timestamp": "2026-09-04", "message_type": "文件", "content": "test.txt"}]

    image_provider = AttachmentProvider(before_rows, image_after)
    image_service = WeChatService(image_provider, AttachmentUIA(image_provider))
    image_result = image_service.send_image(recipient="文件传输助手", path=str(image_path))
    assert image_result["success"] is True
    assert image_result["result_state"] == "SENT_VERIFIED"

    file_provider = AttachmentProvider(before_rows, file_after)
    file_service = WeChatService(file_provider, AttachmentUIA(file_provider))
    file_result = file_service.send_file(recipient="文件传输助手", path=str(file_path))
    assert file_result["success"] is True
    assert file_result["result_state"] == "SENT_VERIFIED"
