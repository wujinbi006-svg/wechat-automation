"""真实微信消息读取验证；只读，不发送、不滚动。"""
import hashlib, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.uia_service import ReplicaUIADriver, WeChatUIAService

def main():
    service = WeChatUIAService(ReplicaUIADriver(), timeout=8)
    out = {"timestamp": time.time(), "provider": "uia", "source": "live_wechat", "runs": []}
    service.connect()
    for _ in range(3):
        before = service.get_current_chat()
        started = time.perf_counter()
        try:
            result = service.read_messages(20)
            messages = result.get("messages", [])
            texts = [m["text"] for m in messages]
            out["runs"].append({"status": "PASS" if texts else "EMPTY", "chat": before,
                                "count": len(texts), "duration_ms": int((time.perf_counter()-started)*1000),
                                "text_hash": hashlib.sha256("\n".join(texts).encode()).hexdigest()})
        except Exception as exc:
            out["runs"].append({"status": "BLOCKED", "error": f"{type(exc).__name__}: {exc}"})
    path = Path("docs/evidence/read_messages_live.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
if __name__ == "__main__":
    main()
