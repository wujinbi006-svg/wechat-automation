# Key 链路差异

| Stage | Reference | Current 4.1.13.12 | Result |
|---|---|---|---|
| Process | `Weixin.exe` | PID 7172 | MATCH |
| Anchor | 字符串 1 次或多次命中 | 由探针实测 | MEASURED |
| Pointer | anchor 地址+长度 pair，固定节点偏移 | 由探针实测 | MEASURED |
| Length | 节点/对象中的 uint64 长度 | 由探针实测 | MEASURED |
| XOR | 固定 mask 循环异或 | 由探针实测 | MEASURED |
| Candidate | 64 hex → 32 bytes | 由探针实测 | MEASURED |
| HMAC | 数据库第一页页 MAC | 未执行随机猜测 | ONLY_IF_CANDIDATE |
