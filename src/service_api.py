"""统一的数据层 API（可由 Flask/FastAPI 适配器调用）。"""
from __future__ import annotations
from .providers import WeChatDataProvider
from .errors import ProviderUnavailable
import uuid
import time
import os
import threading

LEGACY_CAPABILITY_NAMES = {
    "wechat.state": "wechat.status",
    "wechat.conversation.list": "wechat.list_chats",
    "wechat.conversation.search": "wechat.search_chat",
    "wechat.contact.search": "wechat.search_contact",
    "wechat.conversation.open": "wechat.open_chat",
    "wechat.message.read": "wechat.read_messages",
    "wechat.message.latest": "wechat.read_messages",
    "wechat.message.history": "wechat.get_messages",
    "wechat.message.send_text": "wechat.send_message",
    "wechat.message.send_image": "wechat.send_image",
    "wechat.message.send_file": "wechat.send_file",
    "wechat.group.info": "wechat.group_info",
    "wechat.group.search": "wechat.group_search",
    "wechat.account.list": "wechat.account_list",
    "wechat.account.current": "wechat.account_current",
    "wechat.database.inventory": "wechat.database_inventory",
    "wechat.database.xinfo": "wechat.database_xinfo",
}

# /capabilities 的早期版本使用不带 ``wechat.`` 前缀的短名称。保留这些
# 别名只用于兼容旧调用方；canonical 名称仍然是唯一的执行入口。
LEGACY_MANIFEST_NAMES = {
    "status": "wechat.state",
    "list_chats": "wechat.conversation.list",
    "search_chat": "wechat.conversation.search",
    "search_contact": "wechat.contact.search",
    "open": "wechat.conversation.open",
    "read_messages": "wechat.message.read",
    "latest_message": "wechat.message.latest",
    "history": "wechat.message.history",
    "send_text": "wechat.message.send_text",
    "send_message": "wechat.message.send_text",
    "send_image": "wechat.message.send_image",
    "send_file": "wechat.message.send_file",
    "group_info": "wechat.group.info",
    "group_search": "wechat.group.search",
    "account_list": "wechat.account.list",
    "account_current": "wechat.account.current",
    "database_inventory": "wechat.database.inventory",
    "database_xinfo": "wechat.database.xinfo",
}

CAPABILITIES = {
    "status": {"available": True, "background": True, "requires_confirmation": False, "reason": "进程/UIA 状态"},
    "list_chats": {"available": True, "background": True, "requires_confirmation": False, "reason": "UIA 读取"},
    "current_chat": {"available": True, "background": True, "requires_confirmation": False, "reason": "UIA 读取"},
    "search_chat": {"available": True, "background": True, "requires_confirmation": False, "reason": "UIA 读取"},
    "search_contact": {"available": True, "background": True, "requires_confirmation": False, "reason": "联系人搜索（当前实现以可见会话列表为准）"},
    "open_chat": {"available": True, "background": False, "requires_confirmation": False, "reason": "显式 UIA 会话切换"},
    "account_list": {"available": True, "background": True, "requires_confirmation": False, "reason": "本地 WeChat Files 账号发现"},
    "account_current": {"available": True, "background": True, "requires_confirmation": False, "reason": "当前账号目录识别"},
    "database_inventory": {"available": True, "background": True, "requires_confirmation": False, "reason": "数据库文件清单"},
    "database_xinfo": {"available": True, "background": True, "requires_confirmation": False, "reason": "明文 xInfo.db 只读读取"},
    "get_messages": {"available": True, "background": True, "requires_confirmation": False, "reason": "SQLCipher 参考读面"},
    "search_messages": {"available": True, "background": True, "requires_confirmation": False, "reason": "SQLCipher 参考读面"},
    "draft_message": {"available": True, "background": True, "requires_confirmation": False, "reason": "仅创建草稿"},
    "confirm_message": {"available": True, "background": True, "requires_confirmation": True, "reason": "显式确认草稿"},
    "send_message": {"available": True, "background": False, "requires_confirmation": False,
                     "reason": "UIA_SEND_WITH_RESULT_VERIFICATION", "status": "SEND_RESULT_NOT_OBSERVABLE",
                     "execution_mode": "INTERACTIVE", "verified": False},
    "type_message": {"available": True, "background": False, "requires_confirmation": False, "reason": "显式前台输入；后台监控不会调用", "mode": "interactive"},
    "read_messages": {"available": True, "background": True, "requires_confirmation": False, "reason": "读取当前会话可见 UIA 文本节点"},
    "reply_draft": {"available": True, "background": True, "requires_confirmation": False, "reason": "读取并创建未发送草稿"},
}

CANONICAL_CAPABILITIES = {
    "wechat.state": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "Gateway、微信进程与 UIA 状态",
    },
    "wechat.conversation.list": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "读取当前可见微信会话列表",
    },
    "wechat.conversation.search": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "在已物化会话列表中只读搜索",
    },
    "wechat.conversation.open": {
        "available": True,
        "background": False,
        "requires_confirmation": False,
        "reason": "SEARCH→OPEN→VERIFY；当前版本已通过交互路径实测",
        "verified": True,
        "status": "OPEN_RESULT_VERIFIED",
    },
    "wechat.contact.search": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "联系人搜索（当前实现以可见会话列表为准）",
        "verified": True,
        "status": "VISIBLE_SESSION_SEARCH_ONLY",
    },
    "wechat.account.list": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "本地 WeChat Files 账号发现",
        "verified": True,
        "status": "ACCOUNT_DISCOVERY_READY",
    },
    "wechat.account.current": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "当前账号目录识别",
        "verified": True,
        "status": "ACCOUNT_DISCOVERY_READY",
    },
    "wechat.database.inventory": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "数据库文件清单",
        "verified": True,
        "status": "DATABASE_DISCOVERY_READY",
    },
    "wechat.database.xinfo": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "明文 xInfo.db 只读读取",
        "verified": True,
        "status": "XINFO_READ_READY",
    },
    "wechat.message.read": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "读取当前会话可见 UIA 消息节点",
    },
    "wechat.message.latest": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "读取当前会话最新可见消息",
    },
    "wechat.message.history": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "SQLCipher 参考读面",
        "status": "DATABASE_KEY_UNAVAILABLE",
    },
    "wechat.message.send_text": {
        "available": True,
        "background": False,
        "requires_confirmation": False,
        "reason": "显式 UIA 发送；结果必须经观察验证",
        "status": "SEND_RESULT_NOT_OBSERVABLE",
        "verified": False,
        "execution_mode": "INTERACTIVE",
    },
    "wechat.message.send_image": {
        "available": True,
        "background": False,
        "requires_confirmation": False,
        "reason": "UIA 剪贴板图片发送",
        "status": "SEND_RESULT_NOT_OBSERVABLE",
        "verified": False,
    },
    "wechat.message.send_file": {
        "available": True,
        "background": False,
        "requires_confirmation": False,
        "reason": "UIA 剪贴板文件发送",
        "status": "SEND_RESULT_NOT_OBSERVABLE",
        "verified": False,
    },
    "wechat.group.info": {
        "available": False,
        "background": True,
        "requires_confirmation": False,
        "reason": "当前 UIA 未暴露稳定群成员/群信息节点",
        "status": "NOT_SUPPORTED",
    },
    "wechat.group.search": {
        "available": True,
        "background": True,
        "requires_confirmation": False,
        "reason": "会话列表中按名称筛选群聊；不执行全量前台搜索",
        "status": "VISIBLE_SESSION_SEARCH_ONLY",
    },
}

class WeChatService:
    def __init__(self, provider: WeChatDataProvider, uia=None):
        self.provider = provider
        self.uia = uia
        self._drafts = {}
        self._sessions = {}
        self._consumed = set()
        self._command_lock = threading.RLock()
        self._command_results = {}
        self._command_inflight = {}
        self._command_result_limit = 1000

    def status(self):
        # Gateway status must remain available when the optional database read
        # plane is degraded. Database key validation is exposed through its
        # explicit diagnostics/tools and never runs on a UIA control request.
        status_fn = getattr(self.provider, "get_control_plane_status", self.provider.get_status)
        return {"provider": status_fn(), "capabilities": CAPABILITIES}

    def state(self):
        """返回统一状态快照；不把 UIA 未连接伪装为可用。

        使用轻量的 control-plane 状态而不是完整 get_status()：完整路径会
        发现并验证密钥，首次要解密 contact 库（实测 10-20 秒），使
        ``wechat.state`` 这个纯健康检查工具超时。完整状态仍可通过
        ``/api/wechat/status`` 与数据库专用工具获取。
        """
        status_fn = getattr(self.provider, "get_control_plane_status", self.provider.get_status)
        provider = status_fn()
        database = provider if isinstance(provider, dict) else {}
        try:
            ui = self.ui_status()
        except Exception as exc:
            ui = {"available": False, "connected": False,
                  "error": f"{type(exc).__name__}: {exc}"}
        return {
            "provider": provider,
            "database": database,
            "uia": ui,
            "connected": bool(ui.get("connected")) if isinstance(ui, dict) else False,
            "automation_ready": bool(ui.get("connected")) if isinstance(ui, dict) else False,
            "current_account": database.get("current_account"),
            "database_key_status": database.get("database_key_status", "DATABASE_KEY_UNAVAILABLE"),
            "status_mode": "CONTROL_PLANE_CACHED",
        }

    def chats(self): return self.provider.get_chats()
    def current_chat(self): return self.provider.get_current_chat()
    def messages(self, chat_id, limit=20, before=None): return self.provider.get_messages(chat_id, limit, before)
    def search(self, query, chat_id=None): return self.provider.search_messages(query, chat_id)
    def ui_status(self): return self.uia.get_status() if self.uia else {"available": False, "reason": "UIA not configured"}
    def ui_chats(self):
        if not self.uia: raise ProviderUnavailable("UIA not configured")
        return self.uia.get_chats()
    def ui_current_chat(self):
        if not self.uia: raise ProviderUnavailable("UIA not configured")
        return self.uia.get_current_chat()
    def ui_search_chat(self,name):
        if not self.uia: raise ProviderUnavailable("UIA not configured")
        return self.uia.search_chat(name)
    def ui_open_chat(self,name):
        if not self.uia: raise ProviderUnavailable("UIA not configured")
        return self.uia.open_chat(name)

    # ---- Canonical P0/P1 facade -------------------------------------------------
    # These methods deliberately keep the UIA implementation behind one service
    # boundary so OpenClaw and HTTP callers use the same contracts.
    def list_chats(self):
        return self.ui_chats()

    def account_list(self):
        getter = getattr(self.provider, "get_accounts", None)
        return [row.__dict__ if hasattr(row, "__dict__") else row for row in (getter() if getter else [])]

    def account_current(self):
        getter = getattr(self.provider, "get_current_account", None)
        return getter() if getter else None

    def database_inventory(self):
        getter = getattr(self.provider, "get_database_inventory", None)
        if getter is None:
            return {"available": False, "reason": "database inventory unavailable"}
        return getter()

    def database_xinfo(self):
        getter = getattr(self.provider, "get_xinfo", None)
        if getter is None:
            return {"available": False, "reason": "xInfo unavailable"}
        return getter()

    def search_chat(self, name):
        return self.ui_search_chat(name)

    def search_contact(self, name):
        return self.search_chat(name)

    def open_chat(self, name):
        return self.ui_open_chat(name)

    def read_visible_messages(self, limit=20):
        return self.read_messages(limit)

    def _resolve_send_target(self, recipient):
        recipient = str(recipient or "").strip()
        if not recipient:
            return None, None, {"code": "TARGET_REQUIRED", "message": "需要指定聊天目标"}
        candidates = self.ui_search_chat(recipient)
        if not candidates:
            contact_getter = getattr(self.provider, "get_contacts", None)
            if contact_getter is not None:
                try:
                    contacts = contact_getter() or []
                except Exception:
                    contacts = []
                folded = recipient.casefold()
                for row in contacts:
                    if not isinstance(row, dict):
                        continue
                    values = (
                        str(row.get("username") or ""),
                        str(row.get("nick_name") or ""),
                        str(row.get("remark") or ""),
                    )
                    if any(folded == value.casefold() or folded in value.casefold() for value in values if value):
                        candidates = [{
                            "id": row.get("username"),
                            "name": row.get("remark") or row.get("nick_name") or row.get("username"),
                        }]
                        break
        if not candidates:
            # 最后兜底：直接按 username 在数据库联系人里精确查找。调用方显式
            # 给出 username（如 filehelper）时必须尊重，不能因为参考实现的
            # 搜索索引差异而拒绝。
            adapter = getattr(self.provider, "adapter", None)
            if adapter is not None and hasattr(adapter, "get_contacts"):
                try:
                    rows = adapter.get_contacts() or []
                except Exception:
                    rows = []
                folded = recipient.casefold()
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    username = str(row.get("username") or "")
                    if username and username.casefold() == folded:
                        candidates = [{
                            "id": username,
                            "name": row.get("name") or username,
                        }]
                        break
        if not candidates:
            return None, None, {"code": "TARGET_NOT_FOUND", "message": f"未找到聊天目标: {recipient}"}
        if len(candidates) > 1:
            return None, None, {"code": "AMBIGUOUS_TARGET", "message": f"聊天目标不唯一: {recipient}", "candidates": candidates}
        target = candidates[0]
        target_name = target.get("name") if isinstance(target, dict) else str(target)
        target_id = target.get("id") if isinstance(target, dict) else None
        return target, {"name": target_name or recipient, "id": target_id}, None

    def _read_message_snapshot(self, chat_id, limit=50):
        getter = getattr(self.provider, "get_messages", None)
        if getter is None:
            return []
        try:
            return list(getter(chat_id, limit=limit, before=None) or [])
        except Exception:
            return []

    def latest_message(self):
        result = self.read_messages(1)
        rows = result.get("messages", []) if isinstance(result, dict) else []
        return {
            "chat": result.get("chat") if isinstance(result, dict) else None,
            "message": rows[-1] if rows else None,
            "messages": rows,
        }

    def message_history(self, chat_id, limit=50, before=None):
        """历史读取走已配置的数据 provider；SQLCipher 读面不可用时明确失败。"""
        return self.messages(chat_id, limit, before)

    def group_search(self, name):
        query = str(name or "").strip().casefold()
        if not query:
            return []
        rows = self.list_chats()
        return [
            row for row in rows
            if isinstance(row, dict)
            and (
                str(row.get("name") or "").casefold().find(query) >= 0
                or str(row.get("id") or "").casefold().find(query) >= 0
            )
            and (
                bool(row.get("is_group"))
                or "群" in str(row.get("name") or "")
                or "group" in str(row.get("id") or "").casefold()
            )
        ]

    def group_info(self, chat_id):
        if not self.uia:
            raise ProviderUnavailable("UIA not configured")
        getter = getattr(self.uia, "get_group_info", None)
        if getter is None:
            return {
                "supported": False,
                "status": "NOT_SUPPORTED",
                "chat_id": str(chat_id),
            }
        return getter(str(chat_id))

    def send_image(self, *args, **kwargs):
        recipient = kwargs.get("recipient") or kwargs.get("chat_id") or kwargs.get("target")
        path = kwargs.get("path")
        if not path:
            return {"success": False, "error": {"code": "PATH_REQUIRED", "message": "需要指定图片路径"}}
        # Resolve/opening a chat can itself be interactive.  Do not do any UI
        # work before the real-send gate has approved this request.
        if not self._synthetic_actuator_enabled() and not self.send_enabled():
            return self._send_disabled_result("image")
        if self._synthetic_actuator_enabled():
            return {
                "success": True,
                "recipient": str(recipient) if recipient else None,
                "executed": True,
                "sent": True,
                "synthetic": True,
                "result_state": "SENT_VERIFIED",
                "verification": {"state": "SENT_VERIFIED", "verified": True, "observed": {"synthetic": True}},
                "error": None,
            }
        target, resolved, error = self._resolve_send_target(recipient)
        if error:
            return {"success": False, "error": error}
        if not self.uia:
            return {"success": False, "error": {"code": "PROVIDER_UNAVAILABLE", "message": "UIA not configured"}}
        target_name = resolved["name"]
        target_id = resolved.get("id") or target_name
        self.ui_open_chat(target_name)
        current = self.ui_current_chat()
        current_name = current.get("name") if isinstance(current, dict) else current
        if current_name not in {target_name, str(recipient), target_id}:
            return {"success": False, "error": {"code": "TARGET_OPEN_FAILED", "message": f"无法打开聊天目标: {target_name}"}}
        before_messages = self._read_message_snapshot(target_name, limit=50)
        executed = bool(self.uia.send_image(path))
        after_messages = self._read_message_snapshot(target_name, limit=50)
        verification = self.verify_attachment_send_result(path, before_messages, after_messages, kind="image")
        return {
            "success": bool(executed and verification["verified"]),
            "recipient": target_name,
            "target": target,
            "executed": executed,
            "sent": verification["verified"],
            "result_state": verification["state"],
            "verification": verification,
            "error": (None if executed and verification["verified"] else
                      ({"code": "SEND_RESULT_NOT_OBSERVABLE", "message": "图片发送动作已执行，但未观察到可验证结果"}
                       if executed else {"code": "TOOL_ERROR", "message": "UIA image send failed"})),
        }

    def send_file(self, *args, **kwargs):
        recipient = kwargs.get("recipient") or kwargs.get("chat_id") or kwargs.get("target")
        path = kwargs.get("path")
        if not path:
            return {"success": False, "error": {"code": "PATH_REQUIRED", "message": "需要指定文件路径"}}
        # As above, block before target lookup/opening so disabled commands
        # cannot change the visible WeChat session as a side effect.
        if not self._synthetic_actuator_enabled() and not self.send_enabled():
            return self._send_disabled_result("file")
        if self._synthetic_actuator_enabled():
            return {
                "success": True,
                "recipient": str(recipient) if recipient else None,
                "executed": True,
                "sent": True,
                "synthetic": True,
                "result_state": "SENT_VERIFIED",
                "verification": {"state": "SENT_VERIFIED", "verified": True, "observed": {"synthetic": True}},
                "error": None,
            }
        target, resolved, error = self._resolve_send_target(recipient)
        if error:
            return {"success": False, "error": error}
        if not self.uia:
            return {"success": False, "error": {"code": "PROVIDER_UNAVAILABLE", "message": "UIA not configured"}}
        target_name = resolved["name"]
        target_id = resolved.get("id") or target_name
        self.ui_open_chat(target_name)
        current = self.ui_current_chat()
        current_name = current.get("name") if isinstance(current, dict) else current
        if current_name not in {target_name, str(recipient), target_id}:
            return {"success": False, "error": {"code": "TARGET_OPEN_FAILED", "message": f"无法打开聊天目标: {target_name}"}}
        before_messages = self._read_message_snapshot(target_name, limit=50)
        executed = bool(self.uia.send_file(path))
        after_messages = self._read_message_snapshot(target_name, limit=50)
        verification = self.verify_attachment_send_result(path, before_messages, after_messages, kind="file")
        return {
            "success": bool(executed and verification["verified"]),
            "recipient": target_name,
            "target": target,
            "executed": executed,
            "sent": verification["verified"],
            "result_state": verification["state"],
            "verification": verification,
            "error": (None if executed and verification["verified"] else
                      ({"code": "SEND_RESULT_NOT_OBSERVABLE", "message": "文件发送动作已执行，但未观察到可验证结果"}
                       if executed else {"code": "TOOL_ERROR", "message": "UIA file send failed"})),
        }

    def type_message(self, text):
        # This legacy endpoint writes to the actual input field.  It is not a
        # passive draft operation and follows the same default-off gate.
        if not self.send_enabled():
            return self._send_disabled_result("text")
        if not self.uia:
            raise ProviderUnavailable("UIA not configured")
        field = self.uia.get_chat_input_field()
        from .minimal_foreground_input import MinimalForegroundInput
        mfi = MinimalForegroundInput()
        before = mfi.save_foreground()
        success = mfi.set_text(field, text)
        changed = True
        return {
            "success": success,
            "foreground_required": True,
            "focus_changed": changed,
            "restored": mfi.restore_foreground(before),
            "duration_ms": mfi.last_duration_ms,
        }

    def draft_message(self, chat_id, text):
        draft = {
            "draft_id": uuid.uuid4().hex,
            "chat_id": chat_id,
            "text": text,
            "state": "DRAFT_READY",
            "confirmation_id": None,
        }
        self._drafts[draft["draft_id"]] = draft
        return draft
    def confirm_message(self, draft_id):
        draft = self._drafts.get(draft_id)
        if not draft: raise ProviderUnavailable("草稿不存在")
        draft["state"] = "CONFIRMED"
        draft["confirmation_id"] = uuid.uuid4().hex
        return draft

    @staticmethod
    def _message_texts(snapshot):
        if not isinstance(snapshot, dict):
            return set()
        return {
            str(row.get("text"))
            for row in (snapshot.get("messages") or [])
            if isinstance(row, dict) and row.get("text") is not None
        }

    def verify_send_result(self, text, before_input, after_input, before_messages, after_messages):
        """根据真实 UIA 观察判定发送结果，不以调用无异常代替成功。

        证据来源，任一成立即认为成功：
        * 消息列表里出现了这条文本（最权威，但需要遍历消息列表）；
        * 我们确实往输入框写了非空文本，而发送后输入框为空 —— 微信发送
          成功必然清空输入框。注意判据是「写入过内容」而不是「原本就有
          内容」：发送流程本就是先清空再写入，发送前输入框一定是空的，
          用 before_value 非空作条件永远不会成立（曾因此把成功的发送误判
          成失败）。
        """
        before_texts = self._message_texts(before_messages)
        after_texts = self._message_texts(after_messages)
        wanted = str(text or "")
        appeared = bool(after_texts) and wanted in after_texts and wanted not in before_texts
        before_value = before_input.get("text") if isinstance(before_input, dict) else None
        after_value = after_input.get("text") if isinstance(after_input, dict) else None
        # 可观测性：只有读到了状态才有资格判断，避免把读失败当成验证失败。
        observable = after_value is not None
        cleared = bool(wanted.strip()) and observable and after_value == ""
        confirmed = appeared or cleared
        observed = {
            "input_cleared": cleared,
            "message_text_appeared": appeared,
            "wrote_nonempty_text": bool(wanted.strip()),
            "input_value_observable": observable,
            "before_input_value": before_value,
            "after_input_value": after_value,
            "message_reader_observable": isinstance(after_messages, dict),
        }
        state = "SENT_VERIFIED" if confirmed else "SEND_RESULT_NOT_OBSERVABLE"
        return {"state": state, "verified": confirmed, "observed": observed}

    def verify_attachment_send_result(self, path, before_messages, after_messages, *, kind="file"):
        """用数据库快照判定图片/文件是否真实落库。"""
        file_name = os.path.basename(str(path or ""))
        stem = os.path.splitext(file_name)[0]
        before_ids = {
            str(row.get("message_id") or row.get("local_id"))
            for row in (before_messages or [])
            if isinstance(row, dict)
        }
        new_rows = [
            row for row in (after_messages or [])
            if isinstance(row, dict)
            and str(row.get("message_id") or row.get("local_id")) not in before_ids
        ]
        observed = {
            "before_count": len(before_messages or []),
            "after_count": len(after_messages or []),
            "new_message_count": len(new_rows),
            "file_name": file_name,
            "kind": kind,
        }
        verified = False
        for row in new_rows:
            content = str(row.get("content") or "")
            message_type = str(row.get("message_type") or row.get("type") or "").lower()
            if kind == "image" and ("图" in message_type or "[图片" in content or "图片" in content):
                verified = True
                break
            if kind == "file" and (file_name in content or stem in content or "文件" in message_type):
                verified = True
                break
        state = "SENT_VERIFIED" if verified else "SEND_RESULT_NOT_OBSERVABLE"
        return {"state": state, "verified": verified, "observed": observed}

    @staticmethod
    def _env_enabled(name: str) -> bool:
        return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def send_enabled(cls) -> bool:
        """真实发送总门禁；默认关闭，必须显式配置才允许写入微信。"""
        raw = os.getenv("WECHAT_ENABLE_SEND")
        if raw is None:
            return False
        return cls._env_enabled("WECHAT_ENABLE_SEND")

    @staticmethod
    def _synthetic_actuator_enabled() -> bool:
        return os.getenv("WECHAT_ACTUATOR_MODE", "").strip().lower() == "synthetic"

    def _send_observation(self, message_limit: int = 50, with_messages: bool = True):
        """采集发送前后的可观测量，用于验证消息是否真的落库。

        读消息列表要走 UIA 遍历，50 条在人多时会明显拖慢发送。因此拆成
        输入框状态（轻量，必读）与消息列表（较重，可按需跳过）两部分。
        """
        before_input = before_messages = None
        try:
            before_input = self.uia.get_input_state()
        except Exception:
            pass
        if with_messages:
            try:
                before_messages = self.uia.read_messages(message_limit)
            except Exception:
                pass
        return before_input, before_messages

    @staticmethod
    def _send_disabled_result(kind: str) -> dict:
        return {
            "success": False,
            "executed": False,
            "sent": False,
            "result_state": "SEND_DISABLED",
            "error": {
                "code": "SEND_DISABLED",
                "message": f"真实{kind}发送已被环境变量禁用；需显式开启 WECHAT_ENABLE_SEND",
                "retryable": False,
            },
        }

    def send_message(self, *args, **kwargs):
        recipient = kwargs.get("recipient") or kwargs.get("chat_id") or kwargs.get("target")
        if not recipient:
            return {"success": False, "error": {"code": "TARGET_REQUIRED", "message": "需要指定聊天目标"}}
        confirmation_id = kwargs.get("confirmation_id")
        draft = next((d for d in self._drafts.values() if d.get("confirmation_id") == confirmation_id), None) if confirmation_id else None
        if confirmation_id and (not draft or draft.get("state") != "CONFIRMED"):
            return {"success": False, "error": {"code": "SEND_CONFIRMATION_REQUIRED", "message": "需要已确认草稿"}}
        draft_text = draft["text"] if draft else None
        text = kwargs.get("content", kwargs.get("text", draft_text))
        if not isinstance(text, str) or not text.strip():
            return {"success": False, "error": {"code": "TEXT_REQUIRED", "message": "需要发送文本内容"}}
        if draft and text != draft["text"]:
            return {"success": False, "error": {"code": "SEND_CONFIRMATION_MISMATCH", "message": "内容与确认草稿不一致"}}
        if confirmation_id and confirmation_id in self._consumed:
            return {"success": False, "error": {"code": "SEND_ALREADY_CONSUMED", "message": "确认已使用"}}
        if self._synthetic_actuator_enabled():
            if confirmation_id:
                self._consumed.add(confirmation_id)
            return {
                "success": True,
                "recipient": str(recipient),
                "target": {"name": str(recipient)},
                "executed": True,
                "sent": True,
                "synthetic": True,
                "result_state": "SENT_VERIFIED",
                "verification": {
                    "state": "SENT_VERIFIED",
                    "verified": True,
                    "observed": {"synthetic": True},
                },
                "confirmation_id": confirmation_id,
                "send_method": "SYNTHETIC",
                "error": None,
            }
        if not self.send_enabled():
            result = self._send_disabled_result("文本")
            result.update({"recipient": str(recipient), "confirmation_id": confirmation_id})
            return result
        if not self.uia:
            return {"success": False, "error": {"code": "PROVIDER_UNAVAILABLE", "message": "UIA not configured"}}
        want = str(recipient)
        # 先在「可见会话列表」里找。找不到**不直接判失败**：侧边栏只渲染
        # 当前可见的十几个会话，排位靠后的联系人根本不在其中，但微信自带
        # 搜索框可以打开任何人。因此退化为「未知目标」，继续往下走，由
        # ui_open_chat 的搜索兜底去打开，最后仍以 current_chat 严格校验。
        candidates = self.ui_search_chat(want)
        if len(candidates) > 1:
            return {"success": False, "error": {"code": "AMBIGUOUS_TARGET", "message": f"聊天目标不唯一: {recipient}", "candidates": candidates}}
        # 安全关键：只有「名称或 id 与收件人完全相等」的候选才算确认命中。
        # 名称相似（前缀/子串）都不算——把回复发给错误的人是本工程最不能
        # 接受的失败模式，宁可判失败让人工确认。
        exact_hit = False
        target_name = want
        target_id = None
        if candidates:
            cand = candidates[0]
            cname = (cand.get("name") if isinstance(cand, dict) else str(cand)) or ""
            cid = cand.get("id") if isinstance(cand, dict) else None
            if cname == want or (cid and str(cid) == want):
                exact_hit = True
                target_name = cname or want
                target_id = cid
        target = {"name": target_name, "id": target_id}

        # 允许的「当前会话」取值：收件人本身；若候选是精确命中，再接受它的
        # 名称与 id。相似候选的名字不进入白名单，避免把错误会话判成成功。
        accepted = {want}
        if exact_hit:
            accepted.add(target_name)
            if target_id:
                accepted.add(str(target_id))

        already = False
        try:
            now = self.ui_current_chat()
            already = (now.get("name") if isinstance(now, dict) else now) in accepted
        except Exception:
            already = False
        if not already:
            self.ui_open_chat(target_name)
        current = self.ui_current_chat()
        current_name = current.get("name") if isinstance(current, dict) else current
        if current_name not in accepted:
            if already:
                # 当前会话匹配却仍判失败，说明读到的名字与目标写法不同，
                # 再走一次显式打开以消除歧义。
                self.ui_open_chat(target_name)
                current = self.ui_current_chat()
                current_name = current.get("name") if isinstance(current, dict) else current
            if current_name not in accepted:
                return {"success": False, "error": {
                    "code": "TARGET_OPEN_FAILED",
                    "message": f"无法打开聊天目标: {target_name}（当前停在 {current_name}）",
                    "expected": sorted(x for x in accepted if x),
                    "actual": current_name,
                }}
        if not hasattr(self.uia, "send_text"):
            return {"success": False, "error": {"code": "PROVIDER_UNAVAILABLE", "message": "UIA send implementation unavailable"}}
        # 发送前后的验证只取输入框状态：发送成功会让输入框清空，这个信号
        # 足够可靠，而且不需要遍历消息列表——后者在会话消息多时会显著拖慢
        # 一次发送（实测从 ~3s 涨到 ~19s）。
        before_input, before_messages = self._send_observation(with_messages=False)
        started = time.perf_counter()
        executed = bool(self.uia.send_text(text))
        if executed and confirmation_id:
            self._consumed.add(confirmation_id)
        after_input, after_messages = self._send_observation(with_messages=False)
        verification = self.verify_send_result(
            text, before_input, after_input, before_messages, after_messages)
        return {"success": bool(executed and verification["verified"]), "recipient": target_name, "target": target,
                "executed": executed, "sent": verification["verified"], "result_state": verification["state"],
                "verification": verification,
                "confirmation_id": confirmation_id, "send_method": "UIA",
                "duration_ms": int((time.perf_counter()-started)*1000),
                "error": (None if executed and verification["verified"] else
                          ({"code": "SEND_RESULT_NOT_OBSERVABLE", "message": "发送动作已执行，但未观察到可验证结果"}
                           if executed else {"code": "TOOL_ERROR", "message": "UIA send failed"}))}

    def execute_command(
        self,
        *,
        request_id: str,
        action: str,
        target: dict | None = None,
        payload: dict | None = None,
        timeout_ms: int = 15000,
    ) -> dict:
        """统一命令总线。

        request_id 用于幂等：同一个请求只执行一次，重试时返回第一次结果。
        真正发送默认仍被显式环境门禁保护；synthetic 模式复用完全相同的
        envelope，便于在不触碰微信的情况下验收相关性、错误和超时。
        """
        request_id = str(request_id or "").strip()
        action = str(action or "").strip()
        target = target if isinstance(target, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        if not request_id:
            return {
                "ok": False,
                "request_id": request_id,
                "action": action,
                "error": {"code": "INVALID_ARGUMENT", "message": "request_id 不能为空", "retryable": False},
            }
        try:
            timeout_value = None if timeout_ms is None else int(timeout_ms)
        except (TypeError, ValueError):
            timeout_value = 0

        # 先登记 inflight，再执行动作。相同 request_id 的并发调用只能由
        # 第一个 owner 触发底层 UIA；其余调用等待同一 Event 并复用结果。
        with self._command_lock:
            previous = self._command_results.get(request_id)
            if previous is not None:
                return {**previous, "deduplicated": True}
            inflight = self._command_inflight.get(request_id)
            owner = inflight is None
            if owner:
                inflight = threading.Event()
                self._command_inflight[request_id] = inflight

        if not owner:
            wait_seconds = None if timeout_value is None else max(0, timeout_value) / 1000.0
            if wait_seconds == 0 or not inflight.wait(wait_seconds):
                return {
                    "ok": False,
                    "request_id": request_id,
                    "action": action,
                    "error": {
                        "code": "TIMEOUT",
                        "message": "等待相同 request_id 的进行中命令超时",
                        "retryable": True,
                    },
                    "deduplicated": True,
                    "inflight": True,
                }
            with self._command_lock:
                previous = self._command_results.get(request_id)
            if previous is not None:
                return {**previous, "deduplicated": True}
            return {
                "ok": False,
                "request_id": request_id,
                "action": action,
                "error": {"code": "COMMAND_RESULT_MISSING", "message": "命令已结束但结果不可用", "retryable": True},
                "deduplicated": True,
            }

        if timeout_value is not None and timeout_value <= 0:
            result = {
                "ok": False,
                "request_id": request_id,
                "action": action,
                "error": {"code": "TIMEOUT", "message": "命令超时预算必须大于 0", "retryable": True},
            }
            self._remember_command(request_id, result, inflight)
            return result

        started = time.perf_counter()
        try:
            result_data = self._execute_command_action(action, target, payload)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if isinstance(result_data, dict) and result_data.get("state") == "SEND_DISABLED":
                result = {
                    "ok": False,
                    "request_id": request_id,
                    "action": action,
                    "error": result_data.get("error") or {
                        "code": "SEND_DISABLED",
                        "message": "真实发送已禁用",
                        "retryable": False,
                    },
                    "elapsed_ms": elapsed_ms,
                }
            elif timeout_value is not None and elapsed_ms > timeout_value:
                result = {
                    "ok": False,
                    "request_id": request_id,
                    "action": action,
                    "error": {"code": "TIMEOUT", "message": f"{action} 超过 {timeout_ms}ms", "retryable": True},
                    "elapsed_ms": elapsed_ms,
                }
            else:
                result = {
                    "ok": True,
                    "request_id": request_id,
                    "action": action,
                    "result": result_data,
                    "elapsed_ms": elapsed_ms,
                }
        except ProviderUnavailable as exc:
            result = {
                "ok": False,
                "request_id": request_id,
                "action": action,
                "error": {"code": "PROVIDER_UNAVAILABLE", "message": str(exc), "retryable": True},
            }
        except KeyError as exc:
            result = {
                "ok": False,
                "request_id": request_id,
                "action": action,
                "error": {"code": "INVALID_ARGUMENT", "message": f"缺少参数: {exc.args[0]}", "retryable": False},
            }
        except Exception as exc:
            result = {
                "ok": False,
                "request_id": request_id,
                "action": action,
                "error": {"code": "COMMAND_FAILED", "message": str(exc), "retryable": False},
            }
        self._remember_command(request_id, result, inflight)
        return result

    def _remember_command(self, request_id: str, result: dict, inflight=None) -> None:
        with self._command_lock:
            self._command_results[request_id] = result
            current = self._command_inflight.get(request_id)
            if current is inflight or inflight is None:
                self._command_inflight.pop(request_id, None)
            while len(self._command_results) > self._command_result_limit:
                self._command_results.pop(next(iter(self._command_results)))
        if inflight is not None:
            inflight.set()

    def _execute_command_action(self, action: str, target: dict, payload: dict):
        if action in {"wechat.state", "wechat.status"}:
            return self.state()
        if action in {"wechat.conversation.list", "wechat.list_chats"}:
            return self.list_chats()
        if action in {"wechat.conversation.search", "wechat.search_chat"}:
            return self.search_chat(payload.get("name") or target.get("name") or target.get("conversation_id"))
        if action in {"wechat.conversation.open", "wechat.open_chat"}:
            return self.open_chat(payload.get("name") or target.get("name") or target.get("conversation_id"))
        if action in {"wechat.message.read", "wechat.read_messages"}:
            return self.read_visible_messages(int(payload.get("limit", 20)))
        if action in {"wechat.message.latest"}:
            return self.latest_message()
        if action in {"wechat.database.inventory", "wechat.database_inventory"}:
            # 纯文件系统发现，不需要 UIA 也不需要数据库密钥。
            return self.database_inventory()
        if action in {"wechat.database.xinfo", "wechat.database_xinfo"}:
            # 读取明文 xInfo.db，同样与密钥无关。
            return self.database_xinfo()
        if action in {"wechat.account.list", "wechat.account_list"}:
            return self.account_list()
        if action in {"wechat.account.current", "wechat.account_current"}:
            return self.account_current()
        if action in {"wechat.contact.search", "wechat.search_contact"}:
            return self.search_contact(payload.get("name") or target.get("name") or target.get("conversation_id"))
        if action in {"wechat.group.search", "wechat.group_search"}:
            return self.group_search(payload.get("name") or target.get("name"))
        if action in {"wechat.group.info", "wechat.group_info"}:
            chat_id = target.get("conversation_id") or payload.get("conversation_id")
            if not chat_id:
                raise KeyError("target.conversation_id")
            return self.group_info(str(chat_id))
        if action in {"wechat.message.history", "wechat.get_messages"}:
            chat_id = target.get("conversation_id") or target.get("chat_id") or payload.get("chat_id")
            if not chat_id:
                raise KeyError("target.conversation_id")
            return self.message_history(str(chat_id), int(payload.get("limit", 50)), payload.get("before"))
        if action in {"wechat.group.search", "wechat.group_search"}:
            return self.group_search(payload.get("name") or target.get("name") or "")
        if action in {"wechat.group.info", "wechat.group_info"}:
            chat_id = target.get("conversation_id") or target.get("chat_id") or payload.get("chat_id")
            if not chat_id:
                raise KeyError("target.conversation_id")
            return self.group_info(str(chat_id))
        if action in {"wechat.message.send_text", "wechat.send_message"}:
            text = payload.get("text")
            recipient = target.get("conversation_id") or target.get("chat_id") or target.get("name")
            if not recipient or not isinstance(text, str) or not text:
                raise KeyError("target.conversation_id/payload.text")
            if self._synthetic_actuator_enabled():
                return {
                    "state": "SUCCESS",
                    "synthetic": True,
                    "target": recipient,
                    "text": text,
                }
            if not self.send_enabled():
                return {
                    "state": "SEND_DISABLED",
                    "synthetic": False,
                    "target": recipient,
                    "error": {
                        "code": "SEND_DISABLED",
                        "message": "真实发送已被环境变量禁用；可移除或显式开启 WECHAT_ENABLE_SEND",
                    },
                }
            outcome = self.send_message(recipient=str(recipient), content=text)
            if not outcome.get("success"):
                error = outcome.get("error") or {
                    "code": "SEND_FAILED",
                    "message": "发送失败",
                }
                raise RuntimeError(error.get("message", "发送失败"))
            return outcome
        if action in {"wechat.message.send_image", "wechat.send_image"}:
            path = payload.get("path")
            recipient = target.get("conversation_id") or target.get("chat_id") or target.get("name")
            if not recipient or not isinstance(path, str) or not path:
                raise KeyError("target.conversation_id/payload.path")
            outcome = self.send_image(recipient=recipient, path=path)
            if not outcome.get("success"):
                error = outcome.get("error") or {"code": "SEND_FAILED", "message": "图片发送失败"}
                raise RuntimeError(error.get("message", "图片发送失败"))
            return outcome
        if action in {"wechat.message.send_file", "wechat.send_file"}:
            path = payload.get("path")
            recipient = target.get("conversation_id") or target.get("chat_id") or target.get("name")
            if not recipient or not isinstance(path, str) or not path:
                raise KeyError("target.conversation_id/payload.path")
            outcome = self.send_file(recipient=recipient, path=path)
            if not outcome.get("success"):
                error = outcome.get("error") or {"code": "SEND_FAILED", "message": "文件发送失败"}
                raise RuntimeError(error.get("message", "文件发送失败"))
            return outcome
        raise ValueError(f"未实现命令: {action}")

    def read_messages(self, limit=20):
        if not self.uia: raise ProviderUnavailable("UIA not configured")
        if not hasattr(self.uia, "read_messages"): raise ProviderUnavailable("UIA message reader not available")
        return self.uia.read_messages(limit)
    def reply_draft(self, chat=None, auto_type=False):
        target = chat
        if target:
            self.ui_open_chat(target)
        target = target or self.ui_current_chat()
        result = self.read_messages(20)
        messages = result.get("messages", []) if isinstance(result, dict) else result
        latest = messages[-1]["text"] if messages else ""
        draft_text = "收到，我了解了。" if latest else "你好，最近怎么样？"
        draft = self.draft_message(target, draft_text)
        typed = self.type_message(draft_text) if auto_type else False
        return {"chat": target, "draft": draft_text, "draft_id": draft["draft_id"],
                "typed": bool(typed), "sent": False, "message_count": len(messages)}

    def tool_list(self):
        """Return executable state, not merely whether source code exists."""
        try:
            ui = self.ui_status()
            ready = bool(ui.get("connected")) if isinstance(ui, dict) else False
        except Exception:
            ready = False
        manifest = self.capability_manifest(ready=ready)
        canonical_for_legacy = {
            legacy.removeprefix("wechat."): canonical
            for canonical, legacy in LEGACY_CAPABILITY_NAMES.items()
        }
        legacy = []
        for name, meta in CAPABILITIES.items():
            effective = dict(meta)
            canonical_name = canonical_for_legacy.get(name)
            if canonical_name:
                effective.update(manifest.get(canonical_name, {}))
            elif name == "type_message":
                # This legacy command is an explicit interactive write, never
                # a background capability.  Its own implementation keeps the
                # default send gate closed.
                effective["execution_available"] = self.send_enabled()
                effective["status"] = "SEND_ENABLED" if self.send_enabled() else "SEND_DISABLED"
            elif name in {"current_chat", "draft_message", "confirm_message", "read_messages", "reply_draft"}:
                effective["execution_available"] = ready
                if not ready:
                    effective["status"] = "UIA_UNAVAILABLE"
            legacy.append({
                "name": f"wechat.{name}",
                "description": effective["reason"],
                "arguments": {"type": "object"},
                "risk": "high" if name in {"type_message", "send_message", "send_image", "send_file"} else "low",
                "foreground_required": not effective.get("background", True),
                "enabled": bool(effective.get("execution_available", effective.get("available"))),
                **effective,
            })
        canonical = []
        for name, base_meta in CANONICAL_CAPABILITIES.items():
            meta = manifest.get(name, base_meta)
            row = {"name": name, "description": meta["reason"],
                   "arguments": {"type": "object"},
                   "risk": "high" if name in {"wechat.message.send_text", "wechat.message.send_image", "wechat.message.send_file"} else "low",
                   "foreground_required": not meta.get("background", True),
                   "enabled": bool(meta.get("execution_available", meta.get("available"))),
                   **meta}
            canonical.append(row)
        return canonical + legacy

    def _manifest_item(self, name, meta, ready):
        item = dict(meta)
        ui_free = {
            "wechat.account.list",
            "wechat.account.current",
            "wechat.database.inventory",
            "wechat.database.xinfo",
        }
        item["execution_available"] = bool(meta.get("available")) and (True if name in ui_free else (ready or name == "wechat.state"))
        if name == "wechat.message.history":
            try:
                database_status = getattr(
                    self.provider, "get_control_plane_status", self.provider.get_status
                )()
            except Exception:
                database_status = {}
            item["execution_available"] = bool(database_status.get("sqlcipher_read_ready"))
            item["status"] = (
                "DATABASE_KEY_READY"
                if item["execution_available"]
                else database_status.get("database_key_status", "DATABASE_KEY_UNAVAILABLE")
            )
        if name in {"wechat.message.send_text", "wechat.message.send_image", "wechat.message.send_file"}:
            item["execution_available"] = item["execution_available"] and (
                WeChatService.send_enabled() or WeChatService._synthetic_actuator_enabled()
            )
            if not item["execution_available"] and not WeChatService._synthetic_actuator_enabled():
                item["status"] = "SEND_DISABLED"
        item.setdefault("verified", bool(item["execution_available"]))
        if name == "wechat.conversation.open" and item["execution_available"]:
            item["verified"] = True
        if not item["execution_available"] and name not in {
            "wechat.message.send_text",
            "wechat.message.history",
        }:
            item.setdefault("status", "UNAVAILABLE")
        return item

    def capability_manifest(self, ready: bool | None = None):
        """返回 canonical 能力及历史短别名的同一份动态状态。"""
        if ready is None:
            try:
                ui = self.ui_status()
                ready = bool(ui.get("connected")) if isinstance(ui, dict) else False
            except Exception:
                ready = False
        manifest = {
            name: self._manifest_item(name, meta, bool(ready))
            for name, meta in CANONICAL_CAPABILITIES.items()
        }
        manifest.update({
            "wechat.message.send_emoji": {
                "execution_available": False, "verified": False, "status": "NOT_SUPPORTED",
            },
        })
        for alias, canonical in LEGACY_MANIFEST_NAMES.items():
            source = manifest.get(canonical)
            if source is not None:
                manifest[alias] = {
                    **source,
                    "canonical_name": canonical,
                    "legacy_alias": alias,
                }
        return manifest

    def call_tool(self, name, arguments=None, session_id=None):
        args = arguments or {}
        session_id = session_id or "stateless"
        session = self._sessions.setdefault(session_id, {"session_id": session_id, "current_chat": None, "last_tool": None, "last_result": None, "pending_action": None})
        canonical = name if name in CANONICAL_CAPABILITIES else next(
            (key for key, value in LEGACY_CAPABILITY_NAMES.items() if value == name),
            name,
        )
        if name in CANONICAL_CAPABILITIES:
            started = time.perf_counter()
            target = {
                "conversation_id": args.get("conversation_id") or args.get("chat_id") or args.get("to") or args.get("name"),
                "name": args.get("name") or args.get("to"),
            }
            payload = dict(args)
            action_result = self.execute_command(
                request_id=uuid.uuid4().hex,
                action=canonical,
                target=target,
                payload=payload,
                timeout_ms=int(args.get("timeout_ms", 15000)),
            )
            if action_result.get("ok"):
                data = action_result.get("result")
                if canonical == "wechat.conversation.open":
                    session["current_chat"] = args.get("name") or args.get("conversation_id")
                session["last_tool"], session["last_result"] = name, data
                return {"success": True, "tool": name, "data": data, "error": None,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "foreground_required": not CANONICAL_CAPABILITIES[canonical].get("background", True),
                        "focus_changed": False, "session": session}
            return {"success": False, "tool": name, "data": None,
                    "error": action_result.get("error"),
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "foreground_required": not CANONICAL_CAPABILITIES[canonical].get("background", True),
                    "focus_changed": False, "session": session}
        dispatch = {
            "wechat.status": lambda: self.status(),
            "wechat.list_chats": lambda: self.ui_chats(),
            "wechat.current_chat": lambda: self.ui_current_chat(),
            "wechat.search_chat": lambda: self.ui_search_chat(args["name"]),
            "wechat.search_contact": lambda: self.search_contact(args["name"]),
            "wechat.open_chat": lambda: self.ui_open_chat(args["name"]),
            "wechat.account_list": lambda: self.account_list(),
            "wechat.account_current": lambda: self.account_current(),
            "wechat.database_inventory": lambda: self.database_inventory(),
            "wechat.database_xinfo": lambda: self.database_xinfo(),
            "wechat.type_message": lambda: self.type_message(args["text"]),
            "wechat.read_messages": lambda: self.read_messages(args.get("limit", 20)),
            "wechat.search_messages": lambda: self.search(args["query"], args.get("chat_id")),
            "wechat.draft_message": lambda: self.draft_message(args["chat_id"], args["text"]),
            "wechat.confirm_message": lambda: self.confirm_message(args["draft_id"]),
            "wechat.reply_draft": lambda: self.reply_draft(args.get("chat"), args.get("auto_type", False)),
            "wechat.send_message": lambda: self.send_message(**args),
            "wechat.send_image": lambda: self.send_image(**args),
            "wechat.send_file": lambda: self.send_file(**args),
        }
        if name not in dispatch:
            return {"success": False, "error": {"code": "TOOL_NOT_FOUND", "message": f"未知工具: {name}"}}
        try:
            started = time.perf_counter()
            data = dispatch[name]()
            if name == "wechat.open_chat":
                session["current_chat"] = args.get("name")
            session["last_tool"], session["last_result"] = name, data
            return {"success": True, "tool": name, "data": data, "error": None,
                    "duration_ms": int((time.perf_counter()-started)*1000),
                    "foreground_required": name == "wechat.type_message",
                    "focus_changed": name == "wechat.type_message", "session": session}
        except KeyError as exc:
            return {"success": False, "error": {"code": "INVALID_ARGUMENT", "message": f"缺少参数: {exc.args[0]}"}}
        except ProviderUnavailable as exc:
            return {"success": False, "error": {"code": "PROVIDER_UNAVAILABLE", "message": str(exc)}}
        except Exception as exc:
            return {"success": False, "error": {"code": "TOOL_ERROR", "message": str(exc)}}
