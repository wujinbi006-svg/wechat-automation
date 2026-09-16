# wxwal — WeChat 4.x 实时消息 hook（版本无关）

在微信 4.1.13.12 上从零实现的实时数据库写入捕获，**不使用任何微信偏移或特征码**，
因此不随微信版本更新而失效。

## 为什么这个方案不会因微信更新而失效

| 环节 | 依赖 | 为什么稳定 |
|---|---|---|
| hook 点 | `kernel32!WriteFile` | Windows API，永不变 |
| 注入目标 | `Weixin.dll` 的 IAT | 该 DLL 只导入 kernel32，`WriteFile` 在 IAT 里 |
| 数据载体 | SQLite WAL 格式 | SQLite 标准，公开且固定 |
| 解密 | SQLCipher 4 + 每库 salt | 算法固定；密钥我们已持有 |
| 页解析 | SQLite B-tree 页格式 | SQLite 标准 |

对比：`pywxrobot4`（锁 4.1.1.19）、`aixed/WeChat-Hook`（锁 4.1.10.27）、
`WeChatFerry`（锁 3.9.12.17）都靠**写死偏移**，微信一更新即失效。

## 架构

```
Weixin.exe 进程内
┌──────────────────────────────────────────────┐
│ wxwal.dll (zig cc 编译, 190KB)               │
│   IAT hook kernel32!WriteFile                │
│     ├─ GetFinalPathNameByHandleW 判路径       │
│     ├─ 以 .db-wal 结尾 → 复制 buffer          │
│     └─ 其余 → 原样放行                        │
│   命名管道 \\.\pipe\wxwal10 (UTF-8 路径随帧)  │
└──────────────────┬───────────────────────────┘
                   │  [24B header][path][payload]
                   ▼
┌──────────────────────────────────────────────┐
│ Python sidecar                               │
│   wal_capture.py    管道服务器 + 帧重组       │
│   decrypt2.py       每库派生 raw key + 解密   │
│   sqlite_record.py  解析 SQLite 记录格式      │
│   → 消息行 / 消息文本                         │
└──────────────────────────────────────────────┘
```

## 实测结果

```
records   = 42
frames    = 21        ← 21 个真实 WAL 帧
malformed = 0
```

按库分布：

```
9  message_fts.db-wal
8  message_0.db-wal
4  session.db-wal
```

解密后解出的真实数据：

```
message_0.db page=2656 rowid=228 cols=17
   server_id=9217269085920183677  local_type=25769803825
   sort_seq=1789144164000         create_time=1789144164

message_fts.db page=1047
   "hook" "probe"     ← 测试消息的真实文本
   "wal" "capture" "test"
```

## 文件

| 文件 | 作用 |
|---|---|
| `src/wxwal.c` | hook DLL 源码 |
| `build/wxwal10.dll` | 编译产物（当前版本） |
| `inject.py` | 注入器（CreateRemoteThread + LoadLibraryW） |
| `wal_capture.py` | 命名管道服务器 + WAL 帧重组 |
| `decrypt2.py` | 每库密钥派生 + 页解密 + 记录解码 |
| `sqlite_record.py` | SQLite 记录格式解析 |
| `trigger_safe.py` | 安全的测试触发器（**只对文件传输助手**） |
| `capture_paths.py` | 带路径的实时捕获 |

## 使用

### 开机自启（已注册）

```powershell
# 注册（已执行过，重复运行会替换旧任务）
powershell -ExecutionPolicy Bypass -File hook\install_daemon_task.ps1

# 查看 / 手动启动
Get-ScheduledTask -TaskName wxwal-hook-daemon | Select TaskName,State
Start-ScheduledTask -TaskName wxwal-hook-daemon

# 卸载
powershell -ExecutionPolicy Bypass -File hook\uninstall_daemon_task.ps1
```

任务配置：

| 项 | 值 | 原因 |
|---|---|---|
| 触发器 | `AtLogOn` (当前用户) | 微信在用户会话里，hook 必须同会话注入 |
| LogonType | `Interactive` | 服务或批处理会话看不到微信 |
| RunLevel | `Limited` | 不需要管理员 |
| MultipleInstances | `IgnoreNew` | 防止两个守护进程抢注同一个 DLL |
| ExecutionTimeLimit | 无限 | 常驻 |
| RestartCount | 999 / 每 1 分钟 | 守护进程挂了自动拉起 |

### 手动运行（不用自启时）

```powershell
# 守护进程：等微信 → 注入 → 微信重启后自动重注
python hook\daemon.py 5

# 或者只注入一次
python hook\inject.py hook\build\wxwal10.dll

# 抓帧并解密
python hook\capture_paths.py
python hook\decrypt2.py
```

### 守护进程怎么判断"已经注入过"

**枚举目标进程的模块列表**（Toolhelp32），不是靠日志、文件标记或记录的 PID。

原因：`LoadLibraryW` 对已加载的模块是空操作，所以同一进程内盲目重注无害 ——
但**重启后的微信是全新进程，完全没有 hook**。只有读目标自己的模块列表才能区分
这两种情况，而这正是守护进程存在的意义。

判定后还会再查一次模块列表确认注入成功，不信任调用返回值。

## 调试过程中踩过的坑（都修了）

每个都是"看起来在跑但什么都没抓到"的类型：

1. **管道只在加载时连一次** — sidecar 后启动就永远丢帧。改为每帧按需重连。
2. **`lpOverlapped == NULL` 过滤** — 微信的 WAL 写入是**异步 I/O**，
   这个条件把全部 WAL 写入过滤掉了。已移除。
3. **`GetFinalPathNameByHandleW` 返回值含 null 终止符** — 后缀比较错位一字节，
   导致每个路径判定失败。改用 `wcslen`。
4. **要求流首有 32 字节 WAL 头** — 该头只在 WAL 创建时写一次，
   新增帧只有 24B 帧头 + 4096B 页。改为直接按帧结构解析。
5. **管道字节流未跨读缓冲** — 一条记录被 read 边界切断，产生 7 万字节错位。
   改为累积流 + 按 header 长度切分。
6. **`ok` 标志死锁** — 写路径成功后没置 `ok=TRUE`，payload 永远不写，
   每次单写都成功但整帧被丢。
7. **多个 DLL 混用同一管道/日志** — 注入过的 DLL 无法卸载，新旧协议混流。
   每个协议版本用独立管道名。

## 安全边界

- 原始 `WriteFile` 始终被调用，**从不修改微信写入的内容**
- 管道写是非阻塞、尽力而为：无人监听就丢弃并立即返回，微信不会被拖住
- 只读取 `*.db-wal`，其余文件原样放行
- 注入用同用户权限，不需要管理员

## 测试纪律

`trigger_safe.py` 硬性约束：

- **唯一允许的目标是 `文件传输助手`**（用户自己的设备，无第三方可见）
- 因为微信的发送接口作用于**当前打开的会话**而非指定会话，
  脚本在每次发送前重新打开并**校验当前会话名**，不符即中止
- 不打开任何真实联系人或群

## 已知限制

- `WriteFileEx`（异步完成回调）当前只放行，不捕获；
  实测微信 WAL 走 `WriteFile`，未受影响
- 帧里不含数据库路径的旧协议版本仍在内存中（历史注入），
  但它们写的是旧管道，不干扰当前链路
- **已注入的 DLL 无法卸载**，只有重启微信才能清掉。
  因此每次改协议都必须换新的管道名（见调试坑 #7）
- 守护进程重注依赖微信重启；微信不重启而 hook 失效的情况不会自愈

