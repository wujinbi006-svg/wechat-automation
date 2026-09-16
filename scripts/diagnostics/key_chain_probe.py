"""Deprecated process-memory key-chain diagnostic.

Key discovery must not inspect WeChat process memory, inject code, or bypass
SQLCipher page HMAC validation. This command intentionally records a blocked
result instead of attempting any process access.
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
        "validation_requirement": "PAGE_HMAC_VALID",
        "key_candidates": 0,
        "valid_keys": 0,
    }
    evidence = ROOT / "docs" / "evidence" / "key_chain_probe_blocked.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
