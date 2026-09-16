import ctypes, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.uia_service import ReplicaUIADriver
from src.minimal_foreground_input import MinimalForegroundInput

def fg():
    h = ctypes.windll.user32.GetForegroundWindow()
    return int(h)

def main():
    out = {"method": "ValuePattern.SetValue", "text": "BACKGROUND_UIA_TEST_01"}
    driver = ReplicaUIADriver()
    driver.ensure_window()
    field = driver.get_chat_input_field()
    out["field"] = {"class_name": field.ClassName, "automation_id": field.AutomationId, "name": field.Name}
    out["foreground_before"] = fg()
    started = time.perf_counter()
    try:
        vp = field.GetValuePattern()
        vp.SetValue(out["text"])
        out["input_value_after"] = vp.Value
        out["input_verified"] = out["input_value_after"] == out["text"]
        out["status"] = "PASS" if out["input_verified"] and fg() == out["foreground_before"] else "BLOCKED"
    except Exception as exc:
        out["status"] = "BLOCKED"
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["foreground_after"] = fg()
    out["focus_changed"] = out["foreground_after"] != out["foreground_before"]
    out["duration_ms"] = int((time.perf_counter()-started)*1000)
    out["notes"] = "未使用注入、内存修改、鼠标或键盘模拟；未发送。"
    p = Path("docs/evidence/background_type_experiments.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
if __name__ == "__main__": main()
