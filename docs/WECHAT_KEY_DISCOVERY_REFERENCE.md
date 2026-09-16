# Key Discovery 参考实现复盘

来源：`wechatauto/db.py`（wheel 1.1.1）。

- `REFERENCE_PROCESS`：通过 `tasklist` 只枚举 `Weixin.exe`。
- `REFERENCE_MODULE`：没有模块限定；通过 `VirtualQueryEx` 扫描目标进程的可读/可执行区域。
- `REFERENCE_PATTERN`：先找 ASCII `com.Tencent.WCDB.Config.Cipher`，再找其地址与长度组成的指针对。
- `REFERENCE_EXTRACTION`：按固定偏移读取节点、配置对象和数据 blob；使用源码内 XOR mask 解码；从 `x'...'` 字面量切出 64 个十六进制字符，即 32 字节候选。
- `REFERENCE_VALIDATION`：候选需通过 `_probable_key`，再用数据库第一页的 PBKDF2-HMAC-SHA512/HMAC 校验；通过后才算有效 key。

重要限制：参考实现只扫描 `Weixin.exe`，不枚举 `WeChatAppEx`；pattern 与节点偏移是实现假设，不是对 4.1.13.12 的独立证明。
