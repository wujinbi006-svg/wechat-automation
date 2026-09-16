"""本机 Interactive Agent IPC。

使用 127.0.0.1 上的受保护 JSON-line 通道，避免 Session 0 服务直接触碰 UIA。
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_agent_env() -> None:
    """加载 config/agent.env（若存在）。

    Windows 计划任务无法直接设置环境变量，因此 IPC 口令由 setup.py 写进
    该文件，服务与看门狗两侧各自加载，保证口令一致。
    """
    path = ROOT / "config" / "agent.env"
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass


_load_agent_env()

IPC_HOST = "127.0.0.1"
IPC_PORT = int(os.getenv("WECHAT_AGENT_IPC_PORT", "18010"))

# 口令必须显式配置。这里**故意不提供默认值**：如果退回一个写死在仓库里的
# 弱口令，本机任何进程都能通过 18010 端口操作微信。缺失时立即失败，让配置
# 问题在启动阶段就暴露，而不是留下一个静默的不安全状态。
IPC_TOKEN = os.getenv("WECHAT_AGENT_IPC_TOKEN", "").strip()
if not IPC_TOKEN:
    raise RuntimeError(
        "WECHAT_AGENT_IPC_TOKEN 未设置。\n"
        "  首次部署请运行:  python setup.py\n"
        "  它会生成 config/agent.env（含随机口令）并在两侧加载。\n"
        "  也可手动设置环境变量 WECHAT_AGENT_IPC_TOKEN=<随机串>。"
    )
if IPC_TOKEN == "change-me-local-agent-token":
    raise RuntimeError(
        "WECHAT_AGENT_IPC_TOKEN 仍是仓库里的占位值，本机任何进程都能用它操作微信。\n"
        "  请运行:  python setup.py --force   重新生成随机口令。"
    )


def rpc(method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> Any:
    payload = {"token": IPC_TOKEN, "method": method, "params": params or {}}
    with socket.create_connection((IPC_HOST, IPC_PORT), timeout=timeout) as conn:
        conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    if not data:
        raise ConnectionError("interactive agent returned no response")
    result = json.loads(data.decode("utf-8"))
    if not result.get("success"):
        raise RuntimeError(result.get("error", "interactive agent request failed"))
    return result.get("data")


class InteractiveAgentProxy:
    """Service 进程中的 UIA 代理；所有 UIA 操作在 Agent 进程执行。"""

    def __init__(self):
        self.connected = False

    def connect(self):
        status = rpc("connect")
        self.connected = bool(status.get("connected"))
        return status

    def disconnect(self):
        try:
            rpc("disconnect")
        finally:
            self.connected = False

    def get_status(self):
        return rpc("status")

    def get_chats(self):
        return rpc("get_chats")

    def get_current_chat(self):
        return rpc("get_current_chat")

    def search_chat(self, name):
        return rpc("search_chat", {"name": name})

    def direct_open_chat(self, name):
        # UIA navigation includes a post-action verification pass and can take
        # longer than the short status/read RPC budget.  Keep the Gateway
        # request alive long enough to receive an actual success or failure.
        return rpc("open_chat", {"name": name}, timeout=30.0)

    # 统一服务层接口；保留 direct_open_chat 兼容旧调用方。
    def open_chat(self, name):
        return self.direct_open_chat(name)

    def read_messages(self, limit=20):
        return rpc("read_messages", {"limit": limit})

    def get_input_state(self):
        return rpc("get_input_state")

    def diagnostics(self):
        return rpc("diagnostics", timeout=20.0)

    def get_chat_input_field(self):
        raise RuntimeError("interactive agent owns UIA input; use send_text through the controlled command path")

    def send_text(self, text):
        """在 Session 1 Agent 中执行显式发送。"""
        return rpc("send_text", {"text": text}, timeout=20.0)

    def send_image(self, path):
        return rpc("send_image", {"path": path}, timeout=20.0)

    def send_file(self, path):
        return rpc("send_file", {"path": path}, timeout=20.0)
