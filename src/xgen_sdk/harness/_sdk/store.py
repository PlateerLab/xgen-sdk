"""XgenDB 백엔드 SessionStore — 하네스 세션을 플랫폼 DB(harness_sessions)에 영속."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from xgen_sdk.db import XgenDB

_TABLE = "harness_sessions"
_LIST_LIMIT = 100_000


class XgenDBSessionStore:
    def __init__(self, db: "XgenDB", table: str = _TABLE) -> None:
        self._db = db
        self._table = table
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._db.execute_raw_query(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "session_id VARCHAR(64) PRIMARY KEY, "
            "session_data TEXT NOT NULL, "
            "updated_at DOUBLE PRECISION)"
        )

    def save(self, session_id: str, data: dict[str, Any]) -> None:
        payload = {
            "session_data": json.dumps(data, ensure_ascii=False),
            "updated_at": time.time(),
        }
        existing = self._db.find_records_by_condition(
            self._table, {"session_id": session_id}, limit=1
        )
        if existing:
            self._db.update_records_by_condition(
                self._table, payload, {"session_id": session_id}
            )
        else:
            self._db.insert_record(self._table, {"session_id": session_id, **payload})

    def load(self, session_id: str) -> Optional[dict[str, Any]]:
        rows = self._db.find_records_by_condition(
            self._table, {"session_id": session_id}, limit=1
        )
        if not rows:
            return None
        raw = rows[0].get("session_data")
        return json.loads(raw) if isinstance(raw, str) else raw

    def list_sessions(self) -> list[str]:
        rows = self._db.find_records_by_condition(
            self._table, {}, limit=_LIST_LIMIT, select_columns=["session_id"]
        )
        return [r["session_id"] for r in rows]

    def delete(self, session_id: str) -> bool:
        res = self._db.delete_records_by_condition(self._table, {"session_id": session_id})
        return bool(res.get("affected_rows", 0))
