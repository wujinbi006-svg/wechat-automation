import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.uia_service import ReplicaUIADriver

def main():
    d = ReplicaUIADriver(); d.ensure_window()
    out = {"timestamp": time.time(), "source": "live_wechat", "chat": None, "send_button_found": False, "candidates": []}
    out["chat"] = d.current_chat()
    win = d._impl._win
    def walk(node, chain=()):
        try:
            cls, name = node.ClassName or "", (node.Name or "").strip()
            typ = getattr(node, "ControlTypeName", "")
            if typ == "ButtonControl" or "send" in name.lower() or "发送" in name:
                item = {"name": name, "class_name": cls, "automation_id": node.AutomationId or "", "control_type": typ,
                        "ancestor": list(chain)}
                try:
                    item["invoke_available"] = bool(node.GetInvokePattern())
                except Exception:
                    item["invoke_available"] = False
                out["candidates"].append(item)
                if "发送" in name or "send" in name.lower(): out["send_button_found"] = True
            for child in node.GetChildren():
                walk(child, chain + ((cls, name),))
        except Exception:
            return
    walk(win)
    Path("docs/evidence").mkdir(parents=True, exist_ok=True)
    Path("docs/evidence/background_send_button.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
if __name__ == "__main__": main()
