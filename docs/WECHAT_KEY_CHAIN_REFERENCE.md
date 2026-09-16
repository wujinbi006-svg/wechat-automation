# Key 提取链路复盘

- `REFERENCE_ANCHOR`：搜索 `com.Tencent.WCDB.Config.Cipher`。
- `REFERENCE_POINTER`：搜索 anchor 地址与字符串长度组成的两个 `uint64`；命中后从 `qaddr-0x10` 读取节点。
- `REFERENCE_LENGTH`：节点偏移 `0x18` 为字符串长度；配置对象偏移 `0x88` 后的对象中，`+0x10` 为 blob 长度。
- `REFERENCE_XOR`：blob 每字节与源码中的 `CONFIG_XOR_MASK` 循环异或。
- `REFERENCE_CANDIDATE`：解码结果匹配 `x'...'`，每 64 个十六进制字符切出 32 字节候选。
- `REFERENCE_VALIDATION`：对每个数据库第一页执行 PBKDF2-HMAC-SHA512 页 MAC 校验；通过才算有效 key。

本文件不包含任何当前机器的密钥或内存内容。
