# Architecture — wechat-service 工程架构

## 目标

让 OpenClaw / DSH 能长期、稳定、随时调用本机微信自动化能力。控制面、数据面、事件面彼此解耦；后续即使更换 UIA、数据库、OCR、媒体、消息监听等底层实现，也不需要重写整个系统。

## 六个平面

```
┌──────────────────────────────────────────────────────┐
│                 Integration Plane                      │
│  OpenClaw adapter (index.mjs) ←→ Gateway HTTP/WS       │
│  Plugin: wechat-gateway, channel: wechat               │
└──────────────────────┬───────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────┐
│                  Control Plane                         │
│  api.py (FastAPI)                                      │
│  WeChatService (service_api.py)                        │
│  GatewaySupervisor (gateway_supervisor.py)             │
│  EventHub · /tools/call · /control/execute             │
└──────┬───────────┬──────────┬──────────┬─────────────┘
       │           │          │          │
┌──────▼────┐ ┌───▼────┐ ┌───▼────┐ ┌───▼──────┐
│ Data Plane│ │ Event  │ │ Media  │ │ Lifecycle│
│           │ │ Plane  │ │ Plane  │ │ Plane    │
│ Database  │ │ EventHub│ │ UIA    │ │ Agent    │
│ Provider  │ │ WS     │ │ Send   │ │ Recovery │
│ KeyResolver│ │ Dedupe │ │ Clip   │ │ State    │
│ SQLCipher  │ │ Replay │ │ board  │ │ Machine  │
└───────────┘ └────────┘ └────────┘ └──────────┘
```

### Control Plane

**owner**: `api.py` + `src/service_api.py`
**startup**: WinSW → `python -m uvicorn api:app --port 8010`
**dependencies**: DatabaseDataProvider, InteractiveAgentProxy, GatewaySupervisor
**runtime state**: Gateway PID (port 8010), supervisor thread alive, IPC to Agent
**failure mode**: Agent unreachable → UIA unavailable; DB key missing → DB read unavailable
**recovery**: WinSW auto-restart; Agent health loop auto-reconnect; UIA bootstrap retry
**API**: HTTP `/health` `/status` `/tools/list` `/tools/call` `/capabilities`; WS `/ws/events`
**test coverage**: `test_control_plane.py` (capabilities, control execute, synthetic event, status)

### Data Plane

**owner**: `src/providers.py` (DatabaseDataProvider) + `src/database_adapter.py` + `src/key_resolver.py`
**startup**: 模块导入时发现 WeChat Files 目录
**dependencies**: WeChat Files 目录可见；可选 wechatauto_replica 包；可选 WECHAT_DB_KEY 环境变量
**runtime state**: key_discovery_state (PENDING/READY/PARTIAL/INVALID), read_plane (xinfo_only/sqlcipher_*)
**failure mode**: 无密钥 → PENDING；目录不可见 → UNAVAILABLE；密钥无效 → INVALID
**recovery**: KeyDiscoveryCoordinator 后台低频重试（指数退避 5s→300s）
**API**: `get_status()` `get_messages()` `get_chats()` `get_contacts()` `search_messages()` `get_database_inventory()` `get_xinfo()`
**test coverage**: `test_providers.py` (MockDataProvider/ImportedDataProvider), `test_database_plane.py`

### Event Plane

**owner**: `src/gateway_supervisor.py` (EventHub + GatewaySupervisor)
**startup**: supervisor.start() — 在 FastAPI lifespan 中启动
**dependencies**: UIA Agent (消息读取), EventHub (广播), WS clients (订阅)
**runtime state**: event_count, dedupe_dropped, last_message_event, last_success, last_error, reconnect_count
**failure mode**: UIA 断连 → 轮询失败累积 → DEGRADED；foreground safety violation → 永久 BLOCKED
**recovery**: 指数退避重连 (3s→60s)；UIA driver 重建；不自动恢复 foreground safety block
**API**: WS `/ws/events` (subscribe), POST `/test/inject/message` (synthetic, test mode only)
**test coverage**: `test_control_plane.py::test_synthetic_event_is_broadcast`, foreground safety tests

### Media Plane

**owner**: `src/uia_service.py` (send_image/send_file) + `src/minimal_foreground_input.py`
**startup**: 通过 InteractiveAgent IPC 调用
**dependencies**: UIA Agent connected, WECHAT_ENABLE_SEND=true
**runtime state**: gated by `_synthetic_actuator_enabled()` or `send_enabled()`
**failure mode**: 默认禁用；未观测到结果 → SEND_RESULT_NOT_OBSERVABLE
**recovery**: 不自动恢复发送；需显式确认
**API**: `/control/execute` action=wechat.message.send_text/send_image/send_file
**test coverage**: `test_control_plane.py` (synthetic actuator), manual live tests

### Integration Plane

**owner**: `openclaw-adapter/index.mjs`
**startup**: OpenClaw 插件注册 (`onStartup: true`)
**dependencies**: Gateway HTTP (8010) + WS (/ws/events)
**runtime state**: WebSocket connected, retry count, seen fingerprints (dedupe)
**failure mode**: Gateway 不可达 → WS 重连指数退避
**recovery**: 自动重连 (1s→30s, 2^retry cap 5)
**API**: registerTool(17 tools), WS event → normalize → resolveRoute → api.dispatchMessage
**test coverage**: node --check 语法验证; 功能测试在 OpenClaw 端

### Lifecycle Plane

**owner**: `interactive_agent.py` + `src/interactive_ipc.py` + `scripts/interactive_agent_watchdog.py`
**startup**: `scripts/start_interactive_agent.ps1` 或计划任务
**dependencies**: Session 1 (interactive desktop), WeChat running, COM initialized
**runtime state**: lifecycle (STARTING→CONNECTING_UIA→READY→DEGRADED→FOREGROUND_SAFETY_BLOCKED)
**failure mode**: WeChat 未运行 → WAITING_FOR_WECHAT; UIA 断连 → DEGRADED; foreground 偷窃 → BLOCKED
**recovery**: bootstrap retry (0,2,5,10,20,30s); health loop (3s interval, exponential reconnect); driver rebuild
**API**: IPC JSON-line TCP (127.0.0.1:18010), methods: status/connect/disconnect/get_chats/open_chat/read_messages/send_*
**test coverage**: foreground safety tests, manual live tests

## 进程拓扑

```
Session 0 (Service)                    Session 1 (Interactive)
┌─────────────────────┐                ┌─────────────────────┐
│ WeChatGateway       │  IPC :18010    │ InteractiveAgent     │
│ (WinSW → uvicorn    │←─────────────→│ (socketserver)       │
│  api:app :8010)     │                │                      │
│                     │                │ WeChatUIAService      │
│ GatewaySupervisor   │                │ ReplicaUIADriver      │
│ EventHub            │                │                      │
│ DatabaseDataProvider│                │ ←→ Weixin.exe        │
└────────┬────────────┘                │   (UIA / COM)        │
         │ WS :8010/ws/events          └──────────────────────┘
         ▼
┌─────────────────────┐
│ OpenClaw adapter    │
│ (Node.js, index.mjs) │
│ :18789 gateway       │
└─────────────────────┘
```

## 关键设计决策

1. **Session 隔离**: WinSW 服务在 Session 0 运行，不能直接操作 UIA；通过 IPC 委托给 Session 1 的 InteractiveAgent。
2. **Foreground safety**: 被动监控路径硬编码 `NEVER_STEAL`，任何被动 UIA 操作改变前台窗口都会被视为安全违规并暂停恢复。
3. **Key 安全边界**: KeyResolver 明确禁用 `process_memory_scan`、`dynamic_hook`、`process_injection`，只接受显式提供或缓存验证的密钥。
4. **证据优先**: 发送结果必须通过 UIA 观察验证（消息出现 + 输入框清空），不能仅凭调用无异常判定成功。
5. **Provider 解耦**: 数据面通过 WeChatDataProvider 抽象，UIA 面通过 WeChatUIAService 抽象，底层实现可替换。

## 历史说明

豆包（ doubao集成）只调用 HTTP Service，不接触 UIA、Qt、HWND 或数据库解密细节。`WeChatService` 通过 `WeChatDataProvider` 访问数据。数据库 provider 明确处于 BLOCKED 状态，不返回伪造数据。真实发送尚未启用；`type_message` 属于前台必需操作。
