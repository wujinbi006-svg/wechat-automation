# WeChat Compatibility — 版本兼容性

## 当前环境

| 项 | 值 |
|---|---|
| Windows | Windows 10 Home China, Build 26200, x64 |
| 微信版本 | Weixin.exe 4.1.13.12 |
| Weixin.dll | 4.1.13.12 (约 187.6 MiB) |
| 架构 | x64 (AMD64) |
| 进程模型 | Weixin.exe 主进程 + --type= 子进程 (wxpublic/wxutility/wxocr/wxplayer) |

## 版本变化历史

### 微信 3.x (legacy)

- 进程名：`WeChat.exe`
- 内核模块：`WeChatWin.dll`
- 数据布局：`Documents\WeChat Files\<wxid>\Msg\` + `MSG*.db`
- 数据库密钥：旧版偏移表 (`WX_OFFS.json`)
- PyWxDump 兼容：是（3.1.46，但上游已清空）
- 本机状态：**不存在** WeChatWin.dll

### 微信 4.x (current)

- 进程名：`Weixin.exe`
- 内核模块：`Weixin.dll` (约 188 MiB, ImageBase 0x180000000)
- 数据布局：`xwechat_files\<wxid>_\<hash>\db_storage\`
- 数据库密钥：SQLCipher 4, PBKDF2-HMAC-SHA512 (64000 轮), 32 字节 key
- 进程模型：主进程 + `--type=wxpublic/wxutility/wxocr/wxplayer` 子进程
- PyWxDump 兼容：**否**（硬编码 WeChat.exe/WeChatWin.dll, Issue #203 未解决）
- WeChatDataAnalysis 兼容：**partial**（v1.18.5 有 V4 路径，无 4.1.13.12 明确版本映射）

## 兼容性矩阵

| 版本范围 | 数据布局 | UIA 行为 | 数据库密钥 | 本机验证 |
|---|---|---|---|---|
| 3.x (legacy) | Msg/MSG*.db | WeChat.exe + WeChatWin.dll | 旧偏移表 | N/A (不在本机) |
| 4.0.x–4.1.8 | db_storage | Weixin.exe + Weixin.dll | SQLCipher 4 | 未验证 |
| 4.1.9.23 | db_storage | 同上 | 同上 | 第三方声明 (ssq123123) |
| 4.1.12.26 | db_storage | 同上 | 同上 | 第三方声明 (stargazer-2026) |
| **4.1.13.12** | **db_storage** | **同上** | **同上** | **本机实测** |

## VersionProfile 设计

未来不同微信版本**不允许**出现：
```python
if version == "4.x":
    magic_behavior()
```

而应该：

```python
@dataclass
class VersionProfile:
    wechat_version: str
    process_name: str          # "Weixin.exe" / "WeChat.exe"
    main_module: str           # "Weixin.dll" / "WeChatWin.dll"
    data_layout: str            # "db_storage" / "legacy_msg"
    sqlcipher_version: int      # 4
    key_derivation: str         # "PBKDF2-HMAC-SHA512"
    uia_class_root: str         # "mmui::MainWindow"
    uia_qt_gate: bool           # True
    child_process_types: list[str]
    supported_features: list[str]
    known_limitations: list[str]
```

当前 4.1.13.12 的 VersionProfile：

```python
VERSION_4_1_13_12 = VersionProfile(
    wechat_version="4.1.13.12",
    process_name="Weixin.exe",
    main_module="Weixin.dll",
    data_layout="db_storage",
    sqlcipher_version=4,
    key_derivation="PBKDF2-HMAC-SHA512",
    uia_class_root="mmui::MainWindow",
    uia_qt_gate=True,
    child_process_types=["wxpublic", "wxutility", "wxocr", "wxplayer"],
    supported_features=[
        "uia_read_only", "uia_foreground_safety",
        "database_discovery", "xinfo_read",
        "event_broadcast", "synthetic_event_inject",
    ],
    known_limitations=[
        "database_key_unavailable", "send_not_verified",
        "no_group_info", "uia_qt_gate_required",
    ],
)
```

## CapabilityProfile

VersionProfile 不直接决定能力可用性；它提供 CapabilityProfile 给 ProviderSelection：

```
VersionProfile → CapabilityProfile → ProviderSelection
     ↑                                      ↓
     版本探测                           选择对应 Provider 实现
```

## 已知限制（当前版本）

1. **PyWxDump 不兼容** — 硬编码 WeChat.exe/WeChatWin.dll，无 4.1.13.12 偏移
2. **WeChatDataAnalysis V4** — 有通用 V4 路径，无 4.1.13.12 明确映射
3. **UIA Qt Accessibility Gate** — 需要在启动期执行 `active_initialize_accessibility`
4. **Foreground safety** — 被动路径不能改变前台窗口
5. **Session 隔离** — 服务在 Session 0，UIA 需要 Session 1
6. **数据库密钥** — 当前不可用（PENDING），KeyResolver 禁用进程内存扫描/hook

## 参考项目兼容性

| 项目 | 声称版本 | 方法 | 本机验证 | 采用 |
|---|---|---|---|---|
| PyWxDump (xaoyaoo) | 3.x | 偏移表 + ReadProcessMemory | ❌ 不兼容 | 未采用 |
| PyWxDump (JellyHoney) | 3.1.45 | 同上 | ❌ 无 Release | 未采用 |
| WeChatDataAnalysis (LifeArchiveProject) | V4 通用 | YARA + 进程内存 + DLL 扫描 | partial (2 候选命中) | 参考实现 (work/wechatauto_pkg) |
| wechatauto_replica (fanyuantaier) | 4.x UIA | UIA 自动化 + 数据库读取 | ✅ 已集成 | 已采用 (src/uia_service.py ReplicaUIADriver) |
| wechat-4.1.12-decrypt (stargazer-2026) | 4.1.12.26 | Frida hook | ❌ 版本不符+方法越界 | 未采用 |
| weixin-db-decrypt-panel (ssq123123) | 4.1.9.23 | 进程内存 | ❌ 版本不符 | 未采用 |
| ReadWxKey (yinhuacha869) | 声称广覆盖 | 声称内存扫描 | ❌ 无可审计代码 | 未采用 |
