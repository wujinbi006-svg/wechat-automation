# Current Status — 能力分层与当前状态

## Capability Model

不再使用 "BLOCKED / PASS" 二元模型。每个能力按以下六层评估：

| 层级 | 含义 |
|---|---|
| IMPLEMENTED | 代码存在 |
| WIRED | 已接入服务总线/路由 |
| RUNTIME_READY | 运行时可调用（不崩） |
| REAL_E2E | 真实环境端到端验证通过 |
| RECOVERABLE | 有定义的故障检测与恢复路径 |
| PRODUCTION_READY | 可长期稳定运行，有证据 |

判定规则：**不再通过修改 acceptance matrix 让 BLOCKED 变 PASS。状态只来自 observable runtime evidence。**

## 能力清单

### Control Plane

| 能力 | IMPLEMENTED | WIRED | RUNTIME_READY | REAL_E2E | RECOVERABLE | PRODUCTION_READY | 证据 |
|---|---|---|---|---|---|---|---|
| wechat.state | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 61 tests pass; /status 200 |
| wechat.conversation.list | ✅ | ✅ | ✅ | partial | ✅ | partial | UIA connected 时可用; Agent 依赖 |
| wechat.conversation.search | ✅ | ✅ | ✅ | partial | ✅ | partial | 可见会话列表搜索 |
| wechat.conversation.open | ✅ | ✅ | ✅ | ✅ | ✅ | partial | OPEN_RESULT_VERIFIED |
| wechat.contact.search | ✅ | ✅ | ✅ | partial | ✅ | partial | VISIBLE_SESSION_SEARCH_ONLY |
| wechat.account.list | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ACCOUNT_DISCOVERY_READY |
| wechat.account.current | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ACCOUNT_DISCOVERY_READY |

### Data Plane

| 能力 | IMPLEMENTED | WIRED | RUNTIME_READY | REAL_E2E | RECOVERABLE | PRODUCTION_READY | 证据 |
|---|---|---|---|---|---|---|---|
| wechat.database.inventory | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | DATABASE_DISCOVERY_READY |
| wechat.database.xinfo | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | XINFO_READ_READY |
| wechat.message.history | partial | ✅ | ✅ | ❌ | ✅ | ❌ | DATABASE_KEY_UNAVAILABLE |
| wechat.message.read (UIA) | ✅ | ✅ | ✅ | partial | ✅ | partial | UIA 读取可见消息 |
| wechat.message.latest | ✅ | ✅ | ✅ | partial | ✅ | partial | 同上 |

### Database Key

| 层级 | 状态 | 说明 |
|---|---|---|
| IMPLEMENTED | ✅ | KeyResolver + KeyDiscoveryCoordinator + ReferenceMemorySource |
| WIRED | ✅ | DatabaseDataProvider → DatabaseAdapter → KeyResolver → CredentialResolver |
| RUNTIME_READY | ✅ | `DATABASE_KEY_READY`；24/24 库 `PAGE_HMAC_VALID` |
| REAL_E2E | ✅ | 真实消息历史读取（含 zstd 解压 + Name2Id 解析） |
| RECOVERABLE | ✅ | 显式密钥 / 缓存 / 参考实现三重来源 + 后台重试 |
| PRODUCTION_READY | ✅ | 配置固化于 WinSW `<env>` + 用户环境变量 |

密钥材料支持两种模式：`raw_key` 与 `passphrase`（PBKDF2-HMAC-SHA512/256000，
每库 salt 不同）。解密路径统一使用派生后的 raw key。

内存提取策略（`ReferenceMemorySource`）默认关闭，由
`WECHAT_ALLOW_KEY_EXTRACTION=1` 显式启用；它复用 `work/wechatauto_pkg`
既有实现，不新增扫描算法。`dynamic_hook` / `process_injection` 仍未实现。

### Media Plane

| 能力 | IMPLEMENTED | WIRED | RUNTIME_READY | REAL_E2E | RECOVERABLE | PRODUCTION_READY | 证据 |
|---|---|---|---|---|---|---|---|
| wechat.message.send_text | ✅ | ✅ | ✅ | ✅ | ❌ | partial | SENT_VERIFIED（本机自聊实测） |
| wechat.message.send_image | ✅ | ✅ | ✅ | ✅ | ❌ | partial | SENT_VERIFIED + 落库复核 |
| wechat.message.send_file | ✅ | ✅ | ✅ | ✅ | ❌ | partial | SENT_VERIFIED + 落库复核 |
| type_message | ✅ | ✅ | gated | ❌ | ❌ | ❌ | 前台必需 |

### Event Plane

| 能力 | IMPLEMENTED | WIRED | RUNTIME_READY | REAL_E2E | RECOVERABLE | PRODUCTION_READY | 证据 |
|---|---|---|---|---|---|---|---|
| WS event broadcast | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | test_synthetic_event_is_broadcast |
| Message dedupe | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | fingerprint + message_id |
| UIA message poll | ✅ | ✅ | ✅ | partial | ✅ | partial | 依赖 UIA connected |
| Synthetic event inject | ✅ | ✅ | ✅ | ✅ | n/a | n/a | test mode only |

### Lifecycle Plane

| 能力 | IMPLEMENTED | WIRED | RUNTIME_READY | REAL_E2E | RECOVERABLE | PRODUCTION_READY | 证据 |
|---|---|---|---|---|---|---|---|
| WinSW service | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Running, Automatic |
| Interactive agent | ✅ | ✅ | ✅ | partial | ✅ | partial | health loop + watchdog |
| Foreground safety | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | NEVER_STEAL; snapshot diff |
| UIA bootstrap retry | ✅ | ✅ | ✅ | ✅ | ✅ | partial | 8-stage retry |
| UIA reconnect | ✅ | ✅ | ✅ | ✅ | ✅ | partial | exponential backoff |

### Integration Plane

| 能力 | IMPLEMENTED | WIRED | RUNTIME_READY | REAL_E2E | RECOVERABLE | PRODUCTION_READY | 证据 |
|---|---|---|---|---|---|---|---|
| OpenClaw tool registration | ✅ | ✅ | ✅ | partial | ✅ | partial | 17 tools; node --check pass |
| WS reconnect | ✅ | ✅ | ✅ | partial | ✅ | partial | exponential backoff |
| Event routing | ✅ | ✅ | ✅ | partial | ✅ | partial | resolveRoute → dispatch |

## 总结

```
UIA_READY         = ✅ (Agent READY, session 1, conversations=7)
DATABASE_READY    = ✅ (DATABASE_KEY_READY, sqlcipher_read_ready=true)
EVENT_READY       = ✅ (listener=true, UIA connected)
OPENCLAW_READY    = ✅ (Gateway 8010 + 三端口 UP)
MEDIA_READY       = ✅ (文本/图片/文件三类均已 SENT_VERIFIED 实测)
```
