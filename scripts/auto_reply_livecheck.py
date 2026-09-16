"""自动回复的真实链路检查（不发送任何消息）。

用**真实配置 + 真实数据库历史 + 真实 LLM**跑一遍引擎，只是把发送出口换成
一个只记录不发送的假出口。用来回答一个问题：

    「如果好友A现在发来一句话，引擎会回什么？」

用法：
    python scripts\auto_reply_livecheck.py                  # 默认 dry（绝不发送）
    python scripts\auto_reply_livecheck.py --text "在吗"     # 换一条模拟来信
    python scripts\auto_reply_livecheck.py --rule 好友A     # 换目标
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.auto_reply import AutoReplyEngine, load_policy   # noqa: E402
from src.auto_reply.policy import TargetRule              # noqa: E402
from src.providers import DatabaseDataProvider            # noqa: E402


class RecordingSender:
    """假发送出口：只记录，绝不调用 WeChatService.send_message。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.allow_real = False

    def __call__(self, recipient: str, text: str) -> dict:
        if self.allow_real:
            raise RuntimeError("livecheck 不允许真实发送")
        self.calls.append((recipient, text))
        return {"success": True, "sent": True, "result_state": "DRY_RUN_LOCAL",
                "recipient": recipient, "duration_ms": 0, "error": None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rule", default=None, help="targets[].name；默认取第一个")
    parser.add_argument("--text", default="在干嘛呢？", help="模拟的好友来信")
    parser.add_argument("--show-history", type=int, default=6,
                        help="打印多少条真实历史（0 表示不打印）")
    args = parser.parse_args()

    policy = load_policy()
    print(f"config     : {policy.config_path}")
    print(f"enabled    : {policy.enabled}   dry_run(config)={policy.dry_run}")
    print(f"targets    : {[(t.name, t.aliases) for t in policy.targets]}")
    if not policy.enabled:
        print("\n配置里 enabled=false，引擎不会动作。")
        return 1

    rules = [t for t in policy.targets if t.enabled]
    if not rules:
        print("\n没有启用的 target。")
        return 1
    rule = next((t for t in rules if t.name == args.rule), rules[0]) if args.rule else rules[0]
    key = rule.aliases[0] if rule.aliases else rule.name
    print(f"rule       : name={rule.name!r} alias={rule.aliases}  -> 会话键 {key!r}")

    provider = DatabaseDataProvider()
    adapter = getattr(provider, "adapter", None)
    self_ids = []
    try:
        if adapter is not None:
            self_ids = list(adapter.discover_accounts() or [])
    except Exception as exc:  # noqa: BLE001
        print(f"discover_accounts failed: {exc}")
    print(f"self_ids   : {self_ids}")

    rows = provider.get_messages(key, limit=max(args.show_history, policy.history_limit))
    if args.show_history:
        from src.auto_reply.engine import normalize_account_id

        mine = {normalize_account_id(x) for x in self_ids}
        print(f"\n最近 {min(args.show_history, len(rows))} 条真实消息（{key}）:")
        for row in reversed(rows[:args.show_history]):
            who = "我" if normalize_account_id(row.get("sender_id")) in mine else "他"
            print(f"  [{who}] {row.get('message_type'):8} {str(row.get('content'))[:60]!r}")
    if not rows:
        print("\n读不到该会话历史 —— 检查 WECHAT_FILES_BASE / 数据库密钥。")
        return 1

    sender = RecordingSender()
    engine = AutoReplyEngine(
        service=provider, policy=policy, self_ids=self_ids,
        send=sender, history=lambda chat_id, limit: provider.get_messages(chat_id, limit),
        log_path=str(ROOT / "logs" / "auto_reply_livecheck.jsonl"), workers=0,
    )
    # 让引擎在 dry_run 下记录决策文本，而不是真的走发送分支。
    engine.policy = AutoReplyPolicy_with_dry_run(policy)

    event = {
        "event": "message.new", "source": "livecheck",
        "conversation_id": key, "chat_id": key, "chat_name": key, "chat_type": "direct",
        "message_id": f"livecheck-{int(time.time())}",
        "sender_id": rule.aliases[0] if rule.aliases else key,
        "sender_name": rule.name, "message_type": "text",
        "content": args.text, "timestamp": int(time.time()),
    }
    print(f"\n模拟来信   : sender_id={event['sender_id']!r} content={args.text!r}")
    decision = engine.on_event(event)
    print(f"判定       : action={decision.action} reason={decision.reason} rule={decision.rule}")
    if decision.action != "queued":
        print("\n没有进入生成阶段，原因见上。")
        return 1

    engine._process(key)
    final = engine.decisions[-1] if engine.decisions else None
    print("\n=== 结果 ===")
    if final is None:
        print("没有任何决策记录。")
        return 1
    print(f"action     : {final.action}")
    print(f"reason     : {final.reason}")
    print(f"inbound    : {final.inbound}")
    print(f"reply      : {final.reply!r}")
    print(f"send       : {json.dumps(final.send, ensure_ascii=False)}")
    print(f"llm        : {json.dumps(engine.status()['llm'], ensure_ascii=False)}")
    print(f"\n假出口记录 : {sender.calls}")
    print("\n注意：本次没有调用任何发送路径，微信里不会出现任何消息。")
    return 0


def AutoReplyPolicy_with_dry_run(policy):
    """复制一份策略，强制 dry_run=True（livecheck 永不发送）。"""
    from dataclasses import replace

    return replace(policy, dry_run=True)


if __name__ == "__main__":
    raise SystemExit(main())
