from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from pydantic import BaseModel
from src.providers import DatabaseDataProvider, UIADataProvider
from src.service_api import WeChatService
from src.uia_service import WeChatUIAService, LazyReplicaUIADriver
from src.interactive_ipc import InteractiveAgentProxy
from src.errors import ProviderUnavailable
from src.gateway_supervisor import GatewaySupervisor

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.FileHandler("logs/gateway.log", encoding="utf-8"), logging.StreamHandler()],
)

# 生产服务默认只连接 Session 1 Interactive Agent；旧进程内 UIA 仅用于显式兼容模式。
if os.getenv("WECHAT_UIA_MODE", "interactive").lower() == "legacy":
    _uia = WeChatUIAService(driver=LazyReplicaUIADriver())
else:
    _uia = InteractiveAgentProxy()
service=WeChatService(DatabaseDataProvider(), _uia)
supervisor = GatewaySupervisor(service)

def _wire_auto_reply():
    """把 AI 自动回复引擎挂到 Gateway 事件流上。

    引擎默认关闭（config/auto_reply.json 里 enabled=false），所以挂载本身
    不改变任何行为；开启后才对白名单好友生效。
    """
    try:
        from src.auto_reply import AutoReplyEngine, load_policy
    except Exception as exc:  # noqa: BLE001 - 子系统不可用不应阻止 Gateway 启动
        logging.getLogger("wechat.gateway").warning("auto-reply unavailable: %s", exc)
        return None
    policy = load_policy()
    if not policy.enabled and not policy.targets:
        logging.getLogger("wechat.gateway").info(
            "auto-reply engine not attached (disabled in %s)", policy.config_path)
        return None
    adapter = getattr(getattr(service, "provider", None), "adapter", None)
    self_ids = []
    try:
        if adapter is not None and hasattr(adapter, "discover_accounts"):
            self_ids = list(adapter.discover_accounts() or [])
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("wechat.gateway").warning("self-id discovery failed: %s", exc)
    engine = AutoReplyEngine(service, policy, self_ids=self_ids)
    engine.start()
    supervisor.add_event_listener(engine.on_event)
    supervisor.auto_reply = engine
    logging.getLogger("wechat.gateway").info(
        "auto-reply attached: enabled=%s dry_run=%s targets=%s self_ids=%d",
        policy.enabled, policy.dry_run, [t.name for t in policy.targets], len(self_ids))
    return engine

_auto_reply = _wire_auto_reply()

@asynccontextmanager
async def lifespan(_app):
    supervisor.start()
    yield
    supervisor.stop()
    if _auto_reply is not None:
        _auto_reply.stop()

app=FastAPI(title="WeChat Automation Service", version="0.1.0", lifespan=lifespan)
class Query(BaseModel):
    query: str
    chat_id: str|None=None
class ChatRequest(BaseModel):
    chat_id: str
class TypeRequest(BaseModel):
    text: str
class DraftRequest(BaseModel):
    chat_id: str
    text: str
class ConfirmRequest(BaseModel):
    draft_id: str
class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = {}
    session_id: str | None = None
class ControlRequest(BaseModel):
    request_id: str
    action: str
    target: dict = {}
    payload: dict = {}
    timeout_ms: int = 15000
class SyntheticEventRequest(BaseModel):
    event: dict
def ok(data): return {"success":True,"data":data}
@app.get("/health")
def health(): return {"success": True, "service": "wechat-automation", "status": "ready"}
@app.get("/status")
def runtime_status(): return {"success": True, "data": supervisor.status()}
@app.get("/agent/status")
def agent_status():
    try:
        status = service.ui_status()
        return ok(status)
    except Exception as exc:
        return {"success": False, "error": {"code": "AGENT_UNAVAILABLE", "message": str(exc)}}
@app.get("/uia/diagnostics")
def uia_diagnostics():
    try:
        return ok(service.uia.diagnostics())
    except Exception as exc:
        return {"success": False, "error": {"code": "UIA_DIAGNOSTICS_FAILED", "message": str(exc)}}
@app.get("/api/wechat/status")
def status(): return ok(service.status())
@app.get("/api/wechat/capabilities")
def capabilities(): return ok(service.status()["capabilities"])
@app.get("/tools/list")
def tools_list(): return ok(service.tool_list())
@app.post("/tools/call")
def tools_call(req: ToolCallRequest): return service.call_tool(req.name, req.arguments, req.session_id)
@app.get("/capabilities")
def capabilities_manifest():
    # 能力清单必须在 Agent 暂时未启动时也可读取；否则健康探针会因为
    # IPC ConnectionRefused 直接变成 500，无法用于启动前诊断。这里仅将
    # 运行态标记为未连接，不把未连接伪装成 UIA 已就绪。
    try:
        ui = service.ui_status()
    except Exception as exc:
        ui = {"connected": False, "available": False,
              "error": f"{type(exc).__name__}: {exc}"}
    ready = bool(ui.get("connected")) if isinstance(ui, dict) else False
    manifest = service.capability_manifest(ready=ready)
    return ok({"wechat": {"ready": ready, "capabilities": manifest}})

@app.post("/test/inject/message")
def inject_message(req: SyntheticEventRequest):
    if os.getenv("WECHAT_TEST_MODE", "").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=404, detail="test mode disabled")
    try:
        return ok(supervisor.inject_event(req.event))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/control/execute")
def control_execute(req: ControlRequest):
    """统一 command envelope；所有动作复用同一服务总线。"""
    return service.execute_command(
        request_id=req.request_id,
        action=req.action,
        target=req.target,
        payload=req.payload,
        timeout_ms=req.timeout_ms,
    )

@app.websocket("/ws/events")
async def events_socket(websocket: WebSocket):
    await websocket.accept()
    queue = supervisor.events.subscribe()
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        supervisor.events.unsubscribe(queue)
@app.get("/api/wechat/chats")
def chats():
    try: return ok(service.ui_chats())
    except ProviderUnavailable as e: return {"success":False,"error":{"code":"UIA_UNAVAILABLE","message":str(e)}}
@app.get("/api/wechat/current-chat")
def current_chat():
    try: return ok(service.ui_current_chat())
    except ProviderUnavailable as e: return {"success":False,"error":{"code":"UIA_UNAVAILABLE","message":str(e)}}
@app.post("/api/wechat/search")
def search(q: Query): return ok(service.search(q.query,q.chat_id))
@app.post("/api/wechat/open-chat")
def open_chat(req: ChatRequest):
    try: return ok(service.ui_open_chat(req.chat_id))
    except ProviderUnavailable as e: return {"success":False,"error":{"code":"UIA_UNAVAILABLE","message":str(e)}}
@app.post("/api/wechat/type-message")
def type_message(req: TypeRequest):
    try: return ok(service.type_message(req.text))
    except ProviderUnavailable as e: return {"success":False,"error":{"code":"UIA_UNAVAILABLE","message":str(e)}}
@app.get("/api/messages")
def messages(chat_id: str, limit: int=20):
    try: return ok(service.messages(chat_id,limit))
    except ProviderUnavailable as e: return {"success":False,"error":{"code":"DATABASE_UNAVAILABLE","message":str(e)}}
@app.post("/api/wechat/draft-message")
def draft(req: DraftRequest): return ok(service.draft_message(req.chat_id, req.text))
@app.post("/api/wechat/confirm-message")
def confirm(req: ConfirmRequest):
    try: return ok(service.confirm_message(req.draft_id))
    except ProviderUnavailable as e: return {"success":False,"error":{"code":"DRAFT_NOT_FOUND","message":str(e)}}
@app.post("/api/wechat/send")
def send(): return service.send_message()

# ---------------------------------------------------------------- 自动回复控制面
class ResumeRequest(BaseModel):
    conversation_id: str

@app.get("/auto-reply/status")
def auto_reply_status():
    if _auto_reply is None:
        return ok({"attached": False, "enabled": False,
                   "hint": "在 config/auto_reply.json 里配置并重启 Gateway 后生效"})
    return ok({"attached": True, **_auto_reply.status()})

@app.post("/auto-reply/reload")
def auto_reply_reload():
    if _auto_reply is None:
        return {"success": False, "error": {"code": "NOT_ATTACHED",
                                            "message": "自动回复引擎未挂载"}}
    try:
        from src.auto_reply import load_policy
        _auto_reply.reload(load_policy())
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": {"code": "RELOAD_FAILED", "message": str(exc)}}
    return ok(_auto_reply.status())

@app.post("/auto-reply/resume")
def auto_reply_resume(req: ResumeRequest):
    if _auto_reply is None:
        return {"success": False, "error": {"code": "NOT_ATTACHED",
                                            "message": "自动回复引擎未挂载"}}
    return ok({"resumed": _auto_reply.resume(req.conversation_id),
               "conversation_id": req.conversation_id})
