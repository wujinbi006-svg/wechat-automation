"""受支持的数据来源抽象：不包含数据库密钥提取或绕过逻辑。"""
from __future__ import annotations
import csv, json
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

@dataclass
class Message:
    message_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    timestamp: str
    message_type: str
    content: str

class WeChatDataProvider:
    available = True
    def get_status(self) -> dict: return {"available": self.available, "provider": self.__class__.__name__}
    def get_chats(self) -> list[dict]: return []
    def get_current_chat(self) -> dict | None: return None
    def get_contacts(self) -> list[dict]: return []
    def get_messages(self, chat_id: str, limit: int = 20, before: str | None = None) -> list[dict]: return []
    def search_messages(self, query: str, chat_id: str | None = None) -> list[dict]: return []
    def export_messages(self, *args, **kwargs): raise NotImplementedError

class MockDataProvider(WeChatDataProvider):
    def __init__(self, messages: list[Message] | None = None):
        self.messages = messages or []
    def get_messages(self, chat_id, limit=20, before=None):
        rows = [asdict(m) for m in self.messages if m.chat_id == chat_id]
        return rows[:limit]
    def search_messages(self, query, chat_id=None):
        return [asdict(m) for m in self.messages if query in m.content and (chat_id is None or m.chat_id == chat_id)]
    def get_chats(self):
        ids = list(dict.fromkeys(m.chat_id for m in self.messages))
        return [{"chat_id": i} for i in ids]

class ImportedDataProvider(WeChatDataProvider):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.messages = self._load()
    def _load(self):
        raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.suffix.lower()==".json" else None
        if raw is not None:
            rows = raw if isinstance(raw, list) else raw.get("messages", [])
        elif self.path.suffix.lower()==".csv":
            with self.path.open(encoding="utf-8-sig", newline="") as f: rows = list(csv.DictReader(f))
        else:
            rows = []
            text = None
            for enc in ("utf-8-sig", "gbk"):
                try: text = self.path.read_text(encoding=enc); break
                except UnicodeDecodeError: pass
            if text is None: raise ValueError("无法识别导入文件编码")
            for i, line in enumerate(text.splitlines()):
                if line.strip(): rows.append({"message_id": str(i), "content": line})
        return [Message(message_id=str(r.get("message_id", i)), chat_id=str(r.get("chat_id","")),
                        sender_id=str(r.get("sender_id","")), sender_name=str(r.get("sender_name","")),
                        timestamp=str(r.get("timestamp","")), message_type=str(r.get("message_type", r.get("type","text"))),
                        content=str(r.get("content",""))) for i,r in enumerate(rows)]
    def get_messages(self, chat_id, limit=20, before=None): return [asdict(m) for m in self.messages if m.chat_id==chat_id][:limit]
    def search_messages(self, query, chat_id=None): return [asdict(m) for m in self.messages if query in m.content and (chat_id is None or m.chat_id==chat_id)]
    def get_chats(self): return [{"chat_id": i} for i in dict.fromkeys(m.chat_id for m in self.messages)]

class UIADataProvider(WeChatDataProvider):
    """仅读取已验证的 UIA 状态；发送/SetValue 不在此 provider 中。"""
    def __init__(self, driver: Any = None):
        self.driver = driver
        self.available = driver is not None
    def get_status(self):
        if not self.driver: return {"available": False, "provider": self.__class__.__name__, "reason": "UIA driver not configured"}
        return {"available": True, "provider": self.__class__.__name__, "running": bool(self.driver.is_running())}
    def get_current_chat(self):
        if self.driver and hasattr(self.driver, "current_chat"): return self.driver.current_chat()
        return None

class DatabaseDataProvider(WeChatDataProvider):
    """本地数据库只读发现层。

    当前实现优先复用参考项目的 ``wechatauto.db.WeChatDB``，在本机完成
    SQLCipher 读面、消息查询与联系人解析；如果参考实现初始化失败，则
    仍保留明确的不可用状态，不伪造可读成功。
    """

    def __init__(self, base_dir: str | Path | None = None, wxid: str | None = None):
        from .database_adapter import DatabaseAdapter
        self.adapter = DatabaseAdapter(wxid=wxid, base_dir=base_dir)
        self._reference_db = None
        self._reference_db_error: str | None = None
        self._reference_db_attempted = False
        self._reference_db_dir = str(self.adapter.base_dir)
        self._reference_db = self._load_reference_db()

    @staticmethod
    def _reference_pkg_root() -> Path:
        return Path(__file__).resolve().parents[1] / "work" / "wechatauto_pkg" / "unzipped"

    def _load_reference_db(self):
        if self._reference_db_attempted:
            return self._reference_db
        self._reference_db_attempted = True
        account_dir = self.adapter.current_account_dir()
        if account_dir is None:
            self._reference_db_error = "REFERENCE_ACCOUNT_NOT_FOUND: 未找到当前微信账号目录"
            return None
        if not (account_dir / "db_storage").is_dir():
            self._reference_db_error = (
                "REFERENCE_LAYOUT_UNSUPPORTED: 当前账号使用旧式 Msg 数据库布局；"
                "参考 WeChatDB 只支持 db_storage 布局"
            )
            return None
        pkg_root = self._reference_pkg_root()
        if pkg_root.exists():
            import sys
            if str(pkg_root) not in sys.path:
                sys.path.insert(0, str(pkg_root))
        try:
            from wechatauto.db import WeChatDB  # type: ignore
        except Exception as exc:
            self._reference_db_error = f"REFERENCE_PACKAGE_UNAVAILABLE: {type(exc).__name__}: {exc}"
            return None
        try:
            return WeChatDB(db_dir=self._reference_db_dir)
        except Exception as exc:
            self._reference_db_error = f"REFERENCE_DB_INIT_FAILED: {type(exc).__name__}: {exc}"
            return None

    def _db(self):
        if self._reference_db is None:
            self._load_reference_db()
        if self._reference_db is None:
            from .errors import DatabaseUnavailable
            raise DatabaseUnavailable(self._reference_db_error or "SQLCipher read backend unavailable")
        return self._reference_db

    @staticmethod
    def _resolve_user(db, chat_id: str) -> str:
        query = str(chat_id or "").strip()
        if not query:
            return query
        if query.startswith("wxid_") or "@" in query or query == "filehelper":
            return query
        for row in getattr(db, "get_sessions", lambda *_: [])(limit=200) or []:
            if str(row.get("username") or "") == query:
                return query
        for row in getattr(db, "search_contact", lambda *_: [])(query) or []:
            for key in ("username", "remark", "nick_name"):
                value = row.get(key)
                if value and str(value).strip() == query:
                    return str(row.get("username") or query)
        return query

    @staticmethod
    def _normalize_message(chat_id: str, row: dict, db=None) -> dict:
        sender_username = row.get("sender_username") or ""
        sender_name = sender_username
        if db is not None and sender_username:
            getter = getattr(db, "get_nickname", None)
            if getter is not None:
                try:
                    sender_name = getter(sender_username) or sender_username
                except Exception:
                    sender_name = sender_username
        create_time = row.get("create_time")
        timestamp = None
        if isinstance(create_time, (int, float)):
            from datetime import datetime, timezone
            timestamp = datetime.fromtimestamp(float(create_time), tz=timezone.utc).isoformat()
        message_id = row.get("message_id") or row.get("local_id")
        content = row.get("content")
        return {
            "message_id": str(message_id) if message_id is not None else None,
            "chat_id": str(chat_id),
            "sender_id": row.get("sender_id"),
            "sender_name": sender_name or sender_username or None,
            "timestamp": timestamp,
            "create_time": create_time,
            "message_type": row.get("type") or row.get("message_type") or "text",
            "content": content if content is not None else "",
            "sort_seq": row.get("sort_seq"),
            "local_id": row.get("local_id"),
        }

    def get_status(self):
        status = self.adapter.get_status()
        status["key_discovery"] = self.adapter.key_discovery_status()
        status["provider"] = self.__class__.__name__
        account_dir = self.adapter.current_account_dir()
        if account_dir is None:
            status["reference_layout"] = "unknown"
        elif (account_dir / "db_storage").is_dir():
            status["reference_layout"] = "db_storage"
        elif (account_dir / "Msg").is_dir():
            status["reference_layout"] = "legacy_msg"
        else:
            status["reference_layout"] = "unknown"
        # schema probe 对两种布局都适用：它直接尝试用已通过校验的密钥打开
        # 并读取 sqlite_master。此前只看 legacy_msg，导致 4.x 下即使解密封
        # 成功也永远报 sqlcipher 不可用。
        schema_probe = self._probe_legacy_schema()
        encrypted_names = [name for name, info in self.adapter.discover_databases().items()
                           if info.encrypted]
        all_encrypted_ready = bool(encrypted_names) and all(
            self.adapter._key_resolutions.get(name)
            and self.adapter._key_resolutions[name].status == "READY"
            for name in encrypted_names
        )
        reference_ready = self._reference_db is not None
        # 仅当参考库或全部加密库均有通过页级 HMAC 的 key 时，才声明 SQLCipher
        # 读面可用。单库 schema probe 只能作为诊断信息，绝不能升级能力状态。
        sqlcipher_ready = bool(reference_ready or (all_encrypted_ready and schema_probe))
        status["read_plane"] = ("sqlcipher_reference" if reference_ready else
                                 "sqlcipher_4x_adapter" if sqlcipher_ready else
                                 "sqlcipher_legacy" if sqlcipher_ready else status.get("read_plane"))
        status["key_discovered"] = bool(reference_ready or any(
            item.status == "READY" for item in self.adapter._key_resolutions.values()))
        status["sqlcipher_open_success"] = sqlcipher_ready
        status["sqlcipher_read_ready"] = sqlcipher_ready
        if self._reference_db_error and self._reference_db is None:
            status["reference_db_error"] = self._reference_db_error
            status["reference_db_error_code"] = self._reference_db_error.split(":", 1)[0]
        return status

    def get_control_plane_status(self):
        """Return a bounded database summary for Gateway health endpoints.

        ``get_status`` is intentionally exhaustive: it discovers database
        files and validates supplied/cached keys. Running that work from
        ``/capabilities`` or ``/tools/list`` makes an unavailable SQLCipher
        read plane block UIA control discovery. The control plane only needs
        the last known key state, so it must not trigger key resolution here.
        Explicit database tools still use the exhaustive path above.
        """
        account_dir = self.adapter.current_account_dir()
        known = dict(self.adapter._key_resolutions)
        ready = [item for item in known.values() if item.status == "READY"]
        return {
            "available": bool(account_dir),
            "provider": self.__class__.__name__,
            "current_account_dir": str(account_dir) if account_dir else None,
            "layout": self.adapter.layout,
            "read_plane": "sqlcipher_reference" if self._reference_db is not None else "unavailable",
            "database_key_status": "DATABASE_KEY_READY" if ready else "DATABASE_KEY_UNAVAILABLE",
            "key_discovered": bool(ready),
            "sqlcipher_open_success": bool(self._reference_db is not None),
            "sqlcipher_read_ready": bool(self._reference_db is not None),
            "key_discovery": self.adapter.key_discovery_status(),
            "status_mode": "CONTROL_PLANE_CACHED",
        }

    @staticmethod
    def _quote_identifier(value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'

    def _probe_legacy_schema(self) -> bool:
        """只读打开一份已验证密钥的旧式 Msg 数据库并读取 sqlite_master。"""
        for name, resolution in self.adapter.resolve_keys().items():
            if resolution.status != "READY":
                continue
            try:
                conn = self.adapter.open(name)
                conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                conn.close()
                return True
            except Exception:
                continue
        return False

    def _legacy_message_rows(self, chat_id: str, limit: int) -> list[dict]:
        """兼容旧式 Msg/MSG*.db；表结构按实际 schema 动态选择，不猜测成功。"""
        if not any(item.status == "READY" for item in self.adapter.resolve_keys().values()):
            from .errors import DatabaseUnavailable
            raise DatabaseUnavailable(self._reference_db_error or "KEY_DISCOVERY_PENDING: 数据库密钥不可用")
        databases = self.adapter.discover_databases()
        preferred = sorted(
            (name for name in databases if databases[name].encrypted and
             ("msg" in name.lower() or "chatmsg" in name.lower())),
            key=lambda item: ("multi" not in item.lower(), item.lower()),
        )
        target = "Msg_" + hashlib.md5(str(chat_id).encode()).hexdigest()
        for name in preferred:
            try:
                conn = self.adapter.open(name)
            except Exception:
                continue
            try:
                tables = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                candidates = [target] if target in tables else [
                    table for table in tables if table.lower().startswith("msg")
                ]
                for table in candidates:
                    columns = [row[1] for row in conn.execute(
                        f"PRAGMA table_info({self._quote_identifier(table)})"
                    ).fetchall()]
                    if not columns:
                        continue
                    names_by_lower = {column.lower(): column for column in columns}
                    content_col = next((names_by_lower[c] for c in
                                        ("message_content", "content", "msg_content", "text")
                                        if c in names_by_lower), None)
                    if content_col is None:
                        continue
                    order_col = next((names_by_lower[c] for c in
                                      ("sort_seq", "create_time", "local_id", "id")
                                      if c in names_by_lower), None)
                    order_sql = f" ORDER BY {self._quote_identifier(order_col)} DESC" if order_col else ""
                    rows = conn.execute(
                        f"SELECT * FROM {self._quote_identifier(table)}{order_sql} LIMIT ?",
                        (int(limit),),
                    ).fetchall()
                    return [dict(row) for row in rows]
            finally:
                conn.close()
        return []

    def get_accounts(self):
        return self.adapter.discover_account_records()

    def get_current_account(self):
        return self.adapter.current_account()

    def get_database_inventory(self, wxid: str | None = None):
        databases = self.adapter.discover_databases(wxid)
        return {
            name: {
                "path": str(info.path),
                "size": info.size,
                "modified": info.modified.isoformat(),
                "encrypted": info.encrypted,
                "has_wal": info.wal_path is not None,
                "has_shm": info.shm_path is not None,
                "is_active": info.is_active,
            }
            for name, info in databases.items()
        }

    def get_xinfo(self, wxid: str | None = None):
        return self.adapter.read_xinfo(wxid)

    def get_contacts(self):
        if self._reference_db is not None:
            return list(getattr(self._reference_db, "search_contact", lambda *_: [])("") or [])
        # 4.x：直接用适配器读取解密副本，不依赖参考实现。
        if self._adapter_keys_ready():
            return self.adapter.get_contacts()
        return []

    def _adapter_keys_ready(self) -> bool:
        """本适配器是否已为全部加密库拿到通过校验的密钥。"""
        try:
            databases = self.adapter.discover_databases()
            encrypted = [name for name, info in databases.items() if info.encrypted]
            if not encrypted:
                return False
            resolutions = self.adapter.resolve_keys()
            return all(resolutions.get(name) and resolutions[name].status == "READY"
                       for name in encrypted)
        except Exception:
            return False

    def get_chats(self):
        if self._reference_db is not None:
            sessions = []
            for row in getattr(self._reference_db, "get_sessions", lambda *_: [])(limit=200) or []:
                sessions.append({
                    "id": row.get("username"),
                    "chat_id": row.get("username"),
                    "name": row.get("username"),
                    "preview": row.get("summary"),
                    "time": row.get("last_time"),
                    "unread": row.get("unread"),
                })
            return sessions
        if self._adapter_keys_ready():
            return self.adapter.get_chats()
        return []

    def get_messages(self, chat_id, limit=20, before=None):
        if self._reference_db is None:
            if self.adapter.layout == "legacy_msg":
                rows = self._legacy_message_rows(chat_id, int(limit))
                return [self._normalize_message(str(chat_id), row) for row in rows]
            if self._adapter_keys_ready():
                return self.adapter.get_messages(str(chat_id), int(limit), before)
            return []
        db = self._db()
        user = self._resolve_user(db, chat_id)
        rows = getattr(db, "get_messages")(user, limit=int(limit))
        if before is not None:
            try:
                before_seq = int(before)
                rows = [row for row in rows if int(row.get("sort_seq") or 0) < before_seq]
            except (TypeError, ValueError):
                pass
        rows = list(rows or [])
        return [self._normalize_message(user, row, db=db) for row in rows[: int(limit)]]

    def search_messages(self, query, chat_id=None, limit=20):
        if self._reference_db is None:
            if self.adapter.layout == "legacy_msg":
                rows = self._legacy_message_rows(chat_id or "", int(limit) * 4)
                return [self._normalize_message(str(chat_id or ""), row)
                        for row in rows if str(query) in str(row.get("content") or row.get("message_content") or "")][:int(limit)]
            if self._adapter_keys_ready():
                return self.adapter.search_messages(str(query), chat_id, int(limit))
            return []
        db = self._db()
        if chat_id is not None:
            user = self._resolve_user(db, chat_id)
            rows = getattr(db, "search_messages")(query, chat_id=user, limit=int(limit))
            return [self._normalize_message(user, row, db=db) for row in (rows or [])[: int(limit)]]
        results = []
        for row in getattr(db, "get_sessions", lambda *_: [])(limit=200) or []:
            user = row.get("username")
            try:
                rows = getattr(db, "search_messages")(query, chat_id=user, limit=int(limit))
            except Exception:
                continue
            results.extend(self._normalize_message(user, item, db=db) for item in (rows or []))
            if len(results) >= int(limit):
                break
        return results[: int(limit)]
