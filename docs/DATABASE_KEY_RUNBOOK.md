# 微信数据库密钥运行手册

当前微信账号使用旧式 `Msg` 布局。消息数据库是 SQLCipher 加密文件；在没有经过页级 HMAC 验证的 32 字节密钥前，数据库读面必须保持 `DATABASE_KEY_UNAVAILABLE`。

## 支持的密钥来源

适配器只接受调用方显式提供的密钥，或本地已验证且与数据库签名绑定的缓存：

- `WECHAT_DB_KEY`：单个 64 位十六进制密钥，适用于所有库相同的场景。
- `WECHAT_DB_KEY_FILE`：文件第一行的 64 位十六进制密钥。
- `WECHAT_DB_KEYS_FILE`：JSON 映射，键可使用数据库绝对路径、相对路径、文件名或 `*`。
- `work/database_key_cache.json`：仅由页级 HMAC 验证成功后写入，数据库大小、修改时间和首页摘要变化会自动失效。

密钥不会写入日志或 evidence。不要把密钥文件加入 Git，也不要通过进程注入、内存转储或 hook 获取密钥。

## 验证标准

只有以下链路全部成功才会进入 SQLCipher read-ready：

```text
PAGE_HMAC_VALID
    -> 解密主库及当前 WAL 世代到临时副本
    -> sqlite_master 可读
```

原始微信数据库只读打开；WAL 合并和解密结果均写入 `work/decrypted_db/`。如果微信在读取期间 checkpoint 或追加 WAL，适配器会放弃本次临时副本并重试，最多三次。

## 诊断命令

```powershell
python scripts/diagnostics/database_key_status.py
```

没有显式密钥时，预期结果是：

```text
key_discovery_state = PENDING
database_key_status = DATABASE_KEY_UNAVAILABLE
sqlcipher_open_success = false
schema_read = false
retryable = true
```

这表示“等待合法密钥”，不是数据库损坏，也不表示 UIA 消息读取失败。拿到密钥后，应在隔离环境设置上述变量，再重新执行诊断；错误密钥会报告 `INVALID / PAGE_HMAC_INVALID`，不会静默回退旧缓存。

`database_status_code` 用于区分密钥问题和文件布局问题：

| 状态码 | 含义 |
| --- | --- |
| `DATABASE_DIRECTORY_UNAVAILABLE` | 当前账号目录不可见，尚不能判断是否需要密钥 |
| `DATABASE_FILES_UNAVAILABLE` | 账号目录存在，但没有发现数据库文件 |
| `DATABASE_KEY_NOT_REQUIRED` | 发现的数据库均为明文，不需要 SQLCipher 密钥 |
| `DATABASE_KEY_UNAVAILABLE` | 发现加密数据库，但没有通过验证的密钥 |
| `DATABASE_READ_READY` | 所有发现的加密数据库均已通过验证并可读 |

## 当前实测状态

2026-09-04 在当前账号 `wxid_EXAMPLE` 上实测：

- `__USERPROFILE__\Documents\WeChat Files` 可见，旧式 `Msg` 布局，共发现 43 个数据库。
- `xInfo.db` 可明文读取；其余数据库均为 SQLCipher 加密库。
- `WECHAT_DB_KEY`、`WECHAT_DB_KEY_FILE`、`WECHAT_DB_KEYS_FILE` 均未配置；缓存只有测试数据库条目，和当前真实库签名不匹配。
- 当前状态为 `DATABASE_KEY_UNAVAILABLE` / `KEY_DISCOVERY_PENDING`，`database_status_code` 同为 `DATABASE_KEY_UNAVAILABLE`。

参考项目中的进程内存扫描、注入或 hook 不属于本适配器的合法密钥来源，不会被启用。只有用户通过受信任渠道提供密钥，且该密钥通过页级 HMAC 和 `sqlite_master` 校验后，消息读面才会切换为可用。
