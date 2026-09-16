"""Deprecated database-key diagnostic.

This repository deliberately does not read another process's memory. The
previous diagnostic implemented such a scan; retaining an executable version
would make it too easy for a routine investigation to cross that boundary.
Only explicit user-supplied keys and locally cached keys that pass page HMAC
validation are supported by :mod:`src.key_resolver`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    result = {
        "generated_at": time.time(),
        "status": "BLOCKED",
        "reason": "PROCESS_MEMORY_SCAN_PROHIBITED",
        "allowed_sources": [
            "WECHAT_DB_KEY",
            "WECHAT_DB_KEY_FILE",
            "WECHAT_DB_KEYS_FILE",
            "validated_cache",
        ],
        "validation_requirement": "PAGE_HMAC_VALID",
    }
    evidence = ROOT / "docs" / "evidence" / "key_memory_probe_blocked.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
