"""仓库内运行时检查；不发送消息、不调用 type/send。"""
from __future__ import annotations
import sys
from pathlib import Path
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).parents[1]))
from api import app, supervisor

with TestClient(app) as client:
    assert client.get("/health").json()["status"] == "ready"
    status = client.get("/status").json()["data"]
    assert status["gateway"] == "running"
    assert status["message_listener"] is True
    supervisor._last_error = "simulated"
    assert client.get("/status").status_code == 200
print("gateway recovery smoke test passed")
