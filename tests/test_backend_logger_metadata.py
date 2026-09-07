import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from xgen_sdk.logging.backend_logger import BackendLogger


class _DBStub:
    """insert_record 로 들어온 payload 를 그대로 붙잡아 두는 스텁."""

    def __init__(self):
        self.rows = []

    def insert_record(self, table, data):
        self.rows.append((table, data))
        return True


def test_metadata_with_datetime_is_persisted(caplog):
    """DB row 를 metadata 로 그대로 넘겨도 로그 행이 유실되지 않아야 한다.

    회귀: json.dumps 가 datetime 에서 TypeError 를 내면 _log 의 except 가 삼켜
    backend_logs 행이 통째로 누락됐다 (배포상태 조회 경로에서 실제 발생).
    """
    db = _DBStub()
    caplog.set_level(logging.ERROR, logger="backend-logger")

    BackendLogger(db, user_id=41380).success(
        "Deploy status retrieved successfully",
        metadata={
            "workflow_id": "wf_1785285623355_x73gftt",
            "created_at": datetime(2026, 8, 5, 6, 16, 34, tzinfo=timezone.utc),
            "score": Decimal("1.5"),
            "trace_id": UUID("12345678-1234-5678-1234-567812345678"),
        },
        function_name="get_deploy_status",
    )

    assert len(db.rows) == 1, "직렬화 실패로 행이 유실되면 안 된다"
    assert "Error logging backend data" not in caplog.text

    table, row = db.rows[0]
    assert table == "backend_logs"
    parsed = json.loads(row["metadata"])
    assert parsed["workflow_id"] == "wf_1785285623355_x73gftt"
    assert parsed["created_at"].startswith("2026-08-05")
    assert parsed["score"] == "1.5"


def test_metadata_keeps_korean_readable():
    """ensure_ascii=False — 관리자 화면에서 한글 metadata 가 \\uXXXX 로 보이지 않도록."""
    db = _DBStub()

    BackendLogger(db, user_id=1).info("작업 기록", metadata={"이름": "김진수"})

    _, row = db.rows[0]
    assert "김진수" in row["metadata"]


def test_no_metadata_stores_empty_object():
    db = _DBStub()

    BackendLogger(db, user_id=1).info("no metadata")

    _, row = db.rows[0]
    assert row["metadata"] == "{}"
