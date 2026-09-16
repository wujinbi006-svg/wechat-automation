# 密钥获取指南

> **核心原则：所有密钥都必须在本机重新获取，不能从别人的机器复制。**
>
> 数据库密钥与账号 + 机器绑定；API Key 与你的账号绑定。照抄别人的密钥
> 既解不开你自己的数据库，也会把对方的凭据泄露给你。

本文覆盖三种密钥：

| 密钥 | 用途 | 能否复制 |
|---|---|---|
| **数据库密钥** | 解密聊天记录数据库 | ❌ 每账号每机器都不同 |
| **图片密钥** | 解密 `.dat` 图片 | ⚠️ 本地推导，不需外部获取 |
| **LLM API Key** | AI 生成回复 | ❌ 你自己的账号 |

---

## 一、数据库密钥（最关键）

### 它是什么

微信 4.x 用 SQLCipher 加密本地数据库。密钥是 **32 字节**（写作 64 位小写十六进制）
的对称密钥，每个微信账号在自己机器上首次登录时生成。

```
形态：  恰好 64 个十六进制字符（= 32 字节），例如 a1b2c3... 这类小写串
位置：  work/db.key   （一行，无空格、无换行、无 BOM）
校验：  python -c "import pathlib,re; k=pathlib.Path('work/db.key').read_text().strip(); print(len(k), bool(re.fullmatch(r'[0-9a-f]{64}', k)))"
```

> 本文**故意不给示例密钥**。任何看起来像真实密钥的字符串都不该出现在文档里，
> 而且照抄别人的密钥对你毫无用处（见下）。

### 为什么不能复制

```
别人的密钥 → 由别人的账号 + 别人的机器生成
           → 解开的是别人的数据库，解不开你的
           → 同时你把别人的数据库暴露给了自己（反之亦然）
```

### 获取原理（一句话）

密钥在微信运行时常驻在 `Weixin.exe` 进程内存里。取钥工具做的事是：
在进程内存中搜出候选的 32 字节密钥 → 用每个候选去试解一个已知数据库 →
**能通过 HMAC 校验的那个就是真密钥**。

### 三条获取路径

#### 路径 A：用现成的开源取钥工具（推荐）

社区里有多个针对微信的取钥项目，原理相同，差异在于支持的微信版本：

| 工具 | 说明 |
|---|---|
| `chatlog` | 支持较新版本微信，跨平台，有主动取钥能力 |
| `PyWxDump` | 老牌工具，对微信 3.x 支持好；4.x 需确认版本 |
| `WeChatMsg` | 聊天记录导出，内置取钥 |
| `wx_key` | 专注密钥推导，常被其它项目引用 |

**关键点：先确认工具支持你的微信版本。**

```powershell
# 先查你的微信版本
Get-ChildItem "C:\Program Files\Tencent\Weixin" -Directory |
  Where-Object { $_.Name -match '^\d+(\.\d+)+$' } | Select-Object Name
```

拿到版本号后，去对应项目的 Release 说明里核对支持的版本区间。
**版本不匹配时会取到错误密钥或直接失败**，不是工具坏了。

取到密钥后：

```powershell
# 写入 work/db.key（注意：不要有多余空格或换行）
Set-Content -Path work\db.key -Value "你的64位密钥" -NoNewline -Encoding ASCII

# 验证格式
python -c "import pathlib,re; k=pathlib.Path('work/db.key').read_text().strip(); print('长度',len(k),'合法' if re.fullmatch(r'[0-9a-f]{64}',k) else '不合法')"
```

#### 路径 B：用本工程内置的密钥发现

本工程的 `src/key_resolver.py` 支持从多种来源加载密钥。可用的环境变量：

| 变量 | 含义 |
|---|---|
| `WECHAT_DB_KEY` | 直接给 64 位十六进制密钥 |
| `WECHAT_DB_KEY_FILE` | 指向含密钥的文件（默认指向 `work/db.key`） |
| `WECHAT_DB_KEYS_FILE` | JSON 映射文件，格式 `{数据库路径: 密钥}` |

**注意**：本工程的密钥发现**默认不做内存扫描 / 进程注入**
（`disabled_strategies` 明确包含 `process_memory_scan`、`dynamic_hook`、
`process_injection`）。所以它需要你**先把密钥喂进去**，而不是自己去取。

换句话说：路径 B 是"怎么用密钥"，路径 A 才是"怎么拿到密钥"。

#### 路径 C：手动 + 工具混合

如果你的取钥工具输出的是 JSON（含路径到密钥的映射），可以直接接：

```powershell
$env:WECHAT_DB_KEYS_FILE = "C:\path\to\keys.json"
```

或在 `service/WeChatGateway.xml` 里加对应的 `<env>` 行。

### 验证密钥是否正确（必做）

**不要跳过这一步。** 密钥格式对不代表内容对。

**方法 1：用网关接口验证**

```powershell
# 1) 重启网关让它重新读密钥
.\service\WeChatGateway.exe stop
Start-Sleep 6
.\service\WeChatGateway.exe start
Start-Sleep 25

# 2) 查读面状态
curl http://127.0.0.1:8010/status
```

期望看到的字段：

```
sqlcipher_open_success  : True
sqlcipher_read_ready    : True
read_plane              : sqlcipher_reference
```

如果是 `False` / `unavailable` → 密钥不对，或网关没重启。

**方法 2：直接读一条消息**

```powershell
curl -X POST http://127.0.0.1:8010/tools/call `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"wechat.message.history\",\"arguments\":{\"conversation_id\":\"文件传输助手\",\"limit\":3}}"
```

期望：`"success": true` 并返回若干条消息内容。

**方法 3：用独立脚本验证（不依赖网关）**

```powershell
python check_db.py
```

### 常见失败原因

| 现象 | 原因 | 处理 |
|---|---|---|
| 格式校验通过但读不出数据 | 密钥来自别的账号/机器的数据库 | 重新取钥 |
| `sqlcipher_open_success: false` | 网关缓存了旧状态 | 重启网关服务 |
| 取钥工具报找不到密钥 | 微信版本不在工具支持范围 | 换工具，或等工具更新 |
| 密钥变了 | 微信重装 / 换账号 / 清了配置 | 重新取钥（密钥会变） |
| `WECHAT_DB_KEY` 设了但没生效 | 服务环境变量优先于用户环境变量 | 改 `service/WeChatGateway.xml` |

---

## 二、图片密钥

**不需要外部获取**——微信 4.x 的图片密钥可以从本地数据推导。

本工程的 `image_decryptor.py` 负责解密 `.dat` 文件。
部分场景需要 `wx_key.get_image_key()`：

```python
import wx_key
key = wx_key.get_image_key()   # 本地推导，不联网
```

**已知限制**：只对**本地有缓存**的图片有效。微信只把缩略图存在本地，
原图在 CDN 上，本地无缓存时拿不到——这不是密钥问题。

---

## 三、LLM API Key

任何 **OpenAI 兼容**端点都能用。

### DeepSeek（推荐，便宜且中文好）

1. 访问 https://platform.deepseek.com/
2. 注册 → 「API Keys」→ 创建
3. 复制形如 `sk-xxxxxxxx` 的密钥

填进 `config/auto_reply.json`：

```json
"llm_base_url": "https://api.deepseek.com/v1",
"llm_api_key": "sk-你的密钥",
"llm_model": "deepseek-flash",
"llm_fallback_models": ["deepseek-v4-pro"]
```

### 其它可选

| 服务 | base_url | 备注 |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | 需海外支付 |
| 阿里通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 国内直连 |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | 国内直连 |
| 月之暗面 | `https://api.moonshot.cn/v1` | 国内直连 |
| **本地 Ollama** | `http://127.0.0.1:11434/v1` | **完全免费，api_key 随便填** |

**先用 Ollama 试水**是最省钱的方案：

```powershell
ollama pull qwen2.5:7b
# 然后 config/auto_reply.json:
#   llm_base_url = http://127.0.0.1:11434/v1
#   llm_api_key  = ollama
#   llm_model    = qwen2.5:7b
```

### 验证 API Key

```powershell
# 直接测端点
curl https://api.deepseek.com/v1/models -H "Authorization: Bearer sk-你的密钥"

# 或试运行（不会真发消息）
python scripts\auto_reply_livecheck.py --rule 你的对象名 --text "在吗"
```

期望看到 `llm` 字段里 `"result": "ok"`。

---

## 四、IPC 口令（本工程内部用）

**不需要你操心**——`setup.py` 会自动生成一个 32 字节随机口令，写入
`config/agent.env`，并由 Gateway 服务与 Agent 看门狗共同使用。

**为什么重要**：它保护本机 `127.0.0.1:18010` 这个 IPC 端口。
如果沿用默认值，本机任何进程都能通过它操作你的微信。

检查它是否已被随机化：

```powershell
python -c "import pathlib; t=pathlib.Path('config/agent.env').read_text(); print('长度', len(t), '行数', t.count(chr(10)))"
```

**如果口令值仍然是仓库里那个默认串，说明没生效**，重跑 `python setup.py --force`。

判断标准：口令值应当是 40 字符以上的随机串。若看到形如 `change-me-...` 的
可读英文短语，就是默认值，必须换掉。

---

## 五、密钥安全清单

部署完成前请逐条确认：

- [ ] `work/db.key` 是本机取的，不是复制的
- [ ] `config/agent.env` 里不是默认口令
- [ ] `config/console.json` 里不是 `PUT-YOUR-API-KEY-HERE`
- [ ] `.gitignore` 覆盖了 `work/`、`config/*.json`、`config/agent.env`
- [ ] 没有把 `work/decrypted_db/` 提交到任何仓库
- [ ] 如果要分享代码，先跑 `python tools/build_release.py --check` 确认无泄漏

`tools/build_release.py` 会自动做最后一项：它内置了敏感模式扫描
（个人 wxid、本机路径、API Key 形态、64 位十六进制密钥），
命中任何一项就**拒绝产出压缩包**。

---

## 六、脱敏字典 `work/scrub_rules.json`（分享代码前必建）

`tools/build_release.py` 需要知道**你本人的真实身份标识**才能把它们换成占位符。
这些值不能写死在构建脚本里 —— 否则脚本自己就带着它们进包了。
所以放在一个运行时读取的文件里：

```jsonc
// work/scrub_rules.json    —— 该文件已在 EXCLUDE 名单里，绝不进包
{
  "我的真实备注名": "好友A",          // 微信里出现的中文姓名/备注名
  "另一个真实姓名": "好友B",
  "wxid_myaccount123": "wxid_EXAMPLE", // 你自己的 wxid
  "MY-PC-HOSTNAME": "__HOSTNAME__"     // 计算机名
}
```

### 为什么必须有

姓名是**自由文本**，形态扫描（正则）抓不到 —— 它出现在文档、控制台前端、
测试夹具、日志示例里，格式毫无规律。唯一可靠的兜底是「已知真实值的残留比对」：
构建脚本把这份字典里的每个真实值，在产物的**文本内容和文件名**里都搜一遍，
搜到就中止构建。

### 会被自动处理的

| 位置 | 怎么处理 |
|---|---|
| 文本文件正文 | 按字典替换成占位符 |
| **文件名** | 若含真实值，构建直接失败（提示你改名） |
| 大小写变体 | wxid、主机名这类 ASCII 标识会自动补大写/小写规则 |

### 注意

- 字典按**长度倒序**应用，所以带后缀的长 wxid 不会被较短的同类规则先截断。
- 转义形态要各写一条：同一路径在源码里既有单反斜杠写法，也有双反斜杠写法，两种都要登记。
- 新增好友自动回复后，**把新备注名加进这份字典再重新构建**。
- 构建脚本的文本替换覆盖 `.py .md .json .html .js .css .yml .cmd .ps1 .vbs .c .h`
  等；如果你引入了新的文本扩展名，记得同步加进 `TEXT_EXT`。
- **拼音和微信号必须单独登记**：真实姓名经常以拼音形态出现（脚本文件名、
  PowerShell 提示词里的英文名、微信号），中文字符扫描完全抓不到。
- **文件名里不允许含真实值**：构建会直接失败并提示你改名。注意
  `MANIFEST.json` 会列出全部文件名，所以文件名一旦泄漏就会被一起带出去。
