"""AI 自动回复引擎离线自检。

不联网、不调用 WeChat、不发送任何真实消息：
    * LLM 用假的；
    * 发送出口用假的（记录被拦截的收件人和文本）；
    * 工作线程数 0，由测试同步驱动 ``_process``。

运行：
    __PYTHON__ scripts\\auto_reply_selftest.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.auto_reply import AutoReplyEngine, AutoReplyPolicy, TargetRule  # noqa: E402
from src.auto_reply.engine import normalize_account_id                   # noqa: E402
from src.auto_reply.llm import sanitize_reply                            # noqa: E402
from src.auto_reply.policy import validate_policy                        # noqa: E402

FRIEND = "wxid_friend001"
FRIEND_NAME = "小李"
ME = "wxid_me"
OTHER = "wxid_other002"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


class FakeLLM:
    base_url = "https://fake.invalid/v1"
    model = "fake-model"

    def __init__(self) -> None:
        self.configured = True
        self.calls: list[list[dict]] = []
        # 固定回复文本；None 表示按来信内容拼一个。
        self.text: str | None = None

    def reply(self, system, messages, *, max_chars=300):
        self.calls.append([dict(m) for m in messages])
        raw = self.text if self.text is not None else f"好的，{messages[-1]['content']}"
        return sanitize_reply(raw, max_chars=max_chars)


class FakeSends:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_next = False

    def __call__(self, recipient: str, text: str) -> dict:
        if self.fail_next:
            self.fail_next = False
            return {"success": False, "error": {"code": "TARGET_NOT_FOUND", "message": "not found"}}
        self.sent.append((recipient, text))
        return {"success": True, "sent": True, "result_state": "SENT_VERIFIED",
                "recipient": recipient, "duration_ms": 120, "error": None}


def build(policy: AutoReplyPolicy):
    sends = FakeSends()
    llm = FakeLLM()
    engine = AutoReplyEngine(
        service=object(), policy=policy, llm=llm, self_ids=[ME],
        log_path=tempfile.mktemp(suffix=".jsonl"),
        send=sends, history=lambda chat_id, limit: [], workers=0,
    )
    return engine, sends, llm


def inbound(text: str, *, mid: str = "", sender: str = FRIEND, chat_type: str = "direct",
            chat_id: str = FRIEND, name: str = FRIEND_NAME,
            timestamp: float | None = None) -> dict:
    return {
        "event": "message.new", "source": "selftest", "conversation_id": chat_id,
        "chat_id": chat_id, "chat_name": name, "chat_type": chat_type,
        "message_id": mid or None, "sender_id": sender, "sender_name": sender,
        "message_type": "text", "content": text,
        "timestamp": time.time() if timestamp is None else timestamp,
    }


def base_policy(**overrides) -> AutoReplyPolicy:
    data = dict(
        enabled=True, dry_run=False, debounce_seconds=0.0, debounce_max_wait_seconds=0.0,
        min_delay_seconds=0.0, max_delay_seconds=0.0, min_interval_seconds=0.0,
        quiet_hours=[], human_override_minutes=15.0,
        targets=[TargetRule(name=FRIEND_NAME, enabled=True, max_chars=100)],
    )
    data.update(overrides)
    policy = AutoReplyPolicy(**data)
    validate_policy(policy)
    return policy


def main() -> int:
    print("== 1. 默认关闭时不动作 ==")
    engine, sends, _ = build(base_policy(enabled=False))
    d = engine.on_event(inbound("在吗", mid="m1"))
    check("disabled -> skipped", d.action == "skipped" and d.reason == "auto_reply_disabled",
          f"{d.action}/{d.reason}")
    engine._process(FRIEND)
    check("disabled -> no send", sends.sent == [])

    print("== 2. 非白名单不动作 ==")
    engine, sends, _ = build(base_policy())
    d = engine.on_event(inbound("你好", mid="m1", sender=OTHER, chat_id=OTHER, name="陌生人"))
    check("not whitelisted -> skipped", d.reason == "not_whitelisted", d.reason)

    print("== 3. 白名单好友来信 -> 生成并发送 ==")
    engine, sends, llm = build(base_policy())
    d = engine.on_event(inbound("明天有空吃饭吗", mid="m2"))
    check("whitelisted -> queued", d.action == "queued" and d.reason == "queued_for_reply",
          f"{d.action}/{d.reason}")
    engine._process(FRIEND)
    check("first message -> exactly 1 send", len(sends.sent) == 1, str(sends.sent))
    check("sent to configured name", sends.sent and sends.sent[0][0] == FRIEND_NAME, str(sends.sent))
    check("reply generated from LLM", sends.sent and "明天有空吃饭吗" in sends.sent[0][1],
          str(sends.sent))
    check("last action = sent", engine.decisions[-1].action == "sent", engine.decisions[-1].action)

    print("== 4. 不回自己的消息：识别自己发的 ==")
    engine, sends, _ = build(base_policy())
    d = engine.on_event(inbound("我自己说的话", mid="m3", sender=ME))
    check("sender == self -> outbound, no queue", d.action in {"skipped", "takeover"},
          f"{d.action}/{d.reason}")
    check("owner takeover recorded", engine.counters["takeover"] == 1, str(engine.counters))
    engine._process(FRIEND)
    check("no reply to own message", sends.sent == [])

    print("== 5. 机主手动发言后自动静默 ==")
    d = engine.on_event(inbound("好友又说话了", mid="m4"))
    check("during takeover -> skipped", d.reason == "owner_takeover_active", d.reason)
    check("resume clears takeover", engine.resume(FRIEND) is True)
    d = engine.on_event(inbound("接管解除后再说话", mid="m5"))
    check("after resume -> queued", d.action == "queued", f"{d.action}/{d.reason}")

    print("== 6. 重复消息与消息类型过滤 ==")
    engine, sends, _ = build(base_policy())
    engine.on_event(inbound("第一条", mid="dup1"))
    engine._process(FRIEND)
    before = len(sends.sent)
    d = engine.on_event(inbound("第一条", mid="dup1"))
    check("duplicate message_id -> skipped", d.reason == "duplicate_message_id", d.reason)
    engine._process(FRIEND)
    check("duplicate produced no extra send", len(sends.sent) == before, str(sends.sent))
    d = engine.on_event({**inbound("这是一张图", mid="img1"), "message_type": "image"})
    check("non-text -> skipped", d.reason == "non_text_message", d.reason)

    print("== 7. 连发消息合并成一次回复 ==")
    engine, sends, llm = build(base_policy())
    for index in range(4):
        engine.on_event(inbound(f"消息{index}", mid=f"b{index}"))
    engine._process(FRIEND)
    check("burst -> one reply", len(sends.sent) == 1, str(sends.sent))
    check("burst -> all texts in prompt", len(llm.calls) == 1 and
          all(f"消息{i}" in llm.calls[0][-1]["content"] for i in range(4)),
          str(llm.calls[-1:] if llm.calls else None))

    print("== 8. 群聊默认不处理 ==")
    engine, sends, _ = build(base_policy(targets=[TargetRule(name="某群", enabled=True)]))
    d = engine.on_event(inbound("群消息", mid="g1", chat_type="group",
                                chat_id="12345@chatroom", name="某群", sender=OTHER))
    check("group -> skipped", d.reason == "group_not_allowed", d.reason)
    engine_ok, sends_ok, _ = build(base_policy(
        targets=[TargetRule(name="某群", enabled=True, allow_group=True)]))
    d = engine_ok.on_event(inbound("群消息", mid="g2", chat_type="group",
                                   chat_id="12345@chatroom", name="某群", sender=OTHER))
    check("group allowed when opted in -> queued", d.action == "queued", f"{d.action}/{d.reason}")
    engine_ok._process("12345@chatroom")
    check("group opted in -> replied once", len(sends_ok.sent) == 1, str(sends_ok.sent))
    d = engine_ok.on_event(inbound("我自己在群里说", mid="g3", chat_type="group",
                                   chat_id="12345@chatroom", name="某群", sender=ME))
    check("group own message -> no reply", d.action != "queued", f"{d.action}/{d.reason}")

    print("== 9. 防死循环：对面疑似机器人高频刷屏 ==")
    engine, sends, _ = build(base_policy(bot_burst_count=5, bot_burst_seconds=30.0,
                                         mute_minutes=60.0))
    engine.on_event(inbound("刷屏0", mid="f0"))
    for index in range(1, 6):
        engine.on_event(inbound(f"刷屏{index}", mid=f"f{index}"))
    check("peer burst -> muted", FRIEND in engine._mute_reason or
          any(k == "peer_bot_burst" for k in engine._mute_reason.values()),
          str(engine._mute_reason))
    d = engine.on_event(inbound("还在刷", mid="f9"))
    check("muted -> skipped", d.reason.startswith("muted:"), d.reason)
    check("mute is reported in status", "muted_seconds_left" in engine.status())

    print("== 10. 连续自动回复上限 ==")
    engine, sends, _ = build(base_policy(max_consecutive_auto_replies=2))
    for index in range(2):
        engine.on_event(inbound(f"第{index}轮", mid=f"c{index}"))
        engine._process(FRIEND)
    check("two replies sent", len(sends.sent) == 2, str(sends.sent))
    d = engine.on_event(inbound("第三轮", mid="c3"))
    check("third reply blocked by loop guard", d.reason.startswith("muted:max_consecutive"),
          d.reason)

    print("== 11. 窗口限速 ==")
    engine, sends, _ = build(base_policy(max_replies_per_conversation=2,
                                         conversation_window_minutes=30.0))
    for index in range(2):
        engine.on_event(inbound(f"限速{index}", mid=f"r{index}"))
        engine._process(FRIEND)
    d = engine.on_event(inbound("再来一条", mid="r9"))
    check("conversation rate limit -> muted",
          d.reason == "muted:rate_limit_conversation", d.reason)

    print("== 12. 全局限速 ==")
    engine, sends, _ = build(base_policy(max_replies_global_per_minute=1))
    engine.on_event(inbound("全局1", mid="gl1"))
    engine._process(FRIEND)
    d = engine.on_event(inbound("全局2", mid="gl2"))
    check("global rate limit -> skipped", d.reason == "rate_limit_global", d.reason)

    print("== 13. dry_run 不发送 ==")
    engine, sends, _ = build(base_policy(dry_run=True))
    engine.on_event(inbound("试运行", mid="d1"))
    engine._process(FRIEND)
    check("dry_run -> no real send", sends.sent == [], str(sends.sent))
    check("dry_run recorded", engine.decisions[-1].action == "dry_run",
          engine.decisions[-1].action)
    check("dry_run has reply text", bool(engine.decisions[-1].reply), "reply empty")

    print("== 14. 发送失败被捕获 ==")
    engine, sends, _ = build(base_policy())
    sends.fail_next = True
    engine.on_event(inbound("会失败的", mid="sf1"))
    engine._process(FRIEND)
    check("send failure -> failed action", engine.decisions[-1].action == "failed",
          engine.decisions[-1].action)
    check("send failure -> reason carries code",
          "TARGET_NOT_FOUND" in engine.decisions[-1].reason, engine.decisions[-1].reason)
    check("failure counter", engine.counters["failed"] == 1, str(engine.counters))

    print("== 15. LLM 故障 = 这条不回 ==")
    engine, sends, llm = build(base_policy())
    def boom(*_a, **_k):
        raise RuntimeError("network down")
    llm.reply = boom
    engine.on_event(inbound("LLM 挂了", mid="le1"))
    engine._process(FRIEND)
    check("llm error -> failed", engine.decisions[-1].action == "failed",
          engine.decisions[-1].action)
    check("llm error -> nothing sent", sends.sent == [], str(sends.sent))
    check("llm error counter", engine.counters["llm_errors"] == 1, str(engine.counters))

    print("== 16. 静默时段 ==")
    policy = base_policy(quiet_hours=[["23:30", "08:00"]])
    check("23:59 in quiet hours", policy.in_quiet_hours(23 * 60 + 59))
    check("03:00 in quiet hours", policy.in_quiet_hours(3 * 60))
    check("12:00 not in quiet hours", not policy.in_quiet_hours(12 * 60))
    engine, sends, _ = build(policy)
    engine._now = lambda: 1700000000.0
    import src.auto_reply.engine as engine_mod
    original = engine_mod._local_minutes
    engine_mod._local_minutes = lambda _now: 23 * 60 + 45
    try:
        d = engine.on_event(inbound("半夜消息", mid="q1"))
        check("quiet hours -> skipped", d.reason == "quiet_hours", d.reason)
    finally:
        engine_mod._local_minutes = original

    print("== 17. 白名单按 wxid 或备注名均可命中 ==")
    policy = base_policy(targets=[TargetRule(name=FRIEND, enabled=True)])
    engine, sends, _ = build(policy)
    d = engine.on_event(inbound("按 wxid 命中", mid="w1"))
    check("match by wxid", d.action == "queued", f"{d.action}/{d.reason}")

    print("== 18. 方向判定：单聊里发信人不是会话对象 = 自己 ==")
    engine, sends, _ = build(base_policy())
    check("sender==conversation -> inbound", engine._direction(
        {"sender_id": FRIEND, "conversation_id": FRIEND}, "direct") == "inbound")
    check("sender==self -> outbound", engine._direction(
        {"sender_id": ME, "conversation_id": FRIEND}, "direct") == "outbound")
    check("sender unknown, no assume -> unknown", engine._direction(
        {"sender_id": None, "conversation_id": FRIEND}, "direct") == "unknown")
    engine2, _, _ = build(base_policy(assume_inbound_when_unknown=True))
    check("sender unknown, assume -> inbound", engine2._direction(
        {"sender_id": None, "conversation_id": FRIEND}, "direct") == "inbound")

    print("== 19. 回复文本清洗 ==")
    check("strip quotes", sanitize_reply('"你好呀"', max_chars=50) == "你好呀")
    check("first line only", sanitize_reply("好的\n解释：因为……", max_chars=50) == "好的")
    check("truncate", sanitize_reply("x" * 40, max_chars=10).endswith("…"))
    check("collapse spaces", sanitize_reply("a   b", max_chars=50) == "a b")
    check("empty stays empty", sanitize_reply("   ", max_chars=50) == "")

    print("== 20. 策略校验 ==")
    for bad, label in [
        (dict(min_delay_seconds=5, max_delay_seconds=1), "min>max delay"),
        (dict(max_replies_per_conversation=0), "zero conversation limit"),
        (dict(max_consecutive_auto_replies=0), "zero consecutive limit"),
        (dict(quiet_hours=[["25:00", "08:00"]]), "bad quiet hour"),
    ]:
        try:
            validate_policy(base_policy(**bad))
            check(f"reject {label}", False, "accepted invalid policy")
        except ValueError:
            check(f"reject {label}", True)
    try:
        AutoReplyPolicy.from_dict({"enabled": True, "no_such_key": 1})
        check("reject unknown config key", False, "accepted unknown key")
    except ValueError:
        check("reject unknown config key", True)

    print("== 21. 示例配置可被解析 ==")
    example = ROOT / "config" / "auto_reply.example.json"
    if example.exists():
        import json
        raw = json.loads(example.read_text(encoding="utf-8"))
        policy = AutoReplyPolicy.from_dict(raw)
        check("example config parses (x_ keys are comments)",
              policy.dry_run is True and len(policy.targets) == 2,
              f"dry_run={policy.dry_run} targets={len(policy.targets)}")
        check("example target keeps aliases",
              policy.targets and policy.targets[0].aliases == ["wxid_EXAMPLE_FRIEND_A"],
              str(policy.targets[0].aliases if policy.targets else None))
        check("example declares fallback models",
              len(policy.llm_fallback_models) == 2, str(policy.llm_fallback_models))
    else:
        check("example config present", False, str(example))

    print("== 22. 账号 ID 归一化（真实数据踩过的坑） ==")
    check("strip account dir suffix",
          normalize_account_id("wxid_EXAMPLE_ACCOUNT") == "wxid_EXAMPLE")
    check("plain wxid unchanged",
          normalize_account_id("wxid_EXAMPLE_FRIEND_A") == "wxid_EXAMPLE_FRIEND_A")
    check("None -> empty", normalize_account_id(None) == "")
    engine, sends, _ = build(base_policy())
    engine._self_ids = {normalize_account_id("wxid_me_EXAMPLE")}
    check("self message with suffix is outbound",
          engine._direction({"sender_id": "wxid_me", "conversation_id": FRIEND},
                            "direct") == "outbound")
    engine.on_event(inbound("我自己说的", mid="n1", sender="wxid_me"))
    engine._process(FRIEND)
    check("no reply to own message", sends.sent == [])

    print("== 23. 别名匹配（chat_name 可能是 wxid） ==")
    rule = TargetRule(name="好友A", aliases=["wxid_EXAMPLE_FRIEND_A"])
    engine, sends, _ = build(base_policy(targets=[rule]))
    d = engine.on_event(inbound("在吗", mid="a1", sender="wxid_EXAMPLE_FRIEND_A",
                                chat_id="wxid_EXAMPLE_FRIEND_A",
                                name="wxid_EXAMPLE_FRIEND_A"))
    check("alias hit by wxid", d.action == "queued", f"{d.action}/{d.reason}")
    engine._process("wxid_EXAMPLE_FRIEND_A")
    check("sends to remark name not wxid",
          sends.sent and sends.sent[0][0] == "好友A", str(sends.sent))

    print("== 24. 重放防护（Gateway 重启会重投旧消息） ==")
    engine, sends, _ = build(base_policy())
    d = engine.on_event(inbound("一小时前的旧消息", mid="old1",
                                timestamp=time.time() - 3600))
    check("stale replay -> skipped", d.reason == "stale_replay", d.reason)
    engine._process(FRIEND)
    check("stale replay -> no reply", sends.sent == [])
    d = engine.on_event(inbound("刚发的", mid="new1", timestamp=time.time() - 5))
    check("fresh message still queued", d.action == "queued", f"{d.action}/{d.reason}")
    event = inbound("没有时间戳", mid="nt1")
    event.pop("timestamp")
    check("no timestamp -> accepted", engine.on_event(event).action == "queued")

    print("== 25. 回声容错（漏判会让引擎自锁） ==")
    engine2, sends2, llm2 = build(base_policy())
    llm2.text = "在打游戏呢，你呢？"
    engine2.on_event(inbound("你好", mid="e1"))
    engine2._process(FRIEND)
    reply = sends2.sent[0][1]
    check("reply text", reply == "在打游戏呢，你呢？", repr(reply))
    check("exact echo detected",
          engine2.on_event(inbound(reply, mid="e2", sender=ME)).reason == "own_reply_echo")
    check("whitespace-tolerant echo detected",
          engine2.on_event(inbound(f"  {reply}  ", mid="e3", sender=ME)).reason == "own_reply_echo")
    check("trailing-marker echo detected",
          engine2.on_event(inbound(reply + " [微笑]", mid="e4", sender=ME)).reason
          == "own_reply_echo")
    check("echo never counts as owner takeover", engine2.counters["takeover"] == 0)
    check("short manual message still triggers takeover",
          engine2.on_event(inbound("好的", mid="e5", sender=ME)).reason == "owner_took_over")

    print()
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for item in FAIL:
        print(f"  - {item}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
