import json
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

import pytest

from xgen_sdk.logging.backend_logger import BackendLogger


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"created_at": datetime(2026, 10, 23, 1, 2, 3)}, {"created_at": "2026-10-23T01:02:03"}),
        (
            {"changes": [{"at": datetime(2026, 10, 23, tzinfo=timezone.utc)}]},
            {"changes": [{"at": "2026-10-23T00:00:00+00:00"}]},
        ),
        (
            {"date": date(2026, 10, 23), "id": UUID(int=7), "amount": Decimal("12.5")},
            {"date": "2026-10-23", "id": str(UUID(int=7)), "amount": 12.5},
        ),
        ({"enabled": True, "count": 2, "empty": None}, {"enabled": True, "count": 2, "empty": None}),
        ({}, {}),
        (None, {}),
    ],
)
def test_metadata_is_persisted_as_json(metadata, expected, caplog):
    db = Mock()
    BackendLogger(db, user_id=7).info("configuration updated", metadata, function_name="update")

    db.insert_record.assert_called_once()
    table, record = db.insert_record.call_args.args
    assert table == "backend_logs"
    assert json.loads(record["metadata"]) == expected
    assert record["user_id"] == 7
    assert record["function_name"] == "update"
    assert record["message"] == "configuration updated"
    assert "Error logging backend data" not in caplog.text


def test_serialization_does_not_mutate_input():
    db = Mock()
    timestamp = datetime(2026, 10, 23)
    metadata = {"rows": [{"at": timestamp}]}
    BackendLogger(db).success("saved", metadata)
    assert metadata["rows"][0]["at"] is timestamp


def test_database_failure_is_reported(caplog):
    db = Mock()
    db.insert_record.side_effect = RuntimeError("database unavailable")
    BackendLogger(db).info("saved", {"count": 1})
    assert "Error logging backend data" in caplog.text
