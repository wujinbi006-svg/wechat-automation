"""wxwal Session-1 supervisor: runs the injector and the WAL sidecar together.

Both must live in Session 1:
  - the injector, because the hook has to be loaded into WeChat's process
  - the sidecar, because the hook's named pipe cannot be connected from
    Session 0 (Win32 error 5), where the Gateway runs

A single logon task starts this file, and this file keeps both children alive.
Killing it stops both; each child is restarted independently if it dies.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "build" / "supervisor.log"
PYTHON = sys.executable

# name -> (script, restart delay seconds)
CHILDREN = {
    "injector": ("daemon.py", 10),
    "sidecar": ("wal_sidecar.py", 5),
}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def spawn(script: str) -> subprocess.Popen:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Configuration the sidecar needs; kept here so the task itself stays simple.
    root = BASE.parent
    env.setdefault("WECHAT_FILES_BASE", r"__USERPROFILE__\xwechat_files")
    env.setdefault("WECHAT_DB_KEY_FILE", str(root / "work" / "db.key"))
    env.setdefault("WECHAT_ALLOW_KEY_EXTRACTION", "1")
    return subprocess.Popen(
        [PYTHON, str(BASE / script)],
        cwd=str(BASE),
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def main() -> int:
    log(f"supervisor starting (python={PYTHON})")
    procs: dict[str, subprocess.Popen | None] = {k: None for k in CHILDREN}
    started: dict[str, float] = {}

    try:
        while True:
            for name, (script, delay) in CHILDREN.items():
                proc = procs[name]
                if proc is None or proc.poll() is not None:
                    if proc is not None:
                        log(f"{name} exited code={proc.returncode}; restarting in {delay}s")
                        time.sleep(delay)
                    procs[name] = spawn(script)
                    started[name] = time.time()
                    log(f"{name} started pid={procs[name].pid} ({script})")
            time.sleep(3)
    except KeyboardInterrupt:
        log("supervisor interrupted")
    finally:
        for name, proc in procs.items():
            if proc is not None and proc.poll() is None:
                log(f"stopping {name} pid={proc.pid}")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
