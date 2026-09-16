"""Interactive Agent 专用看门狗。

只管理当前仓库启动的 interactive_agent.py 子进程，不按进程名批量终止，
也不会触碰其他 Python 进程或未知 PID。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "interactive_agent_watchdog.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("interactive-agent-watchdog")


def load_agent_env() -> None:
    """从 config/agent.env 读取环境变量（KEY=VALUE 每行一条，支持 # 注释）。

    IPC 口令必须由 Gateway 与 Agent 两侧共享，而 Windows 计划任务无法直接
    设置环境变量，所以由 setup 生成该文件、由看门狗在拉起子进程前注入。
    已存在的环境变量优先，便于临时覆盖调试。
    """
    path = ROOT / "config" / "agent.env"
    if not path.exists():
        return
    loaded = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    if loaded:
        log.info("已从 config/agent.env 注入环境变量: %s", ", ".join(loaded))


load_agent_env()


def start_agent() -> subprocess.Popen:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.Popen(
        [sys.executable, str(ROOT / "interactive_agent.py")],
        cwd=ROOT,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def main() -> int:
    child: subprocess.Popen | None = None
    try:
        while True:
            if child is None or child.poll() is not None:
                if child is not None:
                    log.warning("interactive agent exited code=%s; restarting", child.returncode)
                child = start_agent()
                log.info("interactive agent started pid=%s", child.pid)
            time.sleep(3)
    except KeyboardInterrupt:
        return 0
    finally:
        if child is not None and child.poll() is None:
            log.info("stopping managed interactive agent pid=%s", child.pid)
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    raise SystemExit(main())
