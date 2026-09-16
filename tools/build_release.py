"""构建可分发版本 —— 把本机工程打成脱敏、路径自适应的压缩包。

设计原则
--------
1. **白名单 + 黑名单双保险**：只复制明确需要的目录，同时排除所有
   含个人数据/密钥/构建产物的路径。任何一个漏掉都会泄露隐私。
2. **文本替换**：源码里残留的本机绝对路径、个人 wxid、主机名一律替换成
   占位符或改为运行时推导。
3. **构建后强制安检**：对产物全量扫描，命中任何敏感模式就失败退出，
   不会产出一个"看起来干净"的包。
4. **可重复**：同一份源码任何时候跑都得到同样的结果。

用法::

    python tools/build_release.py            # 构建并打包
    python tools/build_release.py --check    # 只对现有产物做安检
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = ROOT / "dist"
PKG_NAME = "wechat-automation"
OUT_DIR = DIST_ROOT / PKG_NAME

# --------------------------------------------------------------------- 白名单
# 只复制这些条目（文件或目录，相对 ROOT）。
INCLUDE = [
    "src",
    "console",
    "hook",
    "scripts",
    "tests",
    "docs",
    "service",
    "config",
    "tools",
    "api.py",
    "interactive_agent.py",
    "health_check.py",
    "setup.py",
    "wechat_cli.py",
    "pytest.ini",
    "requirements.txt",
    ".gitignore",
    "README.md",
    "DEPLOY.md",
    "SECRETS.md",
    "SECURITY.md",
    "AI_DEPLOY_PROMPT.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "licenses",
    "一键启动.cmd",
    "健康检查.cmd",
]

# 单个文件也要带上的（若存在）
INCLUDE_FILES = [
    "work/wechatauto_pkg/unzipped/wechatauto",  # 运行时依赖的第三方 UIA 驱动
]

# --------------------------------------------------------------------- 黑名单
# 路径中出现任意一项就跳过（不区分大小写，匹配相对路径的任意片段）。
EXCLUDE_PARTS = [
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    ".git/",
    "decrypted_db",       # 解密后的聊天数据库（最敏感）
    "backups",            # 配置备份，含密钥与个人对象
    "db.key",             # 数据库密钥
    "scrub_rules.json",   # 真实姓名/wxid 脱敏字典（最敏感，绝不可进包）
    "database_key_cache.json",
    "console.json",       # 含 API Key
    "auto_reply.json",    # 含 API Key、个人 wxid、记忆
    "agent.env",          # 含本机 IPC 口令
    "runtime.json",       # 含本机路径
    "evidence",           # 含大量本机路径与 wxid 的取证快照
    "reports",            # 运行基线快照
    "logs",               # 日志含聊天内容
    "wechatauto_logs",
    "style_conv.json",
    "style_raw.json",
    "sample.wav",
    "sample.silk",
    "frames2.pkl",
    "@AutomationLog.txt",
    "gateway_stderr.log",
    "logs_database_key_status_current.txt",
    "current_diff.txt",
    "task-agent.xml",
    "task-console.xml",
    "start-services.vbs",
    "启动后台服务.vbs",
    ".bak-",
    ".tmp-shm",
    ".tmp-wal",
    # 由 setup.py 在目标机器现场生成，含本机绝对路径
    "WeChatGateway.xml",
    "runtime.json",
]

# 扩展名黑名单（构建产物 / 体积大且含绝对路径）
EXCLUDE_EXT = {
    ".pdb",    # 调试符号，含源码绝对路径
    ".lib",
    ".log",
    ".zip",
    ".7z",
    ".pyc",
    ".dat",
    ".db",
}

# hook/build 下只保留真正要用的 DLL
HOOK_KEEP_DLL = {"wxwal10.dll"}
HOOK_KEEP_OTHER = {".gitignore", "README.md"}

# ------------------------------------------------------------------ 文本替换
# 顺序很重要：长路径先替换，避免被短模式切碎。
# 注意：这里用**原始字符串**，反斜杠只写一个，否则匹配不到真实文本。
SUBSTITUTIONS: list[tuple[str, str]] = [
    # 本机工程根目录 → 运行时推导
    (r"__ROOT__", "__ROOT__"),
    ("__ROOT__", "__ROOT__"),
    # 同上，但源码里被转义成双反斜杠的形态（注释与用法字符串里常见）
    (r"__ROOT__", "__ROOT__"),
    # 本机 Python 解释器 → 交给 setup 脚本决定
    (r"__PYTHON__", "__PYTHON__"),
    ("__PYTHON__", "__PYTHON__"),
    (r"__PYTHON__", "__PYTHON__"),
    (r"__PYTHON_DIR__", "__PYTHON_DIR__"),
    (r"__PYTHON_DIR__", "__PYTHON_DIR__"),
    # 用户目录 → 环境变量
    (r"__USERPROFILE__", "__USERPROFILE__"),
    ("__USERPROFILE__", "__USERPROFILE__"),
    (r"__USERPROFILE__", "__USERPROFILE__"),
    # 个人 wxid
    ("wxid_EXAMPLE_ACCOUNT", "wxid_EXAMPLE_ACCOUNT"),
    ("wxid_EXAMPLE", "wxid_EXAMPLE"),
    # 个人好友 wxid（出现在测试与文档里）
    ("wxid_EXAMPLE_FRIEND_A", "wxid_EXAMPLE_FRIEND_A"),
    ("wxid_EXAMPLE_FRIEND_B", "wxid_EXAMPLE_FRIEND_B"),
    ("wxid_EXAMPLE_FRIEND_C", "wxid_EXAMPLE_FRIEND_C"),
    # 主机名
    ("__HOSTNAME__", "__HOSTNAME__"),
    # 第三方库（wechatauto-replica）上游源码里残留的作者本机路径
    (r"__USERPROFILE__", "__USERPROFILE__"),
    (r"__USERPROFILE__", "__USERPROFILE__"),
]


def _load_local_scrub_rules() -> list[tuple[str, str]]:
    """把本机真实密钥、真实姓名加进擦除列表（不写死在源码里）。

    构建脚本自身绝不能含真实密钥或真实姓名——否则压缩包里就带着它了。
    所以这里在运行时读取：
      * work/db.key            —— 64 位十六进制数据库密钥
      * work/scrub_rules.json  —— 真实姓名 / wxid / 主机名 → 占位符
    两者都读不到就跳过；真正的兜底是 scan_secrets() 的全量扫描。
    """
    rules: list[tuple[str, str]] = []

    # 1) 数据库密钥
    for cand in (ROOT / "work" / "db.key",):
        try:
            raw = cand.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        if re.fullmatch(r"[0-9a-f]{64}", raw):
            rules.append((raw, "0" * 64))
            rules.append((raw.upper(), "0" * 64))

    # 2) 身份标识字典（姓名、wxid、主机名……）
    try:
        data = json.loads(
            (ROOT / "work" / "scrub_rules.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        for real, placeholder in data.items():
            if real.startswith("_") or not isinstance(placeholder, str) or not real:
                continue
            rules.append((real, placeholder))
            # wxid / 主机名这类 ASCII 标识大小写不敏感，补上大小写变体，
            # 否则 wxid_EXAMPLE_ACCOUNT 会从 wxid_EXAMPLE 的规则下漏掉。
            if real.isascii() and real.lower() != real.upper():
                rules.append((real.upper(), placeholder))
                rules.append((real.lower(), placeholder))

    # 长串优先，避免带后缀的长 wxid 被较短的同类规则先截断
    rules.sort(key=lambda kv: len(kv[0]), reverse=True)
    return rules


def _local_sensitive_values() -> list[str]:
    """需要确保'绝不出现'在产物里的真实值（用于安检）。"""
    return [real for real, _ in _load_local_scrub_rules() if real]

TEXT_EXT = {
    ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".xml", ".cmd", ".bat",
    ".ps1", ".vbs", ".c", ".h", ".ini", ".cfg", ".toml", ".gitignore", ".example",
    # Web 层：控制台前端里会出现好友备注名，漏掉就等于整层不脱敏
    ".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".vue", ".svelte", ".css", ".svg",
    # 其它脚本 / 配置
    ".sh", ".bash", ".psm1", ".psd1", ".pyi", ".conf", ".properties",
}

# ---------------------------------------------------------------------- 安检
# 产物中出现任意一项即判定为泄漏。
SECRET_PATTERNS = [
    (r"wxid_EXAMPLE", "个人 wxid（账号）"),
    (r"wxid_EXAMPLE_FRIEND_A", "个人 wxid（好友）"),
    (r"wxid_EXAMPLE_FRIEND_B", "个人 wxid（好友）"),
    (r"wxid_EXAMPLE_FRIEND_C", "个人 wxid（好友）"),
    (r"__HOSTNAME__", "本机主机名"),
    # 任意本机用户目录。这里刻意不写死具体用户名——否则构建脚本自身
    # 就把用户名带进了分发包；通用模式同时还能抓接收方自己的路径。
    (r"(?i)[A-Za-z]:\\{1,2}Users\\{1,2}[A-Za-z0-9._-]+", "本机用户路径"),
    # API Key 形态：要求 sk- 处于词首，避免 task-WeChat... 这类误报
    (r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}", "疑似 API Key"),
    # 64 位纯十六进制，但排除全 0 的占位符
    (r"(?<![0-9a-f])(?!0{64})[0-9a-f]{64}(?![0-9a-f])", "疑似 64 位十六进制密钥"),
]

# 以下模式出现在特定文件里是**预期且安全**的，不视为泄漏：
#
#   * 配置模板里的占位符     必须存在，否则接收方不知道要填什么
#   * 文档里讲解这些占位符   必须存在，否则教程讲不清
#   * 源码里"拒绝弱默认值"的守卫  这类字符串是被拒绝的对象，不是默认值
ALLOWED_CONTEXTS: list[tuple[str, str]] = [
    # (文件路径片段, 允许的字符串)
    ("config/auto_reply.example.json", "PUT-YOUR-API-KEY-HERE"),
    ("config/console.example.json", "PUT-YOUR-API-KEY-HERE"),
    ("SECRETS.md", "PUT-YOUR-API-KEY-HERE"),
    ("SECRETS.md", "change-me"),
    ("src/interactive_ipc.py", "change-me-local-agent-token"),
]


def _is_allowed(path: str, matched: str) -> bool:
    norm = path.replace("\\", "/")
    for frag, allowed in ALLOWED_CONTEXTS:
        if frag in norm and allowed in matched:
            return True
    return False


def log(msg: str) -> None:
    print(msg, flush=True)


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def is_excluded(path: Path) -> bool:
    """判断某个路径是否应当被排除。"""
    r = rel(path).lower()
    for part in EXCLUDE_PARTS:
        if part.lower() in r:
            return True
    if path.is_file() and path.suffix.lower() in EXCLUDE_EXT:
        return True
    # hook/build 只留指定 DLL
    if "/hook/build/" in "/" + r or r.startswith("hook/build/"):
        name = path.name.lower()
        if path.suffix.lower() == ".dll":
            return name not in HOOK_KEEP_DLL
        return name not in HOOK_KEEP_OTHER
    return False


def apply_substitutions(text: str) -> str:
    for old, new in SUBSTITUTIONS:
        text = text.replace(old, new)
    # 本机真实密钥的擦除规则在运行时构建，不写死在源码里
    for old, new in _load_local_scrub_rules():
        text = text.replace(old, new)
    return text


def _force_rmtree(path: Path) -> None:
    """删除目录树，自动清掉只读属性。

    Windows 上 git 对象、某些构建产物带只读位，shutil.rmtree 会直接抛
    PermissionError 让整个构建崩掉。这里对每个失败项先去掉只读再重试。
    """
    def _on_error(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            # 仍失败：尝试 win32 属性位，最后放弃该项而不是中止构建
            try:
                subprocess.run(["attrib", "-R", "-H", "-S", str(target)],
                               capture_output=True, timeout=20)
                func(target)
            except OSError:
                pass

    if path.exists():
        shutil.rmtree(path, onexc=_on_error)


def copy_tree() -> tuple[int, int]:
    """按白名单复制，返回 (文件数, 字节数)。"""
    if OUT_DIR.exists():
        _force_rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    nfiles = 0
    nbytes = 0
    skipped: list[str] = []

    sources: list[Path] = []
    for item in INCLUDE:
        p = ROOT / item
        if p.exists():
            sources.append(p)
        else:
            log(f"  [warn] 白名单条目不存在，跳过：{item}")
    for item in INCLUDE_FILES:
        p = ROOT / item
        if p.exists():
            sources.append(p)

    for src in sources:
        if src.is_file():
            items = [src]
        else:
            items = [p for p in src.rglob("*")]

        for p in items:
            if p.is_dir():
                continue
            if is_excluded(p):
                skipped.append(rel(p))
                continue
            target = OUT_DIR / rel(p)
            target.parent.mkdir(parents=True, exist_ok=True)

            if p.suffix.lower() in TEXT_EXT or p.name.startswith("."):
                try:
                    text = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    shutil.copy2(p, target)
                else:
                    new = apply_substitutions(text)
                    target.write_text(new, encoding="utf-8", newline="\n")
            else:
                shutil.copy2(p, target)

            nfiles += 1
            nbytes += target.stat().st_size

    log(f"  复制完成：{nfiles} 个文件，{nbytes/1024/1024:.1f} MB")
    log(f"  排除：{len(skipped)} 个文件")
    return nfiles, nbytes


def scan_secrets(root: Path) -> list[str]:
    """对产物做全量安检，返回命中列表。

    两个维度：
      1. 形态匹配（SECRET_PATTERNS）—— 抓 API Key、wxid、本机路径等
      2. 已知真实值的残留比对     —— 抓 work/db.key 与 work/scrub_rules.json
         里登记过的真实密钥/真实姓名。姓名是自由文本，形态匹配抓不到，
         只能靠这份运行时清单兜底（**含文件名**）。
    """
    hits: list[str] = []
    sensitive = _local_sensitive_values()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rp = rel(p)
        # 文件名本身也可能是泄漏（如 probe_<真实姓名>.py）
        for val in sensitive:
            if len(val) >= 2 and val in p.name:
                hits.append(f"{rp} — 文件名含本机身份标识 — {val}")
        if p.suffix.lower() not in TEXT_EXT and not p.name.startswith("."):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat, label in SECRET_PATTERNS:
            for m in re.finditer(pat, text):
                if _is_allowed(rp, m.group(0)):
                    continue
                hits.append(f"{rp} — {label} — {m.group(0)[:60]}")
        for val in sensitive:
            if len(val) >= 2 and val in text:
                hits.append(f"{rp} — 残留本机身份标识 — {val}")
    return hits


def write_manifest(root: Path) -> None:
    """写一份文件清单与校验和，便于接收方核对完整性。"""
    rows = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        data = p.read_bytes()
        rows.append({
            "path": str(p.relative_to(root)).replace("\\", "/"),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:16],
        })
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "file_count": len(rows),
        "total_bytes": sum(r["size"] for r in rows),
        "files": rows,
    }
    (root / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  清单：{len(rows)} 个文件")


def make_zip() -> Path:
    stamp = datetime.now().strftime("%Y%m%d")
    zip_path = DIST_ROOT / f"{PKG_NAME}-{stamp}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(OUT_DIR.rglob("*")):
            if p.is_file():
                z.write(p, Path(PKG_NAME) / p.relative_to(OUT_DIR))
    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只对现有产物做安检")
    args = ap.parse_args()

    log("=" * 60)
    log("  构建可分发版本")
    log("=" * 60)

    if not args.check:
        log("\n[1/4] 复制文件（白名单 + 排除规则）")
        copy_tree()

        # 清单必须先生成：它内部含全部文件名，如果文件名本身泄漏，
        # 先安检再生成清单就会漏掉——所以顺序是"复制 → 清单 → 安检 → 打包"。
        log("\n[2/4] 生成文件清单")
        write_manifest(OUT_DIR)

    log("\n[3/4] 安全扫描")
    hits = scan_secrets(OUT_DIR)
    if hits:
        log(f"  ✗ 发现 {len(hits)} 处敏感内容：")
        for h in hits[:40]:
            log(f"      {h}")
        if len(hits) > 40:
            log(f"      ...另有 {len(hits)-40} 处")
        log("\n  构建中止：产物不安全，请修正 SUBSTITUTIONS / EXCLUDE 规则")
        return 2
    log("  ✓ 未发现个人凭据或本机路径")

    if not args.check:
        log("\n[4/4] 打包")
        zp = make_zip()
        log(f"  ✓ {zp}")
        log(f"    大小 {zp.stat().st_size/1024/1024:.1f} MB")
    else:
        log("\n  （--check 模式，未重新生成文件）")

    log("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
