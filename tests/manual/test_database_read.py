"""手工实机验收：不伪造 PASS，直接读取诊断 evidence。"""
import json
from pathlib import Path


def test_database_read_layers():
    p = Path(__file__).parents[2] / "docs" / "evidence" / "database_open_result.json"
    assert p.exists(), "请先运行 scripts/diagnostics/test_database_read.py"
    result = json.loads(p.read_text(encoding="utf-8"))
    assert result["database_files_found"] is True
    # 手工验收允许 FAIL/BLOCKED；这里只验证结果结构完整。
    for key in ("encrypted", "key_discovered", "sqlcipher_open_success",
                "schema_discovered", "message_query_success", "message_read_success"):
        assert key in result
