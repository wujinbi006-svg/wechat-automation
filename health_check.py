"""微信自动回复 —— 健康检查与自愈。

检查四个本地服务、微信进程、网关与自动回复状态；发现缺失时调用对应的
Windows 计划任务把它们拉起来。逻辑全部放在 Python 里，避免批处理引号问题。

用法:
    python health_check.py            交互式检查，可询问是否修复
    python health_check.py --fix      检查并直接修复
    python health_check.py --no-fix   只检查，不修复
    python health_check.py --quiet    安静模式，只输出状态码与一行结果
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SERVICES = [
    (8010, "主服务 WeChatGateway", "WeChatGateway"),
    (8011, "控制台", "WeChatAutoReplyConsole"),
    (18010, "UIA Agent（发送消息）", "WeChatUIAgent"),
    (18011, "WAL 事件源（接收消息）", "wxwal-hook-daemon"),
]

OK = "[OK]"
BAD = "[X] "


def port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def get_json(url: str, timeout: float = 8.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def run_cmd(args, timeout: float = 20.0):
    """执行外部命令；失败或不存在的命令一律返回 None，不抛出。"""
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout)
    except Exception:
        return None


def task_exists(name: str) -> bool:
    p = run_cmd(["schtasks", "/query", "/tn", name])
    return bool(p and p.returncode == 0)


def run_task(name: str) -> bool:
    p = run_cmd(["schtasks", "/run", "/tn", name], timeout=25.0)
    return bool(p and p.returncode == 0)


def wechat_running() -> bool:
    p = run_cmd(["tasklist", "/fi", "imagename eq Weixin.exe"])
    return bool(p and b"Weixin.exe" in (p.stdout or b""))


def check_services(verbose: bool = True):
    missing = []
    for port, label, task in SERVICES:
        up = port_open(port)
        if verbose:
            print(f"  {OK if up else BAD} {label:<24} (端口 {port})")
        if not up:
            missing.append((port, label, task))
    return missing


def report_gateway():
    st = get_json("http://127.0.0.1:8010/status")
    if not st or not st.get("success"):
        print(f"  {BAD} 主服务无响应")
        return
    d = st.get("data") or {}
    ar = d.get("auto_reply") or {}
    targets = ar.get("targets") or []
    print(f"  自动回复 : {'开启' if ar.get('enabled') else '已关闭'}")
    print(f"  目标对象 : {'、'.join(targets) if targets else '(无)'}")
    print(f"  事件源   : {d.get('active_event_source')}")
    print(f"  微信连接 : {d.get('wechat_connected')}")

    ag = get_json("http://127.0.0.1:8010/agent/status")
    if ag and ag.get("success"):
        a = ag.get("data") or {}
        print(f"  {OK if a.get('uia_ready') else BAD} UIA: {a.get('lifecycle')} / uia_ready={a.get('uia_ready')}")


def main() -> int:
    args = set(sys.argv[1:])
    auto_fix = bool({"--fix", "-f"} & args)
    no_fix = bool({"--no-fix", "-n"} & args)
    quiet = bool({"--quiet", "-q"} & args)

    if not quiet:
        print()
        print("=" * 58)
        print("  微信自动回复 · 健康检查")
        print("=" * 58)
        print()

    missing = check_services(verbose=not quiet)

    if not quiet:
        print()
        print("------------------ 微信进程 ------------------")
        if wechat_running():
            print(f"  {OK} 微信正在运行")
        else:
            print(f"  {BAD} 微信没在运行 —— 微信必须开着，否则自动化全部无效")
        print()
        print("------------------ 网关与自动回复 ------------------")
        report_gateway()
        print()

    if not missing:
        if quiet:
            print("ALL_OK")
        else:
            print("=" * 58)
            print("  全部正常")
            print("=" * 58)
            print()
            print("  控制台地址：http://127.0.0.1:8011")
        return 0

    if quiet:
        print("MISSING:" + ",".join(label for _, label, _ in missing))
    else:
        print("=" * 58)
        print(f"  有 {len(missing)} 项未运行")
        print("=" * 58)
        for port, label, task in missing:
            hint = f"计划任务 {task}" + ("" if task_exists(task) else "（不存在）")
            print(f"  - {label}  {hint}")

    if no_fix:
        return 1

    if not auto_fix:
        if quiet:
            return 1
        print()
        try:
            ans = input("  现在自动修复吗？(Y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans != "y":
            return 1
    elif not quiet:
        print("\n  --fix：直接修复")

    print()
    print("  正在拉起服务...")
    for t in sorted({t for _, _, t in missing}):
        print(f"    {'已触发' if run_task(t) else '失败  '} {t}")
    print("  等待 20 秒...")
    time.sleep(20)

    print()
    print("------------------ 修复后 ------------------")
    still_ports = {p for p, _, _ in check_services(verbose=False)}
    for port, label, _ in SERVICES:
        print(f"  {BAD if port in still_ports else OK} {label}")
    if still_ports:
        print()
        print("  仍未恢复的，请依次检查：")
        print("    1. 微信是否正在运行")
        print("    2. 计划任务是否存在：schtasks /query /tn WeChatUIAgent")
        print("    3. 手工拉起：schtasks /run /tn WeChatUIAgent")
        return 1
    print()
    print("  全部恢复 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
