# 第三方组件声明

本工程包含第三方组件。它们**不适用**本工程的 MIT 许可，各自保留原许可。
本文件同时履行 Apache License 2.0 第 4(b)、4(c) 条要求的
「修改声明」与「保留归属声明」义务。

---

## 1. wechatauto-replica（微信 UIA 驱动）

**这是本工程唯一被修改过的第三方组件，请特别注意第 4 节。**

| 项 | 内容 |
|---|---|
| 名称 | wechatauto-replica |
| 作者 | fanyuantaier |
| 仓库 | https://github.com/fanyuantaier/wechatauto-replica |
| 联系方式 | fanyuantaier@163.com |
| 版本 | 1.2.2.2 |
| 许可 | **Apache License 2.0** |
| 许可证副本 | [`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt) |
| 本工程中的位置 | `work/wechatauto_pkg/unzipped/wechatauto/` |

### 1.1 上游声明（原文照录）

> **Apache-2.0.** This project is for personal learning and automation research
> only — please respect the WeChat software license agreement and applicable laws.

上游项目描述：

> Windows WeChat automation for Windows (non-web), wxauto replica supporting
> WeChat 4.x - Windows 微信客户端自动化（非网页版）复刻版

### 1.2 本工程如何使用它

仅作为**运行时依赖**通过 `sys.path` 导入，用于：

- 读取 `mmui::` 控件树（`wechatauto.uia_driver.WeChatUIA`）
- 定位搜索框、会话列表、聊天输入框

本工程**自己的**数据库解密、事件流、发送编排、自动回复引擎均在 `src/`
下独立实现，不来自该组件。

### 1.3 为何打包而不通过 pip 安装

上游项目处于低频维护状态（作者高中在读，仅周日可能更新）。
本工程将 1.2.2.2 版本随包分发，以保证部署可复现——
pip 拉取最新版可能导致控件锚点不匹配。

---

## 2. ⚠️ 对 wechatauto 的修改声明

依据 Apache License 2.0 第 **4(b)** 条：*"You must cause any modified files to
carry prominent notices stating that You changed the files."*

**本工程修改了以下文件，特此声明：**

### `work/wechatauto_pkg/unzipped/wechatauto/uia_driver.py`

修改者：Small-tailqwq · 修改日期：2026-09-15

| # | 改动 | 原因 |
|---|---|---|
| 1 | `_paste_into()` 增加 `ValuePattern.SetValue` 后台写入路径，并加 `WECHAT_INPUT_MODE` 开关 | 原实现用 `Click()` + `SendKeys(Ctrl+V)`，会移动真实光标并抢占前台 |
| 2 | 新增 `_press_enter()`，默认用 `PostMessage` 投递回车替代 `SendKeys("{Enter}")`，加 `WECHAT_SEND_MODE` 开关 | 同上，`SendKeys` 依赖前台窗口 |
| 3 | 新增 `_capture_foreground()` / `_restore_foreground()`，在 `send_text()` 前后记录并还原前台窗口 | 微信 Qt 输入框在值变化时会自行置前，需把它还回去 |
| 4 | 新增 `_click_element()`，点击后立即还原用户光标位置 | 微信只认真实鼠标点击，但不应留下光标位移 |
| 5 | `ensure_window()` 增加 `WECHAT_NO_FOCUS` 开关，默认不再调用 `_activate()` | 拿到控件树即可操作，无需把窗口置前 |
| 6 | 新增 `_logical_to_physical()`，按 DPI 比例换算点击坐标 | 未声明 DPI 感知时 UIA 坐标与鼠标坐标相差一个缩放因子 |

所有改动均以环境变量提供回退开关，设为 `keyboard` / `0` 即可恢复上游行为。

### `work/wechatauto_pkg/unzipped/wechatauto/` 其它文件

**未修改**。除 `uia_driver.py` 外，该组件其余文件保持上游原样。

---

## 3. WinSW

| 项 | 内容 |
|---|---|
| 名称 | WinSW (Windows Service Wrapper) |
| 作者 | WinSW contributors |
| 仓库 | https://github.com/winsw/winsw |
| 许可 | **MIT License** |
| 本工程中的位置 | `service/WinSW.exe`、`service/WeChatGateway.exe` |

说明：`service/WeChatGateway.exe` 是 `WinSW.exe` 的副本，重命名以匹配服务
ID（WinSW 的常规用法）。两者内容相同。

本工程**未修改** WinSW 源码，仅使用其官方发布的二进制。

---

## 4. wxauto（间接参考）

wechatauto-replica 是 wxauto 在微信 4.x 上的复刻。上游 wxauto：

| 项 | 内容 |
|---|---|
| 名称 | wxauto |
| 作者 | cluic |
| 仓库 | https://github.com/cluic/wxauto |
| 许可 | **Apache License 2.0** |

本工程**未直接包含 wxauto 的任何文件**，此处列出仅为标明谱系。

---

## 5. 未包含的第三方内容

以下内容**不在**本工程分发包内，部署时需要你自行获取或安装：

| 内容 | 许可 | 获取方式 |
|---|---|---|
| Python 解释器 | PSF License | python.org |
| Python 依赖包 | 各自许可 | `pip install -r requirements.txt` |
| WeChat 客户端 | 腾讯专有 | 官方渠道 |
| zig（仅编译 hook 时需要） | MIT | ziglang.org |
| 取钥工具（可选） | 各自许可 | 见 `SECRETS.md` |

各依赖包的许可见其各自的发行说明。

---

## 6. 合规要点复述

使用或再分发本工程时：

1. **保留本文件与 `LICENSE`**——MIT 与 Apache-2.0 的共同要求
2. **保留 `licenses/Apache-2.0.txt`**——Apache-2.0 第 4(a) 条
3. **若你再次修改 `uia_driver.py`，请继续标注你的改动**——第 4(b) 条
4. **不要移除上游的归属声明**——第 4(c) 条
5. **注意上游的用途限制**：仅供个人学习与自动化研究，
   需遵守微信软件许可协议与当地法律

---

## 7. 免责

本工程按「原样」提供，不对适销性、特定用途适用性或非侵权性作任何担保。
使用者自行承担全部风险。详见 `LICENSE` 与 `SECURITY.md`。
