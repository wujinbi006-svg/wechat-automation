"""AI 自动回复子系统。

事件来源是 Gateway 的 WAL 事件流（真实数据库写入），发送出口复用
``WeChatService.send_message`` —— 也就是同一条「搜索 → 唯一性校验 →
打开会话 → 校验当前会话 → 发送 → 验证」的安全路径，不新增任何
UIA 或注入代码。

模块划分：
    policy.py  ── 白名单 / 限速 / 静默时段 / 人类接管
    llm.py     ── OpenAI 兼容 Chat Completions 客户端
    engine.py  ── 事件判定 + 上下文组装 + 生成 + 发送编排
"""
from .policy import AutoReplyPolicy, TargetRule, load_policy          # noqa: F401
from .llm import ChatLLM, LLMError                                     # noqa: F401
from .engine import AutoReplyEngine, Decision, normalize_account_id    # noqa: F401

__all__ = [
    "AutoReplyPolicy",
    "TargetRule",
    "load_policy",
    "ChatLLM",
    "LLMError",
    "AutoReplyEngine",
    "Decision",
    "normalize_account_id",
]
