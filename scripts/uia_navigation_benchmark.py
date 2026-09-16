"""有限次数 UIA 导航基准测试。

只测试明确传入的聊天目标，不群发、不发送消息；结果写入 logs/。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen


def call(base: str, name: str, arguments: dict):
    body = json.dumps({"name": name, "arguments": arguments, "session_id": "uia-benchmark"}).encode()
    req = Request(f"{base}/tools/call", body=body, headers={"content-type": "application/json"})
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    rows = []
    for _ in range(max(1, args.rounds)):
        for target in args.targets:
            started = time.perf_counter()
            result = call(args.base, "wechat.open_chat", {"name": target})
            rows.append({"target": target, "elapsed_ms": int((time.perf_counter() - started) * 1000),
                         "success": bool(result.get("success") and result.get("data", {}).get("success", True)),
                         "result": result})
    output = Path("logs") / "uia_navigation_benchmark.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"targets": args.targets, "rounds": args.rounds, "rows": rows},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "rows": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
