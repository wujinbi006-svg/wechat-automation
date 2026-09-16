# 部署指南

面向手动部署。如果你想让 AI 帮你做，直接把 `AI_DEPLOY_PROMPT.md` 整个复制给它。

---

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 |
| Python | 3.11 或更高（3.13 实测通过） |
| 微信 | **4.x 桌面版**，已安装、已登录、保持运行 |
| 权限 | 注册 Windows 服务与计划任务需要管理员 |
| 磁盘 | 约 200 MB（解密缓存会随聊天量增长） |

> **微信 3.x 不适用**。本项目依赖微信 4.x 的 `mmui::` 控件结构与
> `Weixin.dll` 内存布局。

---

## 部署七步

### 1. 安装 Python 依赖

```powershell
cd <解压后的目录>
python -m pip install -r requirements.txt
```

如果 `uiautomation` / `pywin32` 装不上，先单独装：

```powershell
python -m pip install uiautomation pywin32 comtypes
python -m pip install -r requirements.txt
```

### 2. 环境检查

```powershell
python setup.py --check
```

它会报告：微信安装目录与版本、微信数据目录、依赖是否齐全。
**有 [FAIL] 就先解决，不要继续。**

### 3. 生成配置

```powershell
python setup.py
```

生成的都是**本机专属**文件：

```
config/agent.env              随机 IPC 口令
config/auto_reply.json        自动回复配置（从模板复制）
config/console.json           控制台 API Key（从模板复制）
service/WeChatGateway.xml     Windows 服务定义（含本机 Python 路径）
work/task-*.xml               计划任务定义
work/runtime.json             本次探测结果
```

### 4. 获取数据库密钥

**这一步无法跳过、无法复制。** 详见 `SECRETS.md`。

简版：

```powershell
# 1) 用取钥工具拿到 64 位十六进制密钥
# 2) 写入文件
Set-Content -Path work\db.key -Value "你的密钥" -NoNewline -Encoding ASCII

# 3) 验证格式
python -c "import pathlib,re; k=pathlib.Path('work/db.key').read_text().strip(); print('长度',len(k),'合法' if re.fullmatch(r'[0-9a-f]{64}',k) else '不合法')"
```

### 5. 注册后台服务

```powershell
# a) hook 事件源（接收消息用）—— 普通权限即可
powershell -ExecutionPolicy Bypass -File hook\install_daemon_task.ps1

# b) 主服务 + 其余计划任务
#    注册 Windows 服务需要管理员，请用管理员 PowerShell
python setup.py --register
```

主服务也可以不用管理员注册，直接手工启动：

```powershell
python -m uvicorn api:app --host 127.0.0.1 --port 8010
```

### 6. 启动并验证

```powershell
python health_check.py --fix
```

期望四个端口全绿：

```
[OK] 主服务 WeChatGateway        (端口 8010)
[OK] 控制台                      (端口 8011)
[OK] UIA Agent（发送消息）        (端口 18010)
[OK] WAL 事件源（接收消息）        (端口 18011)
```

然后验证数据库读面（**证明密钥对了**）：

```powershell
curl http://127.0.0.1:8010/status
```

看这三个字段：

```
sqlcipher_open_success : True
sqlcipher_read_ready   : True
read_plane             : sqlcipher_reference
```

### 7. 配置自动回复对象

打开 <http://127.0.0.1:8011>

1. 「添加对象」→ 填好友**备注名**（必须唯一命中）
2. 点「查 wxid」自动解析
3. 填「人格」（说话风格）和「记忆」（你记得的关于他的事）
4. 保存

**先保持总开关关闭，观察一天再开。**

---

## 首次启用的安全流程

```
① 总开关 关  →  dry_run true   →  观察，什么都不会发
② 总开关 开  →  dry_run true   →  只生成不发送，看日志质量
③ 总开关 开  →  dry_run false  →  真实发送，从小范围开始
```

第 ② 步怎么看质量：

```powershell
python scripts\auto_reply_livecheck.py --rule 好友备注名 --text "在干嘛呢"
```

它会打印 AI 会回的原文，**但不会真的发出**。

---

## 日常运维

### 桌面脚本（建议放桌面）

| 脚本 | 用途 |
|---|---|
| `一键启动.cmd` | 检查缺失的服务并补齐 |
| `健康检查.cmd` | 完整体检 + 询问是否修复 |

两个脚本都会自己找 Python（依次尝试 `PYTHON` 环境变量 → `py -3` → `python`）。

### 命令行

```powershell
python health_check.py            # 体检，问你要不要修
python health_check.py --fix      # 直接修
python health_check.py --no-fix   # 只看不修
python health_check.py --quiet    # 只输出 ALL_OK / MISSING:...
```

### 重启网关（改了配置后）

```powershell
.\service\WeChatGateway.exe stop
Start-Sleep 6
.\service\WeChatGateway.exe start
```

> 服务有时会回 "has already started" 但实际没起来，**再执行一次 start**。

### 改配置不用重启的情况

自动回复的 `enabled` / `targets` / 人格记忆等，热加载即可：

```powershell
curl -X POST http://127.0.0.1:8010/auto-reply/reload
```

**例外**：改 `llm_model` / `llm_base_url` / `llm_api_key` **必须重启网关**
（LLM 客户端在启动时创建）。

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 控制台打不开 | 8011 没监听 | `python health_check.py --fix` |
| 显示「网关离线」 | 8010 没监听 | 重启 `WeChatGateway` 服务 |
| 不回复消息 | 18010 或 18011 断了 | `python health_check.py --fix` |
| 决策日志出现 `SEND_EXCEPTION` | UIA Agent 挂了 | 同上 |
| 决策日志出现 `SEND_RESULT_NOT_OBSERVABLE` | 发送了但没验证到 | 通常消息已发出，检查输入框读取 |
| 读不到聊天记录 | 密钥不对 | 回 `SECRETS.md` 验证密钥 |
| `微信 UIA 主窗口不可用` | 微信被关到托盘 | 把微信窗口点出来 |
| 切换会话失败 | 目标不在侧边栏可见列表 | 在微信里把该好友**置顶** |
| 回复了一堆 `[文本]` | 模型质量差 | 换更好的模型（DeepSeek） |

---

## 卸载

```powershell
# 停服务
python -c "import subprocess; [subprocess.run(['schtasks','/end','/tn',t]) for t in ['WeChatUIAgent','WeChatAutoReplyConsole','wxwal-hook-daemon']]"

# 删任务
schtasks /delete /tn WeChatUIAgent /f
schtasks /delete /tn WeChatAutoReplyConsole /f
powershell -ExecutionPolicy Bypass -File hook\uninstall_daemon_task.ps1

# 删服务（管理员）
.\service\WeChatGateway.exe uninstall

# 删数据
Remove-Item -Recurse -Force work\decrypted_db
```

**注意**：hook 的 DLL 一旦注入微信就**无法卸载**，需要重启微信进程才会消失。

---

## 目录结构

```
wechat-automation/
├── AI_DEPLOY_PROMPT.md   交给 AI 的部署提示词
├── DEPLOY.md             本文件
├── SECRETS.md            密钥获取教程
├── SECURITY.md           安全与合规说明
├── README.md             项目总览
├── setup.py              首次部署配置生成
├── health_check.py       健康检查与自愈
├── api.py                网关入口（FastAPI）
├── interactive_agent.py  UIA 执行侧（必须在交互会话里跑）
├── requirements.txt
├── 一键启动.cmd
├── 健康检查.cmd
├── src/                  核心逻辑
│   ├── service_api.py        服务总线
│   ├── database_adapter.py   数据库解密与读取
│   ├── key_resolver.py       密钥解析
│   ├── uia_service.py        UIA 门面与前台安全
│   ├── wal_event_source.py   WAL 事件客户端
│   ├── auto_reply/           自动回复引擎
│   │   ├── engine.py
│   │   ├── llm.py
│   │   └── policy.py
│   └── ...
├── hook/                 WAL 事件源（C 源码 + Python 侧）
│   ├── src/wxwal.c           hook DLL 源码
│   ├── wal_sidecar.py        管道持有者
│   ├── daemon.py             注入守护
│   ├── run_hook_stack.py     计划任务入口
│   └── build/                编译产物
├── console/              Web 控制台
│   ├── server.py
│   └── index.html
├── scripts/              工具脚本
├── tests/                测试
├── config/               配置（*_example.json 是模板）
├── service/              WinSW 服务包装
├── tools/build_release.py    打包与脱敏校验
└── work/                 运行时数据（密钥、缓存、解密库）
```

---

## 重新编译 hook DLL（改了 `wxwal.c` 之后）

需要 [zig](https://ziglang.org/)（自带 C 编译器，无需装 MSVC）：

```powershell
$ZIG = "C:\path\to\zig.exe"
cd hook
& $ZIG cc -shared -O2 -o build\wxwal10.dll src\wxwal.c -lkernel32
```

> **改协议名必须同时改两处**：`src/wxwal.c` 里的 `WXWAL_PIPE_NAME` 与
> `wal_capture.py` 里的 `PIPE_NAME`，否则新旧 DLL 会在同一管道混流。
>
> **已注入的 DLL 无法卸载**，彻底清理需重启微信。
