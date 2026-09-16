"""解析某个好友的会话身份：wxid、备注名、昵称，以及该会话消息的方向归属。

只读。用于给 config/auto_reply.json 填正确的 targets[].name。

    python scripts\diagnostics\resolve_friend.py 好友A
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database_adapter import DatabaseAdapter  # noqa: E402


def main() -> int:
    needle = sys.argv[1] if len(sys.argv) > 1 else "好友A"
    adapter = DatabaseAdapter()
    accounts = adapter.discover_accounts()
    print("accounts:", accounts)

    hits: list[dict] = []
    for name in adapter._message_db_names():
        try:
            conn = adapter._open_readonly(name)
        except Exception as exc:
            print(f"  skip {name}: {exc}")
            continue
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "Name2Id" in tables:
                for rowid, user_name in conn.execute(
                        "SELECT rowid, user_name FROM Name2Id ORDER BY rowid"):
                    if needle in str(user_name):
                        hits.append({"db": name, "table": "Name2Id",
                                     "rowid": rowid, "user_name": user_name})
        finally:
            conn.close()

    print(f"\nName2Id hits for {needle!r}: {len(hits)}")
    for h in hits:
        print(" ", json.dumps(h, ensure_ascii=False))

    chat = adapter.resolve_username(needle)
    print(f"\nresolve_username({needle!r}) -> {chat!r}")

    if chat:
        # 会话表里查这一行的元数据（备注名）
        for db_name in ("session/session.db",):
            try:
                conn = adapter._open_readonly(db_name)
            except Exception:
                continue
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for tbl in sorted(tables):
                    cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{tbl}")')]
                    if "username" not in cols:
                        continue
                    text_cols = [c for c in cols if c not in
                                 ("username", "sort_timestamp", "last_timestamp")
                                 and c in {"remark", "nick_name", "alias", "local_type"}]
                    sel = ", ".join(["username"] + text_cols)
                    for row in conn.execute(
                            f'SELECT {sel} FROM "{tbl}" WHERE username=?', (chat,)):
                        print(f"  {db_name}:{tbl} -> {dict(zip(['username']+text_cols, row))}")
            finally:
                conn.close()

        rows = adapter.get_messages(chat, limit=12)
        print(f"\nlast {len(rows)} messages in {chat}:")
        name_map: dict[int, str] = {}
        for db_name in adapter._message_db_names():
            try:
                conn = adapter._open_readonly(db_name)
            except Exception:
                continue
            try:
                if {r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")} >= {"Name2Id"}:
                    for rowid, user_name in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                        name_map[rowid] = user_name
            finally:
                conn.close()
        for row in reversed(rows):
            sender = row.get("sender_id")
            kind = "INBOUND(friend)" if sender == chat else "OUTBOUND(me/other)"
            print(f"  [{kind:22}] sender_id={sender!r:28} type={row.get('message_type')!r:12} "
                  f"{str(row.get('content'))[:48]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
