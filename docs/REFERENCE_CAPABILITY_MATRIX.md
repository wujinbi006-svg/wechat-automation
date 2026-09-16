# 参考项目源码级 Parity Matrix

审计日期：2026-09-04  
参考源码：`__USERPROFILE__\Documents\Codex\2026-09-02\new-chat\work\reference\wechatauto-replica`  
参考版本：`wechatauto.__version__ = 1.2.0.3`（源码声明；不是本机微信能力证明）  
当前客户端：`Weixin.dll 4.1.13.12`

## 证据等级

- `CODE_PRESENT`：源码中存在可调用符号。
- `DOCUMENTED`：README/注释/元数据声称存在。
- `TESTED_BY_REFERENCE_PROJECT`：参考源码包含演示/测试路径，但本审计未重新运行其真实微信流程。
- `VERIFIED_ON_CURRENT_MACHINE`：2026-09-04 在本机当前微信上取得运行态证据。

## 能力矩阵

| capability | reference_file / symbol | reference_execution_path | reference_dependencies | reference_fallback | reference_verification | current_status | gap | implementation_plan |
|---|---|---|---|---|---|---|---|---|
| UIA 热激活 | `wechatauto/uia_driver.py`：`_scan_qaccessible_active_rva`、`_hot_activate_accessibility`、`_wake_accessibility` | Weixin.exe/Weixin.dll → Qt accessibility gate → `ControlFromHandle` | Windows UIA、进程只读读写权限、版本特征扫描 | 版本 RVA 表 | `CODE_PRESENT`；源码注释声称 4.1.12+ | `VERIFIED_ON_CURRENT_MACHINE=PASS`：RVA=181507688，active=1 | 需要继续监测微信升级后的指纹变化 | 保留动态扫描；每次恢复记录 gate read-back 与前台 before/after |
| mmui 树 | `uia_driver.py:_find_main` | HWND → `mmui::MainWindow` | UIA provider 已物化 | OCR/坐标 GUI | `CODE_PRESENT` | `VERIFIED_ON_CURRENT_MACHINE=PASS`：主窗 HWND 329920，PID 5064 | Raw/Content View 不返回会话 cell，视图差异需保留 | 限制在主窗 subtree，禁止 Desktop 全局遍历 |
| 会话列表 | `uia_driver.py:_find_main`；`ui/sessionbox.py:SessionBox.get_session` | `session_list` → `ChatSessionCell` | ControlView、可见会话 | 滚动/搜索 | `CODE_PRESENT` | `VERIFIED_ON_CURRENT_MACHINE=PASS`：7 个可见 `mmui::ChatSessionCell` | 仅覆盖当前可见窗口，非全量列表 | 保持 UIA 可见列表；数据库可用后再扩展全量 |
| 联系人搜索 | `uia_driver.py:open_chat`、`_collect_results` | 搜索框 → `search_list` → `search_item_*` | 剪贴板、Ctrl+V、前台交互 | OCR 搜索 | `CODE_PRESENT`；未在本审计重跑 | `PARTIAL=PASSIVE_VISIBLE_SESSION_SEARCH_ONLY` | 当前普通 `search_chat` 不填充搜索框，不支持后台全联系人搜索 | 提供显式 `INTERACTIVE_MODE` 搜索入口，默认禁用 |
| 群搜索 | `uia_driver.py:open_chat`、`guia.py:_search_chat` | 搜索结果分区/群聊 OCR | UIA 或 OCR、前台输入 | 坐标 | `CODE_PRESENT` | `NOT_TESTED` | 当前工程无安全的后台全量群搜索 | 仅在显式交互模式实现并验证唯一目标 |
| 打开会话 | `uia_driver.py:open_chat`；`guia.py:open_chat` | 搜索 → 选择/点击 → `current_chat` 校验 | UIA Invoke/Selection 或鼠标键盘 | OCR/坐标 | `CODE_PRESENT`；参考示例存在 | `VERIFIED_ON_CURRENT_MACHINE=PASS`：`open_chat("好友B")` 真实切换成功 | 仍然属于交互式能力，不能当作被动后台只读能力 | 保持 `SEARCH→OPEN→VERIFY`，失败不得隐式前台回退 |
| 当前会话校验 | `uia_driver.py:current_chat` | 读取 `chat_input_field.Name` | UIA EditControl | OCR 标题 | `CODE_PRESENT` | `VERIFIED_ON_CURRENT_MACHINE=PARTIAL`：输入框控件当前快照为 false | 当前窗口存在会话但输入框并非始终暴露 | 用多证据校验，不把单一 Name 当绝对成功 |
| 消息读取 | `db.py:WeChatDB.get_messages`；`uia_driver.py` 消息树 | DB 解密查询或 UIA 消息节点 | SQLCipher key 或 UIA ContentView | 无 | `CODE_PRESENT`；DB 本机未成功 | `VERIFIED_ON_CURRENT_MACHINE=PARTIAL`：UIA 返回结构节点，完整文本不稳定 | 数据库 key 不可用；UIA 文本可能是容器节点 | 保留 UIA 只读读取；DB adapter 仅返回明确不可用状态 |
| 实时监听 | `db.py:Listener`、`DBListener` | 增量 sort_seq/local_id 轮询 → 去重回调 | 可解密 DB、后台线程 | UIA 轮询 | `CODE_PRESENT`；本机 DB 未验证 | `PARTIAL=UIA_POLLING_ONLY` | 尚无稳定 DB listener，真实 `message.new` 需谨慎标记 | 继续 UIA polling + fingerprint 去重，DB 可用时切换 |
| 文本发送 | `uia_driver.py:send_text`；`guia.py:send_msg` | 打开 → 输入 → Enter/发送按钮 → DB/UIA 校验 | 前台窗口、剪贴板、键盘/鼠标 | OCR/坐标 | `CODE_PRESENT`；参考路径是前台交互 | `SEND_RESULT_NOT_OBSERVABLE` | 当前微信发送结果无法可靠回读 | 保持显式确认、单次执行、不可自动重试，验证不足返回 UNKNOWN |
| 剪贴板输入 | `uia_driver.py:_paste_into`；`guia.py:input_text` | Clipboard → Ctrl+V | 前台焦点 | 拼音/Unicode 输入 | `CODE_PRESENT` | `NOT_TESTED_IN_CURRENT_PARITY_RUN` | 属于交互副作用，不能进入 passive path | 仅作为 `INTERACTIVE_MODE` capability |
| OCR 回退 | `guia.py:ScreenOCR`、`ocr_zoomed` | 截图 → Windows.Media.Ocr → 文字框 | WinRT OCR、屏幕截图 | 多轮放大/投票 | `CODE_PRESENT`；未在本机本轮运行 | `NOT_SUPPORTED_IN_PASSIVE_MODE` | OCR 读取屏幕且常与前台操作耦合 | 做策略接口与配置门禁，不隐式启用 |
| 坐标回退 | `guia.py:WinInput`、`find_session`、`click_send` | DPI/窗口矩形校准 → 屏幕输入 | Win32 SendInput、DPI、布局校准 | OCR/UIA | `CODE_PRESENT`；参考实现含真实输入 | `NOT_SUPPORTED_IN_PASSIVE_MODE` | 坐标输入会抢前台，且本机未安全验证 | 只允许显式交互模式，需布局指纹与验证 |
| 历史消息 | `db.py:WeChatDB.get_messages` | DB offset/limit 查询 | SQLCipher key、schema | 无 | `CODE_PRESENT` | `NOT_SUPPORTED`（数据库 key 不可用） | 当前没有可验证 read plane | 仅实现安全 facade，返回 `DATABASE_KEY_UNAVAILABLE` |
| 图片/文件/语音/视频读取 | `media.py:MediaDownloader` | DB 行 → 本地 dat/资源 → 解密落地 | DB、媒体密钥、文件布局 | 无 | `CODE_PRESENT`；未在当前机验证 | `NOT_SUPPORTED` | 依赖未验证的 DB key/媒体 key | capability manifest 明确列出，不伪造可用 |
| 图片/文件发送 | `guia.py:send_image`、`send_file` | CF_HDROP 剪贴板 → Ctrl+V → Enter | 前台交互、剪贴板 | 文件对话框 | `CODE_PRESENT` | `NOT_SUPPORTED` | 当前项目无安全验证路径 | 后续单独交互能力，默认关闭 |
| 回复/引用 | `guia.py:reply_msg` | 悬停/右键 → 回复 → 输入 → 发送 | OCR、鼠标、前台 | 无 | `CODE_PRESENT` | `NOT_SUPPORTED` | 需要前台交互且结果验证不足 | 仅作为显式 command，未验证前保持关闭 |
| @成员/群成员 | `guia.py:at_member`；`db.py:get_group_members` | 输入 @ → 成员选择 → 发送 | 前台交互或 DB | OCR | `CODE_PRESENT` | `NOT_SUPPORTED` | 群成员读取与交互均未在本机验证 | DB 可用后先做只读成员 facade |
| 朋友圈 | `moment.py:Moment`、`MomentDB` | UIA/OCR 控制或 sns.db 只读 | UIA、OCR、sns.db | 无 | `CODE_PRESENT`；实验性 | `NOT_SUPPORTED` | 不属于当前 Gateway P0，且有前台副作用 | 单独 experimental capability，不计入稳定 parity |
| 多账号 | `db.py:list_accounts` | 账号目录发现 | 本地文件系统 | 无 | `CODE_PRESENT` | `NOT_TESTED` | 当前服务按单账号代理运行 | 先做只读 account discovery，不改变登录态 |
| UIA 线程安全 | 当前 `src/uia_service.py`：固定单线程 executor/MTA | 所有 UIA 调用进入 dedicated worker | COM MTA、单一队列 | 重建 service | `CODE_PRESENT` | `VERIFIED_ON_CURRENT_MACHINE=PASS`：worker dedicated、MTA | 常驻 UIA events 尚未安装 | 保持单 worker，超时按操作覆盖，失败重建 |
| CacheRequest | 当前 `uia_service.py:_native_view_diagnostics` | BuildCache walker + 属性缓存 | UIAutomationCore | 普通 Walker | `CODE_PRESENT` | `VERIFIED_ON_CURRENT_MACHINE=PASS` | 仅诊断路径使用，未用于生产 reader | 对会话/消息 reader 逐步引入，遇 provider 错误回退 |
| UIA events | 当前 `uia_service.py:_probe_event_subscription` | 注册 → 移除临时 handler | COM event handler | polling | `CODE_PRESENT` | `VERIFIED_ON_CURRENT_MACHINE=PASS`（注册并移除探针） | 非常驻，不能等价于实时监听 | 生产继续 polling fallback，避免泄漏 |

## 当前实测快照

证据文件：[reference_parity_audit.json](__ROOT__/docs/evidence/reference_parity_audit.json)

- `Weixin.dll`：`C:\Program Files\Tencent\Weixin\4.1.13.12\Weixin.dll`
- 主窗：`mmui::MainWindow`，HWND `329920`，PID `5064`
- Qt accessibility：RVA `181507688`，read-back `active=1`
- ControlView：7 个 `mmui::ChatSessionCell`
- RawView/ContentView：均成功遍历，但本次计数为 0
- CacheRequest：`PASS`，Subtree + 6 个属性
- UIA events：结构变化/Name 属性事件均 `REGISTERED_AND_REMOVED`
- 当前 agent：Session 1，dedicated MTA worker，`uia_ready=true`

## Parity 计算

稳定核心集合按 12 项计算：热激活、mmui 树、会话列表、可见搜索、当前会话校验、消息读取、UIA worker、COM threading、CacheRequest、UIA events、去重监听、发送结果验证。

2026-09-04 本机明确通过 4 项（热激活、mmui 树、会话列表、UIA worker/COM/CacheRequest/events 作为一组基础可靠性能力不重复计数），其余核心能力未完成真实验证，因此当前保守 parity 为 **33.3%（4/12）**。该百分比不把参考项目文档声明、实验性 Moments、OCR/坐标前台回退或未解密数据库计入 PASS。

## 真实阻塞

1. `SEARCH→OPEN→VERIFY` 在当前微信版本仍未取得无歧义的真实切换证据。
2. 数据库 key/SQLCipher read plane 未验证，导致历史消息、全量联系人/群、媒体读取不能宣称可用。
3. 发送路径本质需要前台交互；当前发送结果保持 `SEND_RESULT_NOT_OBSERVABLE`，不自动重发。
4. 参考项目的 OCR/坐标 fallback 不能直接移植到被动后台模式，否则会违反前台安全约束。
