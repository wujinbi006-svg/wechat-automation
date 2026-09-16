"""自动回复独立控制台 —— 后端服务。

设计要点：
* 独立进程，不依赖 DSH、不依赖 AI 会话，开机即可用。
* 只监听 127.0.0.1，不对外暴露。
* 所有写操作直接改 config/auto_reply.json，然后调 Gateway 的
  /auto-reply/reload 热加载，因此控制台与模型侧工具改的是同一份配置，
  两边不会互相覆盖。
* API key 只存在于服务端（本文件 + config/console.json），不下发到浏览器。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "auto_reply.json"
CONSOLE_CONFIG = ROOT / "config" / "console.json"
BACKUP_DIR = ROOT / "config" / "backups"
GATEWAY = "http://127.0.0.1:8010"
HOST = "127.0.0.1"
PORT = int(os.getenv("WECHAT_CONSOLE_PORT", "8011"))

sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- io
def load_console_config() -> dict:
    if CONSOLE_CONFIG.exists():
        try:
            # utf-8-sig 容忍 Windows 工具写出的 BOM，否则 json 解析会失败
            return json.loads(CONSOLE_CONFIG.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            pass
    return {"api_key": "", "api_base": "https://api.deepseek.com/v1", "model": "deepseek-chat"}


def save_console_config(data: dict) -> None:
    CONSOLE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    # 不写 BOM，避免其它读取方（标准 utf-8 解码）解析失败
    CONSOLE_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def save_config(cfg: dict) -> str:
    """原子写入 + 备份。返回备份文件名。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"auto_reply.{stamp}.json"
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, backup)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    return backup.name


def gateway(path: str, method: str = "GET", body: dict | None = None, timeout: float = 30.0):
    url = GATEWAY + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"success": False, "error": {"code": "HTTP", "message": str(e)}}
    except Exception as e:
        return {"success": False, "error": {"code": type(e).__name__, "message": str(e)}}


def reload_gateway():
    return gateway("/auto-reply/reload", method="POST")


# --------------------------------------------------------------------- resolver
def resolve_wxid(name: str) -> dict:
    """用项目的只读解析器把备注名换成 wxid。返回 {ok, wxid, detail}。"""
    script = ROOT / "scripts" / "diagnostics" / "resolve_friend.py"
    if not script.exists():
        return {"ok": False, "detail": "resolve_friend.py 不存在"}
    py = sys.executable
    try:
        p = subprocess.run([py, "-X", "utf8", str(script), name],
                           cwd=str(ROOT), capture_output=True, timeout=120)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    m = re.search(r"resolve_username\('.*?'\)\s*->\s*'([^']+)'", out)
    if m:
        return {"ok": True, "wxid": m.group(1), "detail": out[-1200:]}
    return {"ok": False, "detail": out[-1200:] or "未解析到 wxid"}


def session_candidates(limit: int = 40) -> list[dict]:
    """从 Gateway 拿可见会话，作为「选择好友」的候选列表。"""
    r = gateway("/tools/call", method="POST",
                body={"name": "wechat.conversation.list", "arguments": {}}, timeout=60)
    if not r.get("success"):
        return []
    rows = r.get("data") or []
    out = []
    for row in rows[:limit]:
        if isinstance(row, dict):
            out.append({"name": row.get("name") or "", "id": row.get("id") or ""})
    return out


# ----------------------------------------------------------------------- router
class Handler(BaseHTTPRequestHandler):
    server_version = "AutoReplyConsole/1.0"

    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    # ---- helpers
    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"ok": False, "error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/index.html"):
            return self._file(Path(__file__).parent / "index.html", "text/html; charset=utf-8")
        if p == "/api/state":
            return self._json(self._state())
        if p == "/api/sessions":
            return self._json({"ok": True, "sessions": session_candidates()})
        if p == "/api/health":
            return self._json({"ok": True, "service": "auto-reply-console", "ts": time.time()})
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---- POST
    def do_POST(self):
        p = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            if p == "/api/toggle-global":
                return self._json(self._toggle_global(bool(body.get("enabled"))))
            if p == "/api/toggle-target":
                return self._json(self._toggle_target(str(body.get("name") or ""),
                                                      bool(body.get("enabled"))))
            if p == "/api/add-target":
                return self._json(self._add_target(body))
            if p == "/api/remove-target":
                return self._json(self._remove_target(str(body.get("name") or "")))
            if p == "/api/update-target":
                return self._json(self._update_target(body))
            if p == "/api/resolve":
                return self._json(resolve_wxid(str(body.get("name") or "")))
            if p == "/api/console-config":
                return self._json(self._set_console_config(body))
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---- state
    def _state(self) -> dict:
        cfg = load_config()
        gw = gateway("/auto-reply/status", timeout=20)
        status = gw.get("data") if gw.get("success") else None
        cc = load_console_config()
        return {
            "ok": True,
            "config": {
                "enabled": cfg.get("enabled", False),
                "dry_run": cfg.get("dry_run", True),
                "quiet_hours": cfg.get("quiet_hours", []),
                "human_override_minutes": cfg.get("human_override_minutes", 0),
                "model": cfg.get("llm_model", ""),
                "targets": [
                    {
                        "name": t.get("name", ""),
                        "aliases": t.get("aliases", []),
                        "enabled": t.get("enabled", True),
                        "persona": t.get("persona", ""),
                        "memory": t.get("memory", ""),
                        "max_chars": t.get("max_chars", 120),
                        "history_limit": t.get("history_limit", 0),
                        "allow_group": t.get("allow_group", False),
                    }
                    for t in cfg.get("targets", [])
                ],
            },
            "gateway": {
                "online": bool(gw.get("success")),
                "enabled": (status or {}).get("enabled"),
                "dry_run": (status or {}).get("dry_run"),
                "counters": (status or {}).get("counters", {}),
                "recent": (status or {}).get("recent", [])[-8:],
                "queue_depth": (status or {}).get("queue_depth", 0),
            },
            "console": {
                "api_key_set": bool(cc.get("api_key")),
                "api_key_masked": ("*" * 8 + cc.get("api_key", "")[-4:]) if cc.get("api_key") else "",
                "api_base": cc.get("api_base", ""),
                "model": cc.get("model", ""),
            },
        }

    # ---- mutations
    def _toggle_global(self, enabled: bool) -> dict:
        cfg = load_config()
        cfg["enabled"] = enabled
        backup = save_config(cfg)
        gw = reload_gateway()
        return {"ok": bool(gw.get("success")), "enabled": enabled,
                "backup": backup, "gateway": gw}

    def _toggle_target(self, name: str, enabled: bool) -> dict:
        cfg = load_config()
        hit = False
        for t in cfg.get("targets", []):
            if t.get("name") == name:
                t["enabled"] = enabled
                hit = True
        if not hit:
            return {"ok": False, "error": f"未找到 {name}"}
        backup = save_config(cfg)
        gw = reload_gateway()
        return {"ok": bool(gw.get("success")), "name": name, "enabled": enabled,
                "backup": backup, "gateway": gw}

    def _add_target(self, body: dict) -> dict:
        name = str(body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "缺少 name"}
        cfg = load_config()
        if any(t.get("name") == name for t in cfg.get("targets", [])):
            return {"ok": False, "error": f"{name} 已存在"}
        aliases = body.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        if not aliases:
            r = resolve_wxid(name)
            if r.get("ok"):
                aliases = [r["wxid"]]
        cfg.setdefault("targets", []).append({
            "name": name,
            "aliases": aliases,
            "enabled": bool(body.get("enabled", True)),
            "persona": str(body.get("persona") or "好友，说话简短、口语，不用书面语。"),
            "memory": str(body.get("memory") or ""),
            "max_chars": int(body.get("max_chars") or 120),
            "history_limit": int(body.get("history_limit") or 0),
            "allow_group": False,
        })
        backup = save_config(cfg)
        gw = reload_gateway()
        return {"ok": bool(gw.get("success")), "name": name, "aliases": aliases,
                "backup": backup, "gateway": gw}

    def _remove_target(self, name: str) -> dict:
        cfg = load_config()
        before = len(cfg.get("targets", []))
        cfg["targets"] = [t for t in cfg.get("targets", []) if t.get("name") != name]
        if len(cfg["targets"]) == before:
            return {"ok": False, "error": f"未找到 {name}"}
        backup = save_config(cfg)
        gw = reload_gateway()
        return {"ok": bool(gw.get("success")), "removed": name,
                "backup": backup, "gateway": gw}

    def _update_target(self, body: dict) -> dict:
        name = str(body.get("name") or "")
        new_name = str(body.get("new_name") or "").strip()
        cfg = load_config()
        for t in cfg.get("targets", []):
            if t.get("name") == name:
                if "persona" in body:
                    t["persona"] = str(body["persona"])
                if "memory" in body:
                    t["memory"] = str(body["memory"])
                if "max_chars" in body:
                    t["max_chars"] = int(body["max_chars"])
                if "history_limit" in body:
                    t["history_limit"] = int(body["history_limit"])
                if "allow_group" in body:
                    t["allow_group"] = bool(body["allow_group"])
                if "enabled" in body:
                    t["enabled"] = bool(body["enabled"])
                if "aliases" in body:
                    a = body["aliases"]
                    t["aliases"] = ([x.strip() for x in a.split(",") if x.strip()]
                                    if isinstance(a, str) else a)
                # 改名要同时改 targets[].name（UIA 搜索用）与匹配键
                if new_name and new_name != name:
                    if any(x.get("name") == new_name for x in cfg.get("targets", [])):
                        return {"ok": False, "error": f"{new_name} 已存在"}
                    t["name"] = new_name
                backup = save_config(cfg)
                gw = reload_gateway()
                return {"ok": bool(gw.get("success")), "name": t.get("name"),
                        "backup": backup, "gateway": gw}
        return {"ok": False, "error": f"未找到 {name}"}

    def _set_console_config(self, body: dict) -> dict:
        cc = load_console_config()
        if body.get("api_key"):
            cc["api_key"] = str(body["api_key"]).strip()
        if body.get("api_base"):
            cc["api_base"] = str(body["api_base"]).strip()
        if body.get("model"):
            cc["model"] = str(body["model"]).strip()
        save_console_config(cc)
        return {"ok": True, "api_key_set": bool(cc.get("api_key")),
                "api_key_masked": ("*" * 8 + cc["api_key"][-4:]) if cc.get("api_key") else ""}


def main() -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[auto-reply-console] listening on http://{HOST}:{PORT}", flush=True)
    print(f"[auto-reply-console] gateway = {GATEWAY}", flush=True)
    print(f"[auto-reply-console] config  = {CONFIG_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
