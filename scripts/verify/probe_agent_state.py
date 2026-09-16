import sys, json
sys.path.insert(0, r"__ROOT__")
from src.interactive_ipc import rpc

for m in ("status", "get_current_chat", "get_input_state"):
    try:
        print(f"--- {m} ---")
        print(json.dumps(rpc(m, timeout=30.0), ensure_ascii=False, indent=1)[:1500])
    except Exception as e:
        print(f"ERR {type(e).__name__}: {e}")
