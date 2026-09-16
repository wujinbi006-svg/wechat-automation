"""Run a passive foreground-invariance observation for the Interactive Agent."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.interactive_ipc import rpc  # noqa: E402
from src.uia_service import ReplicaUIADriver  # noqa: E402


def foreground_snapshot() -> dict:
    return ReplicaUIADriver._foreground_snapshot()


def foreground_window_changed(before: dict, after: dict) -> bool:
    """Return whether the user's foreground *window* changed.

    The driver-level invariant intentionally also treats focus/capture/z-order
    mutations as suspicious, because it protects a single UIA operation.  A
    long-running observation is different: z-order can fluctuate while the
    foreground HWND and process remain unchanged (for example, Chrome's
    transient child windows).  That is useful telemetry, but not evidence that
    the monitoring agent stole the foreground.
    """
    return (
        before.get("hwnd") != after.get("hwnd")
        or before.get("pid") != after.get("pid")
    )


def wechat_pids() -> list[int]:
    try:
        import psutil

        result = []
        for process in psutil.process_iter(["pid", "name", "exe"]):
            info = process.info
            name = str(info.get("name") or "").lower()
            exe = str(info.get("exe") or "").lower()
            if name == "weixin.exe" or exe.endswith("\\weixin.exe"):
                result.append(int(info["pid"]))
        return sorted(set(result))
    except Exception:
        return []


def read_agent() -> dict:
    result = {}
    # ``status`` is deliberately observational.  Do not turn a foreground
    # safety test into a reconnect/load test when the UIA tree is absent.
    try:
        status = rpc("status", {}, timeout=8.0)
        result["status"] = status
    except Exception as exc:
        result["status"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return result

    if not status.get("uia_ready"):
        result["get_chats"] = {"ok": False, "skipped": "UIA_NOT_READY"}
        result["read_messages"] = {"ok": False, "skipped": "UIA_NOT_READY"}
        return result

    for method, params in (("get_chats", {}), ("read_messages", {"limit": 5})):
        try:
            value = rpc(method, params, timeout=8.0)
            if method == "get_chats":
                result[method] = {"ok": True, "count": len(value or [])}
            else:
                result[method] = {"ok": True, "count": len(value or [])}
        except Exception as exc:
            result[method] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return result


def main() -> int:
    duration = float(os.getenv("FOREGROUND_MONITOR_SECONDS", "60"))
    interval = float(os.getenv("FOREGROUND_MONITOR_INTERVAL", "1"))
    started = time.time()
    baseline = foreground_snapshot()
    pids = wechat_pids()
    samples = []
    while time.time() - started < duration:
        now = time.time()
        foreground = foreground_snapshot()
        agent = read_agent()
        samples.append(
            {
                "timestamp": now,
                "foreground": foreground,
                "foreground_changed": ReplicaUIADriver._foreground_snapshot_changed(
                    baseline, foreground
                ),
                "foreground_window_changed": foreground_window_changed(
                    baseline, foreground
                ),
                "wechat_pids": pids,
                "wechat_foreground": foreground.get("pid") in pids if pids else False,
                "agent": agent,
            }
        )
        remaining = duration - (time.time() - started)
        if remaining > 0:
            time.sleep(min(interval, remaining))
    ended = time.time()
    wechat_foreground_samples = [sample for sample in samples if sample["wechat_foreground"]]
    strict_changed_samples = [sample for sample in samples if sample["foreground_changed"]]
    window_changed_samples = [sample for sample in samples if sample["foreground_window_changed"]]
    payload = {
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": ended - started,
        "baseline": baseline,
        "wechat_pids": pids,
        "sample_count": len(samples),
        "foreground_steal_detected": bool(wechat_foreground_samples),
        "foreground_changed": bool(strict_changed_samples),
        "foreground_window_changed": bool(window_changed_samples),
        "foreground_window_stable": not bool(window_changed_samples),
        "uia_ready": bool(
            samples
            and samples[-1].get("agent", {}).get("status", {}).get("uia_ready")
        ),
        "message_listener": bool(
            samples
            and samples[-1].get("agent", {}).get("status", {}).get("message_listener")
        ),
        "samples": samples,
    }
    payload["background_monitoring_status"] = (
        "PASS"
        if (
            not payload["foreground_steal_detected"]
            and payload["uia_ready"]
            and payload["message_listener"]
        )
        else "BLOCKED"
    )
    payload["blocking_reasons"] = [
        reason
        for reason, failed in (
            ("foreground_steal_detected", payload["foreground_steal_detected"]),
            ("uia_not_ready", not payload["uia_ready"]),
            ("message_listener_not_ready", not payload["message_listener"]),
        )
        if failed
    ]
    evidence = ROOT / "docs" / "evidence" / "foreground_monitor_60s.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "samples"}, ensure_ascii=False, indent=2))
    if payload["foreground_steal_detected"] or payload["foreground_changed"]:
        return 2
    return 0 if payload["background_monitoring_status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
