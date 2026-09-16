"""诊查/修复微信 UIA 的窗口状态。

微信 4.x 是 Qt 自绘，UIA 驱动要求主窗口「存在、可见、且渲染器已挂载」。
关到托盘或窗口刚被 ShowWindow 唤醒时，常见两种坏状态：

  A. 主窗口不可见        → `微信 UIA 主窗口不可用`（所有 UIA 工具失败）
  B. 可见但 UIA 树是空壳  → `Find Control Timeout: session_list`、搜索返回 []

这个脚本把两种状态都查出来，并可以就地修复（`--fix`）。

用法：
    python scripts\\diagnostics\\dump_uia_tree.py            # 只诊断
    python scripts\\diagnostics\\dump_uia_tree.py --fix      # 修复（会短暂动一下窗口）
    python scripts\\diagnostics\\dump_uia_tree.py --depth 4  # 打印更深的树
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

MAIN_TITLES = ("微信", "Weixin")


def weixin_pids() -> set[int]:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process Weixin -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Id"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return {int(x) for x in out.split() if x.strip().isdigit()}
    except Exception as exc:  # noqa: BLE001
        print(f"  (enumerate Weixin pids failed: {exc})")
        return set()


def find_main_windows() -> list[dict]:
    """返回所有标题匹配的顶层窗口（含不可见的）。"""
    try:
        import win32gui
        import win32process
    except ImportError as exc:
        print(f"pywin32 unavailable: {exc}")
        return []
    pids = weixin_pids()
    found: list[dict] = []

    def cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in pids:
                return True
            title = win32gui.GetWindowText(hwnd)
            if title not in MAIN_TITLES:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            found.append({
                "hwnd": hwnd, "pid": pid, "title": title,
                "class": win32gui.GetClassName(hwnd),
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "minimized": bool(win32gui.IsIconic(hwnd)),
                "size": f"{right - left}x{bottom - top}",
                "area": (right - left) * (bottom - top),
            })
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception as exc:  # noqa: BLE001
        print(f"EnumWindows failed: {exc}")
    found.sort(key=lambda item: item["area"], reverse=True)
    return found


def dump_tree(depth: int, limit: int = 60) -> list[str]:
    try:
        import uiautomation as auto
    except ImportError as exc:
        return [f"uiautomation unavailable: {exc}"]
    lines: list[str] = []

    def walk(control, level: int) -> None:
        if level > depth or len(lines) >= limit:
            return
        try:
            children = control.GetChildren()
        except Exception:
            return
        for child in children:
            if len(lines) >= limit:
                return
            try:
                lines.append(
                    "  " * level + "- %-32s aid=%-24s name=%r" % (
                        (child.ClassName or "")[:32],
                        (child.AutomationId or "")[:24],
                        (child.Name or "")[:28],
                    )
                )
            except Exception:
                continue
            walk(child, level + 1)

    try:
        root = auto.GetRootControl()
        for window in root.GetChildren():
            try:
                if (window.ClassName or "") in ("mmui::MainWindow", "Qt51514QWindowIcon"):
                    lines.append(f"== {window.ClassName} | {window.Name!r}")
                    walk(window, 1)
            except Exception:
                continue
    except Exception as exc:  # noqa: BLE001
        lines.append(f"UIA root enumeration failed: {exc}")
    if not lines:
        lines.append("(没有找到 mmui::MainWindow / Qt51514QWindowIcon)")
    return lines


def fix(hwnd: int) -> None:
    """显示窗口，然后做一次真实的最小化→还原，强制 Qt 渲染器重新挂载。"""
    import win32con
    import win32gui

    print(f"  fix: show + minimize/restore hwnd={hwnd:#x}")
    win32gui.ShowWindow(hwnd, 4)                      # SW_SHOWNOACTIVATE
    time.sleep(0.8)
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    time.sleep(1.2)
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    time.sleep(1.0)
    win32gui.ShowWindow(hwnd, 4)
    time.sleep(1.5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="显示窗口并做最小化→还原")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--handle", default=None, help="指定 hwnd（十六进制），默认自动探测")
    args = parser.parse_args()

    windows = find_main_windows()
    print(f"微信主窗口候选: {len(windows)}")
    for item in windows:
        print(f"  hwnd={item['hwnd']:#x} pid={item['pid']} 可见={item['visible']} "
              f"最小化={item['minimized']} 尺寸={item['size']} 类={item['class']} "
              f"标题={item['title']!r}")

    if not windows:
        print("\n没有找到标题为 微信/Weixin 的顶层窗口 —— 微信没在运行，或窗口被销毁。")
        return 1

    target = max(windows, key=lambda item: item["area"])
    if args.handle:
        target = next((w for w in windows if w["hwnd"] == int(args.handle, 16)), target)
    print(f"\n选中 hwnd={target['hwnd']:#x}")

    if args.fix:
        fix(target["hwnd"])
        windows = find_main_windows()
        target = max(windows, key=lambda item: item["area"])
        print(f"  修复后 可见={target['visible']} 尺寸={target['size']}")

    print(f"\nUIA 树（depth={args.depth}）:")
    lines = dump_tree(args.depth)
    print("\n".join("  " + line for line in lines))

    joined = "\n".join(lines)
    visible = target["visible"]
    has_content = ("mmui::MainView" in joined) or ("session_list" in joined) \
        or ("mmui::TitleBar" in joined)
    print("\n判定:")
    print(f"  窗口可见     : {visible}   {'OK' if visible else '-> 需要 --fix'}")
    print(f"  mmui 树有内容: {has_content}   {'OK' if has_content else '-> 需要 --fix'}")
    if visible and has_content:
        print("  UIA 可用。若 Agent 仍报 '主窗口不可用'，重启 interactive_agent.py。")
        return 0
    print("  运行本脚本加 --fix，然后重启 interactive_agent.py。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
