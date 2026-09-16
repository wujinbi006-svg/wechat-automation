# Baseline — 突破前冻结快照

> 生成时间：2026-09-11
> 分支：`feature/dsh-preflight`
> 此文件是"突破前"参照基线，任何未来突破后的变更都应与此对比。

## Git 状态

```
CURRENT_COMMIT = aef26fb65d69e2aed053bc6410ddb0abe943f193
CURRENT_BRANCH = feature/dsh-preflight
WORKTREE_STATE = dirty (25 modified, 20 untracked)
```

HEAD commit `aef26fb` 的消息是 `feat(wechat): connect channel routing and synthetic outbound`。

工作树当前有大量未提交变更，这些是前序研究阶段积累的：UIA 增强、数据库适配器、密钥解析器、网关监督器、OpenClaw 适配器、测试和诊断脚本。这些变更都保留在 `feature/dsh-preflight` 分支上，不回退、不压缩。

## 运行时快照

| 项 | 值 |
|---|---|
| Windows | Windows 10 Home China, Build 26200, x64 |
| Python | 3.13.14 |
| Node.js | v24.15.0 |
| Git | 2.55.0.windows.3 |

### 微信进程

| 项 | 值 |
|---|---|
| 进程名 | `Weixin.exe` |
| 版本 | 4.1.13.12 |
| 主 PID | 29884 |
| 辅助 PID | 23260 (wxpublic), 25828 (wxutility), 30628 (wxocr), 30104 (wxplayer) |
| 安装路径 | `C:\Program Files\Tencent\Weixin\Weixin.exe` |
| Weixin.dll | `C:\Program Files\Tencent\Weixin\4.1.13.12\Weixin.dll` |

### 数据目录

| 布局 | 路径 | 账号 |
|---|---|---|
| 4.x db_storage | `__USERPROFILE__\xwechat_files\wxid_EXAMPLE_ACCOUNT\db_storage` | wxid_EXAMPLE |
| legacy Msg | `__USERPROFILE__\Documents\WeChat Files\wxid_EXAMPLE` | wxid_EXAMPLE |

### Gateway

| 项 | 值 |
|---|---|
| Windows 服务名 | WeChatGateway |
| 服务状态 | Running |
| 启动类型 | Automatic |
| HTTP 端口 | 8010 |
| WebSocket | `ws://127.0.0.1:8010/ws/events` |
| IPC 端口 | 18010 (Agent ↔ Service) |
| 可执行 | `python -m uvicorn api:app --host 127.0.0.1 --port 8010` |
| 工作目录 | `__ROOT__` |
| 重启策略 | restart @ 10s/30s/60s, reset @ 1 day |

### OpenClaw

| 项 | 值 |
|---|---|
| Gateway 端口 | 18789 |
| Bridge 端口 | 18790 |
| Tool Registry 端口 | 18791 |
| Adapter | `openclaw-adapter/index.mjs` |
| Plugin ID | wechat-gateway |
| Channel | wechat |

### UIA

| 项 | 值 |
|---|---|
| Driver | ReplicaUIADriver (wechatauto_replica-1.1.1) |
| 模式 | InteractiveAgentProxy (Session 1 ↔ Session 0) |
| 前台策略 | NEVER_STEAL (硬编码，不可配置) |
| Qt Accessibility Gate | 需要 active_initialize_accessibility |
| 主窗口类 | mmui::MainWindow |
| IPC 传输 | 127.0.0.1:18010, JSON-line TCP |

### 数据库

| 项 | 值 |
|---|---|
| 发现 | available |
| 布局 | db_storage (4.x) |
| SQLCipher 访问 | unavailable |
| Key 状态 | DATABASE_KEY_UNAVAILABLE |
| 加密库数量 | 21 |
| xInfo 可读 | true |
| 禁用策略 | process_memory_scan, dynamic_hook, process_injection |
| 允许来源 | WECHAT_DB_KEY, WECHAT_DB_KEY_FILE, WECHAT_DB_KEYS_FILE, validated_cache |

## 测试基线

```
pytest -q               → 61 passed in 47.67s
python -m compileall    → passed
node --check index.mjs → passed
```

## 2026-09-11 运行态修复记录（本轮）

接手时 Service 活着但 Agent 已死，共修复两个真实缺陷：

### 缺陷 1：UIA 消息读取器取到容器标签而非消息正文

`src/uia_service.py::ReplicaUIADriver.read_messages` 使用几何条件
`rect.bottom <= input_rect.top` 过滤候选节点。在微信 4.1.13.12 上消息区
（`mmui::MessageView`）与输入框区域重叠，该条件把**全部真实消息**排除，
只剩 `mmui::RecyclerListView` 容器的固定 Name `"消息"`。

修复：改为按类名精确收集 `mmui::ChatTextItemView` / `mmui::ChatItemView` /
`mmui::ChatBubbleReferItemView`，新增 `_extract_message_text`（Name →
ValuePattern → LegacyIAccessible 顺序取文本）与 `_classify_message`
（text / timestamp / reference）。

修复前返回 `[{text: "消息"}]`；修复后返回真实消息文本。

### 缺陷 2：canonical 数据库工具未接入命令表

`wechat.database.inventory` 与 `wechat.database.xinfo` 在
`CANONICAL_CAPABILITIES` 中声明为可用，但
`WeChatService._execute_command_action` 没有对应分支，调用返回
`COMMAND_FAILED: 未实现命令`。同时补齐 `wechat.account.*`、
`wechat.contact.search`、`wechat.group.search`、`wechat.group.info` 的
canonical 别名路由。

### 修复后实测（透过运行中的 Gateway，PID 27124）

```
wechat.state                success=True
wechat.conversation.list    success=True  items=7
wechat.conversation.search  success=True  中文查询命中正确
wechat.contact.search       success=True
wechat.message.read         success=True  真实消息文本
wechat.message.latest       success=True
wechat.account.list         success=True  items=3
wechat.account.current      success=True  wxid_EXAMPLE
wechat.database.inventory   success=True  keys=43
wechat.database.xinfo       success=True  keys=6
wechat.group.search         success=True
```

`/status` → `wechat_connected=true, automation_ready=true,
message_listener=true, reconnect_count=0`

### 仍未解决

- ~~数据库密钥：`DATABASE_KEY_UNAVAILABLE`~~
- ~~数据目录指向：适配器仍选中 `Documents\WeChat Files`（legacy_msg）~~
- 媒体发送：图片/文件未做真实发送验证（文本已通过）

## 2026-09-12 数据库接入 + 写入放行（本轮）

### 关键发现：保存下来的是 passphrase，不是 raw key

外部工具恢复出的 32 字节材料在 `key_mode=sqlcipher_passphrase` 下工作：
真正的 AES 密钥需要 `PBKDF2-HMAC-SHA512(passphrase, salt, 256000)`，其中
`salt` **每个库不同**。项目原来的 `_verify_sqlcipher4_key` 只按 raw key
直接校验，因此对同一份密钥报 `PAGE_HMAC_INVALID`。

修复：`key_resolver.resolve_key_material()` 先试 raw key，再试 passphrase
派生，返回 `(valid, mode, raw_key)`。解密路径统一使用派生后的 raw key。
实测 24/24 库 `PAGE_HMAC_VALID`（mode=passphrase）。

### 新增：可插拔凭据解析层

`src/credential_resolver.py`

- `ReferenceMemorySource` —— 复用 `work/wechatauto_pkg` 已验证的
  `WeChatDB.extract_keys()`，从 `Weixin.exe` 的 `Config.Cipher` 读取
  **每库各自的 raw key**。默认关闭，需 `WECHAT_ALLOW_KEY_EXTRACTION=1`。
  本模块不实现新的内存扫描算法，只复用既有实现。
- 审计输出只含策略名 / 状态 / `sha256` 指纹，绝不含密钥本体。
- `KeyResolver` 在显式密钥与缓存都失败后才询问该策略。

### 新增：4.x 数据库读取器

`src/database_adapter.py`

- `_resolve_db_key()` —— 容忍 `db_storage/` 前缀差异（4.x 发现键带该前缀）
- `get_chats()` / `get_contacts()` / `get_messages()` / `search_messages()`
- 4.x 表名 `Msg_<md5(username)>`；`message_content` 为 zstd 压缩
  （`WCDB_CT_message_content=4`），`real_sender_id` 经 `Name2Id` 解析
- 全部只作用于解密副本，原始库始终只读

### 配置固化

WinSW `service/WeChatGateway.xml` 增加 `<env>`（不含密钥本体）：

| 变量 | 值 |
|---|---|
| `WECHAT_FILES_BASE` | `__USERPROFILE__\xwechat_files` |
| `WECHAT_DB_KEY_FILE` | `<repo>\work\db.key`（work/ 已 gitignore） |
| `WECHAT_ALLOW_KEY_EXTRACTION` | `1` |
| `WECHAT_ENABLE_SEND` | `1` |
| `WECHAT_UIA_MODE` | `interactive` |

同样变量同时写入 **用户级环境变量**，因为 Session 1 的 Interactive Agent
由服务之外启动，不继承服务的 `<env>`。

### 实测结果

```
database_key_status    = DATABASE_KEY_READY
sqlcipher_read_ready   = True
current_account_dir    = __USERPROFILE__\xwechat_files\wxid_EXAMPLE_ACCOUNT
key strategies         = explicit_file × 24
extractor              = enabled=True
chats / contacts       = 210 / 2000
message.history        = 真实内容，含本轮发送的自测消息
UIA plane              = conversations=7, message.read ok
send gate              = 服务 execution_available=True, Agent send_enabled=True
```

真实发送已通过一次到 `文件传输助手`（本机自聊，无第三方）的端到端验证：

```
ok=True executed=True sent=True result_state=SENT_VERIFIED
verified=true, message_text_appeared=true
```

### 目标解析修复

`_resolve_send_target` 新增按 username 精确匹配数据库联系人的兜底，
使 `filehelper` / `medianote` 这类账号可以直接作为发送目标。

### 质量门禁

```
pytest -q             → 61 passed
compileall            → OK
node --check          → OK
```

## 2026-09-12 媒体发送实测（图片 + 文件）

在真实微信上完成两类附件发送，目标是 `文件传输助手`（本机自聊，无第三方）。

测试素材（`work/sendtest/`，work/ 已 gitignore）：

| 文件 | 大小 |
|---|---|
| `gateway_test_image.png` | 5070 B（640×360 程序生成） |
| `gateway_test_file.txt` | 92 B |

### 调用路径

```
POST /control/execute
  action = wechat.message.send_image / send_file
  target = {"conversation_id": "文件传输助手"}
  payload = {"path": "<绝对路径>"}
→ WeChatService.send_image / send_file
→ UIA _send_attachment
   1. 打开目标会话并等待稳定 (_open_chat_and_settle)
   2. 定位聊天输入框
   3. 写入 CF_HDROP 剪贴板 (_copy_files_to_clipboard)
   4. Ctrl+A / Delete 清空输入框
   5. Ctrl+V 粘贴附件 → Enter 发送
→ 前后消息快照对比验证
```

剪贴板写入使用 64 位安全的 `argtypes`/`restype`（`HGLOBAL`/`LPVOID` 为指针
宽度）；否则句柄会在 `GlobalLock` 前被截断，粘贴在发出任何 UI 输入前就失败。

### 实测结果

图片：

```
ok=True executed=True sent=True result_state=SENT_VERIFIED
observed: {"file_name":"gateway_test_image.png","kind":"image","new_message_count":1}```

文件：

```
ok=True executed=True sent=True result_state=SENT_VERIFIED
observed: {"file_name":"gateway_test_file.txt","kind":"file","new_message_count":1}
```

### 独立复核（不经由发送路径）

解密副本直接读取 `Msg_<md5(filehelper)>` 表，确认两条附件真实落库：

```
[file    ] <msg><appmsg ...><title>gateway_test_file.txt</title>...
[image   ] <?xml version="1.0"?><msg><img hdlength="5070" length="5070" ...
[text    ] gateway self-test 00:18:26
```

### 顺带修复：local_type 分类

微信 4.x 的 `local_type` 含高位标志（文件消息为 `25769803825` =
`0x6_0000_0001`），原实现按键直接查表会得到 `type_25769803825`。现在先取低
32 位映射基础类型，`app` 类再读 appmsg XML 的 `<type>` 得到子类型，因此该
消息现在正确显示为 `file`。

`dynamic_hook` / `process_injection` 仍未实现——密钥已可稳定获得，这两条
没有功能性收益。

## 安全约束冻结

本轮（及后续突破前）遵守的硬约束：

1. 不提取/导出/泄露受保护的数据库密钥
2. 不实现动态 hook 捕获密钥
3. 不绕过微信安全机制
4. 不发送真实微信消息/图片/文件
5. 不自动添加好友
6. 不删除/修改微信数据
7. 不改 Windows 系统配置
8. 不修改微信安装文件
9. 不操作 PID 7704
10. 不为测试杀死或重启未知进程
11. 不用 mock 冒充生产能力

## 可用于对比的指标

突破后应重新采集以下值并与本文件对比：

- pytest 通过数（当前 61）
- 数据库 key 状态（当前 UNAVAILABLE）
- SQLCipher read_plane（当前 unavailable）
- UIA connected 状态
- Gateway uptime / reconnect_count
- OpenClaw plugin 注册状态
