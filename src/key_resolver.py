"""可审计的微信数据库密钥解析与验证。

本模块只接受调用方显式提供的密钥或本地缓存密钥。它不会读取微信进程
内存、注入进程或安装 hook；这些路径会明确返回 ``UNAVAILABLE``，避免把
凭据提取误当成普通数据库功能。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


PAGE_SIZE = 4096
RESERVE_SIZE = 80
KEY_BYTES = 32
HMAC_SIZE = 64
# SQLCipher 4 默认 KDF 轮数。微信 4.x 的数据库在 passphrase 模式下用它把
# 32 字节候选材料与每库盐值派生成真正的 AES 密钥。
PASSPHRASE_ITERATIONS = 256000

# 密钥材料模式。同一个 32 字节值既可能是已经派生好的 raw key，也可能是还
# 需要配合每库 salt 再派生一次的 passphrase；两者必须区分，否则会静默解密
# 失败。
KEY_MODE_RAW = "raw_key"
KEY_MODE_PASSPHRASE = "passphrase"


@dataclass
class KeyCandidate:
    strategy: str
    candidate_key: str
    db_path: str
    confidence: float
    evidence: dict[str, Any]


@dataclass
class KeyResolution:
    status: str
    strategy: Optional[str]
    db_path: str
    confidence: float
    evidence: dict[str, Any]
    validation: dict[str, Any]


def database_signature(path: str | Path) -> dict[str, Any]:
    """返回不含密钥的数据库签名，用于缓存失效判断。"""
    p = Path(path)
    st = p.stat()
    with p.open("rb") as stream:
        prefix = stream.read(PAGE_SIZE)
    return {
        "path": str(p.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "page1_sha256": hashlib.sha256(prefix).hexdigest(),
    }


def _derive_mac_key(raw_key: bytes, salt: bytes) -> bytes:
    """SQLCipher 4 MAC key 派生：mac_salt = salt ^ 0x3A，2 轮 HMAC-SHA512。"""
    mac_salt = bytes(value ^ 0x3A for value in salt)
    return hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, dklen=KEY_BYTES)


def _hmac_stored(page1: bytes) -> tuple[bytes, bytes]:
    """返回 (参与 MAC 的密文片段, 页面内存储的 MAC)。"""
    hmac_data = page1[16 : PAGE_SIZE - RESERVE_SIZE + 16]
    start = PAGE_SIZE - RESERVE_SIZE + 16
    return hmac_data, page1[start : start + HMAC_SIZE]


def _verify_raw_key(raw_key: bytes, page1: bytes) -> bool:
    """用 raw key 直接校验页 1 的 HMAC（SQLCipher raw key 模式）。"""
    if len(raw_key) != KEY_BYTES or len(page1) < PAGE_SIZE:
        return False
    salt = page1[:16]
    mac_key = _derive_mac_key(raw_key, salt)
    hmac_data, stored = _hmac_stored(page1)
    digest = hmac.new(mac_key, hmac_data, hashlib.sha512)
    digest.update(struct.pack("<I", 1))
    return hmac.compare_digest(digest.digest(), stored)


def _derive_raw_key_from_passphrase(passphrase: bytes, salt: bytes) -> bytes:
    """把 passphrase 与库盐值派生成真正的 AES 密钥。"""
    return hashlib.pbkdf2_hmac(
        "sha512", passphrase, salt, PASSPHRASE_ITERATIONS, dklen=KEY_BYTES
    )


def _fingerprint(key: bytes) -> str:
    """密钥指纹：仅用于审计对齐，不可反推密钥。"""
    return hashlib.sha256(b"wechat-db-key-fingerprint:" + bytes(key)).hexdigest()[:16]


def resolve_key_material(page1: bytes, material: bytes) -> tuple[bool, str, Optional[bytes]]:
    """判定 32 字节材料的模式并返回可直接解密的 raw key。

    返回 ``(valid, mode, raw_key)``。先按 raw key 校验；失败再按 passphrase
    配合该库自身的 salt 派生一次。两种都失败时 ``mode`` 为 ``"invalid"``。
    绝不在失败时返回猜测出来的 key。
    """
    if not isinstance(material, (bytes, bytearray)) or len(material) != KEY_BYTES:
        return False, "invalid", None
    material = bytes(material)
    if len(page1) < PAGE_SIZE:
        return False, "invalid", None
    if _verify_raw_key(material, page1):
        return True, KEY_MODE_RAW, material
    derived = _derive_raw_key_from_passphrase(material, page1[:16])
    if _verify_raw_key(derived, page1):
        return True, KEY_MODE_PASSPHRASE, derived
    return False, "invalid", None


def _verify_sqlcipher4_key(key: bytes, page1: bytes) -> bool:
    """兼容旧调用点：任意模式通过即视为有效。"""
    valid, _mode, _raw = resolve_key_material(page1, key)
    return valid


class DatabaseKeyCache:
    """只在本地保存已通过页校验的密钥及其文件/进程绑定信息。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get(
            "WECHAT_KEY_CACHE",
            Path(__file__).resolve().parents[1] / "work" / "database_key_cache.json",
        ))

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"version": 1, "entries": {}}
        return raw if isinstance(raw, dict) else {"version": 1, "entries": {}}

    def get(self, path: str | Path, signature: dict[str, Any], pid: int | None = None) -> Optional[bytes]:
        entry = self.load().get("entries", {}).get(str(Path(path).resolve()))
        if not isinstance(entry, dict) or entry.get("signature") != signature:
            return None
        if pid is not None and entry.get("pid") not in (None, pid):
            return None
        encoded = entry.get("key")
        if not isinstance(encoded, str):
            return None
        try:
            key = bytes.fromhex(encoded)
        except ValueError:
            return None
        return key if len(key) == KEY_BYTES else None

    def put(self, path: str | Path, signature: dict[str, Any], key: bytes,
            pid: int | None = None, wechat_version: str | None = None,
            key_mode: str | None = None) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("数据库密钥必须是 32 字节")
        data = self.load()
        entries = data.setdefault("entries", {})
        entries[str(Path(path).resolve())] = {
            "signature": signature,
            "key": key.hex(),
            "key_mode": key_mode or KEY_MODE_RAW,
            "pid": pid,
            "wechat_version": wechat_version,
            "timestamp": time.time(),
            "validation_status": "PAGE_HMAC_VALID",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name per write. A fixed "<name>.tmp" collides when two
        # processes refresh the cache at once (the Session 0 Gateway and the
        # Session 1 sidecar both do), and the loser gets
        # "[Errno 13] Permission denied" surfacing as a failed tool call.
        tmp = self.path.with_suffix(
            f"{self.path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # A cache write is an optimisation, never a correctness requirement:
            # the resolver can always re-validate from the database page. Losing
            # a race must not fail the caller.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class KeyResolver:
    """按安全顺序解析并验证数据库密钥。

    支持 ``WECHAT_DB_KEY``（单个 64 位 hex）或 ``WECHAT_DB_KEY_FILE``。
    环境变量和文件内容永远不会写入返回的 evidence 或日志。
    """

    def __init__(self, cache: DatabaseKeyCache | None = None, extractor=None):
        self.cache = cache or DatabaseKeyCache()
        # 可选的内存提取策略（默认关闭，见 credential_resolver）。它只在显式
        # 密钥与已验证缓存都失败后才被询问，且自身也有启用开关。
        self.extractor = extractor

    @staticmethod
    def _explicit_key() -> tuple[bytes | None, str | None]:
        raw = os.environ.get("WECHAT_DB_KEY", "").strip()
        source = "env" if raw else None
        if not raw:
            key_file = os.environ.get("WECHAT_DB_KEY_FILE", "").strip()
            if key_file:
                source = "file"
                try:
                    raw = Path(key_file).read_text(encoding="utf-8").strip().splitlines()[0]
                except (OSError, IndexError):
                    return None, source
        try:
            value = bytes.fromhex(raw.removeprefix("0x"))
        except ValueError:
            return None, source
        return (value if len(value) == KEY_BYTES else None), source

    @classmethod
    def _explicit_key_for(cls, path: Path) -> tuple[bytes | None, str | None]:
        """支持多库密钥映射；映射文件只在本地读取，不进入 evidence。"""
        mapping_file = os.environ.get("WECHAT_DB_KEYS_FILE", "").strip()
        if mapping_file:
            try:
                mapping = json.loads(Path(mapping_file).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                mapping = None
            if isinstance(mapping, dict):
                resolved = path.resolve()
                # 配置文件中的相对键优先按调用进程 cwd 解释，同时支持
                # 相对映射文件目录，便于把 keys.json 与数据库清单一起部署。
                try:
                    relative = os.path.relpath(str(resolved), str(Path.cwd().resolve()))
                except ValueError:
                    relative = str(path)
                try:
                    mapping_relative = os.path.relpath(
                        str(resolved), str(Path(mapping_file).resolve().parent)
                    )
                except ValueError:
                    mapping_relative = str(path)
                candidates = (
                    str(resolved), str(resolved).replace("\\", "/"),
                    str(path), str(path).replace("\\", "/"),
                    relative, relative.replace("\\", "/"), path.name, "*",
                    mapping_relative, mapping_relative.replace("\\", "/"),
                )
                # Windows 路径大小写不敏感，且配置文件常混用斜杠；先按
                #候选顺序寻找精确键，再做规范化比较，避免误匹配同名库。
                entries = list(mapping.items())
                normalized = {
                    str(key).replace("\\", "/").casefold(): value
                    for key, value in entries
                }
                values = []
                for candidate in candidates:
                    if candidate in mapping:
                        values.append(mapping[candidate])
                    else:
                        values.append(normalized.get(candidate.replace("\\", "/").casefold()))
                for raw in values:
                    if not isinstance(raw, str):
                        continue
                    try:
                        value = bytes.fromhex(raw.strip().removeprefix("0x"))
                    except ValueError:
                        continue
                    if len(value) == KEY_BYTES:
                        return value, "file_map"
        return cls._explicit_key()

    @staticmethod
    def validate(path: str | Path, key: bytes) -> dict[str, Any]:
        p = Path(path)
        try:
            with p.open("rb") as stream:
                page1 = stream.read(PAGE_SIZE)
        except OSError as exc:
            return {"valid": False, "status": "READ_ERROR", "error": f"{type(exc).__name__}: {exc}"}
        if page1.startswith(b"SQLite format 3\x00"):
            return {"valid": False, "status": "PLAINTEXT_DATABASE"}
        valid, mode, _raw = resolve_key_material(page1, key)
        return {
            "valid": valid,
            "status": "PAGE_HMAC_VALID" if valid else "PAGE_HMAC_INVALID",
            "key_mode": mode,
        }

    def resolve(self, path: str | Path, pid: int | None = None,
                wechat_version: str | None = None,
                explicit_key: bytes | None = None,
                explicit_source: str | None = None) -> KeyResolution:
        p = Path(path)
        db_path = str(p.resolve())
        try:
            signature = database_signature(p)
        except OSError as exc:
            return KeyResolution("UNAVAILABLE", None, db_path, 0.0, {"reason": "db_not_readable", "error": str(exc)}, {})

        explicit, source = (explicit_key, explicit_source) if explicit_key is not None else self._explicit_key_for(p)
        if explicit is not None:
            validation = self.validate(p, explicit)
            if validation.get("valid"):
                self.cache.put(p, signature, explicit, pid=pid, wechat_version=wechat_version,
                               key_mode=validation.get("key_mode"))
                return KeyResolution("READY", f"explicit_{source}", db_path, 1.0,
                                     {"signature": signature, "key_mode": validation.get("key_mode")},
                                     validation)
            # 调用方显式提供了密钥时，不能静默回退到旧缓存。
            if explicit is not None:
                return KeyResolution("INVALID", f"explicit_{source}", db_path, 0.0,
                                     {"signature": signature, "retryable": True}, validation)

        cached = self.cache.get(p, signature, pid=pid)
        if cached is not None:
            validation = self.validate(p, cached)
            if validation.get("valid"):
                return KeyResolution("READY", "cache", db_path, 1.0, {"signature": signature, "cache": True}, validation)

        # 可选策略：复用参考实现提取的每库 raw key。默认关闭，且只在显式
        # 密钥与缓存都不可用时才询问，避免覆盖调用方的显式意图。
        if self.extractor is not None:
            try:
                extracted = self.extractor.lookup(p)
            except Exception as exc:
                extracted = None
                extraction_error = f"{type(exc).__name__}: {exc}"
            else:
                extraction_error = None
            if extracted is not None:
                validation = self.validate(p, extracted)
                if validation.get("valid"):
                    self.cache.put(p, signature, extracted, pid=pid,
                                   wechat_version=wechat_version,
                                   key_mode=validation.get("key_mode"))
                    return KeyResolution(
                        "READY", "reference_memory_extractor", db_path, 0.9,
                        {
                            "signature": signature,
                            "extractor": getattr(self.extractor, "name", "extractor"),
                            "fingerprint": _fingerprint(extracted),
                        },
                        validation,
                    )
            extractor_status = {}
            if hasattr(self.extractor, "status"):
                try:
                    extractor_status = self.extractor.status()
                except Exception:
                    extractor_status = {}

        return KeyResolution(
            "PENDING",
            None,
            db_path,
            0.0,
            {
                "signature": signature,
                "candidates": [],
                "disabled_strategies": [
                    strategy for strategy, active in (
                        ("process_memory_scan", bool(self.extractor is not None and getattr(self.extractor, "enabled", lambda: False)())),
                        ("dynamic_hook", False),
                        ("process_injection", False),
                    ) if not active
                ],
                "extractor": extractor_status if self.extractor is not None else None,
                "retryable": True,
                "reason": "no_explicit_or_cached_key",
            },
            {"valid": False, "status": "KEY_NOT_PROVIDED"},
        )


class KeyDiscoveryCoordinator:
    """对合法密钥来源做低频、可停止的重试。

    该协调器只重试 ``KeyResolver`` 已支持的显式密钥和已验证缓存，绝不
    扫描微信进程内存、注入进程或安装 hook。它适合挂在 Gateway 的后台
    生命周期中；没有密钥时保持 ``PENDING``，不会让服务线程退出。
    """

    def __init__(
        self,
        resolver: KeyResolver,
        paths_provider: Callable[[], Iterable[str | Path]],
        *,
        pid_provider: Callable[[], int | None] | None = None,
        version_provider: Callable[[], str | None] | None = None,
        initial_interval: float = 5.0,
        max_interval: float = 300.0,
    ):
        if initial_interval <= 0 or max_interval < initial_interval:
            raise ValueError("重试间隔必须为正，且 max_interval 不得小于 initial_interval")
        self.resolver = resolver
        self.paths_provider = paths_provider
        self.pid_provider = pid_provider or (lambda: None)
        self.version_provider = version_provider or (lambda: None)
        self.initial_interval = float(initial_interval)
        self.max_interval = float(max_interval)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._state = "PENDING"
        self._attempts = 0
        self._last_run: float | None = None
        self._last_error: str | None = None
        self._last_results: dict[str, KeyResolution] = {}

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def scan_once(self) -> dict[str, KeyResolution]:
        """执行一次安全解析，并返回不含密钥内容的解析对象。"""
        try:
            paths = list(self.paths_provider())
            pid = self.pid_provider()
            version = self.version_provider()
            results = {
                str(Path(path).resolve()): self.resolver.resolve(
                    path, pid=pid, wechat_version=version
                )
                for path in paths
            }
            with self._lock:
                self._attempts += 1
                self._last_run = time.time()
                self._last_error = None
                self._last_results = results
                statuses = [item.status for item in results.values()]
                if statuses and all(status == "READY" for status in statuses):
                    self._state = "READY"
                elif any(status == "READY" for status in statuses):
                    self._state = "PARTIAL"
                elif any(status == "INVALID" for status in statuses):
                    self._state = "INVALID"
                else:
                    self._state = "PENDING"
            return results
        except Exception as exc:
            with self._lock:
                self._attempts += 1
                self._last_run = time.time()
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._state = "PENDING"
            return {}

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run, name="wechat-key-discovery", daemon=True
            )
            self._thread.start()

    def request_scan(self) -> None:
        """唤醒后台线程尽快重试，不改变当前退避策略。"""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "attempts": self._attempts,
                "last_run": self._last_run,
                "last_error": self._last_error,
                "running": bool(self._thread and self._thread.is_alive()),
                "resolved_count": sum(
                    item.status == "READY" for item in self._last_results.values()
                ),
                "path_count": len(self._last_results),
                "allowed_sources": [
                    "WECHAT_DB_KEY",
                    "WECHAT_DB_KEY_FILE",
                    "WECHAT_DB_KEYS_FILE",
                    "validated_cache",
                ],
                "disabled_strategies": [
                    "process_memory_scan",
                    "dynamic_hook",
                    "process_injection",
                ],
            }

    def _run(self) -> None:
        delay = self.initial_interval
        while not self._stop.is_set():
            self.scan_once()
            if self.state in {"READY", "PARTIAL"}:
                delay = self.initial_interval
            else:
                delay = min(self.max_interval, max(self.initial_interval, delay * 2))
            self._wake.wait(delay)
            self._wake.clear()


def resolution_to_dict(result: KeyResolution) -> dict[str, Any]:
    return asdict(result)
