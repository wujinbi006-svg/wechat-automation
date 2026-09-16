# WeChat 4.1.13.12 Key Discovery 诊断报告

日期：2026-09-02

## 结果

本轮只读审计确认：参考实现只通过 `tasklist` 查找 `Weixin.exe`，随后对该进程的所有符合保护条件的内存区域扫描字符串 `com.Tencent.WCDB.Config.Cipher`，再按固定指针/偏移提取候选。它没有专门限定 `Weixin.dll`，也没有枚举 `WeChatAppEx`。

本机结果见 `docs/evidence/key_module_inventory.json` 与 `key_scan_current.json`。本次参考扫描仍为 `valid_key_count=0`；未输出真实 key。

## 当前阻塞点

`KEY_DISCOVERY_BLOCKED`：现有证据还不能区分是进程目标、字符串/pattern 不匹配、固定结构偏移变化，还是访问权限导致。`pattern_match_count` 与 `candidate_count` 在原实现中未暴露，本轮没有伪造这些数字，记录为 null。

## 版本差异

参考包元数据宣称支持 WeChat 4.1.12+，但源码的进程筛选和固定节点偏移仍可能随 4.1.13.12 布局变化而失效。当前尚未证明 `Weixin.dll` 版本布局与参考验证环境一致。

## 下一步

优先增加“有限、只读、按进程分别计数”的诊断扫描：只统计 anchor 命中、指针对命中和候选验证数量，不保存匹配区域内容；然后再决定是扩展进程范围还是更新结构解析。不要先改数据库适配器。
