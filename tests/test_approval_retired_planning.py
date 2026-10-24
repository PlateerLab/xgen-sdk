"""Retire planning without deleting history or bypassing other approval requirements."""
from copy import deepcopy

import pytest

from xgen_sdk.approval import blocks, engine, registry, store
from tests.test_approval_forms import _assessed, _from_template, _line, db


def legacy_block(db, table, **values):
    row = dict(id=db._next(table), step_index=0, step_order=0,
               block_type=blocks.AGENT_DEV_PLAN, label="Agent 기획서", config={},
               required=True, sort_order=9, data={"plan_id": 42},
               filled_by=1, filled_at=None, **values)
    db.t[table].append(row)
    return row


def test_catalog_and_new_forms_cannot_add_planning(db):
    assert blocks.AGENT_DEV_PLAN not in blocks.known_types()
    with pytest.raises(engine.ApprovalError):
        store.create_form(db, name="old client", owner_id=1,
                          steps=[{"step_index": 0}, {"step_index": 1}],
                          blocks=[{"step_index": 0, "block_type": blocks.AGENT_DEV_PLAN,
                                   "label": "기획서", "required": True}])


def test_old_form_rows_are_preserved_but_not_copied_into_new_requests(db):
    fid = _from_template(db)
    row = legacy_block(db, "approval_form_blocks", form_id=fid)
    before = deepcopy(row)
    assert all(b["block_type"] != blocks.AGENT_DEV_PLAN for b in store.get_form(db, fid)["blocks"])
    line = _line(db)
    store.set_line_form(db, line, fid)
    request = store.submit(db, requester_id=1, title="new request", line_id=line)
    assert all(b["block_type"] != blocks.AGENT_DEV_PLAN for b in request["blocks"])
    assert row == before


@pytest.mark.parametrize("data", [None, {"plan_id": 42}])
def test_old_requests_keep_history_and_require_other_blocks_and_approvers(db, data):
    fid = _from_template(db)
    line = _line(db)
    store.set_line_form(db, line, fid)
    rid = store.submit(db, requester_id=1, title="pending legacy", line_id=line)["id"]
    row = legacy_block(db, "approval_request_blocks", request_id=rid)
    row["data"] = data
    before = deepcopy(row)
    view = next(b for b in store.get(db, rid)["blocks"] if b["id"] == row["id"])
    assert view["retired"] and not view["required"]
    assert view["data"] == data
    with pytest.raises(engine.ApprovalError, match="종료"):
        store.fill_block(db, request_id=rid, block_id=row["id"], actor_id=1, data={"plan_id": 43})
    with pytest.raises(engine.ApprovalError):
        store.decide(db, rid, actor_id=5, action="approved")
    with pytest.raises(engine.ApprovalError, match="AI 위험도 평가"):
        store.decide(db, rid, actor_id=3, action="approved")
    evaluation = next(b for b in store.get(db, rid)["blocks"] if b["block_type"] == blocks.EVALUATION)
    store.fill_block(db, request_id=rid, block_id=evaluation["id"], actor_id=3, data=_assessed("low"))
    registry.register_action("generic", lambda *args, **kwargs: None)
    store.decide(db, rid, actor_id=3, action="approved")
    assert store.get(db, rid)["status"] == "pending"
    store.decide(db, rid, actor_id=4, action="approved")
    assert store.get(db, rid)["status"] == "approved"
    assert row == before


def test_stale_plan_payload_cannot_be_saved_as_a_different_slot():
    with pytest.raises(ValueError, match="종료"):
        blocks.validate_data({"block_type": blocks.ATTACHMENTS}, {"plan_id": 42})


def test_editing_old_form_preserves_archived_blocks_without_copying_them(db):
    fid = _from_template(db, builtin=False, name="editable legacy")
    archived = legacy_block(db, "approval_form_blocks", form_id=fid)
    before = deepcopy(archived)
    store.update_form(db, fid, steps=[{"step_index": 0}, {"step_index": 1}],
                      blocks=[{"step_index": 0, "block_type": blocks.TEXT,
                               "label": "일반 내용", "required": True}])
    assert before in db.t["approval_form_blocks"]
    assert [b["block_type"] for b in store.get_form(db, fid)["blocks"]] == [blocks.TEXT]
    copied = store.copy_form(db, fid, name="new copy", owner_id=1)
    assert [b["block_type"] for b in store.get_form(db, copied)["blocks"]] == [blocks.TEXT]
    line = _line(db, approvers=(3,))
    store.set_line_form(db, line, fid)
    req = store.submit(db, requester_id=1, title="new from edited form", line_id=line)
    assert [b["block_type"] for b in req["blocks"]] == [blocks.TEXT]
    assert before in db.t["approval_form_blocks"]
