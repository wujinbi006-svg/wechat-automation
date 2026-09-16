"""Gateway 长稳观测器；默认 10 分钟，每 10 秒采样，不执行微信写操作。"""
from __future__ import annotations
import argparse, csv, time
from datetime import datetime, timezone
import urllib.request, json

def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8010")
    p.add_argument("--seconds", type=int, default=600)
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--output", default="logs/long_run.csv")
    args = p.parse_args()
    started = time.monotonic()
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        fields = ["timestamp","uptime_seconds","wechat_process","wechat_connected","message_listener","connected_clients","reconnect_count","last_message_event","last_error"]
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        while time.monotonic() - started < args.seconds:
            status = get_json(args.url + "/status")["data"]
            row = {k: status.get(k) for k in fields if k != "timestamp"}
            row["timestamp"] = datetime.now(timezone.utc).isoformat()
            writer.writerow(row); f.flush()
            print(json.dumps(row, ensure_ascii=False))
            time.sleep(args.interval)
    print(f"saved: {args.output}")

if __name__ == "__main__":
    main()
