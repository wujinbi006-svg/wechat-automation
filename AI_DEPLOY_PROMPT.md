# 交给 AI 的部署提示词

> **用法**：把本文件整个内容复制，连同这个压缩包一起交给你的 AI 助手
> （Claude / ChatGPT / DeepSeek / Cursor 等），它会按步骤帮你在这台机器上部署。
>
> 你也可以自己照着 `DEPLOY.md` 手动做一遍——两条路的步骤是一致的。

---

## 复制以下内容给你的 AI

```
你是一名 Windows 部署工程师。我拿到了一个「微信自动化工程」的源码包，需要你
帮我在**这台机器上**完整部署并验证它。

## 极其重要的三条前提

1. **不要照抄任何配置值。** 这个包里所有跟机器相关的值都已被替换成占位符或
   由脚本现场探测。密钥（数据库密钥、API Key）**必须在本机重新获取**，绝不
   能从别处复制——每台机器、每个微信账号的数据库密钥都不同。

2. **每一步都要先验证再继续。** 不要假设命令成功了。跑完一条命令，检查输出
   和退出码，确认符合预期再进入下一步。任何一步失败就停下来告诉我原因，
   不要跳过、不要"大致能跑就行"。

3. **不要修改我给你的安全默认值。** 特别是：
   - 自动回复默认 `enabled=false` / `dry_run=true`，在你我明确确认前不要开启
   - `WECHAT_ENABLE_SEND` 打开意味着任何本机进程都能真实发消息，确认过再开
   - IPC 口令由 setup.py 随机生成，不要改回默认值

## 环境要求

- Windows 10/11
- Python 3.11 或更高
- 微信 4.x 桌面版（已安装并**已登录、保持运行**）
- 管理员权限（注册 Windows 服务与计划任务需要）

## 部署步骤

### 第 0 步：确认环境

请先运行并告诉我结果：

    python --version
    tasklist /fi "imagename eq Weixin.exe"

确认：Python 版本 ≥3.11；微信进程存在。

### 第 1 步：安装依赖

    cd <解压后的目录>
    python -m pip install -r requirements.txt

如果 `uiautomation` 或 `pywin32` 安装失败，先单独装它们再重试。

### 第 2 步：运行环境检查

    python setup.py --check

这一步会探测：微信安装目录与版本、微信数据目录、Python 依赖是否齐全。
**把完整输出发给我看**，确认没有 [FAIL] 项。

### 第 3 步：生成配置

    python setup.py

它会生成（都是本机专属、不可复制到别的机器）：
- `config/agent.env`      —— 随机生成的 IPC 口令
- `service/WeChatGateway.xml` —— 含本机 Python 路径的 Windows 服务配置
- `work/task-*.xml`       —— 计划任务定义
- `config/auto_reply.json`、`config/console.json` —— 从模板复制
- `work/runtime.json`     —— 本次探测结果

### 第 4 步：获取微信数据库密钥（最关键）

**这是唯一必须在本机重新做的一步，密钥无法从任何地方复制。**

请阅读本包内的 `SECRETS.md`，按其中的「数据库密钥」章节操作。
拿到密钥后写入 `work/db.key`：

    # 文件内容就是一行 64 位小写十六进制，不要有空格、换行、BOM
    <64位十六进制>

写完后请验证：

    python -c "import pathlib,re; k=pathlib.Path('work/db.key').read_text().strip(); print('长度', len(k), '合法' if re.fullmatch(r'[0-9a-f]{64}', k) else '不合法')"

### 第 5 步：注册后台服务

    # hook 事件源（接收消息用）
    powershell -ExecutionPolicy Bypass -File hook\install_daemon_task.ps1

    # 其余计划任务（需要管理员权限的 PowerShell）
    python setup.py --register

### 第 6 步：启动并验证

    python health_check.py --fix

期望结果：四个端口全部 [OK]

    8010  主服务 WeChatGateway
    8011  控制台
    18010 UIA Agent（发送消息）
    18011 WAL 事件源（接收消息）

然后验证网关能读到数据：

    curl http://127.0.0.1:8010/health
    curl http://127.0.0.1:8010/status

再验证数据库读面（这一步能证明密钥是对的）：

    curl -X POST http://127.0.0.1:8010/tools/call ^
      -H "Content-Type: application/json" ^
      -d "{\"name\":\"wechat.message.history\",\"arguments\":{\"conversation_id\":\"文件传输助手\",\"limit\":3}}"

期望：`"success": true` 且返回若干条消息。若返回 TIMEOUT 或空，
说明密钥没接通——回到第 4 步检查。

### 第 7 步：配置自动回复（先别开启）

打开 http://127.0.0.1:8011 ，在「自动回复对象」里添加至少一个好友：
- 备注名必须是微信里能**唯一命中**的名字
- 点「查 wxid」自动解析并填入 aliases
- 填人格（说话风格）与记忆（你记得的关于他的事）

**此时保持总开关关闭。**

### 第 8 步：上报给我确认

请把以下信息汇总给我，等我确认后再开启：
1. 四个端口的状态
2. `wechat.message.history` 是否返回数据
3. 控制台里配了哪些对象
4. 遇到的任何警告或异常

## 排障

遇到问题请先跑：

    python health_check.py --no-fix

然后按输出定位。常见问题：
- 端口 18010 不通  -> UIA Agent 没起来，`python health_check.py --fix`
- 端口 18011 不通  -> hook 任务没注册，跑第 5 步第一条命令
- 微信窗口关到托盘  -> UIA 会全瘫，必须让微信窗口可见
- history 超时      -> 密钥不对，或 Gateway 需要重启

## 绝对不要做的事

- 不要把别人的 `work/db.key`、`config/*.json` 复制过来用
- 不要在没有 dry_run 观察的情况下直接开启自动回复
- 不要用这个工具做群发、营销、骚扰
- 不要把它用于任何未经对方知情的场景
- 不要在群里开启自动回复（本项目默认不支持群消息）
```

---

## 给 AI 的一句话版本

如果你只想给 AI 一句简短指令：

```
解压后先读 AI_DEPLOY_PROMPT.md 并严格按它执行部署到这台机器。
关键：不要照抄任何密钥或路径，所有凭据必须在本机重新获取；
每步都要验证；在我确认前不要开启自动回复。
```
