"""数据库凭据解析器 — 可插拔的多策略密钥来源。

本模块把「密钥从哪来」从 KeyResolver 中分离成独立策略，每个策略显式启停，
并留下不含密钥的审计记录（策略名 / 状态 / 指纹 / 数据库身份）。

内置策略：

1. ``ExplicitKeySource``  —— 环境变量 / 文件 / 多库映射（默认启用）
2. ``CacheKeySource``     —— 本地已通过页校验的缓存（默认启用）
3. ``ReferenceMemorySource`` —— 复用已验证的参考实现，从 Weixin.exe 进程的
   ``Config.Cipher`` 结构读取**每库各自的 raw key**。默认关闭，必须显式
   设置 ``WECHAT_ALLOW_KEY_EXTRACTION=1`` 才启用。

第 3 项不实现新的内存扫描算法：它调用 ``work/wechatauto_pkg`` 里已有的
``WeChatDB.extract_keys()``，与本机既有研究保持一致。

任何路径都不把密钥写入日志、状态或 evidence；只记录来源与指纹。
"""
from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

KEY_BYTES = 32

# 显式启用参考内存提取策略的环境变量。
ENABLE_EXTRACTION_ENV = "WECHAT_ALLOW_KEY_EXTRACTION"

# 默认关闭时记录在状态里的说明，避免把「未启用」误读成「提取失败」。
DISABLED_REASON = f"{ENABLE_EXTRACTION_ENV} 未启用"


def extraction_enabled() -> bool:
    return os.environ.get(ENABLE_EXTRACTION_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def key_fingerprint(key: bytes) -> str:
    """密钥指纹：只用于审计对齐，不能反推密钥。"""
    return hashlib.sha256(b"wechat-db-key-fingerprint:" + bytes(key)).hexdigest()[:16]


@dataclass
class CredentialResolution:
    """一次凭据解析的结果；不含密钥本体。"""
    database_identity: str
    strategy: str
    status: str            # READY / INVALID / UNAVAILABLE / DISABLED
    fingerprint: Optional[str] = None
    key_mode: str = "raw_key"
    detail: str = ""
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "database_identity": self.database_identity,
            "strategy": self.strategy,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "key_mode": self.key_mode,
            "detail": self.detail,
            "retryable": self.retryable,
        }


class ReferenceMemorySource:
    """可选策略：复用参考实现从 Weixin.exe 提取每库 raw key。

    该策略**不**实现新的内存扫描逻辑；它只调用 ``wechatauto.db.WeChatDB``
    已有的 ``extract_keys()``。提取结果按 relative db 路径缓存，并同时按
    basename 建立索引，方便调用方用任意一种键查表。
    """

    name = "reference_memory_extractor"

    def __init__(self, package_root: Optional[Path] = None, account: Optional[str] = None):
        self.package_root = Path(package_root) if package_root else None
        self.account = account
        self._lock = threading.RLock()
        self._by_rel: dict[str, bytes] = {}
        self._by_name: dict[str, bytes] = {}
        self._attempted = False
        self._last_error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------
    def enabled(self) -> bool:
        return extraction_enabled()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "strategy": self.name,
                "enabled": self.enabled(),
                "attempted": self._attempted,
                "resolved_count": len(self._by_rel),
                "last_error": self._last_error,
                "requires_env": ENABLE_EXTRACTION_ENV,
            }

    # -- extraction --------------------------------------------------
    @staticmethod
    def _default_package_root() -> Path:
        return Path(__file__).resolve().parents[1] / "work" / "wechatauto_pkg" / "unzipped"

    def _import_db(self):
        root = self.package_root or self._default_package_root()
        if root.exists():
            import sys
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
        from wechatauto.db import WeChatDB  # type: ignore
        return WeChatDB

    def extract(self) -> dict[str, bytes]:
        """执行一次提取；结果缓存，重复调用不会反复扫描进程。"""
        with self._lock:
            if self._attempted:
                return dict(self._by_rel)
            self._attempted = True
            if not self.enabled():
                self._last_error = DISABLED_REASON
                return {}
            try:
                WeChatDB = self._import_db()
                kwargs: dict[str, Any] = {}
                if self.account:
                    kwargs["account"] = self.account
                base = os.environ.get("WECHAT_FILES_BASE", "").strip()
                if base:
                    kwargs["db_dir"] = base
                db = WeChatDB(**kwargs)
                # 参考实现把已提取的 per-db raw key 放在 _keys 里。
                keys = dict(getattr(db, "_keys", {}) or {})
                if not keys:
                    keys = dict(db.extract_keys() or {})
                for rel, value in keys.items():
                    if not isinstance(value, (bytes, bytearray)) or len(value) != KEY_BYTES:
                        continue
                    rel_key = str(rel).replace("\\", "/")
                    self._by_rel[rel_key] = bytes(value)
                    self._by_name[Path(rel_key).name] = bytes(value)
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                return {}
            return dict(self._by_rel)

    def lookup(self, db_path: str | Path) -> Optional[bytes]:
        """按路径或文件名查找该库的 raw key。"""
        self.extract()
        with self._lock:
            if not self._by_rel:
                return None
            p = Path(db_path)
            name = p.name
            if name in self._by_name:
                return self._by_name[name]
            # 回退：用路径尾部逐段匹配（适配 db_storage/... 与 account/... 前缀差异）
            parts = [x for x in p.as_posix().split("/") if x]
            for i in range(len(parts)):
                suffix = "/".join(parts[i:])
                if suffix in self._by_rel:
                    return self._by_rel[suffix]
            for i in range(len(parts)):
                suffix = "/".join(parts[i:])
                candidate = self._by_name.get(Path(suffix).name)
                if candidate is not None and Path(suffix).name == name:
                    return candidate
            return None


def build_reference_source(account: Optional[str] = None) -> ReferenceMemorySource:
    return ReferenceMemorySource(account=account)
