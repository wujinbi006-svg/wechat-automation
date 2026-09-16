# 微信自动化工程

在 Windows 上对微信 4.x 做**本地、可控、可审计**的自动化：
读聊天记录、发消息、按人设自动回复，全部通过本地网页控制台管理。

> ⚠️ **仅供本人账号、本人设备上的个人自动化使用。**
> 不要用于群发、营销、骚扰，不要用于任何对方不知情的场景。
> 详细边界见 `SECURITY.md`。

---

## 它解决什么问题

传统微信自动化方案分两类，各有硬伤：

| 方案 | 问题 |
|---|---|
| 纯 UIA 界面自动化 | 读不到历史消息；界面一改就废；频繁抢前台 |
| 纯数据库读取 | 只能读不能发 |
| 直接调协议 API | 微信 4.x 没有公开协议，改版即失效 |

**本工程的思路是分层**：

```
读取  走数据库直读（SQLCipher 解密）—— 权威、完整、不受界面影响
发送  走 UIA 真实点击 —— 微信只认这个，但做了后台化处理
事件  走 WAL hook 注入 —— 微信写库即捕获，约 1 秒延迟
```

三者各司其职，互不依赖对方的脆弱点。

---

## 能力

| 板块 | 内容 |
|---|---|
| **消息读取** | 全量历史（数据库）、实时新消息（WAL hook）、会话列表、联系人 |
| **消息发送** | 文本 / 图片 / 文件；后台写入，不抢鼠标 |
| **AI 自动回复** | 白名单、每人独立人格与长期记忆、限流、静默时段、防死循环 |
| **归档导出** | 文本、语音（WAV）、图片（.dat 解密）、媒体 |
| **Web 控制台** | 开关、增删改对象、编辑人格记忆、实时统计、决策日志 |
| **运维自愈** | 开机自启、崩溃自动重启、健康检查、一键修复 |
| **安全护栏** | 前台保护、发送结果验证、占位符过滤、本地 IPC 鉴权 |

完整功能清单见部署后的 `docs/`，或控制台页面。

---

## 快速开始

```powershell
python -m pip install -r requirements.txt
python setup.py --check      # 环境检查
python setup.py              # 生成配置
# 取数据库密钥 → 写入 work/db.key（见 SECRETS.md）
python setup.py --register   # 注册后台服务
python health_check.py --fix # 启动并验证
```

然后打开 <http://127.0.0.1:8011> 配置自动回复对象。

**想省事**：把 `AI_DEPLOY_PROMPT.md` 整个复制给你的 AI 助手，让它帮你做。

---

## 架构

```
                    ┌─────────── Session 1（你的交互会话）───────────┐
                    │                                                │
                    │  Weixin.exe                                    │
                    │    └─ wxwal10.dll ← IAT hook kernel32!WriteFile│
                    │            │  只复制 *.db-wal 写入，不改内容    │
                    │            ▼                                   │
                    │     \\\\.\\pipe\\wxwal10                        │
                    │            │                                   │
                    │  run_hook_stack.py（计划任务 wxwal-hook-daemon）│
                    │    ├─ daemon.py      注入 hook + 微信重启后重注 │
                    │    └─ wal_sidecar.py 独占管道 + 转发 :18011    │
                    │                                                │
                    │  interactive_agent.py  UIA 操作（:18010）       │
                    └────────────────────┬───────────────────────────┘
                                         │ tcp
                    ┌────────────────────▼───────────────────────────┐
                    │  WeChatGateway（WinSW 服务，:8010）             │
                    │    ├─ WalEventSource   轮询 :18011             │
                    │    ├─ GatewaySupervisor 事件去重 + 广播         │
                    │    ├─ AutoReplyEngine  白名单→LLM→发送→验证     │
                    │    └─ DatabaseDataProvider  数据库读面          │
                    └────────────────────┬───────────────────────────┘
                                         │ HTTP / WS
                    ┌────────────────────▼───────────────────────────┐
                    │  Web 控制台（:8011） · 任意 HTTP 客户端         │
                    └────────────────────────────────────────────────┘
```

### 为什么必须跨会话

Session 0 创建的命名管道，Session 1 连不上（Win32 error 5，已实测）。

所以：**hook 管道必须由 Session 1 的 sidecar 独占**，Gateway 只能通过
TCP :18011 找它。这是本工程的架构约束，不是可以简化的地方。

### 为什么 hook 不会随微信更新失效

| 环节 | 依赖 | 稳定性 |
|---|---|---|
| hook 点 | `kernel32!WriteFile` | Windows API，永不变 |
| 注入目标 | `Weixin.dll` 的 IAT | 该 DLL 只导入 kernel32 |
| 数据载体 | SQLite WAL 格式 | SQLite 标准 |
| 解密 | SQLCipher + 每库 salt | 密钥已持有 |
| 页解析 | SQLite B-tree | SQLite 标准 |

**零微信硬编码常量。** 对比之下，多数同类项目靠写死偏移，微信一更新即失效。

---

## 主要模块

| 文件 | 职责 |
|---|---|
| `api.py` | 网关入口，FastAPI 应用，35 个工具 |
| `interactive_agent.py` | UIA 执行侧，必须在交互会话运行 |
| `src/service_api.py` | 服务总线：工具分发、发送编排与验证 |
| `src/database_adapter.py` | 数据库发现、分页 AES 解密、4.x 读取 |
| `src/key_resolver.py` | 密钥来源解析与校验 |
| `src/uia_service.py` | UIA 门面 + 前台安全护栏 |
| `src/auto_reply/engine.py` | 自动回复状态机 |
| `src/auto_reply/llm.py` | OpenAI 兼容客户端 + 输出净化 |
| `src/auto_reply/policy.py` | 策略与对象规则 |
| `hook/src/wxwal.c` | WAL 写入 hook（C 源码） |
| `console/server.py` | 控制台后端 |

---

## 依赖

- Python 3.11+，依赖见 `requirements.txt`
- 微信 **4.x** 桌面版（3.x 不适用）
- 编译 hook 需要 [zig](https://ziglang.org/)（可选，已附带编译好的 DLL）

---

## 文档

| 文件 | 内容 |
|---|---|
| `AI_DEPLOY_PROMPT.md` | **交给 AI 的部署提示词** |
| `DEPLOY.md` | 手动部署指南 + 排障 |
| `SECRETS.md` | **数据库密钥 / API Key 获取教程** |
| `SECURITY.md` | 安全边界与合规声明 |
| `hook/README.md` | hook 机制细节 |

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 群消息 | 不支持自动回复（UIA 无稳定群成员节点，且群内误发影响面大） |
| 朋友圈 | 未实现 |
| CDN-only 图片 | 本地无缓存时拿不到原图 |
| 切换会话会短暂抢前台 | 微信 Qt 只认真实鼠标点击，已做到点完立刻还原 |
| 微信必须开在桌面 | 关到托盘则 UIA 全瘫（事件流仍正常） |
| 侧边栏只渲染可见会话 | 排位靠后的联系人需置顶才能自动切换 |
| 发送吞吐 | 串行执行，每次约 2 秒；不适合秒杀级并发 |

---

## 许可

代码部分见 `LICENSE`。

**第三方组件**：
- `work/wechatauto_pkg/` 下的 UIA 驱动来自社区项目，按其原始许可使用
- `service/WinSW.exe` 为 [WinSW](https://github.com/winsw/winsw)（MIT）
- 使用前请自行确认微信服务条款与所在地区法律法规

---

## 免责声明

本工程用于**本人账号、本人设备**上的个人自动化与学习研究。
使用者应自行确保用途合法合规，并自行承担使用风险。
作者不对任何滥用行为或由此产生的后果负责。
