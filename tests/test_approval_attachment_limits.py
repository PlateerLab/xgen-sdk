"""[파일 첨부] 칸 상한 — 플랫폼 공통 업로드 상한(1GiB)과 같다.

* 새로 추가하는 칸의 기본 상한은 1GiB.
* 저장된 옛 기본값(50MB, 화면에서 고칠 수 없는 값)은 기동 때 1GiB 로 올린다 — 양식과 진행 중 결재의 스냅샷 모두.
* 직접 정한 다른 값, 다른 종류의 칸, 상한이 없는 칸은 건드리지 않는다. 멱등.
"""
import json
import re

from xgen_sdk.approval import blocks, store


class _DB:
    def __init__(self, rows):
        self.rows = rows  # {table: [row]}
        self.updates = []

    def execute_raw_query(self, sql, params=()):
        s = " ".join(sql.split())
        m = re.match(r"SELECT id, config FROM (\w+) WHERE block_type IN \((.*)\)", s)
        if m:
            kinds = set(params)
            return {"success": True, "data": [dict(r) for r in self.rows[m.group(1)] if r["block_type"] in kinds]}
        m = re.match(r"UPDATE (\w+) SET config = %s WHERE id = %s", s)
        if m:
            cfg, rid = params
            for r in self.rows[m.group(1)]:
                if r["id"] == rid:
                    r["config"] = cfg
            self.updates.append((m.group(1), rid))
            return {"success": True, "data": []}
        raise AssertionError(f"unexpected SQL: {s}")


def _cfg(max_bytes):
    return json.dumps({"extensions": [], "max_count": 10, "max_bytes": max_bytes})


def test_new_attachment_blocks_default_to_one_gib():
    spec = next(s for s in blocks.catalog() if s["block_type"] == blocks.ATTACHMENTS)
    assert spec["default_config"]["max_bytes"] == 1024 ** 3 == blocks.ATTACHMENT_MAX_BYTES


def test_legacy_default_is_raised_everywhere_and_only_there():
    db = _DB({
        "approval_form_blocks": [
            {"id": 1, "block_type": "attachments", "config": _cfg(52428800)},
            {"id": 2, "block_type": "attachments", "config": _cfg(10 * 1024 * 1024)},  # 직접 정한 값
            {"id": 3, "block_type": "attachments", "config": None},
            {"id": 4, "block_type": "text", "config": json.dumps({"max_length": 52428800})},
        ],
        "approval_request_blocks": [
            {"id": 7, "block_type": "attachments", "config": _cfg(52428800)},
        ],
    })
    assert store.raise_legacy_attachment_limits(db) == 2
    assert sorted(db.updates) == [("approval_form_blocks", 1), ("approval_request_blocks", 7)]
    assert json.loads(db.rows["approval_form_blocks"][0]["config"])["max_bytes"] == 1024 ** 3
    assert json.loads(db.rows["approval_form_blocks"][1]["config"])["max_bytes"] == 10 * 1024 * 1024
    assert db.rows["approval_form_blocks"][2]["config"] is None
    assert store.raise_legacy_attachment_limits(db) == 0, "멱등"
