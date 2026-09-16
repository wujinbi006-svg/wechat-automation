# 参考实现与当前 4.1.13.12 差异

| Layer | Reference | Current 4.1.13.12 | Status |
|---|---|---|---|
| Process | 仅 `Weixin.exe` | 多个 `Weixin.exe` 与 `WeChatAppEx.exe`；anchor 仅在 PID 7172 命中 | PROCESS_TARGET_PLAUSIBLE |
| Module | 未限定模块，扫描进程内存 | PID 7172 可读区域中命中 1 次 | ANCHOR_FOUND |
| Anchor | `com.Tencent.WCDB.Config.Cipher` | PID 7172：1；其余目标进程：0 | ANCHOR_FOUND |
| Pointer/Length | 固定节点偏移与指针/长度关系 | 本轮未解析结构，计数为 UNKNOWN | STRUCTURE_UNKNOWN |
| XOR | 固定 XOR mask | 未进入候选解码统计 | NOT_TESTED |
| Candidate | 64 hex 字符切片为 32 字节 | 未统计（参考实现接口未暴露） | CANDIDATE_UNKNOWN |
| HMAC validation | 数据库第一页 HMAC | 0 个有效 key | VALIDATION_NOT_REACHED_OR_MISMATCH |

当前最强证据是：anchor 并非全局失效，也不在 `WeChatAppEx.exe` 中；它存在于 PID 7172 的 `Weixin.exe`。因此不能直接判定为进程迁移。失败点进一步收敛到 anchor 后的结构解析、候选解码或验证链。
