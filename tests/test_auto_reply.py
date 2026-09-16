"""AI 自动回复引擎的 pytest 覆盖。

全部离线：假 LLM、假发送出口、workers=0 由测试同步驱动。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.auto_reply import AutoReplyEngine, AutoReplyPolicy, TargetRule  # noqa: E402
from src.auto_reply.engine import normalize_account_id                   # noqa: E402
from src.auto_reply.llm import LLMError, sanitize_reply                   # noqa: E402
from src.auto_reply.policy import validate_policy                        # noqa: E402

FRIEND = "wxid_friend001"
FRIEND_NAME = "小李"
ME = "wxid_me"


class FakeLLM:
    base_url = "https://fake.invalid/v1"
    model = "fake-model"
    configured = True

    def __init__(self, text: str = "好的"):
        self.text = text
        self.calls: list = []

    def reply(self, system, messages, *, max_chars=300):
        self.calls.append(messages)
        return sanitize_reply(self.text, max_chars=max_chars)


class FakeSends:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.result: dict = {"success": True, "sent": True, "result_state": "SENT_VERIFIED"}

    def __call__(self, recipient, text):
        self.sent.append((recipient, text))
        return {**self.result, "recipient": recipient}


DEFAULT_REPLY = "在打游戏呢，你呢？"


def make_engine(**overrides):
    llm_text = overrides.pop("llm_text", DEFAULT_REPLY)
    data = dict(
        enabled=True, dry_run=False, debounce_seconds=0.0, debounce_max_wait_seconds=0.0,
        min_delay_seconds=0.0, max_delay_seconds=0.0, min_interval_seconds=0.0,
        quiet_hours=[], human_override_minutes=15.0,
        targets=[TargetRule(name=FRIEND_NAME, enabled=True, max_chars=100)],
    )
    data.update(overrides)
    policy = AutoReplyPolicy(**data)
    validate_policy(policy)
    sends = FakeSends()
    llm = FakeLLM(llm_text)
    engine = AutoReplyEngine(
        service=object(), policy=policy, llm=llm, self_ids=[ME],
        log_path=tempfile.mktemp(suffix=".jsonl"),
        send=sends, history=lambda chat_id, limit: [], workers=0,
    )
    return engine, sends, llm


def inbound(text, *, mid="", sender=FRIEND, chat_type="direct", chat_id=FRIEND,
            name=FRIEND_NAME, message_type="text", timestamp=None):
    return {
        "event": "message.new", "source": "pytest", "conversation_id": chat_id,
        "chat_id": chat_id, "chat_name": name, "chat_type": chat_type,
        "message_id": mid or None, "sender_id": sender, "sender_name": sender,
        "message_type": message_type, "content": text,
        "timestamp": time.time() if timestamp is None else timestamp,
    }


def test_disabled_engine_never_sends():
    engine, sends, _ = make_engine(enabled=False)
    decision = engine.on_event(inbound("在吗", mid="1"))
    assert decision.action == "skipped"
    assert decision.reason == "auto_reply_disabled"
    engine._process(FRIEND)
    assert sends.sent == []


def test_whitelisted_inbound_is_answered_once():
    engine, sends, llm = make_engine()
    decision = engine.on_event(inbound("明天有空吗", mid="1"))
    assert decision.action == "queued"
    engine._process(FRIEND)
    assert len(sends.sent) == 1
    assert sends.sent[0][0] == FRIEND_NAME
    assert engine.decisions[-1].action == "sent"
    assert llm.calls


def test_non_whitelisted_is_ignored():
    engine, sends, _ = make_engine()
    decision = engine.on_event(inbound("你好", mid="1", sender="wxid_other",
                                       chat_id="wxid_other", name="陌生人"))
    assert decision.reason == "not_whitelisted"
    assert sends.sent == []


def test_own_message_reads_as_outbound_and_triggers_takeover():
    engine, sends, _ = make_engine()
    decision = engine.on_event(inbound("我自己发的", mid="1", sender=ME))
    assert decision.action == "takeover"
    assert engine.counters["takeover"] == 1
    assert engine.on_event(inbound("好友说话", mid="2")).reason == "owner_takeover_active"
    assert sends.sent == []
    assert engine.resume(FRIEND) is True
    assert engine.on_event(inbound("继续聊", mid="3")).action == "queued"


def test_duplicate_message_id_is_dropped():
    engine, sends, _ = make_engine()
    engine.on_event(inbound("第一条", mid="dup"))
    engine._process(FRIEND)
    assert len(sends.sent) == 1
    assert engine.on_event(inbound("第一条", mid="dup")).reason == "duplicate_message_id"
    engine._process(FRIEND)
    assert len(sends.sent) == 1


def test_non_text_and_empty_content_are_skipped():
    engine, sends, _ = make_engine()
    assert engine.on_event(inbound("图", mid="1", message_type="image")).reason == "non_text_message"
    assert engine.on_event(inbound("   ", mid="2")).reason == "non_text_message"
    assert sends.sent == []


def test_burst_is_merged_into_one_reply():
    engine, sends, llm = make_engine()
    for index in range(4):
        engine.on_event(inbound(f"消息{index}", mid=f"b{index}"))
    engine._process(FRIEND)
    assert len(sends.sent) == 1
    assert len(llm.calls) == 1
    assert all(f"消息{i}" in llm.calls[0][-1]["content"] for i in range(4))


def test_group_requires_opt_in():
    engine, sends, _ = make_engine(targets=[TargetRule(name="某群", enabled=True)])
    decision = engine.on_event(inbound("群消息", mid="1", chat_type="group",
                                       chat_id="1@chatroom", name="某群", sender="wxid_other"))
    assert decision.reason == "group_not_allowed"
    assert sends.sent == []


def test_group_opted_in_replies_to_member_but_not_to_self():
    engine, sends, _ = make_engine(
        targets=[TargetRule(name="某群", enabled=True, allow_group=True)])
    assert engine.on_event(inbound("群消息", mid="1", chat_type="group",
                                   chat_id="1@chatroom", name="某群",
                                   sender="wxid_member")).action == "queued"
    engine._process("1@chatroom")
    assert len(sends.sent) == 1
    assert engine.on_event(inbound("我也说一句", mid="2", chat_type="group",
                                   chat_id="1@chatroom", name="某群",
                                   sender=ME)).action != "queued"


def test_peer_burst_mutes_conversation():
    engine, _, _ = make_engine(bot_burst_count=4, bot_burst_seconds=30.0)
    for index in range(4):
        engine.on_event(inbound(f"刷屏{index}", mid=f"f{index}"))
    assert engine._mute_reason[FRIEND] == "peer_bot_burst"
    assert engine.on_event(inbound("还在刷", mid="f9")).reason.startswith("muted:")


def test_consecutive_loop_guard():
    engine, sends, _ = make_engine(max_consecutive_auto_replies=2)
    for index in range(2):
        engine.on_event(inbound(f"第{index}轮", mid=f"c{index}"))
        engine._process(FRIEND)
    assert len(sends.sent) == 2
    assert engine.on_event(inbound("第三轮", mid="c3")).reason == "muted:max_consecutive"


def test_conversation_rate_limit():
    engine, _, _ = make_engine(max_replies_per_conversation=2)
    for index in range(2):
        engine.on_event(inbound(f"限速{index}", mid=f"r{index}"))
        engine._process(FRIEND)
    assert engine.on_event(inbound("再来", mid="r9")).reason == "muted:rate_limit_conversation"


def test_global_rate_limit():
    engine, _, _ = make_engine(max_replies_global_per_minute=1)
    engine.on_event(inbound("一", mid="g1"))
    engine._process(FRIEND)
    assert engine.on_event(inbound("二", mid="g2")).reason == "rate_limit_global"


def test_dry_run_never_calls_sender():
    engine, sends, _ = make_engine(dry_run=True)
    engine.on_event(inbound("试运行", mid="1"))
    engine._process(FRIEND)
    assert sends.sent == []
    assert engine.decisions[-1].action == "dry_run"
    assert engine.decisions[-1].reply


def test_send_failure_is_recorded_not_raised():
    engine, sends, _ = make_engine()
    sends.result = {"success": False, "error": {"code": "TARGET_NOT_FOUND"}}
    engine.on_event(inbound("会失败", mid="1"))
    engine._process(FRIEND)
    assert engine.decisions[-1].action == "failed"
    assert "TARGET_NOT_FOUND" in engine.decisions[-1].reason
    assert engine.counters["failed"] == 1


def test_llm_failure_means_no_message_is_sent():
    engine, sends, llm = make_engine()

    def boom(*_args, **_kwargs):
        raise LLMError("network down")

    llm.reply = boom
    engine.on_event(inbound("LLM 挂", mid="1"))
    engine._process(FRIEND)
    assert sends.sent == []
    assert engine.decisions[-1].action == "failed"
    assert engine.counters["llm_errors"] == 1


def test_quiet_hours_window_crossing_midnight():
    policy = AutoReplyPolicy(enabled=True, quiet_hours=[["23:30", "08:00"]])
    assert policy.in_quiet_hours(23 * 60 + 59)
    assert policy.in_quiet_hours(3 * 60)
    assert policy.in_quiet_hours(7 * 60 + 59)
    assert not policy.in_quiet_hours(8 * 60)
    assert not policy.in_quiet_hours(12 * 60)
    assert not policy.in_quiet_hours(23 * 60 + 29)


def test_quiet_hours_blocks_dispatch():
    engine, sends, _ = make_engine(quiet_hours=[["00:00", "23:59"]])
    assert engine.on_event(inbound("半夜", mid="1")).reason == "quiet_hours"
    assert sends.sent == []


def test_target_matches_by_wxid_or_remark_name():
    by_name = AutoReplyPolicy(enabled=True, targets=[TargetRule(name=FRIEND_NAME)])
    assert by_name.match(conversation_id=FRIEND, chat_name=FRIEND_NAME) is not None
    by_id = AutoReplyPolicy(enabled=True, targets=[TargetRule(name=FRIEND)])
    assert by_id.match(conversation_id=FRIEND, chat_name=FRIEND_NAME) is not None
    assert by_name.match(conversation_id="wxid_x", chat_name="别人") is None


def test_direction_rules():
    engine, _, _ = make_engine()
    assert engine._direction({"sender_id": FRIEND, "conversation_id": FRIEND}, "direct") == "inbound"
    assert engine._direction({"sender_id": ME, "conversation_id": FRIEND}, "direct") == "outbound"
    assert engine._direction({"sender_id": "wxid_member",
                              "conversation_id": "1@chatroom"}, "group") == "inbound"
    assert engine._direction({"sender_id": None, "conversation_id": FRIEND}, "direct") == "unknown"

    naive, _, _ = make_engine(assume_inbound_when_unknown=True)
    assert naive._direction({"sender_id": None, "conversation_id": FRIEND}, "direct") == "inbound"

    no_self = AutoReplyEngine(service=object(), policy=make_engine()[0].policy,
                              llm=FakeLLM(), self_ids=[], send=FakeSends(),
                              history=lambda *_: [], workers=0)
    assert no_self._direction({"sender_id": FRIEND, "conversation_id": FRIEND}, "direct") == "inbound"
    assert no_self._direction({"sender_id": ME, "conversation_id": FRIEND}, "direct") == "outbound"
    assert no_self._direction({"sender_id": "wxid_member",
                               "conversation_id": "1@chatroom"}, "group") == "unknown"


def test_reply_echo_is_not_treated_as_friend_message():
    engine, sends, _ = make_engine()
    engine.on_event(inbound("你好", mid="1"))
    engine._process(FRIEND)
    assert len(sends.sent) == 1
    reply = sends.sent[0][1]
    # 自己发的回声：sender_id 缺失时也不能当成好友来信。
    assert engine.on_event(inbound(reply, mid="2", sender=None)).reason == "own_reply_echo"
    # 带回自身 sender_id 时同样被丢在回声判定里，绝不触发「机主接管」。
    assert engine.on_event(inbound(reply, mid="3", sender=ME)).reason == "own_reply_echo"
    assert engine.counters["takeover"] == 0
    engine._process(FRIEND)
    assert len(sends.sent) == 1


def test_sanitize_reply_variants():
    assert sanitize_reply('"你好呀"', max_chars=50) == "你好呀"
    assert sanitize_reply("好的\n解释：因为", max_chars=50) == "好的"
    assert sanitize_reply("a   b", max_chars=50) == "a b"
    assert sanitize_reply("   ", max_chars=50) == ""
    assert sanitize_reply("x" * 40, max_chars=10).endswith("…")


def test_invalid_policies_are_rejected():
    with pytest.raises(ValueError):
        validate_policy(AutoReplyPolicy(min_delay_seconds=5, max_delay_seconds=1))
    with pytest.raises(ValueError):
        validate_policy(AutoReplyPolicy(max_replies_per_conversation=0))
    with pytest.raises(ValueError):
        validate_policy(AutoReplyPolicy(max_consecutive_auto_replies=0))
    with pytest.raises(ValueError):
        validate_policy(AutoReplyPolicy(quiet_hours=[["25:00", "08:00"]]))
    with pytest.raises(ValueError):
        AutoReplyPolicy.from_dict({"enabled": True, "no_such_key": 1})


def test_status_surface_is_json_safe():
    engine, _, _ = make_engine(dry_run=True)
    engine.on_event(inbound("状态", mid="1"))
    engine._process(FRIEND)
    status = engine.status()
    import json
    json.dumps(status)  # 不抛异常即可
    assert status["enabled"] is True
    assert status["dry_run"] is True
    assert status["counters"]["dry_run"] == 1


# --------------------------------------------------------------- 账号 ID 归一化
# 真实数据：discover_accounts() 返回 wxid_EXAMPLE_ACCOUNT（账号目录名），
# 而消息表 real_sender_id 映射出的是 wxid_EXAMPLE。不归一化会把机主
# 自己的发言判成好友来信，机器人于是回给自己。


def test_normalize_account_id_strips_account_dir_suffix():
    assert normalize_account_id("wxid_EXAMPLE_ACCOUNT") == "wxid_EXAMPLE"
    assert normalize_account_id("wxid_EXAMPLE_ACCOUNT") == "wxid_EXAMPLE"
    assert normalize_account_id("wxid_EXAMPLE_FRIEND_A") == "wxid_EXAMPLE_FRIEND_A"
    assert normalize_account_id("  好友A  ") == "好友A"
    assert normalize_account_id(None) == ""


def test_self_message_is_outbound_even_with_account_dir_suffix():
    engine, sends, _ = make_engine()
    engine._self_ids = {normalize_account_id("wxid_me_EXAMPLE")}
    assert engine._self_ids == {"wxid_me"}
    # 消息里的 sender 是纯 wxid，账号列表里的带目录后缀：必须判为 outbound。
    assert engine._direction({"sender_id": "wxid_me", "conversation_id": FRIEND},
                             "direct") == "outbound"
    decision = engine.on_event(inbound("这是我自己发的", mid="1", sender="wxid_me"))
    assert decision.action == "takeover"
    engine._process(FRIEND)
    assert sends.sent == []


def test_prompt_history_marks_own_messages_as_assistant():
    engine, _, _ = make_engine()
    engine._self_ids = {"wxid_me"}
    history = [
        {"sender_id": "wxid_me", "content": "我说的话", "message_type": "text",
         "sort_seq": 1, "chat_type": "direct"},
        {"sender_id": FRIEND, "content": "他说的话", "message_type": "text",
         "sort_seq": 2, "chat_type": "direct"},
    ]
    engine._history_impl = lambda chat_id, limit: history
    messages = engine._build_prompt(FRIEND, engine.policy.targets[0], ["最后一个问题"])
    roles = [(m["role"], m["content"]) for m in messages]
    assert ("assistant", "我说的话") in roles
    assert any("他说的话" in content for _, content in roles)
    assert messages[-1]["role"] == "user"


# --------------------------------------------------------------- 别名匹配
# 真实数据：WAL 事件的 chat_name 就是 wxid，备注名拿不到。只按备注名匹配会
# 整个漏掉，所以 targets 要能声明 aliases（这里是 wxid）。


def test_target_alias_matches_wxid_when_chat_name_is_the_wxid():
    rule = TargetRule(name="好友A", aliases=["wxid_EXAMPLE_FRIEND_A"])
    policy = AutoReplyPolicy(enabled=True, targets=[rule])
    assert policy.match(conversation_id="wxid_EXAMPLE_FRIEND_A",
                        chat_name="wxid_EXAMPLE_FRIEND_A") is not None
    assert policy.match(conversation_id="wxid_EXAMPLE_FRIEND_A", chat_name="好友A") is not None
    assert policy.match(conversation_id="wxid_other", chat_name="别人") is None


def test_alias_target_sends_to_the_configured_name():
    rule = TargetRule(name="好友A", aliases=["wxid_EXAMPLE_FRIEND_A"])
    engine, sends, _ = make_engine(targets=[rule])
    engine.on_event(inbound("在吗", mid="1", sender="wxid_EXAMPLE_FRIEND_A",
                            chat_id="wxid_EXAMPLE_FRIEND_A", name="wxid_EXAMPLE_FRIEND_A"))
    engine._process("wxid_EXAMPLE_FRIEND_A")
    assert len(sends.sent) == 1
    # 发给 UIA 搜索框的必须是备注名，不是 wxid。
    assert sends.sent[0][0] == "好友A"


# --------------------------------------------------------------- 回声容错
# 微信落库的文本可能与引擎发出的不完全一致。漏判回声 → 引擎把自己的回复
# 当成「机主接管」，每次回复后自锁 15 分钟。所以比对必须宽松。


def test_echo_match_tolerates_whitespace_and_case():
    engine, sends, _ = make_engine()
    engine.on_event(inbound("你好", mid="1"))
    engine._process(FRIEND)
    reply = sends.sent[0][1]
    assert reply == DEFAULT_REPLY
    for index, variant in enumerate(
            (f"  {reply}  ", reply.replace("，", "， "), reply.upper())):
        assert engine.on_event(
            inbound(variant, mid=f"e{index}", sender=ME)).reason == "own_reply_echo", variant
    assert engine.counters["takeover"] == 0


def test_echo_match_tolerates_trailing_marker():
    engine, sends, _ = make_engine()
    engine.on_event(inbound("你好", mid="1"))
    engine._process(FRIEND)
    reply = sends.sent[0][1]
    # 落库时多了个尾注 / 表情标记，前缀仍然相同。
    assert engine.on_event(
        inbound(reply + " [微笑]", mid="e2", sender=ME)).reason == "own_reply_echo"
    assert engine.counters["takeover"] == 0


def test_short_manual_message_is_not_swallowed_by_prefix_rule():
    engine, sends, _ = make_engine()
    engine.on_event(inbound("你好", mid="1"))
    engine._process(FRIEND)
    # 机主真正打的一句短消息，不能被前缀规则吞掉。
    decision = engine.on_event(inbound("好的", mid="h1", sender=ME))
    assert decision.action == "takeover"
    assert engine.counters["takeover"] == 1


# --------------------------------------------------------------- 重放防护
# Gateway 重启后事件源会从游标 0 重新投递缓冲区里的旧消息。不挡的话，重启
# 一次就可能对着几小时前的旧消息回一句。


def test_stale_replayed_message_is_dropped():
    engine, sends, _ = make_engine()
    old = time.time() - 3600
    decision = engine.on_event(inbound("一小时前的旧消息", mid="old1", timestamp=old))
    assert decision.action == "skipped"
    assert decision.reason == "stale_replay"
    engine._process(FRIEND)
    assert sends.sent == []


def test_fresh_message_is_not_treated_as_stale():
    engine, sends, _ = make_engine()
    decision = engine.on_event(inbound("刚发的", mid="new1", timestamp=time.time() - 5))
    assert decision.action == "queued"


def test_message_without_timestamp_is_accepted():
    engine, sends, _ = make_engine()
    event = inbound("没有时间戳", mid="nt1")
    event.pop("timestamp")
    assert engine.on_event(event).action == "queued"


def test_replay_grace_is_configurable():
    engine, sends, _ = make_engine(replay_grace_seconds=7200.0)
    decision = engine.on_event(inbound("一小时前", mid="old2", timestamp=time.time() - 3600))
    assert decision.action == "queued", decision.reason
