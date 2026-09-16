# WeChat 4.1.13.12 数据库实机验证

日期：2026-09-02

## 环境

- 微信版本：4.1.13.12
- 当前账号目录：`__USERPROFILE__\Documents\WeChat Files\wxid_EXAMPLE`
- 数据库根目录：该账号目录下的 `Msg\`
- 验证脚本：`scripts/diagnostics/test_database_read.py`

## 分层结果

| 阶段 | 结果 |
|---|---|
| DATABASE_FILES_FOUND | PASS |
| DATABASE_ENCRYPTED | PASS（候选数据库头部不是 SQLite 明文头） |
| KEY_DISCOVERED | FAIL（0 个可验证 key） |
| SQLCIPHER_OPEN_SUCCESS | NOT_AVAILABLE |
| SCHEMA_DISCOVERED | NOT_AVAILABLE |
| MESSAGE_QUERY_SUCCESS | NOT_AVAILABLE |
| MESSAGE_READ_SUCCESS | NOT_AVAILABLE |
| ACCOUNT_BINDING | ACCOUNT_BINDING_UNVERIFIED |

本次没有保存真实 key、内存转储、Cookie、Token 或认证数据，也没有修改原始数据库。

## 观测

发现了当前账号下的数据库及其 `-wal`/`-shm` 伴随文件；完整文件清单见 `docs/evidence/database_inventory.json`。候选消息库包括 `Msg\Multi\MSG0.db`、`MSG1.db`、`MSG2.db`，以及 `MicroMsg.db`、`ChatMsg.db` 等。

参考实现的进程内存扫描在本机本次运行中返回 0 个通过 HMAC 验证的密钥。由于未得到 key，不能把后续“数据库无可用密钥”解释为 SQLCipher 算法失败，也不能据此断言数据库不可读。

## 性能

- key discovery：约 8533.3 ms
- 数据库尝试阶段：约 0.5 ms（因无 key 立即跳过）
- 总耗时：约 9174.2 ms

## 下一步建议

先针对“KEY_DISCOVERED=FAIL”做隔离诊断：确认扫描进程权限/位数、当前微信进程是否持有配置对象、以及参考代码对目录结构和版本的假设；在 key 成功前不要实现完整 `DatabaseAdapter`，也不要接入 Agent。
