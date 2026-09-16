"""WeChat 4.x 数据库最小只读实机验证。

不会保存真实密钥、内存转储或整库；结果仅写入 docs/evidence。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "work" / "wechatauto_pkg" / "unzipped"
EVIDENCE = ROOT / "docs" / "evidence"
ACCOUNT_DIR = Path(os.environ.get("WECHAT_ACCOUNT_DIR", r"__USERPROFILE__\Documents\WeChat Files\wxid_EXAMPLE"))


def load_db():
    sys.path.insert(0, str(PKG))
    from wechatauto.db import WeChatDB  # type: ignore
    return WeChatDB


def discover():
    rows = []
    if not ACCOUNT_DIR.is_dir():
        return rows
    for p in ACCOUNT_DIR.rglob("*"):
        if p.is_file() and (p.name.endswith(".db") or p.name.endswith(".db-wal") or p.name.endswith(".db-shm")):
            st = p.stat()
            rows.append({"path": str(p), "relative": str(p.relative_to(ACCOUNT_DIR)), "size": st.st_size,
                         "mtime": st.st_mtime, "kind": "wal" if p.name.endswith("-wal") else "shm" if p.name.endswith("-shm") else "db"})
    return sorted(rows, key=lambda x: x["relative"])


def main():
    started = time.perf_counter()
    inventory = discover()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "database_inventory.json").write_text(json.dumps({
        "account_dir": str(ACCOUNT_DIR), "account_wxid": ACCOUNT_DIR.name,
        "files": inventory, "database_files_found": bool(inventory),
        "generated_at": time.time()
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"account": ACCOUNT_DIR.name, "database_files_found": bool(inventory),
              "encrypted": None, "key_discovered": False, "sqlcipher_open_success": False,
              "schema_discovered": False, "message_query_success": False,
              "message_read_success": False, "timings_ms": {}}
    dbs = [x for x in inventory if x["kind"] == "db"]
    result["encrypted"] = any(Path(x["path"]).read_bytes()[:16] != b"SQLite format 3\x00" for x in dbs)
    if not dbs:
        result["error"] = "未发现数据库文件"
        (EVIDENCE / "database_open_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2
    t = time.perf_counter()
    try:
        cls = load_db()
        obj = cls.__new__(cls)
        obj.db_dir = str(ACCOUNT_DIR.parent)
        obj.account = ACCOUNT_DIR.name
        obj.account_dir = str(ACCOUNT_DIR)
        obj.workdir = tempfile.mkdtemp(prefix="wechat_db_test_")
        obj.keys_file = os.path.join(obj.workdir, "disabled_keys.json")
        obj._keys = {}
        obj._db_files = [(x["relative"], x["path"], x["size"]) for x in dbs]
        keys = obj.extract_keys()
        result["key_discovered"] = bool(keys)
        result["key_count"] = len(keys)
        result["key_fingerprints"] = {k: hashlib.sha256(v).hexdigest()[:12] for k, v in keys.items()}
        obj._keys.update(keys)
        result["timings_ms"]["key_discovery"] = round((time.perf_counter() - t) * 1000, 1)
        # 选择已验证可解密的首个数据库；所有输出均为结构信息，不保存密钥。
        for rel, _, _ in obj._db_files:
            try:
                conn = obj._open(rel)
                result["sqlcipher_open_success"] = True
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                result["schema_discovered"] = bool(tables)
                result["selected_database"] = rel
                result["tables"] = tables[:100]
                candidates = [t for t in tables if t.lower().startswith("msg") or "message" in t.lower()]
                for table in candidates:
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
                    if not cols:
                        continue
                    result.setdefault("schema", {})[table] = cols
                    rows = conn.execute(f'SELECT * FROM "{table}" LIMIT 20').fetchall()
                    for row in rows:
                        vals = list(row)
                        text = " ".join(str(v) for v in vals if isinstance(v, str))
                        if text:
                            result["message_query_success"] = True
                            result["message_read_success"] = True
                            result["message_preview"] = text[:200]
                            break
                    if result["message_read_success"]:
                        break
                conn.close()
                if result["message_read_success"]:
                    break
            except Exception as exc:
                result.setdefault("open_errors", []).append({"database": rel, "error": f"{type(exc).__name__}: {exc}"})
        result["timings_ms"]["database_open_and_query"] = round((time.perf_counter() - t) * 1000, 1)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["timings_ms"]["key_discovery"] = round((time.perf_counter() - t) * 1000, 1)
    result["timings_ms"]["total"] = round((time.perf_counter() - started) * 1000, 1)
    result["key_fingerprints"] = result.get("key_fingerprints", {})
    (EVIDENCE / "database_open_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVIDENCE / "database_schema.json").write_text(json.dumps({"selected_database": result.get("selected_database"), "schema": result.get("schema", {}), "tables": result.get("tables", [])}, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVIDENCE / "database_single_message.json").write_text(json.dumps({"message_read_success": result["message_read_success"], "message_preview": result.get("message_preview")}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["message_read_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
