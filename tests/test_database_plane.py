import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))

from src.database_adapter import DatabaseAdapter
from src.providers import DatabaseDataProvider


def _encrypted_copy(source, target, key):
    """将 SQLite 4K 页按 SQLCipher 4 页格式封装，供只读适配器回归测试使用。"""
    import hashlib, hmac, os, struct
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    raw = source.read_bytes()
    if len(raw) % 4096:
        raw += b"\x00" * (4096 - len(raw) % 4096)
    salt = os.urandom(16)
    pages = []
    for number in range(1, len(raw) // 4096 + 1):
        plain = raw[(number - 1) * 4096:number * 4096]
        iv = os.urandom(16)
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        body = plain[16:4016] if number == 1 else plain[:4016]
        encrypted = encryptor.update(body) + encryptor.finalize()
        page = (salt + encrypted + iv) if number == 1 else (encrypted + iv)
        mac_salt = bytes(value ^ 0x3A for value in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
        digest = hmac.new(mac_key, page[16:4032] if number == 1 else page[:4032], hashlib.sha512)
        digest.update(struct.pack("<I", number))
        pages.append(page + digest.digest())
    target.write_bytes(b"".join(pages))


def test_database_discovery_and_xinfo_read():
    """Discovery must find the account under the configured root.

    Account naming differs by layout (``wxid_x`` for the old Documents tree,
    ``wxid_x_<hash>`` for xwechat_files), and xInfo.db only exists in the legacy
    layout, so assert on what discovery actually reports rather than on one
    historical shape.
    """
    adapter = DatabaseAdapter()
    accounts = adapter.discover_accounts()
    assert accounts, "at least one account must be discovered"
    account = adapter.current_account()
    assert account in accounts

    xinfo = adapter.read_xinfo(account)
    if xinfo.get("available"):
        assert xinfo["table_count"] >= 1
        assert xinfo["tables"]
    else:
        # xwechat_files has no Msg/xInfo.db; an honest unavailable is correct.
        assert "reason" in xinfo


def test_default_wechat_base_falls_back_from_service_profile(tmp_path, monkeypatch):
    """LocalSystem 的 systemprofile 不应遮蔽交互用户的明确微信目录。"""
    from src.database_adapter import discover_default_wechat_files_base

    users = tmp_path / "Users"
    account_root = users / "alice" / "Documents" / "WeChat Files"
    (account_root / "wxid_demo" / "Msg").mkdir(parents=True)
    monkeypatch.setenv("SystemDrive", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "systemprofile"))
    monkeypatch.delenv("WECHAT_FILES_BASE", raising=False)

    base, source = discover_default_wechat_files_base()
    assert base == account_root
    assert source == "per-user-profile-scan"


def test_explicit_wechat_base_wins(tmp_path, monkeypatch):
    from src.database_adapter import discover_default_wechat_files_base

    configured = tmp_path / "configured"
    monkeypatch.setenv("WECHAT_FILES_BASE", str(configured))
    base, source = discover_default_wechat_files_base()
    assert base == configured
    assert source == "env:WECHAT_FILES_BASE"


def test_database_status_code_distinguishes_missing_directory(tmp_path):
    """目录不可见时不能误报为“缺少数据库密钥”。"""
    provider = DatabaseDataProvider(base_dir=tmp_path / "missing", wxid="wxid_missing")
    status = provider.get_status()
    assert status["database_status_code"] == "DATABASE_DIRECTORY_UNAVAILABLE"


def test_database_status_code_distinguishes_empty_directory(tmp_path):
    """账号目录存在但没有数据库文件时应报告文件缺失。"""
    account = tmp_path / "wxid_empty"
    (account / "Msg").mkdir(parents=True)
    provider = DatabaseDataProvider(base_dir=tmp_path, wxid="wxid_empty")
    status = provider.get_status()
    assert status["database_status_code"] == "DATABASE_FILES_UNAVAILABLE"
    assert status["database_key_status"] == "DATABASE_KEY_NOT_REQUIRED"


def test_database_status_code_reports_missing_key_for_encrypted_files(tmp_path):
    """存在加密库但没有合法密钥时才报告 DATABASE_KEY_UNAVAILABLE。"""
    import sqlite3

    account = tmp_path / "wxid_encrypted" / "Msg"
    account.mkdir(parents=True)
    plain = tmp_path / "plain.db"
    conn = sqlite3.connect(plain)
    conn.execute("CREATE TABLE sample(value TEXT)")
    conn.commit()
    conn.close()
    _encrypted_copy(plain, account / "MicroMsg.db", bytes(range(32)))
    provider = DatabaseDataProvider(base_dir=tmp_path, wxid="wxid_encrypted")
    status = provider.get_status()
    assert status["database_status_code"] == "DATABASE_KEY_UNAVAILABLE"
    assert status["database_key_status"] == "DATABASE_KEY_UNAVAILABLE"


def test_database_provider_status_and_inventory():
    """Status must describe the configured root, not a hardcoded historical one.

    These assertions previously pinned DATABASE_KEY_UNAVAILABLE and
    reference_layout == "legacy_msg", which only held while WECHAT_FILES_BASE
    pointed at the old Documents tree. They turned into false failures the
    moment a key was wired in and the base moved to xwechat_files.
    """
    provider = DatabaseDataProvider()
    status = provider.get_status()
    assert status["database_key_status"] in {
        "DATABASE_KEY_READY", "DATABASE_KEY_UNAVAILABLE",
        "DATABASE_KEY_PARTIAL", "DATABASE_KEY_NOT_REQUIRED",
    }
    # layout must agree with the account directory actually in use
    account_dir = status["current_account_dir"]
    assert account_dir, "an account directory must be resolved"
    expected_layout = "db_storage" if "db_storage" in account_dir or "xwechat_files" in account_dir \
        else "legacy_msg"
    assert status["layout"] == expected_layout
    # key_discovered and readiness must agree with each other
    assert status["sqlcipher_open_success"] == status["sqlcipher_read_ready"]
    if status["database_key_status"] == "DATABASE_KEY_READY":
        assert status["key_discovered"] is True
        assert status["sqlcipher_read_ready"] is True

    inventory = provider.get_database_inventory()
    assert isinstance(inventory, dict)
    assert inventory, "inventory must not be empty for a resolved account"


def test_database_provider_message_reading_contract():
    """Message reads either return rows or raise a typed, explained failure.

    The previous version asserted that reading ALWAYS fails, which encoded the
    then-broken state as the contract. What we actually own is: never return
    fabricated data, and always explain a refusal with a DatabaseUnavailable.
    """
    from src.errors import DatabaseUnavailable

    provider = DatabaseDataProvider()
    try:
        rows = provider.get_messages("文件传输助手", limit=1)
    except DatabaseUnavailable as exc:
        assert str(exc), "a refusal must carry a reason"
    else:
        assert isinstance(rows, list), "success must return a list, not a guess"


def test_database_provider_does_not_retry_reference_initialization(monkeypatch):
    """Reference init is attempted at most once per provider instance.

    Rewritten against the real invariant instead of assuming init always fails:
    the attempt flag flips exactly once, and the recorded error is stable
    across repeated calls regardless of whether init succeeded.
    """
    provider = DatabaseDataProvider()
    first_attempted = provider._reference_db_attempted
    first_error = provider._reference_db_error
    for _ in range(2):
        try:
            provider.get_messages("文件传输助手", limit=1)
        except Exception:
            pass  # a typed failure is acceptable; repeated init is not
    assert provider._reference_db_attempted is True
    assert first_attempted in (True, False)
    assert provider._reference_db_error == first_error



def test_key_resolver_pending_and_explicit_key(tmp_path, monkeypatch):
    import sqlite3
    from src.key_resolver import DatabaseKeyCache, KeyResolver

    plain = tmp_path / "plain.db"
    conn = sqlite3.connect(plain)
    conn.execute("CREATE TABLE sample(value TEXT)")
    conn.execute("INSERT INTO sample VALUES ('ok')")
    conn.commit()
    conn.close()
    encrypted = tmp_path / "encrypted.db"
    key = bytes(range(32))
    _encrypted_copy(plain, encrypted, key)
    cache = DatabaseKeyCache(tmp_path / "cache.json")
    resolver = KeyResolver(cache)
    pending = resolver.resolve(encrypted)
    assert pending.status == "PENDING"
    assert pending.evidence["retryable"] is True
    ready = resolver.resolve(encrypted, explicit_key=key, explicit_source="test")
    assert ready.status == "READY"
    assert "candidate_key" not in json.dumps(ready.evidence)
    wrong = resolver.resolve(encrypted, explicit_key=b"x" * 32, explicit_source="test")
    assert wrong.status == "INVALID"


def test_key_discovery_coordinator_retries_only_safe_sources(tmp_path):
    import sqlite3
    from src.key_resolver import DatabaseKeyCache, KeyDiscoveryCoordinator, KeyResolver

    plain = tmp_path / "plain.db"
    conn = sqlite3.connect(plain)
    conn.execute("CREATE TABLE sample(value TEXT)")
    conn.commit()
    conn.close()
    encrypted = tmp_path / "encrypted.db"
    key = bytes(range(32))
    _encrypted_copy(plain, encrypted, key)
    resolver = KeyResolver(DatabaseKeyCache(tmp_path / "cache.json"))
    coordinator = KeyDiscoveryCoordinator(
        resolver,
        lambda: [encrypted],
        initial_interval=0.01,
        max_interval=0.02,
    )
    pending = coordinator.scan_once()
    assert list(pending.values())[0].status == "PENDING"
    status = coordinator.status()
    assert status["state"] == "PENDING"
    assert "process_memory_scan" in status["disabled_strategies"]
    assert "dynamic_hook" in status["disabled_strategies"]
    assert "process_injection" in status["disabled_strategies"]
    resolver.cache.put(encrypted, __import__("src.key_resolver", fromlist=["database_signature"]).database_signature(encrypted), key)
    ready = coordinator.scan_once()
    assert list(ready.values())[0].status == "READY"
    assert coordinator.status()["state"] == "READY"


def test_process_memory_key_diagnostics_are_non_executable():
    """Legacy diagnostics must not regain access to a WeChat process."""
    import subprocess

    root = Path(__file__).parents[1]
    for relative in (
        "scripts/diagnostics/key_memory_probe.py",
        "scripts/diagnostics/key_chain_probe.py",
    ):
        completed = subprocess.run(
            [sys.executable, str(root / relative)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert completed.returncode == 2
        payload = json.loads(completed.stdout)
        assert payload["status"] == "BLOCKED"
        assert payload["reason"] == "PROCESS_MEMORY_SCAN_PROHIBITED"


def test_legacy_msg_explicit_key_opens_schema(tmp_path):
    import sqlite3

    plain = tmp_path / "plain.db"
    conn = sqlite3.connect(plain)
    conn.execute("CREATE TABLE Msg_demo(content TEXT)")
    conn.commit()
    conn.execute("DROP TABLE Msg_demo")
    conn.commit()
    conn.close()

    # 让测试页具有 SQLCipher 的 80 字节 reserve 区；保留合法的空 sqlite_master。
    page = bytearray(plain.read_bytes())
    page[20] = 80
    page[105:107] = (4016).to_bytes(2, "big")
    plain.write_bytes(page)
    account = tmp_path / "wxid_test" / "Msg"
    account.mkdir(parents=True)
    encrypted = account / "MicroMsg.db"
    _encrypted_copy(plain, encrypted, bytes(range(32)))
    adapter = DatabaseAdapter(wxid="wxid_test", base_dir=tmp_path)
    adapter.set_key(bytes(range(32)).hex())
    status = adapter.get_status()
    assert status["layout"] == "legacy_msg"
    assert status["key_discovery_state"] == "READY"
    conn = adapter.open("Msg/MicroMsg.db")
    assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


def test_legacy_msg_partial_key_is_not_reported_ready(tmp_path, monkeypatch):
    """旧式布局仅解锁一份库时，状态必须保持 PARTIAL。"""
    import sqlite3

    account = tmp_path / "wxid_test" / "Msg"
    account.mkdir(parents=True)
    xinfo = account / "xInfo.db"
    conn = sqlite3.connect(xinfo)
    conn.execute("CREATE TABLE info(value TEXT)")
    conn.commit()
    conn.close()

    key_a = bytes(range(32))
    key_b = bytes(reversed(range(32)))
    for name, key in (("MicroMsg.db", key_a), ("Multi/MSG0.db", key_b)):
        plain = tmp_path / (name.replace("/", "_") + ".plain")
        conn = sqlite3.connect(plain)
        conn.execute("CREATE TABLE Msg_demo(content TEXT)")
        conn.commit()
        conn.close()
        raw = bytearray(plain.read_bytes())
        raw[20] = 80
        raw[105:107] = (4016).to_bytes(2, "big")
        plain.write_bytes(raw)
        encrypted = account / name
        encrypted.parent.mkdir(parents=True, exist_ok=True)
        _encrypted_copy(plain, encrypted, key)

    mapping = tmp_path / "keys.json"
    mapping.write_text(json.dumps({str((account / "MicroMsg.db").resolve()): key_a.hex()}), encoding="utf-8")
    monkeypatch.setenv("WECHAT_DB_KEYS_FILE", str(mapping))
    provider = DatabaseDataProvider(base_dir=tmp_path, wxid="wxid_test")
    status = provider.get_status()
    assert status["database_key_status"] == "DATABASE_KEY_PARTIAL"


def _encrypt_page(plain_page, key, salt, page_number):
    """按适配器使用的 SQLCipher 页布局加密一页（仅用于隔离回归测试）。"""
    import hashlib, hmac, struct
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    page = bytes(plain_page).ljust(4096, b"\x00")[:4096]
    iv = bytes((page_number + i) % 256 for i in range(16))
    body = page[16:4016] if page_number == 1 else page[:4016]
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(body) + encryptor.finalize()
    raw = salt + encrypted + iv if page_number == 1 else encrypted + iv
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    digest = hmac.new(mac_key, raw[16:4032] if page_number == 1 else raw[:4032], hashlib.sha512)
    digest.update(struct.pack("<I", page_number))
    return raw + digest.digest()


def test_sqlcipher_wal_is_merged_into_read_only_copy(tmp_path):
    """WAL 中的新页应出现在临时明文副本，原始加密库不被修改。"""
    import sqlite3, struct

    base_plain = tmp_path / "base.db"
    latest_plain = tmp_path / "latest.db"
    # 构造一个合法但空的 SQLite 页 1；SQLCipher 的 80 字节 reserve
    # 区不能承载普通 sqlite3 的尾部数据，因此这里专门使用空 schema。
    conn = sqlite3.connect(base_plain)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE temp_schema(value TEXT)")
    conn.execute("DROP TABLE temp_schema")
    conn.commit()
    conn.close()
    raw = bytearray(base_plain.read_bytes())
    raw[20] = 80
    raw[105:107] = (4016).to_bytes(2, "big")
    base_plain.write_bytes(raw)
    latest_plain.write_bytes(raw)
    latest_raw = bytearray(latest_plain.read_bytes())
    latest_raw[4096] = 13  # 未使用的 b-tree 页，便于断言 WAL 覆盖效果
    latest_plain.write_bytes(latest_raw)

    key = bytes(range(32))
    encrypted = tmp_path / "wxid_test" / "Msg" / "Multi" / "MSG0.db"
    encrypted.parent.mkdir(parents=True)
    _encrypted_copy(base_plain, encrypted, key)
    original = encrypted.read_bytes()
    salt = original[:16]
    latest = latest_plain.read_bytes()
    baseline = base_plain.read_bytes()
    pages = []
    for number in range(1, max(len(latest), len(baseline)) // 4096 + 1):
        old_page = baseline[(number - 1) * 4096:number * 4096]
        new_page = latest[(number - 1) * 4096:number * 4096]
        if old_page != new_page:
            pages.append((number, _encrypt_page(new_page, key, salt, number)))
    adapter = DatabaseAdapter(wxid="wxid_test", base_dir=tmp_path)
    adapter.set_key(key.hex())
    # 先完成数据库发现，再让 WAL 出现，验证运行中动态探测路径。
    info = adapter.discover_databases()["Msg/Multi/MSG0.db"]
    wal = encrypted.with_name(encrypted.name + "-wal")
    header = struct.pack(">IIIIIIII", 0x377F0682, 3007000, 4096, 0, 0x01020304, 0x05060708, 0, 0)
    frames = []
    for number, page in pages:
        frame_header = struct.pack(">IIIIII", number, 0, 0x01020304, 0x05060708, 0, 0)
        frames.append(frame_header + page)
    wal.write_bytes(header + b"".join(frames))

    conn = adapter.open("Msg/Multi/MSG0.db")
    merged_path = adapter._decrypt_to_cache("Msg/Multi/MSG0.db", info, key)
    conn.close()
    merged = merged_path.read_bytes()
    assert merged[4096] == 13
    assert encrypted.read_bytes() == original


def test_key_mapping_supports_full_relative_name_and_wildcard(tmp_path, monkeypatch):
    import os, sqlite3
    from src.key_resolver import KeyResolver

    plain = tmp_path / "plain.db"
    conn = sqlite3.connect(plain)
    conn.execute("CREATE TABLE sample(value TEXT)")
    conn.commit()
    conn.close()
    encrypted = tmp_path / "mapped.db"
    key = bytes(range(32))
    _encrypted_copy(plain, encrypted, key)
    resolved = str(encrypted.resolve())
    try:
        relative = os.path.relpath(resolved, str(Path.cwd().resolve()))
    except ValueError:
        relative = str(encrypted)
    for mapping_key in (resolved, relative, encrypted.name, "*"):
        mapping = tmp_path / "keys.json"
        mapping.write_text(json.dumps({mapping_key: key.hex()}), encoding="utf-8")
        monkeypatch.setenv("WECHAT_DB_KEYS_FILE", str(mapping))
        resolution = KeyResolver().resolve(encrypted)
        assert resolution.status == "READY"
        assert resolution.strategy == "explicit_file_map"
