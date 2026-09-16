"""
WeChat 4.x 本地数据库只读适配器。

当前实现只做发现与明文 xInfo 读取；对 SQLCipher 消息库保持明确的
`DATABASE_KEY_UNAVAILABLE` 状态，不伪造解密成功。
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import struct
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .key_resolver import KeyDiscoveryCoordinator, KeyResolution, KeyResolver, database_signature


def discover_default_wechat_files_base() -> tuple[Path, str]:
    """发现微信文件目录，兼容以 LocalSystem 运行的 Gateway。

    Windows 服务的 ``USERPROFILE`` 通常指向 systemprofile，而微信数据属于
    交互用户。优先尊重显式配置；否则先尝试当前 profile，再在本机用户目录
    中寻找包含 ``wxid_*`` 账号的 ``Documents\\WeChat Files``。这里只做文件
    系统发现，不读取凭据，也不修改任何目录。
    """
    configured = os.environ.get("WECHAT_FILES_BASE", "").strip()
    if configured:
        return Path(configured), "env:WECHAT_FILES_BASE"

    profile = os.environ.get("USERPROFILE", "").strip()
    profile_candidate = Path(profile) / "Documents" / "WeChat Files" if profile else None
    if profile_candidate and profile_candidate.is_dir():
        return profile_candidate, "USERPROFILE"

    # Service accounts cannot see the interactive profile through USERPROFILE.
    # Restrict the fallback to the conventional per-user Documents location and
    # require at least one wxid directory, avoiding broad recursive searches.
    system_drive = os.environ.get("SystemDrive", "C:").strip() or "C:"
    # ``Path("C:") / "Users"`` is drive-relative on Windows; normalize the
    # common ``C:`` form to the actual drive root before enumerating users.
    if len(system_drive) == 2 and system_drive[1] == ":":
        system_drive += "\\"
    users_root = Path(system_drive) / "Users"
    candidates: list[tuple[float, Path]] = []
    try:
        user_dirs = users_root.iterdir()
    except OSError:
        user_dirs = ()
    for user_dir in user_dirs:
        candidate = user_dir / "Documents" / "WeChat Files"
        try:
            accounts = [item for item in candidate.iterdir()
                        if item.is_dir() and item.name.startswith("wxid_")]
        except OSError:
            continue
        if not accounts:
            continue
        latest = 0.0
        for account in accounts:
            try:
                latest = max(latest, max(
                    (item.stat().st_mtime for item in account.rglob("*.db")),
                    default=0.0,
                ))
            except OSError:
                continue
        candidates.append((latest, candidate))
    if candidates:
        candidates.sort(key=lambda item: (item[0], str(item[1]).casefold()), reverse=True)
        return candidates[0][1], "per-user-profile-scan"

    # Preserve a deterministic path for diagnostics when no directory is
    # visible; status reports unavailable rather than claiming a key failure.
    fallback = profile_candidate or (Path("Documents") / "WeChat Files")
    return fallback, "unresolved"


WECHAT_FILES_BASE, WECHAT_FILES_BASE_SOURCE = discover_default_wechat_files_base()


@dataclass
class DatabaseInfo:
    path: Path
    size: int
    modified: datetime
    encrypted: bool
    wal_path: Optional[Path] = None
    shm_path: Optional[Path] = None
    is_active: bool = False


@dataclass
class AccountInfo:
    wxid: str
    path: Path
    has_xinfo: bool
    database_count: int
    latest_mtime: float
    is_current: bool = False


class DatabaseAdapter:
    """只读发现层：账号、数据库清单、xInfo 与 SQLCipher 读取。

    数据库密钥只接受调用方显式提供的 32 字节密钥或本地已验证缓存。
    不执行进程内存扫描、注入或 hook；无密钥时保留可重试的 PENDING 状态。
    """

    def __init__(self, wxid: Optional[str] = None, base_dir: Optional[str | Path] = None):
        self.wxid = wxid
        self.base_dir = Path(base_dir) if base_dir is not None else WECHAT_FILES_BASE
        self.base_dir_source = "explicit" if base_dir is not None else WECHAT_FILES_BASE_SOURCE
        self._databases: Dict[str, DatabaseInfo] = {}
        self._accounts: Dict[str, AccountInfo] = {}
        self._conn = None
        self._key: Optional[str] = None
        self._key_bytes: Optional[bytes] = None
        # 可选的内存提取策略（默认关闭，需 WECHAT_ALLOW_KEY_EXTRACTION=1）。
        from .credential_resolver import build_reference_source
        try:
            extractor = build_reference_source(account=self.current_account())
        except Exception:
            extractor = None
        self.key_extractor = extractor
        self.key_resolver = KeyResolver(extractor=extractor)
        self._key_resolutions: Dict[str, KeyResolution] = {}
        self.key_discovery = KeyDiscoveryCoordinator(
            self.key_resolver,
            self._encrypted_paths,
        )
        self._decrypted_dir = Path(os.environ.get(
            "WECHAT_DB_WORKDIR",
            Path(__file__).resolve().parents[1] / "work" / "decrypted_db",
        ))
        self._decrypted_dir.mkdir(parents=True, exist_ok=True)
        # 进程内解密记忆化，见 _decrypt_to_cache。
        self._decrypt_memo: dict = {}
        self._decrypt_signatures: dict = {}
        # 联系人名称缓存，见 _name_cache。
        self._names: Optional[Dict[str, str]] = None

    @property
    def layout(self) -> str:
        account_dir = self.current_account_dir()
        if account_dir is None:
            return "unknown"
        if (account_dir / "db_storage").is_dir():
            return "db_storage"
        if (account_dir / "Msg").is_dir():
            return "legacy_msg"
        return "unknown"

    @staticmethod
    def _is_wxid_dir(path: Path) -> bool:
        return path.is_dir() and path.name.startswith("wxid_")

    def discover_account_records(self) -> List[AccountInfo]:
        records: List[AccountInfo] = []
        if not self.base_dir.exists():
            self._accounts = {}
            return []

        current = self.current_account()
        for item in sorted(self.base_dir.iterdir(), key=lambda p: p.name.lower()):
            if not self._is_wxid_dir(item):
                continue
            xinfo = item / "Msg" / "xInfo.db"
            db_count = 0
            latest_mtime = 0.0
            for db in item.rglob("*.db"):
                try:
                    st = db.stat()
                except OSError:
                    continue
                db_count += 1
                latest_mtime = max(latest_mtime, st.st_mtime)
            records.append(
                AccountInfo(
                    wxid=item.name,
                    path=item,
                    has_xinfo=xinfo.exists(),
                    database_count=db_count,
                    latest_mtime=latest_mtime,
                    is_current=item.name == current,
                )
            )
        self._accounts = {row.wxid: row for row in records}
        return records

    def discover_accounts(self) -> List[str]:
        return [row.wxid for row in self.discover_account_records()]

    def current_account(self) -> Optional[str]:
        if self.wxid:
            return self.wxid
        env_dir = os.environ.get("WECHAT_ACCOUNT_DIR")
        if env_dir:
            candidate = Path(env_dir)
            if candidate.name.startswith("wxid_"):
                return candidate.name
        if self.base_dir.exists():
            candidates = []
            for item in self.base_dir.iterdir():
                if not self._is_wxid_dir(item):
                    continue
                try:
                    latest = max((p.stat().st_mtime for p in item.rglob("*.db")), default=0.0)
                except OSError:
                    latest = 0.0
                candidates.append((latest, item.name))
            if candidates:
                candidates.sort(reverse=True)
                return candidates[0][1]
        return None

    def current_account_dir(self) -> Optional[Path]:
        wxid = self.current_account()
        if not wxid:
            return None
        candidate = self.base_dir / wxid
        return candidate if candidate.exists() else None

    def discover_databases(self, wxid: Optional[str] = None) -> Dict[str, DatabaseInfo]:
        account = wxid or self.current_account()
        if not account:
            self._databases = {}
            return {}
        base = self.base_dir / account
        databases: Dict[str, DatabaseInfo] = {}
        if not base.exists():
            self._databases = {}
            return {}

        for db_file in base.rglob("*.db"):
            rel_name = db_file.relative_to(base).as_posix()
            try:
                st = db_file.stat()
            except OSError:
                continue
            wal = db_file.with_suffix(".db-wal")
            shm = db_file.with_suffix(".db-shm")
            databases[rel_name] = DatabaseInfo(
                path=db_file,
                size=st.st_size,
                modified=datetime.fromtimestamp(st.st_mtime),
                encrypted=db_file.name.lower() != "xinfo.db",
                wal_path=wal if wal.exists() else None,
                shm_path=shm if shm.exists() else None,
                is_active=wal.exists() or shm.exists(),
            )
        self._databases = databases
        return databases

    def _encrypted_paths(self) -> list[Path]:
        """返回当前账号的加密库路径；仅做文件系统发现。"""
        return [info.path for info in self.discover_databases().values() if info.encrypted]

    def start_key_discovery(self) -> None:
        """启动低频合法密钥重试，不会扫描或附加到微信进程。"""
        self.key_discovery.start()

    def stop_key_discovery(self) -> None:
        self.key_discovery.stop()

    def key_discovery_status(self) -> Dict[str, Any]:
        return self.key_discovery.status()

    def xinfo_path(self, wxid: Optional[str] = None) -> Optional[Path]:
        account_dir = self.current_account_dir() if wxid is None else self.base_dir / wxid
        if account_dir is None:
            return None
        path = account_dir / "Msg" / "xInfo.db"
        return path if path.exists() else None

    def read_xinfo(self, wxid: Optional[str] = None) -> Dict[str, Any]:
        path = self.xinfo_path(wxid)
        if path is None:
            return {"available": False, "reason": "xInfo.db not found"}

        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            table_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            tables = [row["name"] for row in table_rows]
            payload: Dict[str, Any] = {
                "available": True,
                "path": str(path),
                "tables": tables,
                "table_count": len(tables),
                "current_account": wxid or self.current_account(),
                "records": {},
            }
            for table in tables:
                cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
                rows = conn.execute(f'SELECT * FROM "{table}" LIMIT 20').fetchall()
                serialised = []
                for row in rows:
                    item = {}
                    for col in cols:
                        value = row[col]
                        if isinstance(value, bytes):
                            item[col] = {
                                "type": "bytes",
                                "length": len(value),
                                "hex_prefix": value[:32].hex(),
                            }
                        else:
                            item[col] = value
                    serialised.append(item)
                payload["records"][table] = {
                    "columns": cols,
                    "row_count": len(rows),
                    "rows": serialised,
                }
            return payload
        finally:
            conn.close()

    def get_status(self) -> Dict[str, Any]:
        account = self.current_account()
        account_dir = self.current_account_dir()
        xinfo = self.read_xinfo(account) if account else {"available": False}
        databases = self.discover_databases(account)
        encrypted = any(info.encrypted for info in databases.values() if info.path.name.lower() != "xinfo.db")
        resolutions = self.resolve_keys()
        encrypted_names = [name for name, info in databases.items() if info.encrypted]
        ready_names = [name for name, item in resolutions.items() if item.status == "READY"]
        invalid_names = [name for name, item in resolutions.items() if item.validation.get("status") == "PAGE_HMAC_INVALID"]
        if account_dir is None:
            database_status_code = "DATABASE_DIRECTORY_UNAVAILABLE"
        elif not databases:
            database_status_code = "DATABASE_FILES_UNAVAILABLE"
        elif not encrypted_names:
            database_status_code = "DATABASE_KEY_NOT_REQUIRED"
        elif ready_names and len(ready_names) == len(encrypted_names):
            database_status_code = "DATABASE_READ_READY"
        else:
            database_status_code = "DATABASE_KEY_UNAVAILABLE"
        if not encrypted_names:
            key_state = "NOT_REQUIRED"
        elif ready_names:
            key_state = "READY" if len(ready_names) == len(encrypted_names) else "PARTIAL"
        elif invalid_names:
            key_state = "INVALID"
        else:
            key_state = "PENDING"
        return {
            "available": bool(account_dir),
            "provider": self.__class__.__name__,
            "base_dir": str(self.base_dir),
            "base_dir_source": self.base_dir_source,
            "current_account": account,
            "current_account_dir": str(account_dir) if account_dir else None,
            "accounts_found": len(self.discover_accounts()) if self.base_dir.exists() else 0,
            "database_files_found": bool(databases),
            "database_count": len(databases),
            "encrypted": encrypted,
            "xinfo_available": bool(xinfo.get("available")),
            "xinfo_table_count": xinfo.get("table_count") if xinfo.get("available") else 0,
            "layout": self.layout,
            "read_plane": "xinfo_only" if xinfo.get("available") else "unavailable",
            "key_discovered": bool(ready_names),
            # 保留旧字段，同时明确区分只解锁部分数据库的状态，避免调用方
            # 把单库密钥误解成整个消息读面已经可用。
            "database_key_status": (
                "DATABASE_KEY_NOT_REQUIRED" if not encrypted_names
                else "DATABASE_KEY_READY" if len(ready_names) == len(encrypted_names)
                else "DATABASE_KEY_PARTIAL" if ready_names
                else "DATABASE_KEY_UNAVAILABLE"
            ),
            # 与历史 database_key_status 保持兼容；调用方可用此字段区分
            # “目录不可见/没有数据库”和“数据库存在但缺少合法密钥”。
            "database_status_code": database_status_code,
            "key_discovery_state": key_state,
            "key_resolution": {
                name: {
                    "status": item.status,
                    "strategy": item.strategy,
                    "confidence": item.confidence,
                    "validation": item.validation,
                    "retryable": bool(item.evidence.get("retryable", False)),
                }
                for name, item in resolutions.items()
            },
            "key_source": sorted({item.strategy for item in resolutions.values() if item.strategy}),
            "database_signature": {
                name: item.evidence.get("signature")
                for name, item in resolutions.items()
                if item.evidence.get("signature")
            },
            "key_discovery": self.key_discovery_status(),
            "key_extractor": (self.key_extractor.status()
                              if getattr(self, "key_extractor", None) is not None else None),
        }

    def resolve_keys(self, pid: int | None = None,
                     wechat_version: str | None = None) -> Dict[str, KeyResolution]:
        """为当前账号的每个加密库解析密钥；调用安全且可重复。"""
        databases = self.discover_databases()
        result: Dict[str, KeyResolution] = {}
        for name, info in databases.items():
            if not info.encrypted:
                continue
            result[name] = self.key_resolver.resolve(
                info.path, pid=pid, wechat_version=wechat_version,
                explicit_key=self._key_bytes,
                explicit_source="adapter_set_key" if self._key_bytes is not None else None,
            )
        self._key_resolutions = result
        return result

    def key_resolution(self, db_name: str) -> KeyResolution:
        if db_name not in self._key_resolutions:
            self.resolve_keys()
        try:
            return self._key_resolutions[db_name]
        except KeyError as exc:
            raise FileNotFoundError(f"Database '{db_name}' not found in discovery.") from exc

    def _key_for(self, info: DatabaseInfo) -> Optional[bytes]:
        """取得该库可用的**raw AES key**，不把密钥放入状态或 evidence。

        调用方提供的 32 字节材料可能是 raw key，也可能是还需要配合该库
        salt 再派生一次的 passphrase（微信 4.x 常见）。这里统一解析成 raw key
        后返回，解密路径只认 raw key。
        """
        candidate: Optional[bytes] = None
        if self._key_bytes is not None:
            candidate = self._key_bytes
        else:
            try:
                signature = database_signature(info.path)
            except OSError:
                return None
            cached = self.key_resolver.cache.get(info.path, signature)
            if cached is not None:
                valid, _mode, raw = self._resolve_material(info.path, cached)
                if valid:
                    return raw
            explicit, _ = self.key_resolver._explicit_key_for(info.path)
            if explicit is not None:
                candidate = explicit
        if candidate is None:
            return None
        valid, _mode, raw = self._resolve_material(info.path, candidate)
        return raw if valid else None

    @staticmethod
    def _resolve_material(path, material: bytes):
        """按 raw/passphrase 两种模式解析材料，返回 (valid, mode, raw_key)。"""
        from .key_resolver import resolve_key_material
        try:
            with open(path, "rb") as stream:
                page1 = stream.read(4096)
        except OSError:
            return False, "invalid", None
        return resolve_key_material(page1, material)

    def set_key(self, key: str) -> None:
        raw = str(key or "").strip().removeprefix("0x")
        try:
            value = bytes.fromhex(raw)
        except ValueError as exc:
            raise ValueError("数据库密钥必须是 64 位十六进制字符串") from exc
        if len(value) != 32:
            raise ValueError("数据库密钥必须是 32 字节")
        self._key = raw.lower()
        self._key_bytes = value
        self._key_resolutions = {}

    def derive_key(self) -> Optional[str]:
        return None

    @staticmethod
    def _decrypt_page(key: bytes, page: bytes, page_number: int, page_size: int = 4096) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        reserve = 80
        iv = page[page_size - reserve: page_size - reserve + 16]
        encrypted = page[16: page_size - reserve] if page_number == 1 else page[: page_size - reserve]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(encrypted) + decryptor.finalize()
        return (b"SQLite format 3\x00" + plain if page_number == 1 else plain) + b"\x00" * reserve

    def _decrypt_to_cache(self, db_name: str, info: DatabaseInfo, key: bytes) -> Path:
        # 进程内记忆化：微信持续写 WAL，磁盘签名的 mtime 每次都变，导致全量
        # 重新解密。同一次归档里同一个库只解密一次即可，因此按 (路径, key)
        # 记住结果，避免每条消息查询都付一次全库解密的代价。
        memo_key = (str(info.path), key)
        hit = self._decrypt_memo.get(memo_key)
        if hit is not None and Path(hit).exists():
            resolution = self._key_resolutions.get(db_name)
            # 只有签名未变时才复用；库被写入过就重新解密以保证数据新鲜。
            try:
                current = database_signature(info.path)
            except OSError:
                current = None
            if current is not None and self._decrypt_signatures.get(memo_key) == current:
                return Path(hit)
        signature = database_signature(info.path)
        wal_path = self._current_wal_path(info)
        wal_signature = self._wal_signature(wal_path)
        digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:24]
        target = self._decrypted_dir / f"{digest}_{Path(db_name).name}"
        stamp = target.with_suffix(target.suffix + ".json")
        if target.exists() and stamp.exists():
            try:
                cached = json.loads(stamp.read_text(encoding="utf-8"))
                if (cached.get("format_version") == 3 and
                        cached.get("signature") == signature and
                        cached.get("wal_signature") == wal_signature):
                    return target
            except (OSError, ValueError):
                pass

        # 微信可能在读取期间 checkpoint/追加 WAL。最多重试三次，始终只操作
        # 临时副本；任何一次不一致都不会把半成品暴露给 sqlite3。
        temporary = target.with_suffix(target.suffix + ".tmp")
        last_error: Optional[Exception] = None
        for _attempt in range(3):
            try:
                before_db = database_signature(info.path)
                wal_path = self._current_wal_path(info)
                before_wal = self._wal_signature(wal_path)
                self._decrypt_file(info.path, temporary, key)
                if wal_path and before_wal is not None:
                    self._merge_wal(temporary, wal_path, key)
                after_db = database_signature(info.path)
                after_wal = self._wal_signature(self._current_wal_path(info))
                if before_db != after_db or before_wal != after_wal:
                    raise RuntimeError("数据库在解密期间发生变化")
                self._validate_decrypted_schema(temporary, db_name)
                os.replace(temporary, target)
                self._decrypt_memo[memo_key] = str(target)
                try:
                    self._decrypt_signatures[memo_key] = database_signature(info.path)
                except OSError:
                    self._decrypt_signatures.pop(memo_key, None)
                stamp.write_text(json.dumps({
                    "format_version": 3,
                    "signature": after_db,
                    "wal_signature": after_wal,
                }, ensure_ascii=False), encoding="utf-8")
                return target
            except (OSError, RuntimeError, sqlite3.DatabaseError) as exc:
                last_error = exc
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                # 刷新 WAL/主库元数据后继续下一轮；不在这里高速循环。
                signature = database_signature(info.path)
                wal_path = self._current_wal_path(info)
                wal_signature = self._wal_signature(wal_path)
        raise RuntimeError(f"数据库解密副本构建失败（文件持续变化）: {db_name}: {last_error}")

    @staticmethod
    def _current_wal_path(info: DatabaseInfo) -> Optional[Path]:
        """动态探测 WAL，覆盖首次发现时尚不存在或随后被 checkpoint 的情况。"""
        candidate = Path(str(info.path) + "-wal")
        return candidate if candidate.exists() else None

    @staticmethod
    def _wal_signature(path: Optional[Path]) -> Optional[Dict[str, Any]]:
        if path is None or not path.exists():
            return None
        try:
            stat = path.stat()
            with path.open("rb") as stream:
                header = stream.read(32)
        except OSError:
            return None
        return {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "header_sha256": hashlib.sha256(header).hexdigest(),
        }

    @staticmethod
    def _validate_decrypted_schema(path: Path, db_name: str) -> None:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"SCHEMA_READ_FAILED: 无法读取数据库 schema: {db_name}") from exc
        finally:
            conn.close()

    def _decrypt_file(self, source_path: Path, target_path: Path, key: bytes) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("rb") as source, target_path.open("wb") as output:
            page_number = 1
            while True:
                page = source.read(4096)
                if not page:
                    break
                if len(page) != 4096:
                    raise RuntimeError("数据库页面不完整")
                output.write(self._decrypt_page(key, page, page_number))
                page_number += 1

    def _merge_wal(self, target_path: Path, wal_path: Path, key: bytes) -> int:
        """将当前 WAL 世代的完整帧合并到临时明文副本。"""
        if not target_path.exists() or not wal_path.exists():
            return 0
        frame_size = 24 + 4096
        applied = 0
        max_page = 0
        with wal_path.open("rb") as wal, target_path.open("r+b") as output:
            wal_header = wal.read(32)
            if len(wal_header) < 32:
                return 0
            wal_salt = wal_header[16:24]
            while True:
                frame_header = wal.read(24)
                if not frame_header:
                    break
                if len(frame_header) < 24:
                    break
                page = wal.read(4096)
                if len(page) < 4096:
                    break
                page_number = struct.unpack(">I", frame_header[:4])[0]
                if page_number <= 0 or frame_header[8:16] != wal_salt:
                    continue
                plain = self._decrypt_page(key, page, page_number)
                if page_number == 1:
                    if not plain.startswith(b"SQLite format 3\\x00"):
                        continue
                elif plain[0] not in (0, 2, 5, 10, 13):
                    continue
                output.seek((page_number - 1) * 4096)
                output.write(plain)
                max_page = max(max_page, page_number)
                applied += 1
            output.flush()
            output.seek(0)
            page1 = output.read(4096)
            if len(page1) == 4096 and page1.startswith(b"SQLite format 3\\x00"):
                current_pages = struct.unpack(">I", page1[28:32])[0]
                actual_pages = (output.seek(0, os.SEEK_END) + 4095) // 4096
                page_count = max(current_pages, actual_pages, max_page)
                if page_count != current_pages:
                    output.seek(28)
                    output.write(struct.pack(">I", page_count))
                    output.flush()
        return applied

    def open(self, db_name: str) -> sqlite3.Connection:
        if not self._databases:
            self.discover_databases()
        info = self._databases.get(db_name)
        if info is None:
            raise FileNotFoundError(f"Database '{db_name}' not found in discovery.")
        if not info.encrypted:
            path = info.path
        else:
            resolution = self.key_resolution(db_name)
            key = self._key_for(info)
            if resolution.status != "READY" or key is None:
                code = "KEY_DISCOVERY_PENDING" if resolution.status == "PENDING" else resolution.validation.get("status", resolution.status)
                raise RuntimeError(f"{code}: 数据库无可用密钥: {db_name}")
            path = self._decrypt_to_cache(db_name, info, key)
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError:
            conn.close()
            raise RuntimeError(f"SCHEMA_READ_FAILED: 无法读取数据库 schema: {db_name}")
        return conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- Read operations (WeChat 4.x db_storage) ---
    #
    # 4.x 布局：message/message_<n>.db 里每个会话一张 Msg_<md5(username)> 表，
    # message_content 可能是 zstd 压缩（WCDB_CT_message_content=4）。
    # 这些读取只作用在解密副本上，原始库始终只读。

    # local_type → 可读类型。微信 4.x 的高位（如 25769803825 = 0x6_0000_0001）
    # 是标志位，低位才是真实类型；因此按键查表前先取低 32 位。
    _LOCAL_TYPE_MAP = {
        1: "text", 3: "image", 34: "voice", 42: "card", 43: "video",
        47: "sticker", 48: "location", 49: "app", 50: "voip",
        66: "redpacket", 10000: "system",
    }
    # appmsg 子类型（XML 里的 <type>），仅在 local_type 归为 app 时使用。
    _APPMSG_TYPE_MAP = {
        "1": "text", "2": "image", "3": "music", "4": "video", "5": "link",
        "6": "file", "7": "sticker", "8": "sticker", "17": "forward",
        "19": "chatrecord", "24": "note", "33": "app", "36": "miniprogram",
        "44": "redpacket", "48": "location", "57": "quote", "62": "video",
        "87": "announcement", "2000": "transfer", "2001": "redpacket",
    }

    @classmethod
    def _local_type_base(cls, local_type: Any) -> Optional[int]:
        """取 local_type 的真实低位类型。"""
        try:
            return int(local_type) & 0xFFFFFFFF
        except (TypeError, ValueError):
            return None

    def _classify_local_type(self, local_type: Any, content: str) -> str:
        """把 local_type（必要时结合 appmsg XML）映射成可读类型。"""
        base = self._local_type_base(local_type)
        kind = self._LOCAL_TYPE_MAP.get(base, f"type_{base}" if base is not None else "unknown")
        if kind in {"app", "type_49"} or (base is not None and base > 0xFFFFFFFF):
            # 高位置位的消息通常是 appmsg；读 XML <type> 判断真实子类型。
            match = re.search(r"<type>\s*(\d+)\s*</type>", content or "")
            if match:
                sub = self._APPMSG_TYPE_MAP.get(match.group(1))
                if sub:
                    return sub
        return kind

    def _message_db_names(self, wxid: Optional[str] = None) -> List[str]:
        """按最近修改排序的 4.x 消息库名（排除 fts/resource/媒体索引）。

        发现出来的相对路径在 4.x 下带 ``db_storage/`` 前缀，因此按路径段
        判断，而不是按前缀字符串拼接。
        """
        databases = self.discover_databases(wxid)
        out: List[tuple[float, str]] = []
        for name, info in databases.items():
            segments = [s for s in name.replace("\\", "/").lower().split("/") if s]
            if not segments or "message" not in segments[:-1]:
                continue
            leaf = segments[-1]
            if not leaf.endswith(".db"):
                continue
            if any(tag in leaf for tag in ("fts", "resource", "media")):
                continue
            out.append((info.modified.timestamp(), name))
        out.sort(reverse=True)
        return [name for _mtime, name in out]

    @staticmethod
    def _normalize_db_name(name: str) -> str:
        return str(name).replace("\\", "/").strip("/").removeprefix("./")

    def _resolve_db_key(self, db_name: str) -> Optional[str]:
        """把逻辑名（``contact/contact.db``）映射到实际发现的键。

        4.x 的发现键可能是 ``db_storage/contact/contact.db``；两种写法都接受，
        避免调用方必须知道部署前缀。
        """
        if not self._databases:
            self.discover_databases()
        target = self._normalize_db_name(db_name)
        if target in self._databases:
            return target
        for key in self._databases:
            normalized = self._normalize_db_name(key)
            if normalized == target or normalized.endswith("/" + target):
                return key
        return None

    def _open_readonly(self, db_name: str) -> sqlite3.Connection:
        """打开解密副本的独立连接，不覆盖 self._conn。"""
        key = self._resolve_db_key(db_name)
        if key is None:
            raise FileNotFoundError(f"Database '{db_name}' not found in discovery.")
        info = self._databases[key]
        if not info.encrypted:
            path = info.path
        else:
            resolution = self.key_resolution(key)
            resolved = self._key_for(info)
            if resolution.status != "READY" or resolved is None:
                code = ("KEY_DISCOVERY_PENDING" if resolution.status == "PENDING"
                        else resolution.validation.get("status", resolution.status))
                raise RuntimeError(f"{code}: 数据库无可用密钥: {db_name}")
            path = self._decrypt_to_cache(key, info, resolved)
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn

    @staticmethod
    def _message_table_for(chat_id: str) -> str:
        """4.x 会话表名 = Msg_ + md5(username)。"""
        return "Msg_" + hashlib.md5(str(chat_id).encode("utf-8")).hexdigest()

    @staticmethod
    def _decode_content(value: Any) -> str:
        """解码 message_content：zstd → utf-8；字符串原样返回。

        不做任何猜测式修复，无法解码时返回空串，调用方可据 message_type
        判断消息存在但内容不可读。
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        raw = bytes(value)
        if not raw:
            return ""
        if raw[:4] == b"\x28\xb5\x2f\xfd":  # zstd frame magic
            try:
                import zstandard
                return zstandard.ZstdDecompressor().decompress(raw).decode("utf-8", "replace")
            except Exception:
                return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    def _sender_map(self, conn: sqlite3.Connection) -> Dict[int, str]:
        """real_sender_id → username（Name2Id.rowid）。"""
        try:
            rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
        except sqlite3.DatabaseError:
            return {}
        return {int(row[0]): str(row[1]) for row in rows}

    def _msg_row_to_dict(self, row, sender_map: Dict[int, str]) -> Dict[str, Any]:
        keys = set(row.keys())
        local_type = row["local_type"] if "local_type" in keys else None
        sender_id = None
        if "real_sender_id" in keys and row["real_sender_id"] is not None:
            sender_id = sender_map.get(int(row["real_sender_id"]))
        create_time = row["create_time"] if "create_time" in keys else None
        timestamp = None
        if isinstance(create_time, (int, float)) and create_time:
            from datetime import datetime, timezone
            timestamp = datetime.fromtimestamp(float(create_time), tz=timezone.utc).isoformat()
        content = self._decode_content(row["message_content"]) if "message_content" in keys else ""
        return {
            "message_id": str(row["local_id"]) if "local_id" in keys and row["local_id"] is not None else None,
            "local_id": row["local_id"] if "local_id" in keys else None,
            "server_id": row["server_id"] if "server_id" in keys else None,
            "sender_id": sender_id,
            "sender_name": sender_id,
            "timestamp": timestamp,
            "create_time": create_time,
            "message_type": self._classify_local_type(local_type, content),
            "local_type": local_type,
            "content": content,
            "sort_seq": row["sort_seq"] if "sort_seq" in keys else None,
        }

    def get_accounts_readable(self) -> List[str]:
        return self.discover_accounts()

    def resolve_username(self, query: str) -> Optional[str]:
        """把昵称/备注/群名解析成 username；解析不到原样返回。"""
        q = str(query or "").strip()
        if not q:
            return None
        if q.startswith("wxid_") or "@chatroom" in q or q in {"filehelper", "medianote"}:
            return q
        folded = q.casefold()
        # 1) 会话表
        try:
            for name in self._message_db_names():
                pass
        except Exception:
            pass
        try:
            conn = self._open_readonly("session/session.db")
        except Exception:
            conn = None
        if conn is not None:
            try:
                rows = conn.execute(
                    "SELECT username FROM SessionTable"
                ).fetchall()
                for row in rows:
                    value = str(row["username"] or "")
                    if value.casefold() == folded:
                        return value
            finally:
                conn.close()
        # 2) 联系人表（username / remark / nick_name / alias）
        try:
            conn = self._open_readonly("contact/contact.db")
        except Exception:
            return q
        try:
            for column in ("username", "remark", "nick_name", "alias"):
                row = conn.execute(
                    f'SELECT username FROM contact WHERE "{column}" = ? COLLATE NOCASE LIMIT 1',
                    (q,),
                ).fetchone()
                if row and row["username"]:
                    return str(row["username"])
            row = conn.execute(
                'SELECT username FROM contact WHERE nick_name LIKE ? OR remark LIKE ? '
                'OR alias LIKE ? LIMIT 1',
                (f"%{q}%", f"%{q}%", f"%{q}%"),
            ).fetchone()
            if row and row["username"]:
                return str(row["username"])
        finally:
            conn.close()
        return q

    def _name_cache(self) -> Dict[str, str]:
        """username -> display name, loaded once per process.

        get_nickname() used to open the contact database on every call, and
        get_chats() calls it once per conversation: 210 chats meant 210 database
        opens, measured at ~85 seconds. A single load removes that entirely.
        """
        if self._names is not None:
            return self._names
        names: Dict[str, str] = {}
        try:
            conn = self._open_readonly("contact/contact.db")
        except Exception:
            self._names = names
            return names
        try:
            for row in conn.execute("SELECT username, remark, nick_name FROM contact"):
                u = str(row["username"] or "")
                if u:
                    names[u] = str(row["remark"] or row["nick_name"] or u)
        except Exception:
            pass
        finally:
            conn.close()
        self._names = names
        return names

    def get_nickname(self, username: str) -> str:
        """username → 备注/昵称；查不到返回原值。"""
        names = self._name_cache()
        if names:
            return names.get(str(username), str(username))
        try:
            conn = self._open_readonly("contact/contact.db")
        except Exception:
            return username
        try:
            row = conn.execute(
                'SELECT remark, nick_name FROM contact WHERE username = ? LIMIT 1',
                (str(username),),
            ).fetchone()
            if row:
                return str(row["remark"] or row["nick_name"] or username)
        finally:
            conn.close()
        return username

    def get_contacts(self) -> List[Dict[str, Any]]:
        try:
            conn = self._open_readonly("contact/contact.db")
        except Exception:
            return []
        try:
            rows = conn.execute(
                "SELECT username, remark, nick_name, alias, local_type "
                "FROM contact WHERE delete_flag = 0 OR delete_flag IS NULL LIMIT 2000"
            ).fetchall()
        except sqlite3.DatabaseError:
            rows = conn.execute("SELECT username, remark, nick_name, alias FROM contact LIMIT 2000").fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            username = str(row["username"] or "")
            if not username:
                continue
            out.append({
                "username": username,
                "id": username,
                "name": str(row["remark"] or row["nick_name"] or username) if "remark" in row.keys() else username,
                "remark": str(row["remark"]) if "remark" in row.keys() and row["remark"] is not None else "",
                "nick_name": str(row["nick_name"]) if "nick_name" in row.keys() and row["nick_name"] is not None else "",
                "alias": str(row["alias"]) if "alias" in row.keys() and row["alias"] is not None else "",
            })
        return out

    def get_chats(self) -> List[Dict[str, Any]]:
        try:
            conn = self._open_readonly("session/session.db")
        except Exception:
            return []
        try:
            rows = conn.execute(
                "SELECT username, unread_count, summary, last_timestamp, sort_timestamp, "
                "last_msg_sender, last_sender_display_name "
                "FROM SessionTable ORDER BY sort_timestamp DESC LIMIT 500"
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            conn.close()
        out = []
        for row in rows:
            username = str(row["username"] or "")
            if not username:
                continue
            display = row["last_sender_display_name"] if "last_sender_display_name" in row.keys() else None
            out.append({
                "id": username,
                "chat_id": username,
                "name": self.get_nickname(username),
                "preview": str(row["summary"]) if row["summary"] is not None else "",
                "time": row["sort_timestamp"] if "sort_timestamp" in row.keys() else None,
                "unread": row["unread_count"] if "unread_count" in row.keys() else None,
                "is_group": username.endswith("@chatroom"),
                "last_sender": str(display) if display else None,
            })
        return out

    def get_messages(self, chat_id: str, limit: int = 50,
                     before: Optional[str] = None) -> List[Dict[str, Any]]:
        """读取会话消息（4.x）。只返回解密副本上的内容。"""
        username = self.resolve_username(chat_id) or str(chat_id)
        table = self._message_table_for(username)
        sql = (
            f'SELECT local_id, server_id, local_type, real_sender_id, create_time, '
            f'message_content, WCDB_CT_message_content, sort_seq '
            f'FROM "{table}" ORDER BY sort_seq DESC LIMIT ?'
        )
        for db_name in self._message_db_names():
            try:
                conn = self._open_readonly(db_name)
            except Exception:
                continue
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                sender_map = self._sender_map(conn)
                rows = conn.execute(sql, (int(limit) * 4,)).fetchall()
                out = [self._msg_row_to_dict(r, sender_map) for r in rows]
                if before is not None:
                    try:
                        before_seq = int(before)
                        out = [m for m in out if (m.get("sort_seq") or 0) < before_seq]
                    except (TypeError, ValueError):
                        pass
                return out[: int(limit)]
            except sqlite3.DatabaseError:
                continue
            finally:
                conn.close()
        return []

    def search_messages(self, query: str, chat_id: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        """有界全文搜索：先解密再匹配，因此限制扫描的库与行数。"""
        needle = str(query or "")
        if not needle:
            return []
        results: List[Dict[str, Any]] = []
        if chat_id:
            rows = self.get_messages(chat_id, limit=max(int(limit) * 10, 200))
            return [m for m in rows if needle in (m.get("content") or "")][: int(limit)]
        scanned = 0
        for db_name in self._message_db_names()[:2]:
            try:
                conn = self._open_readonly(db_name)
            except Exception:
                continue
            try:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()]
                sender_map = self._sender_map(conn)
                for table in tables:
                    if len(results) >= int(limit) or scanned > 4000:
                        break
                    try:
                        rows = conn.execute(
                            f'SELECT local_id, server_id, local_type, real_sender_id, create_time, '
                            f'message_content, sort_seq FROM "{table}" '
                            f'ORDER BY sort_seq DESC LIMIT 200'
                        ).fetchall()
                    except sqlite3.DatabaseError:
                        continue
                    scanned += len(rows)
                    for row in rows:
                        info = self._msg_row_to_dict(row, sender_map)
                        if needle in (info.get("content") or ""):
                            info["chat_id"] = None
                            results.append(info)
                            if len(results) >= int(limit):
                                break
            finally:
                conn.close()
        return results[: int(limit)]

    def export_messages(self, chat_id: str, output_path: str, format: str = "json") -> str:
        rows = self.get_messages(chat_id, limit=10000)
        if format == "json":
            with open(output_path, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, indent=2)
        else:
            import csv
            with open(output_path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
                writer.writeheader()
                writer.writerows(rows)
        return output_path

    def to_json(self, output_path: str) -> str:
        data = {
            "base_dir": str(self.base_dir),
            "current_account": self.current_account(),
            "accounts": [
                {
                    "wxid": row.wxid,
                    "path": str(row.path),
                    "has_xinfo": row.has_xinfo,
                    "database_count": row.database_count,
                    "latest_mtime": row.latest_mtime,
                    "is_current": row.is_current,
                }
                for row in self.discover_account_records()
            ],
            "databases": {
                name: {
                    "path": str(info.path),
                    "size": info.size,
                    "modified": info.modified.isoformat(),
                    "encrypted": info.encrypted,
                    "has_wal": info.wal_path is not None,
                    "has_shm": info.shm_path is not None,
                    "is_active": info.is_active,
                }
                for name, info in self._databases.items() or self.discover_databases().items()
            },
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return output_path
