import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from fastapi.testclient import TestClient


def test_capabilities_and_control_contract(monkeypatch):
    monkeypatch.setenv("WECHAT_TEST_MODE", "true")
    from api import app

    with TestClient(app) as client:
        caps = client.get("/capabilities")
        assert caps.status_code == 200
        assert "send_text" in caps.json()["data"]["wechat"]["capabilities"]
        assert caps.json()["data"]["wechat"]["capabilities"]["send_image"]["available"] is True
        assert caps.json()["data"]["wechat"]["capabilities"]["send_file"]["available"] is True
        assert caps.json()["data"]["wechat"]["capabilities"]["open"]["verified"] is True
        assert caps.json()["data"]["wechat"]["capabilities"]["open"]["status"] == "OPEN_RESULT_VERIFIED"

        monkeypatch.setenv("WECHAT_ACTUATOR_MODE", "synthetic")
        synthetic = client.post("/control/execute", json={
            "request_id": "req-1",
            "action": "wechat.message.send_text",
            "target": {"conversation_id": "synthetic"},
            "payload": {"text": "synthetic outbound"},
        })
        assert synthetic.json()["ok"] is True
        assert synthetic.json()["result"]["synthetic"] is True


def test_synthetic_event_is_broadcast(monkeypatch):
    monkeypatch.setenv("WECHAT_TEST_MODE", "true")
    from api import app

    with TestClient(app) as client:
        with client.websocket_connect("/ws/events") as ws:
            response = client.post("/test/inject/message", json={"event": {
                "event": "message.new",
                "message_id": "synthetic-001",
                "chat_id": "synthetic-conversation",
                "chat_name": "Synthetic Chat",
                "chat_type": "direct",
                "sender_id": "synthetic-user",
                "sender_name": "Synthetic User",
                "content": "synthetic test message",
            }})
            assert response.status_code == 200
            event = ws.receive_json()
            assert event["event"] == "message.new"
            assert event["message_id"] == "synthetic-001"
            assert event["content"] == "synthetic test message"


def test_status_does_not_report_listener_while_uia_is_disconnected():
    from src.gateway_supervisor import GatewaySupervisor

    class UIA:
        connected = False

    class Service:
        uia = UIA()

    class LiveThread:
        @staticmethod
        def is_alive():
            return True

    supervisor = GatewaySupervisor(Service())
    supervisor._thread = LiveThread()
    status = supervisor.status()
    assert status["wechat_connected"] is False
    assert status["automation_ready"] is False
    assert status["message_listener"] is False


def test_supervisor_status_uses_live_agent_status_not_proxy_cache():
    from src.gateway_supervisor import GatewaySupervisor

    class UIA:
        # A stale local proxy cache must not make status look connected.
        connected = True

    class Service:
        uia = UIA()

        @staticmethod
        def ui_status():
            return {"connected": False, "message_listener": False}

    supervisor = GatewaySupervisor(Service())
    status = supervisor.status()
    assert status["wechat_connected"] is False
    assert status["automation_ready"] is False
    assert status["message_listener"] is False
