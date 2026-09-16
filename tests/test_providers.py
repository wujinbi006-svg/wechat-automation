import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))

from src.providers import ImportedDataProvider, Message, MockDataProvider, DatabaseDataProvider
from src.service_api import WeChatService

VALID_KEY_STATES = {
    "DATABASE_KEY_READY",
    "DATABASE_KEY_UNAVAILABLE",
    "DATABASE_KEY_PARTIAL",
    "DATABASE_KEY_NOT_REQUIRED",
}


def test_mock_and_service():
    p = MockDataProvider([Message("1", "c", "u", "张三", "2026-09-02", "text", "你好")])
    s = WeChatService(p)
    assert s.messages("c")[0]["content"] == "你好"
    assert s.search("你")[0]["message_id"] == "1"
    assert s.status()["capabilities"]["send_message"]["requires_confirmation"] is False


def test_imported_json(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"messages": [{"chat_id": "c", "content": "hello"}]}), encoding="utf-8")
    assert ImportedDataProvider(f).get_messages("c")[0]["content"] == "hello"


def test_database_account_is_discoverable_regardless_of_key():
    """Account discovery must not depend on whether a database key is present.

    This used to hardcode DATABASE_KEY_UNAVAILABLE, which silently turned into a
    false failure once a working key was wired in. The contract we actually own
    is: the account is discoverable, and the key state is one of the defined
    values — never an undefined string.
    """
    status = DatabaseDataProvider().get_status()
    assert status["available"] is True
    assert status["database_key_status"] in VALID_KEY_STATES
    # Readiness has two independent sources: the key opening the databases, or
    # the reference reader serving them. Assert only that the two most derived
    # flags agree with each other, rather than coupling readiness to the key.
    assert status["sqlcipher_open_success"] == status["sqlcipher_read_ready"]
    # when the key IS ready, key_discovered must say so
    if status["database_key_status"] == "DATABASE_KEY_READY":
        assert status["key_discovered"] is True


def test_no_key_reports_unavailable(monkeypatch):
    """With no key and no reference read plane, the provider must report UNAVAILABLE.

    Readiness is true when EITHER the explicit/cached key opens the databases OR
    the reference reader is available, so both must be disabled for this
    assertion to mean anything. Clearing only the key left the reference plane
    serving reads, which is why an earlier version of this test failed.
    """
    for var in ("WECHAT_DB_KEY", "WECHAT_DB_KEY_FILE", "WECHAT_DB_KEYS_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WECHAT_KEY_CACHE", str(Path(__file__).parent / "_nonexistent_cache.json"))
    monkeypatch.delenv("WECHAT_ALLOW_KEY_EXTRACTION", raising=False)

    provider = DatabaseDataProvider()
    provider.adapter._key_bytes = None
    provider.adapter._key_resolutions = {}
    # also disable the reference read plane
    provider._reference_db = None
    provider._reference_db_attempted = True

    status = provider.get_status()
    assert status["available"] is True
    assert status["database_key_status"] == "DATABASE_KEY_UNAVAILABLE"
    assert status["sqlcipher_read_ready"] is False
