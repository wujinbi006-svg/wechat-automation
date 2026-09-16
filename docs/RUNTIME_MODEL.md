# Runtime Model — 运行时模型

## 进程模型

| 进程 | Session | 角色 | 端口 |
|---|---|---|---|
| WeChatGateway (WinSW) | 0 | HTTP/WS Gateway, Supervisor, DB Provider | 8010 |
| InteractiveAgent | 1 | UIA 操作执行体 | 18010 (IPC) |
| OpenClaw adapter | — | 插件进程 (Node.js) | — |
| Weixin.exe | 1 | 微信客户端 | — |

## 生命周期状态机

所有核心组件统一使用以下状态：

```
STARTING → READY → DEGRADED → RECOVERING → READY
                ↓                          ↓
              FAILED ←──────────────────────┘
                ↓
            STOPPED

特殊：
  FOREGROUND_SAFETY_BLOCKED (UIA only, 不可自动恢复)
  WAITING_FOR_WECHAT (UIA, 可自动恢复)
  WAITING_FOR_CREDENTIAL (Database, 可自动恢复)
```

### 各组件状态定义

| 组件 | STARTING | READY | DEGRADED | FAILED |
|---|---|---|---|---|
| Gateway | uvicorn 启动中 | :8010 监听 + supervisor 线程活跃 | supervisor 轮询失败但 HTTP 存活 | uvicorn 崩溃 |
| Agent | bootstrap 中 | UIA connected + IPC 监听 | UIA 断连但 IPC 存活 | IPC 不可达 |
| UIA | _init_worker 中 | connected=True + probe OK | connected=False 但 driver.is_running | driver 不可用 |
| Database | 模块导入中 | key READY + SQLCipher 可读 | key PENDING 但 discovery 可用 | 目录不可见 |
| Event | supervisor.start() 前 | thread alive + UIA connected | thread alive + UIA disconnected | thread dead |
| OpenClaw | 插件加载中 | WS connected + tools registered | WS disconnected + retrying | 插件崩溃 |

## 恢复策略

| 故障 | 检测方式 | 恢复动作 | 状态存续 |
|---|---|---|---|
| UIA stale | health probe 失败 | 重建 driver + 重新 connect | IPC token, drafts |
| WebSocket lost | onclose/onerror | 指数退避重连 (1s→30s) | seen fingerprints |
| DB connection lost | open() 抛异常 | KeyDiscoveryCoordinator 重试 | key cache |
| Agent crash | IPC ConnectionRefused | watchdog 重启 (计划任务) | 无 — 进程重启 |
| OpenClaw plugin lost | Gateway WS 断开 | adapter 自动重连 | seen fingerprints |
| Foreground safety | before/after snapshot diff | **不恢复** — 暂停所有被动监控 | 无 |

## 健康端点

| 端点 | 用途 |
|---|---|
| `GET /health` | Liveness — Gateway 进程存活 |
| `GET /status` | Readiness — supervisor 状态 |
| `GET /capabilities` | Capability — 工具清单 + ready 标记 |
| `GET /agent/status` | Diagnostics — Agent lifecycle + UIA |
| `GET /uia/diagnostics` | Deep Diagnostics — UIA 树结构 |

`/status` 回答：
- 为什么 UIA 不 READY？→ `wechat_connected: false`, `last_error`
- 为什么 DB 不 READY？→ `provider.database_key_status`
- 为什么 EventSource 不 READY？→ `message_listener: false`
- 最近一次失败？→ `last_error`
- 最近一次恢复？→ `last_success`, `reconnect_count`

## 持久状态

| 状态 | 存储位置 | 用途 |
|---|---|---|
| Key cache | `work/database_key_cache.json` | 密钥 + 文件签名绑定 |
| Decrypted DB cache | `work/decrypted_db/` | 解密副本 + WAL 签名 |
| Event ring buffer | 内存 (deque maxlen=500) | 最近事件回溯 |
| Dedupe fingerprints | 内存 (deque maxlen=2000) | 去重 |
| Drafts | 内存 (self._drafts) | 未发送草稿 |
| Logs | `logs/*.log` | 运行日志 |

进程重启后丢失：事件 ring buffer、去重 fingerprints、drafts。Key cache 和解密副本持久化。
