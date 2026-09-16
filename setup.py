"""首次部署配置 —— 在本机自动生成所有与路径、密钥相关的文件。

为什么需要它
------------
工程里没有任何一处写死机器相关的值。所有随机器变化的东西都由本脚本
在**目标机器上现场生成**：

* Python 解释器路径        -> 探测后写进服务配置
* 工程根目录               -> 由本文件位置推导
* 微信数据目录             -> 扫描本机 xwechat_files / WeChat Files
* Agent IPC 口令           -> 随机生成，两侧共享
* Windows 服务与计划任务   -> 按本机路径生成并注册

用法::

    python setup.py              # 检查 + 生成全部配置
    python setup.py --check      # 只检查前置条件，不写任何文件
    python setup.py --register   # 额外注册 Windows 服务与计划任务
    python setup.py --force      # 覆盖已存在的配置文件
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
WORK_DIR = ROOT / "work"
SERVICE_DIR = ROOT / "service"

MIN_PY = (3, 11)
GATEWAY_PORT = 8010
CONSOLE_PORT = 8011
AGENT_IPC_PORT = 18010
SIDECAR_PORT = 18011


# --------------------------------------------------------------------- 输出
def hr(title: str = "") -> None:
    print()
    if title:
        print("=" * 64)
        print(f"  {title}")
        print("=" * 64)
    else:
        print("-" * 64)


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def info(msg: str) -> None:
    print(f"         {msg}")


# ----------------------------------------------------------------- 前置检查
def check_python() -> bool:
    v = sys.version_info
    if (v.major, v.minor) < MIN_PY:
        bad(f"Python {v.major}.{v.minor} 过低，需要 {MIN_PY[0]}.{MIN_PY[1]}+")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    info(f"解释器: {sys.executable}")
    return True


def check_platform() -> bool:
    if platform.system() != "Windows":
        bad(f"当前系统 {platform.system()}，本工程只能在 Windows 上运行")
        info("（微信 4.x 的 UIA 自动化与 WAL hook 都依赖 Windows API）")
        return False
    ok(f"Windows {platform.release()}")
    return True


def check_wechat_install() -> dict:
    """定位微信安装目录与版本。"""
    result = {"found": False, "dir": None, "version": None, "dll": None}
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tencent" / "Weixin",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tencent" / "Weixin",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tencent" / "WeChat",
    ]
    for base in candidates:
        if not base.is_dir():
            continue
        exe = base / "Weixin.exe"
        if not exe.exists():
            exe = base / "WeChat.exe"
        if not exe.exists():
            continue
        result["dir"] = base
        result["found"] = True
        # 版本目录形如 <base>\4.1.13.12\Weixin.dll
        versions = []
        for sub in base.iterdir():
            if sub.is_dir() and re.fullmatch(r"\d+(\.\d+)+", sub.name):
                dll = sub / "Weixin.dll"
                if dll.exists():
                    versions.append((sub.name, dll))
        if versions:
            versions.sort(key=lambda x: [int(p) for p in x[0].split(".")], reverse=True)
            result["version"], result["dll"] = versions[0]
        break
    return result


def check_wechat_process() -> bool:
    try:
        p = subprocess.run(["tasklist", "/fi", "imagename eq Weixin.exe"],
                           capture_output=True, timeout=20)
        return b"Weixin.exe" in (p.stdout or b"")
    except Exception:
        return False


def find_wechat_data() -> dict:
    """定位微信数据目录与 db_storage。"""
    out = {"base": None, "account_dir": None, "db_storage": None, "accounts": []}
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))

    # 微信 4.x
    for name in ("xwechat_files",):
        base = home / name
        if base.is_dir():
            out["base"] = base
            for d in base.iterdir():
                if d.is_dir() and d.name.startswith("wxid_"):
                    out["accounts"].append(d.name)
                    if (d / "db_storage").is_dir() and out["account_dir"] is None:
                        out["account_dir"] = d
                        out["db_storage"] = d / "db_storage"
            break

    # 微信 3.x 兜底
    if out["base"] is None:
        legacy = home / "Documents" / "WeChat Files"
        if legacy.is_dir():
            out["base"] = legacy
            for d in legacy.iterdir():
                if d.is_dir() and d.name.startswith("wxid_"):
                    out["accounts"].append(d.name)
            if out["accounts"]:
                out["account_dir"] = legacy / out["accounts"][0]
    return out


def check_dependencies() -> list[str]:
    """检查 Python 依赖是否齐全。"""
    missing = []
    for mod, pkg in [
        ("uvicorn", "uvicorn"),
        ("fastapi", "fastapi"),
        ("uiautomation", "uiautomation"),
        ("win32gui", "pywin32"),
        ("cryptography", "cryptography"),
        ("yara", "yara-python"),
    ]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    return missing


# ------------------------------------------------------------------- 生成物
def gen_ipc_token() -> str:
    return secrets.token_urlsafe(32)


def write_agent_env(token: str, force: bool) -> Path:
    path = CONFIG_DIR / "agent.env"
    if path.exists() and not force:
        info(f"已存在，保留：{path.name}")
        return path
    lines = [
        "# 本文件由 setup.py 生成，保存 Agent 与 Gateway 共享的本地 IPC 口令。",
        "# 不要提交到版本库，也不要分享给他人。",
        f"WECHAT_AGENT_IPC_TOKEN={token}",
        f"WECHAT_AGENT_IPC_PORT={AGENT_IPC_PORT}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok(f"已生成 {path.relative_to(ROOT)}（含随机 IPC 口令）")
    return path


def gen_gateway_xml(py: str, data: dict) -> Path:
    """生成 WinSW 服务配置（含本机绝对路径）。"""
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    xml = f"""<service>
  <id>WeChatGateway</id>
  <name>WeChat Automation Gateway</name>
  <description>微信自动化主服务：数据库读面 + 事件流 + 自动回复。由 setup.py 生成。</description>

  <executable>{py}</executable>
  <arguments>-m uvicorn api:app --host 127.0.0.1 --port {GATEWAY_PORT}</arguments>
  <workingdirectory>{ROOT}</workingdirectory>
  <logpath>{ROOT / 'logs'}</logpath>

  <!-- 运行期配置。密钥不写在这里：WECHAT_DB_KEY_FILE 指向 work/db.key，
       该目录已在 .gitignore 中。 -->
  <env name="WECHAT_FILES_BASE" value="{data['files_base']}"/>
  <env name="WECHAT_DB_KEY_FILE" value="{WORK_DIR / 'db.key'}"/>
  <env name="WECHAT_ALLOW_KEY_EXTRACTION" value="1"/>
  <env name="WECHAT_ENABLE_SEND" value="1"/>
  <env name="WECHAT_UIA_MODE" value="interactive"/>
  <env name="WECHAT_TEST_MODE" value="1"/>
  <env name="WECHAT_DB_WORKDIR" value="{WORK_DIR / 'decrypted_db'}"/>
  <env name="WECHAT_KEY_CACHE" value="{WORK_DIR / 'database_key_cache.json'}"/>
  <env name="WECHAT_AGENT_IPC_TOKEN" value="{data['ipc_token']}"/>
  <env name="WECHAT_AGENT_IPC_PORT" value="{AGENT_IPC_PORT}"/>

  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 day</resetfailure>
</service>
"""
    path = SERVICE_DIR / "WeChatGateway.xml"
    path.write_text(xml, encoding="utf-8", newline="\n")
    ok(f"已生成 {path.relative_to(ROOT)}")
    return path


def gen_task_xml(name: str, desc: str, py: str, script: Path, workdir: Path) -> Path:
    """生成 Windows 计划任务 XML（登录自启 + 失败重启）。"""
    user = f"{os.environ.get('COMPUTERNAME', 'PC')}\\{os.environ.get('USERNAME', 'user')}"
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc}</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Count>999</Count>
      <Interval>PT1M</Interval>
    </RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <LogonTrigger>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>"{script}"</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = WORK_DIR / f"task-{name}.xml"
    # schtasks 要求 UTF-16LE + BOM
    enc = "utf-16"
    path.write_text(xml, encoding=enc)
    ok(f"已生成 {path.relative_to(ROOT)}")
    return path


def gen_configs(force: bool) -> None:
    """从示例生成实际配置文件（若不存在）。"""
    pairs = [
        ("auto_reply.example.json", "auto_reply.json"),
        ("console.example.json", "console.json"),
    ]
    for src_name, dst_name in pairs:
        src = CONFIG_DIR / src_name
        dst = CONFIG_DIR / dst_name
        if not src.exists():
            warn(f"缺少模板 {src_name}")
            continue
        if dst.exists() and not force:
            info(f"已存在，保留：{dst_name}")
            continue
        shutil.copy2(src, dst)
        ok(f"已生成 config/{dst_name}")


def write_runtime(data: dict) -> Path:
    """记录本次探测结果，供其它脚本复用。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = WORK_DIR / "runtime.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 注册
def register_tasks(py: str) -> None:
    """注册 Windows 计划任务与服务（需要管理员权限的部分会明确提示）。"""
    hr("注册 Windows 计划任务")

    tasks = [
        ("WeChatUIAgent", WORK_DIR / "task-WeChatUIAgent.xml"),
        ("WeChatAutoReplyConsole", WORK_DIR / "task-WeChatAutoReplyConsole.xml"),
    ]
    for name, xml in tasks:
        if not xml.exists():
            bad(f"缺少 {xml.name}，跳过")
            continue
        try:
            p = subprocess.run(["schtasks", "/create", "/tn", name, "/xml", str(xml), "/f"],
                               capture_output=True, timeout=40)
            if p.returncode == 0:
                ok(f"已注册计划任务 {name}")
            else:
                bad(f"注册 {name} 失败：{(p.stderr or p.stdout or b'').decode('utf-8', 'replace')[:120]}")
        except Exception as e:
            bad(f"注册 {name} 出错：{e}")

    hook_installer = ROOT / "hook" / "install_daemon_task.ps1"
    if hook_installer.exists():
        info("hook 事件源任务需要单独注册（见下方提示）")
    else:
        warn("缺少 hook/install_daemon_task.ps1")


def print_next_steps() -> None:
    hr("下一步")
    print("""
  1) 安装 Python 依赖
       python -m pip install -r requirements.txt

  2) 获取数据库密钥（最关键的一步，见 SECRETS.md）
       - 微信必须已登录并保持运行
       - 把密钥写入 work/db.key（64 位十六进制，一行，无换行符）

  3) 注册 hook 事件源任务（接收消息用）
       powershell -ExecutionPolicy Bypass -File hook\\install_daemon_task.ps1

  4) 重新运行本脚本并加 --register，注册其余计划任务
       python setup.py --register

  5) 启动
       双击「一键启动.cmd」   或   运行 python health_check.py --fix

  6) 打开控制台配置自动回复对象
       http://127.0.0.1:8011

  详细说明见 DEPLOY.md；密钥获取见 SECRETS.md；
  交给 AI 部署见 AI_DEPLOY_PROMPT.md。
""")


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="微信自动化工程 · 首次部署配置")
    ap.add_argument("--check", action="store_true", help="只检查前置条件")
    ap.add_argument("--register", action="store_true", help="注册计划任务与配置")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")
    args = ap.parse_args()

    hr("微信自动化工程 · 环境检查")

    problems = 0
    if not check_python():
        problems += 1
    if not check_platform():
        problems += 1

    wx = check_wechat_install()
    if wx["found"]:
        ok(f"微信安装目录: {wx['dir']}")
        if wx["version"]:
            ok(f"微信版本: {wx['version']}")
        else:
            warn("未找到版本目录（缺少 Weixin.dll）")
    else:
        bad("未找到微信安装目录")
        info("请先安装微信 4.x 桌面版")
        problems += 1

    running = check_wechat_process()
    if running:
        ok("微信正在运行")
    else:
        warn("微信没有运行 —— 登录后才能读取数据、注入 hook")

    data = find_wechat_data()
    if data["db_storage"]:
        ok(f"数据目录: {data['db_storage']}")
        ok(f"发现 {len(data['accounts'])} 个账号目录")
    else:
        warn("未找到微信数据目录（xwechat_files）")
        info("登录一次微信后会自动创建")

    missing = check_dependencies()
    if missing:
        warn(f"缺少 Python 依赖: {', '.join(missing)}")
        info("运行: python -m pip install -r requirements.txt")
    else:
        ok("Python 依赖齐全")

    if args.check:
        hr()
        print(f"  检查完成，发现 {problems} 个阻断问题。")
        return 1 if problems else 0

    if problems:
        hr()
        bad("存在阻断问题，未生成配置。修好后重新运行。")
        return 1

    hr("生成配置文件")
    token = gen_ipc_token()
    runtime = {
        "python": sys.executable,
        "root": str(ROOT),
        "files_base": str(data["base"]) if data["base"] else "",
        "account_dir": str(data["account_dir"]) if data["account_dir"] else "",
        "db_storage": str(data["db_storage"]) if data["db_storage"] else "",
        "wechat_version": wx["version"],
        "wechat_dir": str(wx["dir"]) if wx["dir"] else "",
        "ports": {
            "gateway": GATEWAY_PORT,
            "console": CONSOLE_PORT,
            "agent_ipc": AGENT_IPC_PORT,
            "sidecar": SIDECAR_PORT,
        },
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    write_agent_env(token, args.force)
    gen_gateway_xml(sys.executable, {
        "files_base": runtime["files_base"],
        "ipc_token": token,
    })
    gen_task_xml(
        "WeChatUIAgent",
        "Keeps the UIA interactive agent alive on 127.0.0.1:%d (required for sending WeChat messages)." % AGENT_IPC_PORT,
        sys.executable,
        ROOT / "scripts" / "interactive_agent_watchdog.py",
        ROOT,
    )
    gen_task_xml(
        "WeChatAutoReplyConsole",
        "Serves the auto-reply console on http://127.0.0.1:%d." % CONSOLE_PORT,
        sys.executable,
        ROOT / "console" / "server.py",
        ROOT,
    )
    gen_configs(args.force)
    rt = write_runtime(runtime)
    ok(f"已生成 {rt.relative_to(ROOT)}")

    if args.register:
        register_tasks(sys.executable)

    print_next_steps()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
