"""自动回复策略。

默认全关。只有 ``config/auto_reply.json`` 里 ``enabled=true`` 且目标出现在
``targets`` 白名单里，引擎才会动作；否则任何事件都直接跳过。这样即使
引擎被误接入生产，也不会自己发消息。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "config/auto_reply.json"


@dataclass
class TargetRule:
    """单个好友的自动回复规则。

    ``name`` 是**发送时交给 UIA 搜索框的名字**（备注名或昵称），也是默认的匹配键。
    ``aliases`` 补充其它能命中该会话的键，例如 wxid —— WAL 事件里的
    ``chat_name`` 有时直接就是 wxid，只按备注名匹配会整个漏掉。
    """

    name: str
    enabled: bool = True
    aliases: list[str] = field(default_factory=list)
    persona: str = ""
    system_prompt: str = ""
    max_chars: int = 300
    # 该好友专属的长期记忆：关系、称呼、已知事实、禁忌话题等。
    # 与 persona（说话风格）分开，persona 管「怎么说」，memory 管「说什么/记得什么」。
    memory: str = ""
    # 该好友单独覆盖全局的历史条数；0 表示沿用全局 history_limit。
    history_limit: int = 0
    # 群里不需要 @ 就回复是危险的，默认只处理单聊。
    allow_group: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TargetRule":
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("target rule requires a non-empty name")
        aliases_raw = raw.get("aliases") or []
        if isinstance(aliases_raw, str):
            aliases_raw = [aliases_raw]
        return cls(
            name=name,
            enabled=bool(raw.get("enabled", True)),
            aliases=[str(a).strip() for a in aliases_raw if str(a).strip()],
            persona=str(raw.get("persona") or ""),
            system_prompt=str(raw.get("system_prompt") or ""),
            max_chars=int(raw.get("max_chars") or 300),
            memory=str(raw.get("memory") or ""),
            history_limit=int(raw.get("history_limit") or 0),
            allow_group=bool(raw.get("allow_group", False)),
        )

    def keys(self) -> list[str]:
        """所有能命中该会话的键：发送名 + 别名。"""
        return [self.name, *self.aliases]


@dataclass
class AutoReplyPolicy:
    enabled: bool = False
    dry_run: bool = True
    # 连续多条消息合并成一次回复前的静默等待。
    debounce_seconds: float = 3.0
    debounce_max_wait_seconds: float = 25.0
    # 模拟真人打字节奏的可选随机延迟区间。
    min_delay_seconds: float = 0.5
    max_delay_seconds: float = 2.0
    # 同一会话两条自动回复之间的最小间隔。
    min_interval_seconds: float = 20.0
    # 滑动窗口内的回复上限。
    conversation_window_minutes: float = 30.0
    max_replies_per_conversation: int = 12
    max_replies_global_per_minute: int = 8
    # 静默时段，[["23:30","08:00"]]；跨零点由策略自行处理。
    quiet_hours: list[list[str]] = field(default_factory=list)
    # 用户自己在微信里手动说话了 → 静默这么久，交还控制权。
    human_override_minutes: float = 15.0
    # 防死循环：连续自动回复上限、对面疑似机器人时静默的时长。
    max_consecutive_auto_replies: int = 6
    mute_minutes: float = 60.0
    bot_burst_count: int = 8
    bot_burst_seconds: float = 30.0
    # 拼接到回复末尾的可选签名（例如 "（自动回复）"）。
    signature: str = ""
    history_limit: int = 12
    # 引擎重启后，事件源的缓冲里可能还留着旧消息（sidecar 重启会让游标归零，
    # 整个缓冲区会被重新投递）。比引擎启动时间早这么多的消息一律当成重放丢弃，
    # 否则重启一次就可能对着几小时前的旧消息回一句。
    replay_grace_seconds: float = 120.0
    llm_timeout_seconds: float = 45.0
    llm_temperature: float = 0.8
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 免费路由池会偶发返回空内容；按顺序退到下一个模型。
    llm_fallback_models: list[str] = field(default_factory=list)
    # 单次生成的最大 token 数。0 = 沿用旧行为（max_chars * 3，下限 64、上限 800）。
    #
    # 注意：DeepSeek 的 flash / v4-pro 都是**推理模型**，思考过程与正文共用
    # 这一份预算。max_chars*3 对短回复只有几百 token，思考一旦吃掉全部预算，
    # 接口就会返回 finish_reason=length 且 content 为空——表现为「AI 不回复」。
    # 因此显式给一个足够大的上限更稳。
    llm_max_tokens: int = 0
    # 推理强度：none / minimal / low / medium / high / xhigh / max。
    #
    # 实测（本机 DeepSeek 端点）只有 none 能真正关闭推理：其余档位都会产生
    # 数百个 reasoning token，在预算偏小时照样把正文挤没。
    # 留空表示不发送该参数（沿用服务端默认，通常是开推理）。
    llm_reasoning_effort: str = ""
    system_prompt: str = (
        "你在替机主本人回复微信好友，用机主的口吻、简短、口语、不解释自己是 AI。"
        "只输出要发出去的那条消息正文，不要加引号、不要加前缀、不要多加解释。"
        "不确定的事不要编造具体事实（数字、时间、承诺）；拿不准就回复一句很短的、"
        "留有余地的话。"
    )
    targets: list[TargetRule] = field(default_factory=list)
    # sender_id 缺失时是否仍当作好友来信处理；默认关闭，避免把自己的
    # 发言当成来信而回给自己，形成自问自答。
    assume_inbound_when_unknown: bool = False
    config_path: str = ""

    # ------------------------------------------------------------------ 匹配
    def match(self, *, conversation_id: str, chat_name: str) -> TargetRule | None:
        rid = str(conversation_id or "").strip().casefold()
        rname = str(chat_name or "").strip().casefold()
        for rule in self.targets:
            if not rule.enabled:
                continue
            keys = {k.strip().casefold() for k in rule.keys() if k.strip()}
            if rid and rid in keys:
                return rule
            if rname and rname in keys:
                return rule
        return None

    def in_quiet_hours(self, now_minutes: float) -> bool:
        for window in self.quiet_hours:
            if len(window) != 2:
                continue
            try:
                start = _hhmm(window[0])
                end = _hhmm(window[1])
            except ValueError:
                continue
            if start == end:
                continue
            if start < end:
                if start <= now_minutes < end:
                    return True
            elif now_minutes >= start or now_minutes < end:
                return True
        return False

    # ------------------------------------------------------------------ 载入
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AutoReplyPolicy":
        data = dict(raw or {})
        # 以 _ 开头的键是给示例配置看的注释，不算配置项。
        data = {k: v for k, v in data.items() if not str(k).startswith("_")}
        targets_raw = data.pop("targets", []) or []
        targets: list[TargetRule] = []
        for item in targets_raw:
            if isinstance(item, str):
                targets.append(TargetRule(name=item))
            elif isinstance(item, dict):
                targets.append(TargetRule.from_dict(item))
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown auto_reply config keys: {sorted(unknown)}")
        policy = cls(**data)
        policy.targets = targets
        validate_policy(policy)
        return policy

    @classmethod
    def load(cls, path: str | None = None) -> "AutoReplyPolicy":
        resolved = Path(path or os.getenv("AUTO_REPLY_CONFIG") or DEFAULT_CONFIG_PATH)
        if not resolved.exists():
            policy = cls()
            policy.config_path = str(resolved)
            return policy
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        policy = cls.from_dict(raw)
        policy.config_path = str(resolved)
        return policy


def _hhmm(value: str) -> float:
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad time: {value!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"bad time: {value!r}")
    return hours * 60 + minutes


def validate_policy(policy: AutoReplyPolicy) -> None:
    if policy.debounce_seconds < 0 or policy.debounce_max_wait_seconds < 0:
        raise ValueError("debounce values must be >= 0")
    if policy.min_delay_seconds > policy.max_delay_seconds:
        raise ValueError("min_delay_seconds must be <= max_delay_seconds")
    if policy.max_replies_per_conversation < 1:
        raise ValueError("max_replies_per_conversation must be >= 1")
    if policy.max_consecutive_auto_replies < 1:
        raise ValueError("max_consecutive_auto_replies must be >= 1")
    for window in policy.quiet_hours:
        if len(window) != 2:
            raise ValueError(f"quiet window must have 2 entries: {window!r}")
        _hhmm(window[0])
        _hhmm(window[1])


def load_policy(path: str | None = None) -> AutoReplyPolicy:
    return AutoReplyPolicy.load(path)
