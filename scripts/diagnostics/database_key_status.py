"""安全的数据库密钥状态诊断。

只检查文件布局、页级验证状态和 sqlite_master 是否可读；不会扫描微信进程
内存、注入进程、写入微信数据库或输出任何密钥内容。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.providers import DatabaseDataProvider  # noqa: E402


def main() -> int:
    provider = DatabaseDataProvider()
    status = provider.get_status()
    payload = {
        "generated_at": time.time(),
        "base_dir": status.get("base_dir"),
        "base_dir_source": status.get("base_dir_source"),
        "account": status.get("current_account"),
        "account_dir": status.get("current_account_dir"),
        "layout": status.get("layout") or status.get("reference_layout"),
        "database_files_found": status.get("database_files_found"),
        "database_count": status.get("database_count"),
        "key_discovery_state": status.get("key_discovery_state"),
        "database_key_status": status.get("database_key_status"),
        "database_status_code": status.get("database_status_code"),
        "key_source": status.get("key_source", []),
        "sqlcipher_open_success": status.get("sqlcipher_open_success", False),
        "schema_read": status.get("sqlcipher_read_ready", False),
        "retryable": status.get("key_discovery_state") in {"PENDING", "PARTIAL", "INVALID"},
        "resolution_summary": {
            name: {
                "status": item.get("status"),
                "strategy": item.get("strategy"),
                "validation": item.get("validation"),
                "retryable": item.get("retryable", False),
            }
            for name, item in (status.get("key_resolution") or {}).items()
        },
    }
    evidence = ROOT / "docs" / "evidence" / "database_key_status.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
