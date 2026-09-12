"""결재 양식 — 템플릿에서 시작하든 처음부터 만들든, 자유롭게 고친다.

무엇을 푸는가
-------------
결재는 **결재선 + 사유** 뿐이었다. 실제 절차는 단계마다 하는 일이 다르다 —
기안자는 기획서를 붙이고, 2차는 그것을 보고 위험도를 평가하고, 3차는 결정한다.

XGEN 은 그런 절차를 **템플릿**으로 몇 벌 제공하고, 조직은 그것을 복사해 자기
절차로 고친다. 4차·5차를 더하거나, 기획서를 빼거나, 위험도 평가를 빼거나,
전부 빼고 2차에서 끝내거나 — 그 전부가 되어야 한다.

여기서 고정하는 것
------------------
  · 단계는 **1급**이다. 칸이 없는 단계(3차 승인/반려)도 양식에 남는다.
  · 단계 번호는 끊기지 않고, 결재자는 최소 한 명.
  · 칸은 **있는 단계에만** 놓인다 — 줄이면서 칸을 남기면 아무도 못 채운다.
  · 기본 템플릿은 고치지 못하고 **복사만** 된다.
  · 상신하면 **복사**된다. 뒤에 양식이 바뀌어도 진행 중인 결재는 그대로.
  · 승인은 내 단계의 필수 칸을 다 채워야. **거절은 절대 막지 않는다.**
"""

from __future__ import annotations

import pytest

from xgen_sdk.approval import blocks, engine as E, registry, store, templates
from tests.test_approval_store import FakeDB


@pytest.fixture
def db():
    d = FakeDB()
    for uid, name in ((1, "기안자"), (3, "팀장"), (4, "부장"), (5, "임원")):
        d.add_user(uid, name, name)
    return d


def _line(db, name="배포 결재선", approvers=(3, 4)):
    return store.create_line(
        db, name=name, description="", owner_id=1, is_shared=True,
        steps=[{"approver_id": a, "step_order": i + 1} for i, a in enumerate(approvers)],
    )["id"]


def _from_template(db, tname="AI Agent 배포 결재", *, builtin=True, name=None):
    t = templates.by_name(tname)
    assert t is not None, tname
    return store.create_form(db, name=name or t["name"], description=t["description"],
                             owner_id=None, steps=t["steps"], blocks=t["blocks"],
                             is_builtin=builtin)


# ── 템플릿 ────────────────────────────────────────────────────────────

def test_xgen_ships_several_templates():
    names = [t["name"] for t in templates.BUILTIN_TEMPLATES]
    assert "AI Agent 배포 결재" in names
    assert len(names) >= 2, "템플릿이 하나뿐이면 '고를 수 있다' 가 성립하지 않는다"
    assert len(set(names)) == len(names), "이름이 겹치면 심을 때 어느 것인지 모른다"


def test_the_ai_deploy_template_is_the_three_step_procedure(db):
    form = store.get_form(db, _from_template(db))
    assert [s["step_index"] for s in form["steps"]] == [0, 1, 2]
    assert form["approver_steps"] == 2, "기안 + 결재자 2명"
    kinds = [(b["step_index"], b["block_type"], b["required"]) for b in form["blocks"]]
    assert kinds == [
        (0, blocks.AGENT_DEV_PLAN, True),
        (0, blocks.ATTACHMENTS, False),
        (1, blocks.RISK_ASSESSMENT, True),
    ]
    assert form["steps"][2]["title"], "칸이 없는 3차도 이름을 갖는다 — 없으면 편집기에서 사라진다"


# ── 모양 검증 ─────────────────────────────────────────────────────────

def test_a_form_needs_at_least_one_approver(db):
    with pytest.raises(E.ApprovalError) as e:
        store.create_form(db, name="기안만", owner_id=1,
                          steps=[{"step_index": 0, "title": "1차 기안"}], blocks=[])
    assert "결재자" in str(e.value)


def test_step_numbers_cannot_skip(db):
    with pytest.raises(E.ApprovalError) as e:
        store.create_form(db, name="구멍", owner_id=1, steps=[
            {"step_index": 0, "title": "1차"}, {"step_index": 2, "title": "3차"},
        ], blocks=[])
    assert "2차" in str(e.value), "어느 단계가 비었는지 말해야 고칠 수 있다"


def test_a_block_cannot_sit_on_a_step_that_does_not_exist(db):
    with pytest.raises(E.ApprovalError) as e:
        store.create_form(db, name="떠도는 칸", owner_id=1, steps=[
            {"step_index": 0, "title": "1차"}, {"step_index": 1, "title": "2차"},
        ], blocks=[{"step_index": 2, "block_type": blocks.TEXT, "label": "떠도는 칸"}])
    assert "떠도는 칸" in str(e.value)


def test_an_unknown_block_type_is_refused(db):
    with pytest.raises(E.ApprovalError):
        store.create_form(db, name="모르는 칸", owner_id=1, steps=[
            {"step_index": 0, "title": "1차"}, {"step_index": 1, "title": "2차"},
        ], blocks=[{"step_index": 0, "block_type": "없는종류", "label": "x"}])


# ── 커스터마이즈: 사용자가 실제로 할 일들 ─────────────────────────────

def test_copy_then_add_a_fourth_and_fifth_approver(db):
    copy_id = store.copy_form(db, _from_template(db), name="우리 팀 배포 결재", owner_id=1)
    src = store.get_form(db, copy_id)
    store.update_form(db, copy_id, steps=[
        *[{"step_index": s["step_index"], "title": s["title"], "guide": s.get("guide")}
          for s in src["steps"]],
        {"step_index": 3, "title": "4차 결재"},
        {"step_index": 4, "title": "5차 최종"},
    ])
    assert store.get_form(db, copy_id)["approver_steps"] == 4


def test_copy_then_drop_the_agent_plan(db):
    copy_id = store.copy_form(db, _from_template(db), name="기획서 없는 배포", owner_id=1)
    src = store.get_form(db, copy_id)
    store.update_form(db, copy_id, blocks=[
        {"step_index": b["step_index"], "block_type": b["block_type"], "label": b["label"],
         "required": b["required"], "sort_order": b["sort_order"]}
        for b in src["blocks"] if b["block_type"] != blocks.AGENT_DEV_PLAN
    ])
    kinds = [b["block_type"] for b in store.get_form(db, copy_id)["blocks"]]
    assert blocks.AGENT_DEV_PLAN not in kinds
    assert blocks.RISK_ASSESSMENT in kinds


def test_copy_then_drop_the_risk_assessment(db):
    copy_id = store.copy_form(db, _from_template(db), name="평가 없는 배포", owner_id=1)
    src = store.get_form(db, copy_id)
    store.update_form(db, copy_id, blocks=[
        {"step_index": b["step_index"], "block_type": b["block_type"], "label": b["label"],
         "required": b["required"], "sort_order": b["sort_order"]}
        for b in src["blocks"] if b["block_type"] != blocks.RISK_ASSESSMENT
    ])
    assert all(b["block_type"] != blocks.RISK_ASSESSMENT
               for b in store.get_form(db, copy_id)["blocks"])


def test_shrink_to_plan_only_and_finish_at_the_second_step(db):
    """전부 빼고 기획서 하나만 남겨 2차에서 끝낸다 — 지시한 시나리오 그대로."""
    copy_id = store.copy_form(db, _from_template(db), name="기획서만", owner_id=1)
    store.update_form(
        db, copy_id,
        steps=[{"step_index": 0, "title": "1차 기안"}, {"step_index": 1, "title": "2차 결재"}],
        blocks=[{"step_index": 0, "block_type": blocks.AGENT_DEV_PLAN,
                 "label": "Agent 기획서", "required": True, "sort_order": 1}],
    )
    form = store.get_form(db, copy_id)
    assert form["approver_steps"] == 1
    assert [b["block_type"] for b in form["blocks"]] == [blocks.AGENT_DEV_PLAN]


def test_shrinking_steps_while_leaving_a_block_behind_is_refused(db):
    """줄이면서 칸을 남기면 그 칸은 영영 아무도 못 채운다 — 조용히 버리지 않는다."""
    copy_id = store.copy_form(db, _from_template(db), name="줄이기", owner_id=1)
    src = store.get_form(db, copy_id)
    with pytest.raises(E.ApprovalError) as e:
        store.update_form(db, copy_id, steps=[
            {"step_index": 0, "title": "1차"}, {"step_index": 1, "title": "2차"},
        ], blocks=[*[{"step_index": b["step_index"], "block_type": b["block_type"],
                      "label": b["label"], "required": b["required"],
                      "sort_order": b["sort_order"]} for b in src["blocks"]],
                   {"step_index": 2, "block_type": blocks.TEXT, "label": "3차 의견"}])
    assert "3차" in str(e.value)


def test_a_form_can_be_built_from_scratch(db):
    """템플릿 없이 처음부터 — 4단계, 단계마다 다른 요구."""
    form_id = store.create_form(db, name="우리만의 절차", owner_id=1, steps=[
        {"step_index": 0, "title": "1차 기안"},
        {"step_index": 1, "title": "2차 법무 검토"},
        {"step_index": 2, "title": "3차 보안 검토"},
        {"step_index": 3, "title": "4차 최종"},
    ], blocks=[
        {"step_index": 0, "block_type": blocks.TEXT, "label": "요청 내용", "required": True},
        {"step_index": 1, "block_type": blocks.TEXT, "label": "법무 의견", "required": True},
        {"step_index": 2, "block_type": blocks.ATTACHMENTS, "label": "보안 점검표", "required": True},
    ])
    form = store.get_form(db, form_id)
    assert form["approver_steps"] == 3
    assert [b["step_index"] for b in form["blocks"]] == [0, 1, 2]


# ── 기본 템플릿은 복사만 ──────────────────────────────────────────────

def test_a_builtin_template_cannot_be_edited_or_deleted(db):
    form_id = _from_template(db)
    with pytest.raises(E.ApprovalError) as e:
        store.update_form(db, form_id, name="바꿔치기")
    assert "복사" in str(e.value), "무엇을 하라는 것인지 말해야 한다"
    with pytest.raises(E.ApprovalError):
        store.delete_form(db, form_id)


# ── 결재선에 붙이기 ───────────────────────────────────────────────────

def test_a_three_step_form_cannot_go_on_a_one_person_line(db):
    form_id = _from_template(db)
    with pytest.raises(E.ApprovalError) as e:
        store.set_line_form(db, _line(db, name="한 명", approvers=(3,)), form_id)
    assert "2명" in str(e.value), "몇 명이 필요한지 말해야 고칠 수 있다"


def test_growing_a_form_that_short_lines_already_use_is_refused(db):
    """늘리면 그 결재선의 결재는 채울 사람이 없는 칸을 안고 멈춘다."""
    form_id = store.create_form(db, name="늘릴 양식", owner_id=1, steps=[
        {"step_index": 0, "title": "1차"}, {"step_index": 1, "title": "2차"},
    ], blocks=[])
    store.set_line_form(db, _line(db, approvers=(3,)), form_id)
    with pytest.raises(E.ApprovalError) as e:
        store.update_form(db, form_id, steps=[
            {"step_index": 0, "title": "1차"}, {"step_index": 1, "title": "2차"},
            {"step_index": 2, "title": "3차"},
        ])
    assert "결재선" in str(e.value)


def test_a_form_in_use_cannot_be_deleted(db):
    form_id = store.create_form(db, name="쓰이는 양식", owner_id=1, steps=[
        {"step_index": 0, "title": "1차"}, {"step_index": 1, "title": "2차"}], blocks=[])
    store.set_line_form(db, _line(db, approvers=(3,)), form_id)
    with pytest.raises(E.ApprovalError) as e:
        store.delete_form(db, form_id)
    assert "결재선" in str(e.value)


# ── 상신 · 채우기 · 승인 ──────────────────────────────────────────────

def test_a_line_without_a_form_submits_with_no_blocks(db):
    req = store.submit(db, requester_id=1, title="배포", line_id=_line(db))
    assert req["blocks"] == [], "양식이 없으면 [기본 결재] — 채울 칸이 없다"


def test_submitting_snapshots_the_form(db):
    form_id = _from_template(db)
    line_id = _line(db)
    store.set_line_form(db, line_id, form_id)
    req = store.submit(db, requester_id=1, title="배포", line_id=line_id)
    assert [(b["step_order"], b["block_type"]) for b in req["blocks"]] == [
        (0, blocks.AGENT_DEV_PLAN), (0, blocks.ATTACHMENTS), (1, blocks.RISK_ASSESSMENT)]


def test_editing_the_form_later_does_not_touch_a_live_request(db):
    form_id = store.copy_form(db, _from_template(db), name="사본", owner_id=1)
    line_id = _line(db)
    store.set_line_form(db, line_id, form_id)
    req = store.submit(db, requester_id=1, title="배포", line_id=line_id)
    store.update_form(db, form_id, blocks=[])
    assert len(store.get(db, req["id"])["blocks"]) == 3, "올라간 결재의 칸은 스냅샷이다"


def test_only_the_drafter_fills_step_one(db):
    form_id = _from_template(db)
    line_id = _line(db)
    store.set_line_form(db, line_id, form_id)
    req = store.submit(db, requester_id=1, title="배포", line_id=line_id)
    plan = next(b for b in req["blocks"] if b["block_type"] == blocks.AGENT_DEV_PLAN)
    with pytest.raises(E.ApprovalError):
        store.fill_block(db, request_id=req["id"], block_id=plan["id"], actor_id=3,
                         data={"plan_id": 5})
    store.fill_block(db, request_id=req["id"], block_id=plan["id"], actor_id=1,
                     data={"plan_id": 5})


def test_the_second_approver_must_assess_before_approving(db):
    registry.register_action("generic", lambda *a, **k: None)
    form_id = _from_template(db)
    line_id = _line(db)
    store.set_line_form(db, line_id, form_id)
    rid = store.submit(db, requester_id=1, title="배포", line_id=line_id)["id"]

    with pytest.raises(E.ApprovalError) as e:
        store.decide(db, rid, actor_id=3, action="approved")
    assert "AI 위험도 평가" in str(e.value)

    risk = next(b for b in store.get(db, rid)["blocks"]
                if b["block_type"] == blocks.RISK_ASSESSMENT)
    store.fill_block(db, request_id=rid, block_id=risk["id"], actor_id=3,
                     data={"risk_level": "medium", "item_scores": {"a": 3}})
    store.decide(db, rid, actor_id=3, action="approved")
    store.decide(db, rid, actor_id=4, action="approved")
    assert store.get(db, rid)["status"] == "approved"


def test_rejection_never_waits_for_paperwork(db):
    registry.register_action("generic", lambda *a, **k: None)
    form_id = _from_template(db)
    line_id = _line(db)
    store.set_line_form(db, line_id, form_id)
    rid = store.submit(db, requester_id=1, title="배포", line_id=line_id)["id"]
    store.decide(db, rid, actor_id=3, action="rejected", note="대상이 아님")
    assert store.get(db, rid)["status"] == "rejected"
