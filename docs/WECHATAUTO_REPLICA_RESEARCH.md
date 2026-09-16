# wechatauto-replica 1.1.1 调研报告

日期：2026-09-02  
来源：本地 wheel `work/wechatauto_pkg/wechatauto_replica-1.1.1-py3-none-any.whl`

## 结论摘要

该 wheel 包含完整、可读的 Python 实现，不是仅有声明的空壳，也未发现必须加载的自带二进制扩展。其数据库路线在代码层面确实实现了：

- 从运行中的 `Weixin.exe` 进程只读扫描配置对象，候选密钥按每个数据库分别验证；
- 按 SQLCipher 4 风格（AES-CBC、PBKDF2-HMAC-SHA512、页尾 IV/HMAC）解密 SQLite 页面；
- 合并 WAL 到临时明文副本后使用标准 `sqlite3` 只读查询；
- 联系人、会话、消息读取与导出接口。

但这只是“包内实现已确认”，不是本机微信 4.1.13.12 已成功读取的实证。本次没有执行密钥提取、没有打开真实数据库、没有读取消息。

## 证据分类

### CONFIRMED_FROM_PACKAGE

1. `wechatauto/db.py` 定义 `WeChatDB`，默认从微信配置/账号目录定位数据库。
2. 密钥来源是 `Weixin.exe` 进程内存中的 `com.Tencent.WCDB.Config.Cipher` 相关对象；代码通过 `OpenProcess`、`VirtualQueryEx`、`ReadProcessMemory` 只读扫描。
3. 候选密钥为 32 字节，并通过首个数据库页的 HMAC 校验确认；代码不会把密钥写入源码，但会按十六进制保存到调用方指定的 `keys.json`。
4. 解密实现使用 AES-CBC；PBKDF2-HMAC-SHA512 派生 32 字节密钥；页大小 4096，预留区 80 字节（16 字节 IV + 64 字节 HMAC）。
5. 解密结果写入临时工作目录，WAL 以页为单位解密合并；查询连接使用标准 `sqlite3`，并设置只读 URI。
6. `wechatauto.uia_driver.WeChatUIA` 使用 UIA 类名/AutomationId 定位：`mmui::MainWindow`、`mmui::XValidatorTextEdit`、`search_list`、`search_item_*`、`chat_input_field`。
7. `wechatauto.guia` 明确实现 UIA 优先、失败后坐标/OCR 兜底；OCR 使用 Windows Runtime `Windows.Media.Ocr`，用于会话定位、按钮定位及部分操作。
8. 发送路径使用 `SendKeys`、剪贴板和点击等前台交互；代码注释和流程均表明发送不是纯后台数据库操作。

### CLAIMED_IN_METADATA

`METADATA` 宣称“SQLCipher 4 DB decryption”“UIA/坐标-OCR 混合发送”已验证，并标注支持 WeChat 4.1.12+。这些是项目发布者声明，不能替代本机实测。

项目主页/仓库声明为 `github.com/fanyuantaier/wechatauto-replica`。

### NOT_FOUND

- 未发现独立 SQLCipher 动态库；实现依赖 `cryptography` 与标准 `sqlite3`。
- 未发现绕过微信进程内存扫描的通用静态密钥算法。
- 未发现后台发送 API；数据库模块明确限制为只读。

### NOT_TESTED

- 未在本机 WeChat 4.1.13.12 上运行 `WeChatDB`。
- 未验证本机数据库目录是否与包文档中的 `xwechat_files/.../db_storage/` 一致。
- 未验证本机密钥扫描权限、候选密钥数量、任一数据库 HMAC 成功或消息查询结果。
- 未验证其 `chat_input_field` 是否在本机 UIA 树出现，也未验证 OCR 兜底是否解决前台激活问题。

## 对当前项目的影响

该项目足以作为 P1.2 的高价值参考，尤其是页级解密、HMAC 验证、WAL 合并和只读查询。但当前证据只支持“实现路线可复用/可研究”，不支持直接宣布“本机数据库已可读”。建议下一步在隔离临时目录执行最小只读验证，并禁止把真实密钥写入 Git、日志或 evidence。

UIA 方面，它的定位锚点与当前工程的 `mmui` 树结论相近；发送仍依赖前台交互，OCR 是定位/操作失败时的降级路径，不应被解释为已消除 `SetValue` 或选择操作的前台激活副作用。
