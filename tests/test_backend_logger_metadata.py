"""backend_logger 의 metadata 직렬화 회귀 테스트.

datetime 등 JSON 직렬화 불가 객체가 metadata 에 들어오면 json.dumps 가 죽고,
_log 의 except Exception 이 그걸 삼켜서 로그 레코드 자체가 유실되던 버그 방지.
"""
import json
import sys
import types
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# fastapi / xgen_sdk.db 는 이 테스트에 필요 없으니 최소 스텁으로 대체
if "fastapi" not in sys.modules:
    _fastapi = types.ModuleType("fastapi")
    _fastapi.Request = object
    sys.modules["fastapi"] = _fastapi
if "xgen_sdk.db" not in sys.modules:
    _db = types.ModuleType("xgen_sdk.db")
    _db.XgenDB = object
    sys.modules["xgen_sdk.db"] = _db

from xgen_sdk.logging.backend_logger import BackendLogger, _dump_metadata  # noqa: E402


class FakeDB:
    def __init__(self):
        self.records = []

    def insert_record(self, table, data):
        self.records.append((table, data))


def test_empty_metadata():
    assert _dump_metadata(None) == '{}'
    assert _dump_metadata({}) == '{}'


def test_datetime_family_is_isoformat():
    dt = datetime(2026, 8, 26, 13, 45, 30)
    out = json.loads(_dump_metadata({"dt": dt, "d": date(2026, 8, 26), "t": time(9, 0)}))
    assert out["dt"] == "2026-08-26T13:45:30"
    assert out["d"] == "2026-08-26"
    assert out["t"] == "09:00:00"


def test_decimal_and_uuid():
    uid = UUID("12345678-1234-5678-1234-567812345678")
    out = json.loads(_dump_metadata({"amount": Decimal("12.34"), "id": uid}))
    assert out["amount"] == 12.34
    assert out["id"] == str(uid)


def test_bytes_and_set():
    out = json.loads(_dump_metadata({"b": b"hi", "s": {1}}))
    assert out["b"] == "hi"
    assert out["s"] == [1]


def test_unknown_object_falls_back_to_str():
    class Weird:
        def __repr__(self):
            return "<weird>"

    out = json.loads(_dump_metadata({"w": Weird()}))
    assert out["w"] == "<weird>"


def test_korean_not_escaped():
    assert "한글" in _dump_metadata({"msg": "한글"})


def test_unserializable_dict_key_does_not_raise():
    # default= 로도 막을 수 없는 케이스 — 폴백이 받아야 한다
    out = json.loads(_dump_metadata({date(2026, 8, 26): 1}))
    assert "_serialization_error" in out


def test_circular_reference_does_not_raise():
    a = {}
    a["self"] = a
    out = json.loads(_dump_metadata(a))
    assert "_serialization_error" in out


@pytest.mark.parametrize("bad", [
    {"created_at": datetime(2026, 8, 26)},
    {date(2026, 8, 26): "key"},
])
def test_log_record_is_still_written(bad):
    db = FakeDB()
    log = BackendLogger(db, user_id=7)
    log.info("작업 시작", metadata=bad, function_name="do_work")

    assert len(db.records) == 1, "metadata 때문에 로그 레코드가 유실되면 안 된다"
    table, data = db.records[0]
    assert table == "backend_logs"
    assert data["message"] == "작업 시작"
    json.loads(data["metadata"])  # 항상 유효한 JSON


def test_error_with_exception_still_serializes():
    db = FakeDB()
    log = BackendLogger(db, user_id=1)
    log.error("실패", metadata={"at": datetime(2026, 8, 26)}, exception=ValueError("boom"))

    table, data = db.records[0]
    meta = json.loads(data["metadata"])
    assert meta["exception_type"] == "ValueError"
    assert meta["at"] == "2026-08-26T00:00:00"
