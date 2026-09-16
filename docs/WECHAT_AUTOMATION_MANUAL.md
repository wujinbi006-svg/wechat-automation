# 微信自动化工程 — 完整使用说明书

> **给接手的 AI：读完这一份即可完全使用，不需要试错。**
>
> 最后验证：2026-09-12 · 12/12 工具通过 · pytest 98 passed · 自动回复自检 78/78 · 手册示例 13/13

---

## 0. 30 秒上手

```bash
# 健康检查
curl http://127.0.0.1:8010/health
# → {"success":true,"service":"wechat-automation","status":"ready"}

# 列出所有工具
curl http://127.0.0.1:8010/tools/list

# 调用一个工具
curl -X POST http://127.0.0.1:8010/tools/call \
  -H "Content-Type: application/json" \
  -d '{"name":"wechat.conversation.list","arguments":{}}'
```

**就这样，不用先做任何配置。** 整条链路已经开机自启并在运行。

---

## 1. 能力清单（实测，非推测）

| 能力 | 状态 | 实测证据 |
|---|---|---|
| 读全部历史消息 | ✅ | 18,734 条实测导出 |
| 实时新消息 | ✅ | WAL hook，1 秒内到达 |
| 发文本 | ✅ | `SENT_VERIFIED` |
| 发图片 | ✅ | `SENT_VERIFIED` + 落库复核 |
| 发文件 | ✅ | `SENT_VERIFIED` + 落库复核 |
| 读语音 | ✅ | 1,492 WAV 导出 |
| 解密图片 | ✅ | 1012/1012 |
| 会话列表 / 搜索 / 打开 | ✅ | UIA 实测 |
| AI 自动回复白名单好友 | ✅ | 已上线（好友A）；自检 78/78 |
| 数据库清单 / xInfo | ✅ | 24 库 |
| 群成员信息 | ❌ | UIA 无稳定节点，`NOT_SUPPORTED` |
| 朋友圈 | ❌ | 未实现 |
| 历史图片原图（CDN-only） | ❌ | 本地无缓存，密钥需查看大图时捕获 |

---

## 2. 系统架构

```
┌─────────────── Session 1（交互会话）───────────────┐
│                                                     │
│  Weixin.exe (PID 动态)                              │
│      └─ wxwal10.dll  ← IAT hook kernel32!WriteFile │
│              │  只复制 *.db-wal 写入，不改内容        │
│              ▼                                      │
│  \\\\.\\pipe\\wxwal10  （单实例管道）                 │
│              │                                      │
│  run_hook_stack.py  （计划任务 wxwal-hook-daemon）   │
│      ├─ daemon.py       → 注入 hook + 微信重启后重注  │
│      └─ wal_sidecar.py  → 独占管道 + 转发 :18011     │
│                                                     │
│  interactive_agent.py  → UIA 操作（:18010）          │
└─────────────────────────────────────────────────────┘
              │                    │
       tcp:18011              tcp:18010
              ▼                    ▼
┌─────────────── Session 0（服务）───────────────────┐
│  WeChatGateway (WinSW 服务, :8010)                  │
│    ├─ WalEventSource   → 轮询 :18011               │
│    ├─ GatewaySupervisor → 事件去重 + 广播            │
│    ├─ EventHub         → WebSocket /ws/events       │
│    └─ DatabaseDataProvider → 数据库读面              │
└─────────────────────────────────────────────────────┘
                              │
                    HTTP/WS :8010
                              ▼
                   OpenClaw 插件 / 任意 HTTP 客户端
```

### 为什么必须跨会话

**Session 0 创建的命名管道，Session 1 连不上**（Win32 error 5，已实测）。

Gateway 是服务（Session 0），微信在 Session 1。所以：

- **hook 管道必须由 Session 1 的 sidecar 独占**
- Gateway **不能**直接读管道，只能通过 TCP :18011 找 sidecar

`hook/probe_session_pipe.py` 可以复现这个边界。

### 为什么 hook 不会随微信更新失效

| 环节 | 依赖 | 稳定性 |
|---|---|---|
| hook 点 | `kernel32!WriteFile` | Windows API，永不变 |
| 注入目标 | `Weixin.dll` 的 IAT | 该 DLL 只导入 kernel32 |
| 数据载体 | SQLite WAL 格式 | SQLite 标准 |
| 解密 | SQLCipher + 每库 salt | 密钥已持有 |
| 页解析 | SQLite B-tree | SQLite 标准 |

**零微信硬编码常量。** 对比：`pywxrobot4` 锁 4.1.1.19、`aixed/WeChat-Hook` 锁 4.1.10.27、`WeChatFerry` 锁 3.9.12.17 —— 全部靠写死偏移，微信一更新即失效。

---

## 3. HTTP API 完整参考

**Base URL**：`http://127.0.0.1:8010`

### 3.1 状态与诊断

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 存活探针 |
| GET | `/status` | Gateway 运行状态 + 事件源 |
| GET | `/capabilities` | 能力清单（含可用性） |
| GET | `/agent/status` | UIA Agent 状态 |
| GET | `/uia/diagnostics` | UIA 树深度诊断 |
| GET | `/tools/list` | 工具清单 |

### 3.2 工具调用

```
POST /tools/call
{"name": "<工具名>", "arguments": {...}, "session_id": "optional"}
```

统一返回：

```json
{
  "success": true,
  "tool": "wechat.conversation.list",
  "data": [...],
  "error": null,
  "duration_ms": 168,
  "foreground_required": false,
  "focus_changed": false
}
```

失败时 `success=false`，`error` 为 `{"code": "...", "message": "..."}`。

### 3.3 事件流

```
WS ws://127.0.0.1:8010/ws/events
```

每条消息一个 `message.new` 事件：

```json
{
  "event": "message.new",
  "source": "wal_hook",
  "conversation_id": "filehelper",
  "chat_id": "filehelper",
  "chat_name": "文件传输助手",
  "chat_type": "direct",
  "message_id": "260",
  "sender_id": "wxid_...",
  "sender_name": "...",
  "content": "消息内容",
  "timestamp": 1789171193,
  "message_type": "text"
}
```

`source` 取值：

- `wal_hook` — **权威源**，来自数据库真实写入
- `uia` — 后备源（WAL 不可用时）
- `synthetic` — 仅测试模式

### 3.4 控制面

```
POST /control/execute
{
  "request_id": "唯一id",
  "action": "<canonical 名>",
  "target": {"conversation_id": "文件传输助手"},
  "payload": {"text": "..."},
  "timeout_ms": 60000
}
```

### 3.5 测试注入（需 `WECHAT_TEST_MODE=1`）

```
POST /test/inject/message
{"event": {"event":"message.new", "message_id":"t1",
           "chat_id":"synthetic", "content":"test"}}
```

---

## 4. 工具参考（12 个已验证）

所有工具通过 `POST /tools/call` 调用。实测耗时见括号。

### 只读 — 无需 UIA

#### `wechat.state` （1.5s）

Gateway + UIA + 数据库总览。

```json
{"provider": {...}, "database": {...}, "uia": {...},
 "connected": true, "automation_ready": true,
 "current_account": "wxid_EXAMPLE_ACCOUNT",
 "database_key_status": "DATABASE_KEY_READY"}
```

#### `wechat.account.list` （1.1s）
返回本机发现的微信账号目录列表。

#### `wechat.account.current` （0.5s）
返回当前账号字符串，如 `"wxid_EXAMPLE_ACCOUNT"`。

#### `wechat.database.inventory` （1.1s）
返回 24 个数据库的路径/大小/是否加密/是否有 WAL。

键名形如 `db_storage/message/message_0.db`（**注意带 `db_storage/` 前缀**）。

#### `wechat.database.xinfo` （0.5s）
读取明文 `xInfo.db`。xwechat_files 布局下返回 `{"available": false, "reason": ...}` —— 这是**正常的**，不是错误。

### 只读 — 需要 UIA Agent

#### `wechat.conversation.list` （0.17s）
当前可见会话列表。

```json
[{"id": "filehelper", "name": "文件传输助手", "unread": 0}]
```

#### `wechat.conversation.search` （0.19s）
`{"name": "关键词"}` → 匹配的会话列表。**只搜可见会话**。

#### `wechat.contact.search` （0.17s）
`{"name": "关键词"}` → 同上（当前实现基于可见会话列表）。

#### `wechat.message.read` （0.3s）
`{"limit": 20}` → 当前打开会话的**可见** UIA 消息。

```json
{"chat": "文件传输助手",
 "messages": [{"text": "...", "message_type": "text", "rect": [...]}],
 "foreground_required": false}
```

#### `wechat.message.latest` （0.25s）
当前会话最新一条。

#### `wechat.group.search` （0.17s）
`{"name": "关键词"}` → 可见群聊。

### 权威读面 — 数据库直读，不需要 UIA

#### `wechat.message.history` （0.44s）★ 最常用

```json
{"conversation_id": "filehelper", "limit": 50, "before": null}
```

返回**完整历史**（不是 UIA 可见部分）：

```json
[{"message_id": "260", "chat_id": "filehelper",
  "sender_id": "wxid_...", "sender_name": "...",
  "timestamp": "2026-09-12T08:03:51+00:00",
  "create_time": 1789171431,
  "message_type": "text",
  "content": "消息内容",
  "sort_seq": 1789171431000,
  "local_id": 260}]
```

`conversation_id` 接受 **wxid / 群 id / 昵称 / 备注**，自动解析。

分页用 `before`（传上一页的 `sort_seq`）。

---

## 5. 真实调用示例

### Python（推荐）

```python
import json, urllib.request

BASE = "http://127.0.0.1:8010"

def call(name, **args):
    body = json.dumps({"name": name, "arguments": args}).encode()
    req = urllib.request.Request(BASE + "/tools/call", data=body,
                                 headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    if not r.get("success"):
        raise RuntimeError(r.get("error"))
    return r["data"]

# 列出会话
for c in call("wechat.conversation.list"):
    print(c["name"])

# 读历史（用昵称即可）
for m in call("wechat.message.history", conversation_id="好友B", limit=10):
    print(m["timestamp"], m["sender_name"], m["content"][:50])
```

### 订阅实时消息

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8010/ws/events") as ws:
        while True:
            ev = json.loads(await ws.recv())
            if ev.get("event") == "message.new":
                print(ev["source"], ev["conversation_id"], ev["content"][:60])

asyncio.run(main())
```

### 发送消息

```python
import json, urllib.request, time

body = json.dumps({
    "request_id": f"send-{int(time.time())}",
    "action": "wechat.message.send_text",
    "target": {"conversation_id": "文件传输助手"},
    "payload": {"text": "hello"},
    "timeout_ms": 60000,
}).encode()

req = urllib.request.Request("http://127.0.0.1:8010/control/execute",
                             data=body, headers={"Content-Type": "application/json"})
r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
print(r["ok"], (r.get("result") or {}).get("result_state"))
# → True SENT_VERIFIED
```

`result_state` 取值：

| 值 | 含义 |
|---|---|
| `SENT_VERIFIED` | 已通过消息观察确认落库 |
| `SEND_RESULT_NOT_OBSERVABLE` | 动作已执行但未观测到结果 |
| `SEND_DISABLED` | `WECHAT_ENABLE_SEND` 未开启 |

---

## 6. ⚠️ 发送消息的致命陷阱

**微信的发送作用于「当前打开的会话」，不是「指定会话」。**

`interactive_open_chat("A")` 会把界面切到 A。之后任何不先切换目标的发送，**会发到 A**。

### 错误写法

```python
rpc("interactive_open_chat", {"name": "某群"})
rpc("send_text", {"text": "..."})   # ← 发到那个群了！
```

### 正确写法

```python
SAFE_TARGET = "文件传输助手"

def send_safe(text):
    rpc("interactive_open_chat", {"name": SAFE_TARGET})
    time.sleep(1.5)
    cur = rpc("get_current_chat")           # 必须校验
    if cur != SAFE_TARGET:
        raise RuntimeError(f"当前会话是 {cur}，中止发送")
    rpc("send_text", {"text": text})
```

### 已有安全模块

**`hook/trigger_safe.py`** 已实现上述约束，硬编码只允许 `文件传输助手`：

```python
SAFE_TARGET = "文件传输助手"
FORBIDDEN_HINTS = ("群", "chatroom")
```

**测试时一律用它，不要自己写。**

> 这条规则来自一次真实事故：早期脚本未校验当前会话，把 3 条测试消息发进了一个真实群。用户手动撤回。**不要重犯。**

---

## 7. 运维手册

### 进程与端口

| 组件 | 位置 | 端口 | 自启 |
|---|---|---|---|
| WeChatGateway | Session 0 服务 | 8010 | ✅ WinSW |
| InteractiveAgent | Session 1 | 18010 | ⚠️ 需手动/看门狗，见下 |
| WAL sidecar | Session 1 | 18011 | ✅ 计划任务 |
| hook daemon | Session 1 | — | ✅ 计划任务 |

### ⚠️ UIA 的硬前提：微信主窗口必须存在且可见

UIA 驱动（`work/wechatauto_pkg/.../uia_driver.py`）枚举主窗口时要求：

```python
if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) in ("微信", "Weixin"):
```

所以**把微信关到托盘 = 全部 UIA 工具瘫痪**（搜索、打开会话、发送、
会话列表），报错是 `微信 UIA 主窗口不可用`。只读的数据面（历史消息、
事件流）不受影响。

恢复步骤（两件事都要做，缺一不可）：

```powershell
# 0) 先诊断，脚本会自己找到当前窗口句柄（句柄会变，不要硬编码）
python scripts\diagnostics\dump_uia_tree.py
#   窗口可见=False 或 mmui 树没内容 → 修复

# 1) 显示窗口并做一次最小化→还原，让 Qt 渲染器重新挂载
python scripts\diagnostics\dump_uia_tree.py --fix

# 2) 重启 InteractiveAgent，让它重新做 Qt accessibility gate 激活
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'interactive_agent\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
# 看门狗会在 3 秒内拉起（若没跑：python scripts\interactive_agent_watchdog.py）

# 3) 确认
curl.exe -s http://127.0.0.1:8010/agent/status
#   → "uia_ready": true
```

**另一个坑：窗口可见但 UIA 树是空壳。** 微信 4.x 是 Qt 自绘，
`mmui::MainWindow` 之下只有 `MMUIRenderSubWindowHW` 一个空节点时，
`session_list` 找不到、搜索返回空数组 `[]`。`--fix` 就是为解决这个而写的
（真实的最小化 → 还原会让渲染器重新挂载）。

判断标准 —— 树里必须出现 `mmui::MainView` / `mmui::TitleBar` / `session_list`：

```powershell
python scripts\diagnostics\dump_uia_tree.py --depth 3
```

### 计划任务 `wxwal-hook-daemon`

```powershell
# 状态
Get-ScheduledTask -TaskName wxwal-hook-daemon | Select TaskName,State

# 启动 / 停止
Start-ScheduledTask -TaskName wxwal-hook-daemon
Stop-ScheduledTask  -TaskName wxwal-hook-daemon

# 日志
Get-Content hook\build\supervisor.log -Tail 20
Get-Content hook\build\daemon.log -Tail 20
```

它运行 `run_hook_stack.py`，管住两个子进程：

- `daemon.py` — 注入 hook，微信重启后自动重注
- `wal_sidecar.py` — 独占管道，转发事件到 :18011

**重启任务 = 重启这两个子进程。**

### 三层自愈

```
stack 挂了    → 计划任务 RestartCount=999 每 1 分钟拉起
子进程挂了    → run_hook_stack 5-10 秒后重启它
微信重启      → daemon 检测新 PID → 重新注入
```

### Gateway 服务

```powershell
Get-Service WeChatGateway | Select Name,Status
Restart-Service WeChatGateway          # 需管理员
# 或（不需管理员）
& service\WeChatGateway.exe stop
& service\WeChatGateway.exe start
```

---

## 8. 故障排查

### 症状 → 原因 → 处理

| 症状 | 原因 | 处理 |
|---|---|---|
| `/status` 里 `active_event_source=uia_poll` | WAL 源未就绪 | 检查 18011 端口与 sidecar 进程 |
| `wechat.message.history` 返回空 | 密钥未接通 | 检查 `WECHAT_DB_KEY_FILE` |
| 工具返回 `TIMEOUT` | 首次解密 contact 库 | 重试一次（之后走缓存） |
| `Permission denied ... .tmp` | 缓存并发写（已修复） | 升级到含唯一临时名的版本 |
| 全部 UIA 工具 `UNAVAILABLE` | Agent 未运行 | `python scripts\interactive_agent_watchdog.py`（或 `Start-ScheduledTask wxwal-hook-daemon` 拉 Session-1 栈） |
| `微信 UIA 主窗口不可用` | 微信被关到托盘，主窗口不可见 | `python scripts\diagnostics\dump_uia_tree.py --fix` 然后重启 Agent（见第 7 节） |
| `Find Control Timeout: session_list` | 窗口可见但 mmui 树是空壳 | 同上：`dump_uia_tree.py --fix` |
| `conversation.search` 返回 `[]` | 同上，树没挂载 | 同上 |
| `AMBIGUOUS_TARGET` | 昵称命中多个会话 | 用更精确的备注名，并确认 `conversation.search` 只返回一条 |
| 发送报 `TARGET_NOT_FOUND` | 昵称解析不到 | 改用 wxid 或先 `conversation.search` |
| 管道 `err=5` | 跨会话访问 | 正常现象；必须走 sidecar |

### 诊断命令

```bash
# 一键验证全部 12 个工具
python scripts\verify\verify_tools.py

# 验证本手册中的所有示例
python scripts\verify\verify_manual.py

# UIA 窗口状态：主窗口是否可见 + mmui 树是否有内容（--fix 就地修复）
python scripts\diagnostics\dump_uia_tree.py
python scripts\diagnostics\dump_uia_tree.py --fix

# 解析某个好友的 wxid / 方向归属（给 auto_reply.json 的 aliases 用）
python scripts\diagnostics\resolve_friend.py 好友A

# 系统全貌
curl -s http://127.0.0.1:8010/status | python -m json.tool

# 事件源
curl -s http://127.0.0.1:8010/status | python -c "import sys,json; d=json.load(sys.stdin)['data']; print(json.dumps(d['event_sources'],ensure_ascii=False,indent=1))"

# sidecar 直查
python -c "import json,socket; c=socket.create_connection(('127.0.0.1',18011),timeout=10); c.sendall(b'{\"method\":\"wal_status\"}\n'); print(c.recv(65536).decode())"
```

### 关键日志

| 文件 | 内容 |
|---|---|
| `logs/gateway.log` | Gateway 主日志 |
| `logs/WeChatGateway.err.log` | 服务标准错误 |
| `hook/build/supervisor.log` | 计划任务栈 |
| `hook/build/daemon.log` | 注入器 |
| `hook/build/wxwal10.log` | DLL 计数器（在微信进程内） |

---

## 9. 配置

### 环境变量

**用户级**（Session 1 子进程继承）：

| 变量 | 值 |
|---|---|
| `WECHAT_FILES_BASE` | `__USERPROFILE__\xwechat_files` |
| `WECHAT_DB_KEY_FILE` | `<repo>\work\db.key` |
| `WECHAT_ALLOW_KEY_EXTRACTION` | `1` |
| `WECHAT_ENABLE_SEND` | `1` |
| `WECHAT_UIA_MODE` | `interactive` |

**服务级**（WinSW `<env>`，见 `service/WeChatGateway.xml`）。

### 密钥

- 数据库密钥：`work/db.key`（64 位 hex，**已 gitignore**）
- 图片密钥：`wx_key.get_image_key()` 本地推导，无需 hook
- 密钥缓存：`work/database_key_cache.json`

---

## 10. 目录结构

```
wechat-service/
├── src/
│   ├── service_api.py         服务总线 + 工具分发
│   ├── providers.py           数据 provider
│   ├── database_adapter.py    数据库发现 + 解密 + 4.x 读取
│   ├── key_resolver.py        密钥解析（raw / passphrase）
│   ├── credential_resolver.py 可插拔凭据来源
│   ├── uia_service.py         UIA 门面 + 前台安全
│   ├── gateway_supervisor.py  事件监督 + 去重
│   ├── wal_event_source.py    ★ Gateway 侧 WAL 客户端
│   ├── interactive_ipc.py     Agent IPC 代理
│   ├── text_archiver.py       纯文本归档
│   ├── chat_archiver.py       完整归档（含媒体）
│   ├── voice_exporter.py      语音 → WAV
│   ├── image_decryptor.py     .dat 图片解密
│   └── providers_abstraction/ ★ 可插拔接口层
├── hook/
│   ├── src/wxwal.c            ★ hook DLL 源码
│   ├── wal_sidecar.py         ★ Session 1 管道持有者
│   ├── wal_capture.py         管道服务器 + WAL 帧重组
│   ├── daemon.py              ★ 注入守护
│   ├── run_hook_stack.py      ★ 计划任务入口
│   ├── trigger_safe.py        ★ 安全触发器（只能发给自己）
│   ├── decrypt2.py            帧解密
│   ├── sqlite_record.py       SQLite 记录解码
│   └── README.md              hook 详细文档
├── tests/                     pytest 62 passed
├── work/                      gitignore：密钥、缓存、临时
└── service/WeChatGateway.xml  WinSW 配置
```

---

## 11. 开发指南

### 跑测试

```bash
# 必须用系统 Python（PATH 上的 python 可能是沙箱版本，缺依赖）
__PYTHON__ -m pytest -q
```

### 修改 hook 后重新编译

```bash
ZIG="__USERPROFILE__\AppData\Local\Microsoft\WinGet\Packages\zig.zig_Microsoft.Winget.Source_8wekyb3d8bbwe\zig-x86_64-windows-0.16.0\zig.exe"
"$ZIG" cc -shared -O2 -o hook\build\wxwal10.dll hook\src\wxwal.c -lkernel32
```

> ⚠️ **已注入的 DLL 无法卸载。** 改协议必须同时改：
> 1. `wxwal.c` 里的 `WXWAL_PIPE_NAME`
> 2. `wal_capture.py` 里的 `PIPE_NAME`
>
> 否则新 DLL 与旧副本在同管道混流。彻底清理需重启微信。

### 管道是单实例

**sidecar 和调试脚本不能同时读管道。**

调试前：

```powershell
Stop-ScheduledTask -TaskName wxwal-hook-daemon   # 释放管道
# ... 调试 ...
Start-ScheduledTask -TaskName wxwal-hook-daemon
```

### 契约

- 修改任何工具返回结构 → 同步更新本文档第 4 节
- 修改监听端口 → 同步更新本文档第 3 节
- **不要改 `trigger_safe.py` 的硬约束**

---

## 12. 已知限制

| 限制 | 说明 |
|---|---|
| 群成员信息 | UIA 无稳定节点，`NOT_SUPPORTED` |
| 朋友圈 | 未实现 |
| CDN-only 图片 | 本地无缓存；密钥仅在查看大图时进内存 |
| 管道单实例 | 一个消费者 |
| 发送依赖当前会话 | 见第 6 节，必须校验 |
| 微信重启 | hook 消失，daemon 自动重注（约 5 秒） |
| `WriteFileEx` | 未捕获（实测微信走 `WriteFile`） |
| 自动回复方向判定 | 依赖 `sender_id`；拿不到时默认**不回**（`assume_inbound_when_unknown=false`） |
| 自动回复非文本 | 图片/语音/文件来信只跳过，不回复 |
| 自动回复依赖 UIA | 微信在托盘时能生成但发不出去，记 `failed`（见第 7 节） |
| 自动回复会抢前台 | UIA 发送需要微信窗口在前台，可能打断你当前的操作 |
| 自动回复回复质量 | 免费路由模型的水平，会出现答非所问；不适合替你做承诺 |

---

## 13. 安全须知（必读）

1. **发送是不可逆的对外动作。** 发到群/联系人后对方立即收到。
2. **必须校验当前会话**（第 6 节），这是唯一防止误发的机制。
3. **`WECHAT_ENABLE_SEND=1` 是全局放行的** —— 任何能访问 `127.0.0.1:8010` 的进程都能真实发消息。
4. **密钥文件是明文**，`work/db.key` 能解密全部微信数据。
5. **档案是明文**，导出的聊天记录含全部对话内容。
6. **自动回复在无人看管时对外发送。** 当前状态：**已上线**（目标 好友A，
   `dry_run=false`）。想停就改 `config/auto_reply.json` 的 `enabled=false` 再
   `POST /auto-reply/reload`；第 14.7 节的防死循环闸门不要关。
7. **手动接管是最快的刹车。** 你在微信里给该好友发任意一句话，引擎立刻静默
   15 分钟；要恢复用 `POST /auto-reply/resume`。

---

## 14. AI 自动回复（新）

用 AI 替机主回复指定好友。**默认关闭**：配置文件不存在、或 `enabled=false`、
或好友不在 `targets` 白名单里，引擎都不会动作。

### 14.1 它怎么工作

```
微信写入 WAL
   └─ IAT hook 采到帧 ── sidecar(18011) ── Gateway 事件流
                                              │  去重之后
                                              ▼
                                     AutoReplyEngine.on_event   ← 廉价判定 + 入队
                                              │  工作线程
                                    白名单 → 方向 → 静默/接管/限速 → 合并去抖
                                              │
                                    读该会话最近 N 条消息（既有 DB 读面）
                                              │
                                    调 LLM 生成一条回复
                                              │
                                    WeChatService.send_message(recipient, text)
                                      搜索 → 唯一性 → 打开会话 → 校验当前会话 → 发送 → 验证
```

关键点：**发送出口复用第 6 节那条已验证的安全路径**，自动回复没有新增任何
UIA 代码、没有新的注入、没有绕过 `WECHAT_ENABLE_SEND`。

### 14.2 当前部署（已上线，目标：好友A）

`config/auto_reply.json` 已经写好并生效：

| 项 | 值 |
|---|---|
| 目标 | `name="好友A"`，`aliases=["wxid_EXAMPLE_FRIEND_A"]` |
| 开关 | `enabled=true`，`dry_run=false`（**已上线，会真实发送**） |
| LLM | `http://localhost:3080/v1`，`LLM7-OVHFree` → `traework-free` → `opr` |
| 静默时段 | 23:30–08:00 |
| 机主接管 | 15 分钟 |
| 回复上限 | 同会话 30 分钟 10 条；全局每分钟 6 条；连续 5 条后静默 |

**本机已有一个免费且 OpenAI 兼容的本地 LLM 端点**，不用申请 API key：

```bash
curl -s http://localhost:3080/v1/models
# → {"object":"list","data":[{"id":"traework-free"},{"id":"opr"},{"id":"LLM7-OVHFree"}]}
```

实测可靠性（多轮带历史的真实 prompt，各 6 次）：

| 模型 | 成功 | 平均延迟 | 备注 |
|---|---|---|---|
| `LLM7-OVHFree` | 6/6 | 1.2s | **主力** |
| `traework-free` | 6/6 | 3.2s | 第一备选 |
| `opr` | 4/6 | 1.3s | 偶发返回 `content: null`，第二备选 |

`llm_fallback_models` 会按顺序退让；全部失败才把这轮判成 `failed`（不发消息）。

### 14.3 从零给另一个好友开启

```bash
# 1) 先查出他的 wxid 和方向归属（只读）
python scripts\diagnostics\resolve_friend.py 好友备注名
#   accounts / resolve_username / 消息方向 都会打印出来

# 2) 抄一份配置
copy config\auto_reply.example.json config\auto_reply.json
#   targets 里填 {"name":"好友备注名","aliases":["wxid_..."], "persona":"..."}
#   dry_run 保持 true

# 3) 让 Gateway 读配置（配置存在时才会挂载引擎）
& service\WeChatGateway.exe stop; & service\WeChatGateway.exe start

# 4) 用真实配置 + 真实历史 + 真实 LLM 跑一遍，只记录不发送
python scripts\auto_reply_livecheck.py --rule 好友备注名 --text "在干嘛呢"
#   → 打印 inbound / reply / llm 尝试记录，明确说明「微信里不会出现任何消息」

# 5) 满意后 dry_run 改 false，热加载即可（不用重启）
curl -X POST http://127.0.0.1:8010/auto-reply/reload
```

`aliases` 不是可选项：**WAL 事件的 `chat_name` 有时直接就是 wxid**，
只写备注名会整个漏掉这个会话（首次部署就踩到了）。

### 14.4 控制面

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/auto-reply/status` | 开关、白名单、计数器、队列深度、静默剩余秒数、`started_at`、最近 10 条决策 |
| POST | `/auto-reply/reload` | 热加载 `config/auto_reply.json`，不用重启 |
| POST | `/auto-reply/resume` | 手动解除某会话的「机主接管」或静默锁 |

```bash
curl -X POST http://127.0.0.1:8010/auto-reply/resume \
  -H "Content-Type: application/json" -d '{"conversation_id":"wxid_xxx"}'
```

### 14.5 配置项

| 键 | 默认 | 含义 |
|---|---|---|
| `enabled` | `false` | 总开关 |
| `dry_run` | `true` | 只生成不发送 |
| `targets[].name` | — | **发给 UIA 搜索框的名字**（必须唯一命中），也是默认匹配键 |
| `targets[].aliases` | `[]` | 补充匹配键，**填 wxid** —— WAL 的 `chat_name` 可能就是 wxid |
| `targets[].persona` | — | 该好友的人设补充，拼进 system prompt |
| `targets[].system_prompt` | — | 覆盖全局 prompt（例如对客户加硬约束） |
| `targets[].max_chars` | `300` | 单条回复字数上限 |
| `targets[].allow_group` | `false` | 是否允许群聊（默认不处理群） |
| `debounce_seconds` | `4.0` | 好友连发多条，等这么久没新消息才合并成一次回复 |
| `min_interval_seconds` | `20.0` | 同一会话两条回复的最小间隔 |
| `max_replies_per_conversation` | `12` | `conversation_window_minutes` 窗口内上限 |
| `max_replies_global_per_minute` | `8` | 全局每分钟上限 |
| `quiet_hours` | `[["23:30","08:00"]]` | 静默时段，支持跨零点 |
| `human_override_minutes` | `15.0` | **机主自己发言后，该会话自动静默多久** |
| `max_consecutive_auto_replies` | `6` | 连续回复上限，超过则静默 |
| `bot_burst_count` / `_seconds` | `8` / `30` | 对面刷屏频率达到阈值 → 判定疑似机器人 → 静默 |
| `mute_minutes` | `60.0` | 触发上述保护后静默多久 |
| `history_limit` | `12` | 拼进 prompt 的历史条数 |
| `replay_grace_seconds` | `120.0` | **早于引擎启动这么久的消息当重放丢弃**（见 14.6） |
| `llm_model` / `llm_fallback_models` | — | 主力模型 + 按序退让的备选 |
| `signature` | `""` | 追加到回复末尾的标记，例如 `（自动回复）` |
| `assume_inbound_when_unknown` | `false` | 拿不到 `sender_id` 时是否仍当好友来信（**不建议开**） |

以 `_` 开头的键当作注释忽略；其它未知键直接报错，不会静默忽略。

### 14.6 四个必须知道的坑（全部踩过并已修）

1. **账号 ID 后缀。** `discover_accounts()` 返回 `wxid_EXAMPLE_ACCOUNT`
   （账号目录名），而消息表里的 `real_sender_id` 映射出的是 `wxid_EXAMPLE`。
   不归一化就会把机主自己的发言判成好友来信 → **机器人回给自己**。
   现在 `normalize_account_id()` 会剥掉 `_xxxx` 后缀。

2. **重放。** sidecar 的游标在重启后归零，Gateway 重启会把缓冲区里的旧事件
   重新投递一遍。不挡的话，重启一次就可能对着几小时前的旧消息回一句。
   现在 `replay_grace_seconds` 把「早于引擎启动时间 120 秒」的消息判成
   `stale_replay` 丢掉。

3. **回声。** 引擎自己发出的回复会通过 WAL 回到事件流（`sender_id` = 自己），
   如果被当成「机主手动发言」，每次回复后都会自锁 15 分钟。现在 `_is_echo()`
   用「去空白归一化相等 + 长公共前缀」两档匹配，漏判代价不对称所以宁可宽松。

4. **UIA 前提。** 微信关到托盘时全部 UIA 工具瘫痪，回复会记成
   `failed: TARGET_NOT_FOUND`。见第 7 节 `dump_uia_tree.py --fix`。

### 14.7 三层防死循环（这是最容易出事的地方）

两个机器人互相自动回复会无限刷屏，所以有三道闸：

1. **不回自己的消息。** `sender_id` 属于自己 → 判为 outbound；文本与最近发出的
   回复相同（180 秒内）→ 判为回声。方向判不出来时**默认不回**。
2. **连续回复上限。** 同一会话自动回复超过 `max_consecutive_auto_replies` 次
   → 静默 `mute_minutes` 分钟。
3. **对面疑似机器人。** 对方在 `bot_burst_seconds` 内发满 `bot_burst_count` 条
   → 静默。

再加一个**机主优先**：只要机主自己在微信里发了消息（且不是引擎发的回声），
该会话立刻静默 `human_override_minutes` 分钟，引擎彻底让位。

### 14.8 回复质量与安全建议

- 给工作对象单独写 `system_prompt`，把「报价/合同/时间承诺」硬编码成固定回复。
- 开 `signature: "（自动回复）"`，让对方知道你不在。
- 首次启用务必 `dry_run: true` 跑一天。
- 引擎的每条决策都写进 `logs/auto_reply.jsonl`（含 inbound、reply、发送结果）。
- 发送串行化：UIA 同一时刻只能有一个会话在前台，引擎用全局锁保证不并发。

### 14.9 自检

```bash
# 离线自检：假 LLM + 假发送出口，不联网、不碰微信（78 项）
python scripts\auto_reply_selftest.py

# 真实配置 + 真实历史 + 真实 LLM，但只记录不发送
python scripts\auto_reply_livecheck.py --text "在干嘛呢"

# pytest 覆盖
python -m pytest -q -s tests\test_auto_reply.py
```

---

## 15. 一分钟速查

```
入口       http://127.0.0.1:8010
工具       POST /tools/call  {"name":..., "arguments":{...}}
事件       WS ws://127.0.0.1:8010/ws/events
发送       POST /control/execute  action=wechat.message.send_text

验证       python scripts\verify\verify_tools.py
状态       curl -s http://127.0.0.1:8010/status
UIA 体检   python scripts\diagnostics\dump_uia_tree.py [--fix]
计划任务   Start-ScheduledTask -TaskName wxwal-hook-daemon
Agent      python scripts\interactive_agent_watchdog.py

读历史     wechat.message.history  {conversation_id, limit, before}
列会话     wechat.conversation.list
发消息     wechat.message.send_text（务必先校验当前会话！）

自动回复   目标 好友A（config\auto_reply.json，已上线）
           状态 GET  /auto-reply/status
           热载 POST /auto-reply/reload
           解锁 POST /auto-reply/resume {conversation_id}
           试跑 python scripts\auto_reply_livecheck.py
           自检 python scripts\auto_reply_selftest.py

铁律       发消息前必须确认当前会话 == 目标
           测试只用 trigger_safe.py（硬编码只发给自己）
           管道单实例，调试前先停计划任务
           微信别关到托盘 —— 关了 UIA 全瘫，事件流仍正常
           自动回复先 dry_run=true 观察一整天
```
