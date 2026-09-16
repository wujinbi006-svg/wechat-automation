"""只读审计参考项目的 key discovery 假设；不保存内存内容或真实 key。"""
from __future__ import annotations
import json, os, sys, time, hashlib
from pathlib import Path
import psutil

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "work" / "wechatauto_pkg" / "unzipped"
EVIDENCE = ROOT / "docs" / "evidence"
EVIDENCE.mkdir(parents=True, exist_ok=True)

def proc_info(p):
    try:
        return {"pid": p.pid, "name": p.name(), "exe": p.exe(),
                "cmdline": p.cmdline()}
    except Exception as e:
        return {"pid": p.pid, "name": getattr(p, "info", {}).get("name"), "error": type(e).__name__}

def main():
    names = {"weixin.exe", "wechatappex.exe", "wechatappex.exe"}
    procs = [p for p in psutil.process_iter(["pid","name"]) if (p.info.get("name") or "").lower() in names]
    rows = []
    for p in procs:
        info = proc_info(p)
        mods = []
        try:
            for m in p.memory_maps():
                path = m.path
                if path and path.lower().endswith((".dll", ".exe")):
                    mods.append({"path": path, "rss": m.rss})
        except Exception as e:
            info["module_error"] = type(e).__name__
        info["modules"] = mods
        rows.append(info)
    module_out = {"generated_at": time.time(), "processes": rows,
                  "target_names": sorted(names),
                  "reference_process": "Weixin.exe",
                  "reference_module_assumption": "Weixin.dll",
                  "reference_pattern": "com.Tencent.WCDB.Config.Cipher -> pointer/length node -> XOR decode -> 32-byte hex candidates -> page HMAC"}
    (EVIDENCE / "key_module_inventory.json").write_text(json.dumps(module_out, ensure_ascii=False, indent=2), encoding="utf-8")
    # 运行参考扫描，但只保留计数与指纹；WeChatDB 本身不会返回 pattern/candidate 计数，
    # 因此这里明确把可观测结果标为“参考实现黑盒扫描结果”。
    sys.path.insert(0, str(PKG))
    from wechatauto.db import WeChatDB
    account_dir = Path(os.environ.get("WECHAT_ACCOUNT_DIR", r"__USERPROFILE__\Documents\WeChat Files\wxid_EXAMPLE"))
    dbs = [(str(p.relative_to(account_dir)), str(p), p.stat().st_size) for p in account_dir.rglob("*.db") if p.is_file()]
    obj = WeChatDB.__new__(WeChatDB)
    obj._db_files = dbs
    scan = {"generated_at": time.time(), "processes_scanned": [r["pid"] for r in rows],
            "pattern_name": "com.Tencent.WCDB.Config.Cipher",
            "pattern_match_count": None, "candidate_count": None, "valid_key_count": 0,
            "reference_scan_result": "no_valid_keys",
            "key_discovery_status": "KEY_DISCOVERY_BLOCKED"}
    # 不调用 extract_keys 以外的自定义盲扫；调用其现有只读实现。
    t = time.perf_counter()
    try:
        keys = obj.extract_keys()
        scan["valid_key_count"] = len(keys)
        scan["reference_scan_result"] = "valid_keys_found" if keys else "no_valid_keys"
    except Exception as e:
        scan["error"] = f"{type(e).__name__}: {e}"
    scan["elapsed_ms"] = round((time.perf_counter()-t)*1000, 1)
    (EVIDENCE / "key_scan_current.json").write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"process_count": len(rows), "pids": [r["pid"] for r in rows], **scan}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
