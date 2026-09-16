"""AI 自动回复引擎。

数据流：

    WAL 事件 (message.new)
        │  gateway_supervisor.publish_event  (去重之后)
        ▼
    AutoReplyEngine.on_event          ← 事件线程里只做廉价判定并入队
        │
        ▼   工作线程
    白名单 → 方向 → 静默/接管/限速 → 合并去抖
        │
        ├── 读该会话最近 N 条消息（既有 DB 读取路径）
        ├── 调 LLM 生成一条回复
        └── WeChatService.send_message(recipient=..., text=...)
              └─ 搜索 → 唯一性校验 → 打开会话 → 校验当前会话 → 发送 → 验证

设计上刻意保留的三条硬约束：

1. **只回单聊白名单。** 群聊默认不处理；不在 ``targets`` 里的会话一律跳过。
2. **不回答自己的消息。** 方向判定失败时按「不回」处理，宁可漏回不可自问自答。
3. **可以随时被机主打断。** 机主在微信里手动发言，该会话立刻静默
   ``human_override_minutes`` 分钟。
"""
from __future__ import annotations

import json
import logging
import queue
import random
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .policy import AutoReplyPolicy, TargetRule

DEFAULT_LOG_PATH = "logs/auto_reply.jsonl"

# ``discover_accounts()`` 返回的是账号目录名（``wxid_xxx_1a2b``），而消息表里
# ``real_sender_id`` 映射出的是纯 wxid（``wxid_xxx``）。不归一化就会漏判自己的
# 消息：机器人会把自己的发言当成好友来信，然后回给自己。
_ACCOUNT_SUFFIX_RE = re.compile(r"^(wxid_[0-9a-z]+)_[0-9a-f]{4,}$")


def normalize_account_id(value: Any) -> str:
    """把 ``wxid_xxx_1a2b`` 归一成 ``wxid_xxx``；其它值只做去空白 + 小写。"""
    text = str(value or "").strip().casefold()
    match = _ACCOUNT_SUFFIX_RE.match(text)
    return match.group(1) if match else text


def _normalize_text(value: Any) -> str:
    """用于回声比对的文本归一化：去掉全部空白并小写。

    中文消息里插一个空格不改变语义，但会让逐字节相等比较失败；而回声漏判的
    代价（引擎自锁 15 分钟）比误判高得多，所以这里按「空白无关」比较。
    """
    return "".join(str(value or "").split()).casefold()


@dataclass
class Decision:
    """一次事件的处置结论；同时写进 jsonl 和 /status 的环形缓冲。"""

    ts: float
    conversation_id: str
    chat_name: str
    action: str          # sent | dry_run | skipped | failed | muted | takeover
    reason: str
    rule: str = ""
    inbound: list[str] = field(default_factory=list)
    reply: str = ""
    send: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class AutoReplyEngine:
    def __init__(
        self,
        service,
        policy: AutoReplyPolicy | None = None,
        llm=None,
        *,
        self_ids: Iterable[str] | None = None,
        log_path: str | None = None,
        logger: logging.Logger | None = None,
        send: Callable[[str, str], dict] | None = None,
        history: Callable[[str, int], list] | None = None,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        workers: int = 2,
    ):
        self.service = service
        self.policy = policy or AutoReplyPolicy()
        self.logger = logger or logging.getLogger("wechat.gateway.auto_reply")
        self.log_path = Path(log_path or DEFAULT_LOG_PATH)
        self._now = now or time.time
        self._sleep = sleep or time.sleep
        self._random = random.Random()
        self._started_at = self._now()

        if llm is None:
            from .llm import ChatLLM

            llm = ChatLLM(
                base_url=self.policy.llm_base_url,
                api_key=self.policy.llm_api_key,
                model=self.policy.llm_model,
                timeout=self.policy.llm_timeout_seconds,
                temperature=self.policy.llm_temperature,
                fallback_models=list(self.policy.llm_fallback_models),
                max_tokens=getattr(self.policy, "llm_max_tokens", 0),
                reasoning_effort=getattr(self.policy, "llm_reasoning_effort", ""),
            )
        self.llm = llm
        self._send_impl = send or self._default_send
        self._history_impl = history or self._default_history

        self._self_ids: set[str] = {
            normalize_account_id(x) for x in (self_ids or []) if x
        }

        # 事件线程 → 工作线程
        self._queue: queue.Queue[str] = queue.Queue(maxsize=2000)
        self._workers: list[threading.Thread] = []
        # workers=0 表示不自己起线程，由调用方同步驱动 _process（测试用）。
        self._worker_count = max(0, int(workers))
        self._started = False
        self._stop = threading.Event()

        # 每会话状态
        self._chat_locks: dict[str, threading.Lock] = {}
        self._state_lock = threading.RLock()
        self._pending: dict[str, list[dict]] = {}
        self._chat_names: dict[str, str] = {}
        self._last_inbound_at: dict[str, float] = {}
        self._first_pending_at: dict[str, float] = {}
        self._last_reply_at: dict[str, float] = {}
        self._reply_times: dict[str, deque[float]] = {}
        self._consecutive: dict[str, int] = {}
        self._muted_until: dict[str, float] = {}
        self._mute_reason: dict[str, str] = {}
        self._human_until: dict[str, float] = {}
        # 自己刚发出去的文本，用来在 sender_id 缺失时仍然认出自己的回声。
        self._echo: deque[tuple[str, str, float]] = deque(maxlen=200)

        # 全局
        self._send_lock = threading.Lock()   # UIA 同一时刻只能有一个会话在前台
        self._global_times: deque[float] = deque(maxlen=600)
        self._seen: deque[str] = deque(maxlen=5000)
        self._recent_burst: dict[str, deque[float]] = {}

        self.decisions: deque[Decision] = deque(maxlen=200)
        self.counters = {
            "received": 0, "inbound": 0, "outbound_seen": 0,
            "sent": 0, "skipped": 0, "failed": 0, "dry_run": 0,
            "takeover": 0, "muted": 0, "llm_errors": 0,
        }

    # --------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        for index in range(self._worker_count):
            thread = threading.Thread(target=self._worker, name=f"auto-reply-{index}", daemon=True)
            thread.start()
            self._workers.append(thread)
        self.logger.info(
            "auto-reply engine started enabled=%s dry_run=%s targets=%s",
            self.policy.enabled, self.policy.dry_run,
            [t.name for t in self.policy.targets],
        )

    def stop(self) -> None:
        self._stop.set()
        for _ in self._workers:
            try:
                self._queue.put_nowait("")
            except queue.Full:
                pass
        for thread in self._workers:
            thread.join(timeout=2)
        self._workers = []
        self.logger.info("auto-reply engine stopped")

    def reload(self, policy: AutoReplyPolicy) -> None:
        self.policy = policy
        self.logger.info("auto-reply policy reloaded enabled=%s targets=%s",
                         policy.enabled, [t.name for t in policy.targets])

    # ------------------------------------------------------------------ 事件面
    def on_event(self, event: dict) -> Decision:
        """由 Gateway 事件线程调用；必须廉价、不得阻塞。"""
        decision = self._evaluate(event)
        if decision.action == "queued":
            try:
                self._queue.put_nowait(decision.conversation_id)
            except queue.Full:
                self._record(Decision(
                    ts=self._now(), conversation_id=decision.conversation_id,
                    chat_name=decision.chat_name, action="skipped",
                    reason="worker_queue_full", rule=decision.rule,
                ))
        return decision

    def _evaluate(self, event: dict) -> Decision:
        now = self._now()
        base = Decision(ts=now, conversation_id="", chat_name="", action="skipped", reason="")

        if not isinstance(event, dict):
            base.reason = "not_an_event"
            return base
        if (event.get("event") or event.get("type")) != "message.new":
            base.reason = "not_message_new"
            return base
        self.counters["received"] += 1

        conversation_id = str(event.get("conversation_id") or event.get("chat_id") or "")
        chat_name = str(event.get("chat_name") or conversation_id)
        base.conversation_id = conversation_id
        base.chat_name = chat_name

        if not self.policy.enabled:
            base.reason = "auto_reply_disabled"
            return base

        if conversation_id:
            with self._state_lock:
                self._chat_names[conversation_id] = chat_name

        rule = self.policy.match(conversation_id=conversation_id, chat_name=chat_name)
        if rule is None:
            base.reason = "not_whitelisted"
            return base
        base.rule = rule.name

        chat_type = str(event.get("chat_type") or "direct").lower()
        if chat_type != "direct" and not rule.allow_group:
            base.reason = "group_not_allowed"
            return base

        # 去重与回声判在方向之前：自己刚发出的那条回复必须就地丢弃，
        # 既不能当来信处理，也不能触发「机主接管」（它本来就是引擎发的）。
        message_id = str(event.get("message_id") or "")
        if message_id and message_id in self._seen:
            base.reason = "duplicate_message_id"
            self.counters["skipped"] += 1
            return base
        if message_id:
            self._seen.append(message_id)

        content = str(event.get("content") or "").strip()
        if content and self._is_echo(conversation_id, content):
            base.reason = "own_reply_echo"
            return base

        direction = self._direction(event, chat_type)
        if direction == "outbound":
            self.counters["outbound_seen"] += 1
            self._maybe_takeover(conversation_id, rule, event, base)
            return base
        if direction != "inbound":
            base.reason = f"direction_{direction}"
            return base
        self.counters["inbound"] += 1

        event_ts = event.get("timestamp")
        if isinstance(event_ts, (int, float)) and not isinstance(event_ts, bool):
            if float(event_ts) < self._started_at - self.policy.replay_grace_seconds:
                # 事件源缓冲的重放，不是刚发生的事。这里必须挡在生成之前：
                # Gateway 重启一次就可能把几小时前的旧消息重新投递过来。
                base.reason = "stale_replay"
                self.counters["skipped"] += 1
                return base

        if str(event.get("message_type") or "text") != "text" or not content:
            base.reason = "non_text_message"
            self.counters["skipped"] += 1
            return base

        # 限速/静默判定放在入队前，避免无意义地占用工作线程。
        blocked = self._gate(conversation_id, rule, now)
        if blocked:
            base.reason = blocked
            self.counters["skipped"] += 1
            return base

        if self.policy.in_quiet_hours(_local_minutes(now)):
            base.reason = "quiet_hours"
            self.counters["skipped"] += 1
            return base

        with self._state_lock:
            self._pending.setdefault(conversation_id, []).append({
                "text": content, "ts": now,
                "message_id": message_id or None,
            })
            if conversation_id not in self._first_pending_at:
                self._first_pending_at[conversation_id] = now
            self._last_inbound_at[conversation_id] = now
            self._note_burst(conversation_id, now, rule)

        base.action = "queued"
        base.reason = "queued_for_reply"
        base.inbound = [content]
        return base

    def _direction(self, event: dict, chat_type: str) -> str:
        """判定这条消息是好友来信还是自己发出。

        有自身账号列表时最可靠：``sender_id`` 属于自己 → outbound，否则
        inbound（群里也成立，因为群消息的 sender 是群成员）。
        没有账号列表时只敢在单聊里下判断：单聊的 ``conversation_id`` 就是
        对方 wxid，所以发信人等于会话对象才是来信；群聊一律 unknown。
        """
        sender = event.get("sender_id")
        sender = str(sender) if sender not in (None, "") else ""
        conversation_id = str(event.get("conversation_id") or event.get("chat_id") or "")
        if not sender:
            if self.policy.assume_inbound_when_unknown and chat_type == "direct":
                return "inbound"
            return "unknown"
        if self._self_ids:
            return "outbound" if normalize_account_id(sender) in self._self_ids else "inbound"
        if chat_type == "direct":
            return "inbound" if normalize_account_id(sender) == normalize_account_id(conversation_id) else "outbound"
        return "unknown"

    # ------------------------------------------------------------------ 接管
    def _maybe_takeover(self, conversation_id: str, rule: TargetRule, event: dict,
                        base: Decision) -> None:
        """机主自己手动发消息 → 交还控制权。"""
        content = str(event.get("content") or "")
        if self._is_echo(conversation_id, content):
            base.reason = "own_outbound_echo"
            return
        with self._state_lock:
            self._human_until[conversation_id] = (
                self._now() + self.policy.human_override_minutes * 60
            )
            self._pending.pop(conversation_id, None)
            self._first_pending_at.pop(conversation_id, None)
            self._consecutive[conversation_id] = 0
        self.counters["takeover"] += 1
        base.action = "takeover"
        base.reason = "owner_took_over"
        self._record(base)
        self.logger.info("auto-reply suspended: owner replied manually chat=%s", conversation_id)

    def resume(self, conversation_id: str) -> bool:
        """手动解除接管/静默。"""
        with self._state_lock:
            had = conversation_id in self._human_until or conversation_id in self._muted_until
            self._human_until.pop(conversation_id, None)
            self._muted_until.pop(conversation_id, None)
            self._mute_reason.pop(conversation_id, None)
            self._consecutive[conversation_id] = 0
        return had

    # ------------------------------------------------------------------ 门禁
    def _gate(self, conversation_id: str, rule: TargetRule, now: float) -> str:
        with self._state_lock:
            muted_until = self._muted_until.get(conversation_id, 0.0)
            if muted_until > now:
                self.counters["muted"] += 1
                return f"muted:{self._mute_reason.get(conversation_id, 'loop_guard')}"
            human_until = self._human_until.get(conversation_id, 0.0)
            if human_until > now:
                return "owner_takeover_active"

            last_reply = self._last_reply_at.get(conversation_id, 0.0)
            if now - last_reply < self.policy.min_interval_seconds:
                return "min_interval"

            window = self.policy.conversation_window_minutes * 60
            times = self._reply_times.setdefault(conversation_id, deque(maxlen=200))
            while times and now - times[0] > window:
                times.popleft()
            if len(times) >= self.policy.max_replies_per_conversation:
                self._mute(conversation_id, "rate_limit_conversation", now)
                return "muted:rate_limit_conversation"

            while self._global_times and now - self._global_times[0] > 60:
                self._global_times.popleft()
            if len(self._global_times) >= self.policy.max_replies_global_per_minute:
                return "rate_limit_global"

            if self._consecutive.get(conversation_id, 0) >= self.policy.max_consecutive_auto_replies:
                self._mute(conversation_id, "max_consecutive", now)
                return "muted:max_consecutive"
        return ""

    def _mute(self, conversation_id: str, reason: str, now: float) -> None:
        self._muted_until[conversation_id] = now + self.policy.mute_minutes * 60
        self._mute_reason[conversation_id] = reason
        self.logger.warning("auto-reply muted chat=%s reason=%s for %.0f min",
                            conversation_id, reason, self.policy.mute_minutes)

    def _note_burst(self, conversation_id: str, now: float, rule: TargetRule) -> None:
        """对面在短时间内高频刷消息 → 疑似另一个机器人，进入静默。"""
        window = self.policy.bot_burst_seconds
        burst = self._recent_burst.setdefault(conversation_id, deque(maxlen=100))
        burst.append(now)
        while burst and now - burst[0] > window:
            burst.popleft()
        if len(burst) >= self.policy.bot_burst_count:
            self._mute(conversation_id, "peer_bot_burst", now)
            burst.clear()

    def _is_echo(self, conversation_id: str, content: str) -> bool:
        """这条「自己发的」消息是不是引擎刚发出去的那条回复？

        必须足够宽松：微信落库时的文本可能与引擎发出的不完全一致（空白、
        换行、表情被规范化）。判错方向的代价不对称 ——
        漏判回声会让引擎把自己的回复当成「机主接管」，每次回复后自锁
        ``human_override_minutes`` 分钟；误判则只影响机主打断的及时性。
        所以用「归一化相等 + 长公共前缀」两档匹配。
        """
        cutoff = self._now() - 180
        probe = _normalize_text(content)
        if not probe:
            return False
        with self._state_lock:
            candidates = [
                _normalize_text(text) for chat, text, ts in self._echo
                if chat == conversation_id and ts >= cutoff
            ]
        for candidate in candidates:
            if not candidate:
                continue
            if candidate == probe:
                return True
            short, long = sorted((candidate, probe), key=len)
            if len(short) >= 8 and long.startswith(short):
                return True
        return False

    # ------------------------------------------------------------------ 工作线程
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                conversation_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not conversation_id:
                continue
            try:
                self._process(conversation_id)
            except Exception:  # noqa: BLE001 - 单个会话失败不得拖垮线程
                self.logger.exception("auto-reply processing failed chat=%s", conversation_id)
                self.counters["failed"] += 1

    def _process(self, conversation_id: str) -> None:
        lock = self._chat_locks.setdefault(conversation_id, threading.Lock())
        if not lock.acquire(blocking=False):
            # 同一会话已在处理：让正在跑的那次把它们一起答掉。
            return
        try:
            rule = self._rule_for(conversation_id)
            if rule is None:
                with self._state_lock:
                    self._pending.pop(conversation_id, None)
                    self._first_pending_at.pop(conversation_id, None)
                return

            if not self._wait_for_quiet(conversation_id):
                return

            now = self._now()
            blocked = self._gate(conversation_id, rule, now)
            if blocked:
                return
            if self.policy.in_quiet_hours(_local_minutes(now)):
                return

            with self._state_lock:
                pending = self._pending.pop(conversation_id, [])
                self._first_pending_at.pop(conversation_id, None)
            if not pending:
                return

            inbound = [item["text"] for item in pending]
            messages = self._build_prompt(conversation_id, rule, inbound)
            system = self._system_prompt(rule)

            try:
                reply = self.llm.reply(system, messages, max_chars=rule.max_chars)
            except Exception as exc:  # noqa: BLE001 - LLM 故障 = 这条不回
                self.counters["llm_errors"] += 1
                self._record(Decision(
                    ts=self._now(), conversation_id=conversation_id,
                    chat_name=self._display_name(conversation_id),
                    action="failed", reason=f"llm_error:{type(exc).__name__}",
                    rule=rule.name, inbound=inbound,
                ))
                self.logger.warning("llm failed chat=%s: %s", conversation_id, exc)
                return

            if not reply:
                self._record(Decision(
                    ts=self._now(), conversation_id=conversation_id,
                    chat_name=self._display_name(conversation_id),
                    action="skipped", reason="empty_reply",
                    rule=rule.name, inbound=inbound,
                ))
                return

            if self.policy.signature:
                reply = f"{reply}{self.policy.signature}"

            if self.policy.dry_run:
                self.counters["dry_run"] += 1
                decision = Decision(
                    ts=self._now(), conversation_id=conversation_id,
                    chat_name=self._display_name(conversation_id),
                    action="dry_run", reason="dry_run_enabled",
                    rule=rule.name, inbound=inbound, reply=reply,
                )
                self._record(decision)
                with self._state_lock:
                    self._note_reply(conversation_id, self._now())
                return

            self._deliver(conversation_id, rule, inbound, reply)
        finally:
            lock.release()

    def _deliver(self, conversation_id: str, rule: TargetRule,
                 inbound: list[str], reply: str) -> None:
        # UIA 只能让一个会话处于前台，发送必须全局串行。
        with self._send_lock:
            try:
                result = self._send_impl(rule.name, reply) or {}
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "error": {"code": "SEND_EXCEPTION",
                                                      "message": f"{type(exc).__name__}: {exc}"}}
        ok = bool(result.get("success") and result.get("sent", True))
        now = self._now()
        if ok:
            self.counters["sent"] += 1
            with self._state_lock:
                self._echo.append((conversation_id, reply, now))
                self._note_reply(conversation_id, now)
            action, reason = "sent", "sent"
        else:
            self.counters["failed"] += 1
            code = (result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None
            action, reason = "failed", f"send_failed:{code or 'unknown'}"
        self._record(Decision(
            ts=now, conversation_id=conversation_id,
            chat_name=self._display_name(conversation_id), action=action,
            reason=reason, rule=rule.name, inbound=inbound, reply=reply,
            send=_compact_send(result),
        ))

    def _note_reply(self, conversation_id: str, now: float) -> None:
        self._last_reply_at[conversation_id] = now
        self._reply_times.setdefault(conversation_id, deque(maxlen=200)).append(now)
        self._global_times.append(now)
        self._consecutive[conversation_id] = self._consecutive.get(conversation_id, 0) + 1

    def _wait_for_quiet(self, conversation_id: str) -> bool:
        """等该会话静下来（去抖），把连发的几条合并成一次回复。"""
        deadline = self._now() + self.policy.debounce_max_wait_seconds
        while not self._stop.is_set():
            with self._state_lock:
                last = self._last_inbound_at.get(conversation_id, 0.0)
            idle = self._now() - last
            if idle >= self.policy.debounce_seconds:
                return True
            if self._now() >= deadline:
                return True
            self._sleep(min(0.25, max(0.05, self.policy.debounce_seconds - idle)))

    # ------------------------------------------------------------------ 上下文
    def _build_prompt(self, conversation_id: str, rule: TargetRule,
                      inbound: list[str]) -> list[dict]:
        history: list[dict] = []
        # 每个好友可以单独覆盖历史条数；0 表示沿用全局值。
        limit = getattr(rule, "history_limit", 0) or self.policy.history_limit
        try:
            rows = self._history_impl(conversation_id, limit) or []
        except Exception as exc:  # noqa: BLE001 - 读不到历史也能只凭当前消息回复
            self.logger.warning("history read failed chat=%s: %s", conversation_id, exc)
            rows = []

        ordered = list(rows)
        try:
            ordered.sort(key=lambda r: (r.get("sort_seq") or 0, r.get("create_time") or 0))
        except Exception:
            pass
        # 历史里最后一条通常就是刚到的那条，避免重复。
        drop_last = 0
        if ordered and inbound and str(ordered[-1].get("content") or "").strip() == inbound[-1]:
            drop_last = 1
        if drop_last:
            ordered = ordered[:-1]

        for row in ordered:
            text = str(row.get("content") or "").strip()
            if not text:
                continue
            if str(row.get("message_type") or "text") != "text":
                text = f"[{row.get('message_type')}]"
            sender = str(row.get("sender_id") or "")
            role = "assistant" if (normalize_account_id(sender) in self._self_ids or
                                   (sender and normalize_account_id(sender) !=
                                    normalize_account_id(conversation_id) and
                                    str(row.get("chat_type") or "direct") == "direct")) else "user"
            history.append({"role": role, "content": text})

        # 合并后的多行来信作为最后一条 user 消息。
        tail = "\n".join(inbound)
        if not history or history[-1]["role"] != "user":
            history.append({"role": "user", "content": tail})
        elif len(inbound) > 1:
            history.append({"role": "user", "content": tail})

        # 交错的两个 user 会被部分后端拒绝；折叠成一条。
        collapsed: list[dict] = []
        for item in history:
            if collapsed and collapsed[-1]["role"] == item["role"]:
                collapsed[-1]["content"] += "\n" + item["content"]
            else:
                collapsed.append(dict(item))
        if not collapsed:
            collapsed = [{"role": "user", "content": tail}]
        return collapsed

    def _system_prompt(self, rule: TargetRule) -> str:
        parts = [rule.system_prompt or self.policy.system_prompt]
        if rule.persona:
            parts.append(f"你要扮演的人设背景：{rule.persona}")
        # 长期记忆：这个人是谁、你们的关系、已知事实与禁忌。
        # 放在 persona 之后、输出约束之前，让它优先于风格描述。
        if getattr(rule, "memory", ""):
            parts.append(
                "关于这个人的已知背景（视为你本来就记得的事，不要说自己在查资料）："
                f"{rule.memory}"
            )
        parts.append("只输出消息正文本身。")
        return "\n".join(part for part in parts if part)

    # ------------------------------------------------------------------ 默认实现
    def _default_send(self, recipient: str, text: str) -> dict:
        return self.service.send_message(recipient=recipient, text=text)

    def _default_history(self, chat_id: str, limit: int) -> list:
        return self.service.messages(chat_id, limit) or []

    def _rule_for(self, conversation_id: str) -> TargetRule | None:
        return self.policy.match(conversation_id=conversation_id,
                                 chat_name=self._display_name(conversation_id))

    def _display_name(self, conversation_id: str) -> str:
        """会话显示名：优先用事件带来的 chat_name（备注名），否则退化为 wxid。

        白名单既可能按 wxid 配置，也可能按备注名配置，所以这里保留原样取值，
        由 ``AutoReplyPolicy.match`` 逐一比对。
        """
        with self._state_lock:
            return self._chat_names.get(conversation_id) or conversation_id

    # ------------------------------------------------------------------ 观测
    def _record(self, decision: Decision) -> None:
        self.decisions.append(decision)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(decision.to_json() + "\n")
        except Exception:  # noqa: BLE001 - 审计写失败不能影响回复
            self.logger.debug("decision log write failed", exc_info=True)
        if decision.action in {"sent", "failed", "takeover"}:
            self.logger.info(
                "auto-reply %s chat=%s rule=%s reason=%s reply=%r",
                decision.action, decision.conversation_id, decision.rule,
                decision.reason, decision.reply[:80],
            )

    def status(self) -> dict:
        now = self._now()
        with self._state_lock:
            muted = {k: round(v - now, 1) for k, v in self._muted_until.items() if v > now}
            human = {k: round(v - now, 1) for k, v in self._human_until.items() if v > now}
            pending = {k: len(v) for k, v in self._pending.items() if v}
        return {
            "enabled": self.policy.enabled,
            "dry_run": self.policy.dry_run,
            "config_path": self.policy.config_path,
            "targets": [t.name for t in self.policy.targets if t.enabled],
            "counters": dict(self.counters),
            "queue_depth": self._queue.qsize(),
            "started_at": self._started_at,
            "replay_grace_seconds": self.policy.replay_grace_seconds,
            "pending_inbound": pending,
            "muted_seconds_left": muted,
            "owner_takeover_seconds_left": human,
            "llm": {
                "base_url": self.llm.base_url,
                "model": self.llm.model,
                "models": list(getattr(self.llm, "models", []) or []),
                "configured": bool(self.llm.configured),
                "last_attempts": list(getattr(self.llm, "attempts", []) or []),
                "last_error": getattr(self.llm, "last_error", ""),
            },
            "recent": [asdict(d) for d in list(self.decisions)[-10:]],
        }


def _local_minutes(now: float) -> float:
    stamp = datetime.fromtimestamp(now)
    return stamp.hour * 60 + stamp.minute


def _compact_send(result: Any) -> dict:
    if not isinstance(result, dict):
        return {"raw": str(result)[:200]}
    return {
        "success": bool(result.get("success")),
        "sent": result.get("sent"),
        "result_state": result.get("result_state"),
        "recipient": result.get("recipient"),
        "duration_ms": result.get("duration_ms"),
        "error": result.get("error"),
    }
