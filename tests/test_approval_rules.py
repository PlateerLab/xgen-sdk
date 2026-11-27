"""**조건 규칙** — 요청이 실어 보낸 값으로 자동 승인 · 결재선 · 결재 필요를 정한다(2.30.0).

여기서 보는 것:

  1. 규칙이 없거나 ``metrics`` 를 안 주면 **지금과 똑같다** — 규칙을 읽지도 않는다.
  2. 처음 맞는 규칙이 이긴다. 값이 빠진 조건은 맞지 않는다(자동 승인으로 새지 않게).
  3. 자동 승인은 원장에 「승인(규칙 #n)」 으로 남고, 사람이 승인했을 때와 **같은 적용 길**을 탄다.
  4. 결재선 규칙은 고정 결재선처럼 — 요청자가 다른 줄을 들고 오면 거절한다.
  5. 저장할 때 거른다 — 모르는 값 이름 · 틀린 종류 · 없는 결재선.
  6. 칸이 아직 없는 옛 DB 에서도 깨지지 않는다(규칙이 없는 것으로 본다).
"""
from __future__ import annotations

import json

import pytest

from xgen_sdk.approval import catalog, notifier, policy, registry, store
from xgen_sdk.approval import engine as E
from test_approval_policy import PolicyDB

SPEC = catalog.AGENT_COMPUTER_SPEC
NET = catalog.AGENT_NETWORK


class RulesDB(PolicyDB):
    """정책 표의 ``rules`` 칸 · 결재의 ``rule_decision`` 칸 · 알림까지 아는 메모리 DB."""

    #: 칸이 아직 없는 옛 DB 처럼 굴 것인가.
    old_schema = False

    def __init__(self):
        super().__init__()
        self.notifications: list = []

    def _policy_row(self, action):
        return next((r for r in self.t["approval_action_policies"] if r["action_type"] == action), None)

    def _run(self, s, p):
        if self.old_schema and ("rules" in s.split(" FROM ")[0] or "rule_decision" in s):
            raise RuntimeError('column "rules" does not exist')
        if s.startswith("SELECT rules FROM approval_action_policies"):
            row = self._policy_row(p[0])
            return [{"rules": row.get("rules", "[]")}] if row else []
        if s.startswith("SELECT action_type, rules FROM approval_action_policies"):
            return [{"action_type": r["action_type"], "rules": r.get("rules", "[]")}
                    for r in self.t["approval_action_policies"]]
        if s.startswith("INSERT INTO approval_action_policies (action_type, required, default_line_id, line_locked, rules"):
            row = self._policy_row(p[0])
            if row:
                row.update(rules=p[4], updated_by=p[5], updated_at=p[6])
            else:
                self.t["approval_action_policies"].append(
                    {"action_type": p[0], "required": p[1], "default_line_id": p[2],
                     "line_locked": p[3], "rules": p[4], "updated_by": p[5], "updated_at": p[6]})
            return []
        if s.startswith("UPDATE approval_action_policies SET rules"):
            row = self._policy_row(p[1])
            if row:
                row["rules"] = p[0]
            return []
        if s.startswith("UPDATE approval_action_policies SET default_line_id = NULL"):
            for r in self.t["approval_action_policies"]:
                if r.get("default_line_id") == p[0]:
                    r.update(default_line_id=None, line_locked=False)
            return []
        if s.startswith("SELECT action_type FROM approval_action_policies WHERE default_line_id"):
            return [{"action_type": r["action_type"]} for r in self.t["approval_action_policies"]
                    if r.get("default_line_id") == p[0]]
        if s.startswith("SELECT id, is_active FROM approval_lines"):
            return [{"id": r["id"], "is_active": r.get("is_active", True)}
                    for r in self.t["approval_lines"] if r["id"] == p[0]]
        if s.startswith("SELECT owner_id FROM approval_lines"):
            return [{"owner_id": r["owner_id"]} for r in self.t["approval_lines"] if r["id"] == p[0]]
        if s.startswith("UPDATE approval_lines SET is_active = FALSE"):
            for r in self.t["approval_lines"]:
                if r["id"] == p[0]:
                    r["is_active"] = False
            return []
        if s.startswith("INSERT INTO approval_requests") and "rule_decision" in s:
            row = {"id": self._next("approval_requests"), "title": p[0], "reason": p[1],
                   "action_type": p[2], "payload": p[3], "requester_id": p[4],
                   "line_id": None, "form_id": None, "form_name": None,
                   "status": p[5], "current_step_order": 1, "decided_at": p[6],
                   "target_ref": p[7], "rule_decision": p[8],
                   "canceled_by": None, "cancel_note": None,
                   "applied_at": None, "apply_error": None, "created_at": "T0"}
            self.t["approval_requests"].append(row)
            return [{"id": row["id"]}]
        if s.startswith("UPDATE approval_requests SET rule_decision"):
            for r in self.t["approval_requests"]:
                if r["id"] == p[1]:
                    r["rule_decision"] = p[0]
            return []
        if s.startswith("SELECT id, rule_decision FROM approval_requests WHERE id IN"):
            return [{"id": r["id"], "rule_decision": r.get("rule_decision")}
                    for r in self.t["approval_requests"] if r["id"] in p and r.get("rule_decision")]
        if s.startswith("SELECT rule_decision FROM approval_requests"):
            return [{"rule_decision": r.get("rule_decision")}
                    for r in self.t["approval_requests"] if r["id"] == p[0]]
        if s.startswith("INSERT INTO user_notifications"):
            self.notifications.append({"user_id": p[0], "template_id": p[1],
                                       "title": p[2], "message": p[3], "link_url": p[4]})
            return []
        if s.startswith("SELECT link_url FROM notification_message_templates"):
            return []
        return super()._run(s, p)


@pytest.fixture
def db():
    d = RulesDB()
    for uid, name in ((1, "기안자"), (3, "팀장"), (4, "부장"), (5, "인프라")):
        d.add_user(uid, name)
    return d


def _line(db, *approvers, name="L"):
    return store.create_line(
        db, name=name, description="", owner_id=9, is_shared=True,
        steps=[{"approver_id": a, "step_order": i + 1} for i, a in enumerate(approvers)],
    )["id"]


def _cond(metric, op, value):
    return {"metric": metric, "op": op, "value": value}


SMALL = {"vcpus": 2, "mem_mib": 2048, "disk_gib": 20, "template": "xgen-base",
         "vm_use": False, "use_days": 7}


# ── 카탈로그 선언 ─────────────────────────────────────────────────────


def test_the_catalog_declares_what_each_request_carries():
    """설정 화면이 조건 칸을 그 목록으로 만든다 — 이름 · 사람이 읽는 이름 · 종류 · 단위."""
    spec = {m["name"]: m for m in catalog.metrics_of(SPEC)}
    assert set(spec) == {"vcpus", "mem_mib", "disk_gib", "template", "vm_use", "use_days"}
    assert spec["mem_mib"] == {"name": "mem_mib", "label": "메모리", "kind": "number", "unit": "MiB"}
    assert spec["template"]["kind"] == "string" and spec["vm_use"]["kind"] == "bool"
    net = {m["name"]: m["kind"] for m in catalog.metrics_of(NET)}
    assert net == {"targets": "number", "internal": "bool"}
    assert catalog.metrics_of("agent.deploy") == [] and catalog.metrics_of("no.such") == []
    assert catalog.spec(SPEC).to_dict()["metrics"], "화면이 받는 행위 정보에 값 목록이 없다"


def test_the_screen_gets_rules_and_metrics_with_each_action(db):
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", "<=", 2048)],
                                 "outcome": "auto_approve"}], actor_id=7)
    rows = {r["action_type"]: r for r in policy.list_all(db)}
    assert rows[SPEC]["rules"][0]["outcome"] == "auto_approve"
    assert rows[SPEC]["metrics"]
    assert rows["agent.deploy"]["rules"] == []


def test_requests_that_are_approvals_themselves_are_declared(db):
    """에이전트 컴퓨터 사양 · 나가는 통신은 주인의 요청 자체가 결재다 — [결재 목록 설정] 의 켜고 끔과 상관없이
    결재를 받는다. 맞는 규칙이 없을 때 미리 보기가 「결재 없이 실행」 이라고 하지 않게 ``required`` 가 참이다."""
    always = {sp.action_type for sp in catalog.CATALOG if sp.always_required}
    assert always == {SPEC, NET}
    assert catalog.spec(SPEC).to_dict()["always_required"] is True
    assert catalog.spec("agent.deploy").to_dict()["always_required"] is False
    policy.set_policy(db, SPEC, actor_id=7, required=False)
    rows = {r["action_type"]: r for r in policy.list_all(db)}
    assert rows[SPEC]["always_required"] is True and rows[NET]["always_required"] is True
    assert rows[SPEC]["required"] is False, "저장된 켜고 끔은 그대로 싣는다"
    assert rows["agent.deploy"]["always_required"] is False
    for action in (SPEC, NET):
        assert policy.is_required(db, action) is True
        pv = policy.preview(db, action, {"targets": 1, "internal": False} if action == NET else SMALL)
        assert pv["outcome"] == "policy" and pv["required"] is True and pv["auto_approve"] is False
    # 자동 승인 규칙은 그대로 듣는다.
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", "<=", 2048)], "outcome": "auto_approve"}], actor_id=7)
    pv = policy.preview(db, SPEC, SMALL)
    assert pv["outcome"] == "auto_approve" and pv["required"] is False
    # 선언이 없는 행위는 지금과 같다.
    assert policy.preview(db, "agent.deploy")["required"] is False


def test_callers_can_tell_the_feature_exists():
    assert getattr(policy, "RULES_SUPPORTED", False) is True
    assert callable(getattr(policy, "preview", None))


# ── 규칙 평가(순수) ───────────────────────────────────────────────────


RULES = [
    {"conditions": [_cond("mem_mib", "<=", 2048), _cond("vm_use", "==", False)],
     "outcome": "auto_approve", "line_id": None},
    {"conditions": [_cond("mem_mib", ">=", 8193)], "outcome": "line", "line_id": 42},
    {"conditions": [_cond("template", "in", ["gpu", "xgen-desktop"])], "outcome": "require",
     "line_id": None},
]


def test_the_first_matching_rule_wins():
    hit = policy.match_rule(RULES, SMALL)
    assert hit["rule"] == 1 and hit["outcome"] == "auto_approve"
    hit = policy.match_rule(RULES, {**SMALL, "mem_mib": 16384, "template": "gpu"})
    assert hit["rule"] == 2 and hit["outcome"] == "line" and hit["line_id"] == 42


def test_all_conditions_of_a_rule_must_hold():
    # 메모리는 작지만 처음 쓰는 요청이라 #1 이 맞지 않는다 — 다음 규칙도 안 맞으면 없음.
    assert policy.match_rule(RULES, {**SMALL, "vm_use": True}) is None


def test_in_and_not_equal():
    assert policy.match_rule(RULES, {**SMALL, "mem_mib": 4096, "template": "xgen-desktop"})["rule"] == 3
    rules = [{"conditions": [_cond("template", "!=", "xgen-base")], "outcome": "require"}]
    assert policy.match_rule(rules, {"template": "gpu"})["rule"] == 1
    assert policy.match_rule(rules, {"template": "xgen-base"}) is None


def test_a_missing_value_never_matches():
    """값을 안 보낸 요청은 어떤 조건에도 맞지 않는다 — 특히 자동 승인으로 새지 않는다."""
    assert policy.match_rule(RULES, {"vm_use": False}) is None
    assert policy.match_rule(RULES, {"mem_mib": None, "vm_use": False}) is None
    ne = [{"conditions": [_cond("template", "!=", "gpu")], "outcome": "auto_approve"}]
    assert policy.match_rule(ne, {}) is None, "없는 값을 '다르다' 로 보면 자동 승인이 샌다"


def test_a_value_of_the_wrong_kind_never_matches():
    assert policy.match_rule(RULES, {**SMALL, "mem_mib": "2048"}) is None
    assert policy.match_rule(RULES, {**SMALL, "mem_mib": True}) is None, "True 를 1 로 읽었다"
    ne = [{"conditions": [_cond("vm_use", "!=", True)], "outcome": "auto_approve"}]
    assert policy.match_rule(ne, {"vm_use": "no"}) is None


def test_no_metrics_or_no_rules_match_nothing():
    assert policy.match_rule(RULES, None) is None
    assert policy.match_rule([], SMALL) is None


# ── 규칙이 없을 때 · metrics 를 안 줄 때 = 지금과 같다 ────────────────


def test_without_rules_everything_is_as_before(db):
    lid = _line(db, 3)
    policy.set_policy(db, SPEC, actor_id=7, required=True, default_line_id=lid)
    assert policy.is_required(db, SPEC, SMALL) is True
    assert policy.resolve_line(db, SPEC, metrics=SMALL) == (lid, None)
    out = store.submit(db, requester_id=1, title="사양", action_type=SPEC,
                       line_id=lid, metrics=SMALL)
    assert out["status"] == "pending" and out["rule_decision"] is None
    assert store.rule_decision(db, out["id"]) is None
    pv = policy.preview(db, SPEC, SMALL)
    assert pv["outcome"] == "policy" and pv["required"] is True and pv["line_id"] == lid


def test_callers_without_metrics_never_read_rules(db):
    """``metrics`` 를 모르는 호출부는 규칙이 있어도 2.29.0 그대로 — 규칙 표를 읽지도 않는다."""
    lid = _line(db, 3)
    policy.set_policy(db, SPEC, actor_id=7, required=True, default_line_id=lid)
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", "<=", 4096)],
                                 "outcome": "auto_approve"}], actor_id=7)
    db.old_schema = True                     # 규칙 칸을 읽으려 하면 터진다
    assert policy.is_required(db, SPEC) is True
    assert policy.resolve_line(db, SPEC) == (lid, None)
    out = store.submit(db, requester_id=1, title="사양", action_type=SPEC, line_id=lid)
    assert out["status"] == "pending"


def test_an_old_database_without_the_columns_behaves_as_without_rules(db):
    lid = _line(db, 3)
    policy.set_policy(db, SPEC, actor_id=7, required=True, default_line_id=lid)
    db.old_schema = True
    assert policy.get_rules(db, SPEC) == []
    assert policy.evaluate(db, SPEC, SMALL) is None
    assert policy.resolve_line(db, SPEC, metrics=SMALL) == (lid, None)
    assert policy.preview(db, SPEC, SMALL)["outcome"] == "policy"
    assert {r["action_type"] for r in policy.list_all(db)} == {s.action_type for s in catalog.gated_specs()}
    out = store.submit(db, requester_id=1, title="사양", action_type=SPEC, line_id=lid, metrics=SMALL)
    assert out["status"] == "pending"


def test_old_policy_rows_without_a_rules_value_read_as_no_rules(db):
    db.t["approval_action_policies"].append(
        {"action_type": SPEC, "required": True, "default_line_id": None, "line_locked": False,
         "updated_by": 1, "updated_at": None})               # 옛 행 — rules 키가 없다
    assert policy.get_rules(db, SPEC) == []
    db.t["approval_action_policies"][0]["rules"] = "망가진 JSON"
    assert policy.get_rules(db, SPEC) == []


def test_set_policy_keeps_the_rules(db):
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", "<=", 2048)],
                                 "outcome": "auto_approve"}], actor_id=7)
    policy.set_policy(db, SPEC, actor_id=7, required=True)
    assert len(policy.get_rules(db, SPEC)) == 1
    assert policy.get(db, SPEC)["required"] is True


# ── 자동 승인 ─────────────────────────────────────────────────────────


@pytest.fixture
def auto(db):
    lid = _line(db, 3, 4)
    policy.set_policy(db, SPEC, actor_id=7, required=True, default_line_id=lid)
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", "<=", 2048)],
                                 "outcome": "auto_approve", "label": "작은 사양"}], actor_id=7)
    return lid


def test_auto_approval_runs_the_normal_apply_path(db, auto):
    calls = []
    registry.register_action(SPEC, lambda payload, req: calls.append(payload), label="사양")
    assert policy.is_required(db, SPEC, SMALL) is True, "원장을 거쳐야 자동 승인이 남는다"
    assert policy.resolve_line(db, SPEC, metrics=SMALL) == (None, None)
    out = store.submit(db, requester_id=1, title="사양 변경", action_type=SPEC,
                       payload={"changes": {"vm": {"mem_mib": 2048}}}, target_ref="wf:1",
                       metrics=SMALL)
    assert out["status"] == E.APPROVED and out["decided_at"] is not None
    assert out["steps"] == [] and out["line_id"] is None
    assert calls == [{"changes": {"vm": {"mem_mib": 2048}}}], "적용이 한 번 돌지 않았다"
    assert out["applied_at"] is not None and not out["apply_error"]
    d = out["rule_decision"]
    assert store.is_auto_approved(out)
    assert d["outcome"] == "auto_approve" and d["rule"] == 1 and d["label"] == "작은 사양"
    assert d["metrics"]["mem_mib"] == 2048
    assert "메모리 <= 2048 MiB" in d["text"]
    assert store.rule_decision(db, out["id"])["rule"] == 1


def test_auto_approval_tells_the_requester_it_was_automatic(db, auto):
    registry.register_action(SPEC, lambda p, r: None, label="사양")
    out = store.submit(db, requester_id=1, title="사양 변경", action_type=SPEC, metrics=SMALL)
    sent = [n for n in db.notifications if n["user_id"] == 1]
    assert [n["template_id"] for n in sent] == [notifier.TEMPLATE_AUTO_APPROVED]
    assert "자동 승인" in sent[0]["message"] and "#1" in sent[0]["message"]
    assert f"request={out['id']}" in sent[0]["link_url"]
    assert not any(n["user_id"] in (3, 4) for n in db.notifications), "결재자에게 차례 알림이 갔다"


def test_an_auto_approval_whose_apply_fails_is_an_apply_failure(db, auto):
    def boom(payload, req):
        raise RuntimeError("컴퓨터에 닿지 않음")

    registry.register_action(SPEC, boom, label="사양")
    out = store.submit(db, requester_id=1, title="사양 변경", action_type=SPEC, metrics=SMALL)
    assert out["status"] == E.APPROVED, "적용 실패가 승인을 물렸다"
    assert "컴퓨터에 닿지 않음" in (out["apply_error"] or "")
    assert notifier.TEMPLATE_APPLY_FAIL in [n["template_id"] for n in db.notifications]


def test_an_auto_approval_for_another_service_is_left_for_its_worker(db, auto):
    """적용을 가진 서비스가 아니면(코어에서 올렸다면) 지금처럼 소유 서비스의 워커가 가져간다."""
    registry._ACTIONS.pop(SPEC, None)
    registry.register_action(SPEC, None, label="사양")
    out = store.submit(db, requester_id=1, title="사양 변경", action_type=SPEC, metrics=SMALL)
    assert out["status"] == E.APPROVED and out["applied_at"] is None
    assert [r["id"] for r in store.claim_settlements(db, [SPEC])] == [out["id"]]


def test_a_request_that_does_not_match_still_goes_to_people(db, auto):
    registry.register_action(SPEC, lambda p, r: None, label="사양")
    big = {**SMALL, "mem_mib": 4096}
    assert policy.resolve_line(db, SPEC, metrics=big) == (auto, None)
    out = store.submit(db, requester_id=1, title="사양 변경", action_type=SPEC,
                       line_id=auto, metrics=big)
    assert out["status"] == E.PENDING and out["rule_decision"] is None


def test_auto_approval_still_refuses_a_second_pending_request_on_the_same_target(db, auto):
    registry.register_action(SPEC, lambda p, r: None, label="사양")
    store.submit(db, requester_id=1, title="큰 것", action_type=SPEC, line_id=auto,
                 target_ref="wf:1", metrics={**SMALL, "mem_mib": 9000})
    with pytest.raises(E.ApprovalError, match="이미 진행 중인 결재"):
        store.submit(db, requester_id=1, title="작은 것", action_type=SPEC,
                     target_ref="wf:1", metrics=SMALL)


# ── 결재선 고르기 · 결재 필요 ─────────────────────────────────────────


def test_a_line_rule_picks_the_line_and_locks_it(db):
    light = _line(db, 3, name="팀장")
    heavy = _line(db, 3, 5, name="팀장+인프라")
    policy.set_policy(db, SPEC, actor_id=7, required=True, default_line_id=light)
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", ">=", 8193)],
                                 "outcome": "line", "line_id": heavy}], actor_id=7)
    big = {**SMALL, "mem_mib": 16384}
    assert policy.resolve_line(db, SPEC, metrics=big) == (heavy, None)
    assert policy.resolve_line(db, SPEC, line_id=heavy, metrics=big) == (heavy, None)
    with pytest.raises(E.ApprovalError, match="조건 규칙 #1"):
        policy.resolve_line(db, SPEC, line_id=light, metrics=big)
    with pytest.raises(E.ApprovalError, match="조건 규칙 #1"):
        policy.resolve_line(db, SPEC, steps=[{"approver_id": 3, "step_order": 1}], metrics=big)
    # 작은 요청은 지금 정책 그대로(요청자가 고를 수 있다)
    assert policy.resolve_line(db, SPEC, line_id=heavy, metrics=SMALL) == (heavy, None)
    pv = policy.preview(db, SPEC, big)
    assert pv == {**pv, "outcome": "line", "required": True, "line_id": heavy,
                  "line_locked": True, "rule": 1, "auto_approve": False}
    out = store.submit(db, requester_id=1, title="큰 사양", action_type=SPEC,
                       line_id=heavy, metrics=big)
    assert [s["approver_id"] for s in out["steps"]] == [3, 5]
    assert store.rule_decision(db, out["id"])["outcome"] == "line"


def test_a_line_rule_beats_a_locked_default_line(db):
    light = _line(db, 3, name="팀장")
    heavy = _line(db, 3, 5, name="팀장+인프라")
    policy.set_policy(db, SPEC, actor_id=7, required=True, default_line_id=light, line_locked=True)
    policy.set_rules(db, SPEC, [{"conditions": [_cond("disk_gib", ">=", 100)],
                                 "outcome": "line", "line_id": heavy}], actor_id=7)
    assert policy.resolve_line(db, SPEC, metrics={"disk_gib": 200}) == (heavy, None)
    assert policy.resolve_line(db, SPEC, metrics={"disk_gib": 10}) == (light, None)


def test_a_line_rule_with_open_slots_asks_the_requester(db):
    open_line = store.create_line(db, name="임의", description="", owner_id=9, is_shared=True,
                                  steps=[{"approver_id": 3, "step_order": 1},
                                         {"approver_id": None, "step_order": 2}])["id"]
    policy.set_rules(db, NET, [{"conditions": [_cond("internal", "==", True)],
                                "outcome": "line", "line_id": open_line}], actor_id=7)
    with pytest.raises(E.ApprovalLineRequired) as err:
        policy.resolve_line(db, NET, metrics={"targets": 1, "internal": True})
    assert err.value.default_line_id == open_line


def test_a_line_rule_whose_line_vanished_refuses_instead_of_going_lighter(db):
    heavy = _line(db, 3, 5)
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", ">=", 8193)],
                                 "outcome": "line", "line_id": heavy}], actor_id=7)
    db.t["approval_line_steps"] = []
    with pytest.raises(E.ApprovalError, match="찾을 수 없습니다"):
        policy.resolve_line(db, SPEC, metrics={"mem_mib": 9000})


def test_require_turns_an_off_action_on_for_that_request(db, monkeypatch):
    # 「결재 필요」 규칙의 일반 성질 — 요청 자체가 결재인 선언을 뗀 행위로 본다(두 행위 모두 그 선언이 있다).
    import dataclasses

    monkeypatch.setitem(catalog._BY_TYPE, NET, dataclasses.replace(catalog.spec(NET), always_required=False))
    lid = _line(db, 3)
    policy.set_policy(db, NET, actor_id=7, required=False, default_line_id=lid)
    policy.set_rules(db, NET, [{"conditions": [_cond("internal", "==", True)],
                                "outcome": "require"}], actor_id=7)
    assert policy.is_required(db, NET, {"targets": 1, "internal": True}) is True
    assert policy.is_required(db, NET, {"targets": 1, "internal": False}) is False
    assert policy.is_required(db, NET) is False
    assert policy.resolve_line(db, NET, metrics={"internal": True}) == (lid, None)
    pv = policy.preview(db, NET, {"internal": True})
    assert pv["outcome"] == "require" and pv["required"] is True and pv["line_locked"] is False
    assert policy.preview(db, NET, {"internal": False})["required"] is False


def test_preview_of_an_auto_approval(db, auto):
    pv = policy.preview(db, SPEC, SMALL)
    assert pv["outcome"] == "auto_approve" and pv["auto_approve"] is True
    assert pv["required"] is False and pv["rule"] == 1 and pv["rule_label"] == "작은 사양"
    assert pv["rules_supported"] is True
    assert policy.preview(db, SPEC)["outcome"] == "policy", "값 없이 물으면 규칙은 맞지 않는다"


# ── 저장 · 검증 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("rules, match", [
    ([{"conditions": [_cond("ram", "<=", 1)], "outcome": "auto_approve"}], "보내지 않는 값"),
    ([{"conditions": [_cond("mem_mib", "<=", "2048")], "outcome": "auto_approve"}], "종류가 맞지 않습니다"),
    ([{"conditions": [_cond("mem_mib", "<=", True)], "outcome": "auto_approve"}], "종류가 맞지 않습니다"),
    ([{"conditions": [_cond("template", "<=", "a")], "outcome": "auto_approve"}], "비교를 쓸 수 없습니다"),
    ([{"conditions": [_cond("vm_use", "in", [True])], "outcome": "auto_approve"}], "비교를 쓸 수 없습니다"),
    ([{"conditions": [_cond("template", "in", [])], "outcome": "auto_approve"}], "값 목록"),
    ([{"conditions": [_cond("template", "in", ["a", 1])], "outcome": "auto_approve"}], "종류가 맞지 않습니다"),
    ([{"conditions": [_cond("mem_mib", "<", 1)], "outcome": "auto_approve"}], "알 수 없는 비교"),
    ([{"conditions": [], "outcome": "auto_approve"}], "조건을 하나 이상"),
    ([{"conditions": [_cond("mem_mib", "<=", 1)], "outcome": "allow"}], "알 수 없는 결과"),
    ([{"conditions": [_cond("mem_mib", "<=", 1)], "outcome": "line"}], "결재선을 골라"),
    ([{"conditions": [_cond("mem_mib", "<=", 1)], "outcome": "line", "line_id": 999}], "결재선을 찾을 수 없습니다"),
    ("규칙", "목록"),
])
def test_bad_rules_are_refused(db, rules, match):
    with pytest.raises(ValueError, match=match):
        policy.set_rules(db, SPEC, rules, actor_id=7)
    assert policy.get_rules(db, SPEC) == []


def test_rules_need_an_action_that_declares_values(db):
    with pytest.raises(ValueError, match="실어 보내는 값이 없어"):
        policy.set_rules(db, "agent.deploy", [{"conditions": [_cond("x", "==", 1)],
                                               "outcome": "require"}], actor_id=7)
    with pytest.raises(ValueError, match="알 수 없는 행위"):
        policy.set_rules(db, "no.such", [], actor_id=7)


def test_an_inactive_line_is_refused(db):
    lid = _line(db, 3)
    db.t["approval_lines"][0]["is_active"] = False
    with pytest.raises(ValueError, match="결재선을 찾을 수 없습니다"):
        policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", ">=", 1)],
                                     "outcome": "line", "line_id": lid}], actor_id=7)


def test_rules_are_normalized_and_written_down(db):
    lid = _line(db, 3)
    saved = policy.set_rules(db, SPEC, [
        {"conditions": [{"metric": " mem_mib ", "op": "<=", "value": 2048, "extra": 1}],
         "outcome": "auto_approve", "line_id": 5, "label": "  작은 것  "},
        {"conditions": [_cond("vcpus", ">=", 8)], "outcome": "line", "line_id": str(lid)},
    ], actor_id=7)
    assert saved == [
        {"conditions": [{"metric": "mem_mib", "op": "<=", "value": 2048}],
         "outcome": "auto_approve", "line_id": None, "label": "작은 것"},
        {"conditions": [{"metric": "vcpus", "op": ">=", "value": 8}],
         "outcome": "line", "line_id": lid},
    ]
    stored = json.loads(db.t["approval_action_policies"][0]["rules"])
    assert stored == saved
    h = [x for x in policy.history(db) if x["target"] == f"{SPEC}:rules"]
    assert len(h) == 1 and h[0]["before"] == "규칙 없음" and "#1 메모리 <= 2048 MiB → 자동 승인" in h[0]["after"]
    assert h[0]["changed_by"] == 7
    # 같은 것을 다시 저장하면 이력이 늘지 않는다, 빈 목록이면 지운다
    policy.set_rules(db, SPEC, saved, actor_id=7)
    assert len([x for x in policy.history(db) if x["target"] == f"{SPEC}:rules"]) == 1
    assert policy.set_rules(db, SPEC, [], actor_id=7) == []
    assert policy.evaluate(db, SPEC, SMALL) is None


def test_rules_do_not_touch_the_rest_of_the_policy(db):
    lid = _line(db, 3)
    policy.set_policy(db, SPEC, actor_id=7, required=True, default_line_id=lid, line_locked=True)
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", "<=", 1)],
                                 "outcome": "require"}], actor_id=7)
    got = policy.get(db, SPEC)
    assert got["required"] is True and got["default_line_id"] == lid and got["line_locked"] is True


# ── 결재선을 내릴 때 ──────────────────────────────────────────────────


def test_a_line_used_by_a_rule_is_not_dropped_quietly(db):
    heavy = _line(db, 3, 5)
    policy.set_rules(db, SPEC, [
        {"conditions": [_cond("mem_mib", ">=", 8193)], "outcome": "line", "line_id": heavy},
        {"conditions": [_cond("mem_mib", "<=", 2048)], "outcome": "auto_approve"},
    ], actor_id=7)
    assert policy.rules_using_line(db, heavy) == [SPEC]
    with pytest.raises(E.ApprovalError, match="조건 규칙"):
        store.delete_line(db, heavy, actor_id=9, is_superuser=False)
    store.delete_line(db, heavy, actor_id=9, is_superuser=False, force=True)
    rules = policy.get_rules(db, SPEC)
    assert rules[0]["outcome"] == "require" and rules[0]["line_id"] is None, \
        "줄을 잃은 규칙이 사라지면 더 가벼운 규칙이 대신 맞는다"
    assert rules[1]["outcome"] == "auto_approve"


# ── 자동 승인하지 않는 값(카탈로그 선언) ──────────────────────────────

NET_AUTO = [{"conditions": [_cond("targets", "<=", 3)], "outcome": "auto_approve", "label": "적은 대상"}]
INTERNAL = {"targets": 1, "internal": True}
PUBLIC = {"targets": 1, "internal": False}
BLOCKED = "내부망 대상이 있는 요청은 자동 승인하지 않습니다"


def test_the_catalog_declares_what_is_never_auto_approved(db):
    assert catalog.no_auto_of(NET) == [{"metric": "internal", "value": True, "label": "내부망 대상이 있는 요청"}]
    assert catalog.spec(NET).to_dict()["no_auto"] == catalog.no_auto_of(NET)
    assert catalog.no_auto_of(SPEC) == [] and catalog.no_auto_of("no.such") == []
    for sp in catalog.CATALOG:
        for n in sp.no_auto:
            assert sp.metric(n.metric), f"{sp.action_type} 의 no_auto 가 선언하지 않은 값 {n.metric} 을 본다"
    rows = {r["action_type"]: r for r in policy.list_all(db)}
    assert rows[NET]["no_auto"] == catalog.no_auto_of(NET)
    assert rows[SPEC]["no_auto"] == [] and rows["agent.deploy"]["no_auto"] == []


def test_match_rule_stays_pure_and_the_action_path_applies_the_guard():
    assert policy.match_rule(NET_AUTO, INTERNAL)["outcome"] == "auto_approve", "순수 함수는 행위를 모른다"
    hit = policy.match_for_action(NET, NET_AUTO, INTERNAL)
    assert hit["outcome"] == "require" and hit["rule"] == 1
    assert hit["auto_blocked"] is True and hit["auto_blocked_reason"] == BLOCKED
    assert policy.match_for_action(NET, NET_AUTO, PUBLIC)["outcome"] == "auto_approve"
    assert "auto_blocked" not in policy.match_for_action(NET, NET_AUTO, PUBLIC)


def test_internal_targets_skip_the_auto_rule_and_take_the_next_line_rule(db):
    light = _line(db, 3, name="팀장")
    sec = _line(db, 3, 5, name="보안")
    policy.set_policy(db, NET, actor_id=7, required=True, default_line_id=light)
    policy.set_rules(db, NET, NET_AUTO + [
        {"conditions": [_cond("internal", "==", True)], "outcome": "line", "line_id": sec},
    ], actor_id=7)
    assert policy.resolve_line(db, NET, metrics=INTERNAL) == (sec, None)
    assert policy.resolve_line(db, NET, metrics=PUBLIC) == (None, None)
    hit = policy.evaluate(db, NET, INTERNAL)
    assert hit["rule"] == 2 and hit["outcome"] == "line" and hit["auto_blocked"] is True
    with pytest.raises(E.ApprovalError, match="조건 규칙 #2"):
        policy.resolve_line(db, NET, line_id=light, metrics=INTERNAL)


def test_internal_targets_skip_the_auto_rule_and_take_the_next_require_rule(db):
    lid = _line(db, 3)
    policy.set_policy(db, NET, actor_id=7, required=False, default_line_id=lid)
    policy.set_rules(db, NET, NET_AUTO + [
        {"conditions": [_cond("targets", ">=", 1)], "outcome": "require"},
    ], actor_id=7)
    hit = policy.evaluate(db, NET, INTERNAL)
    assert hit["rule"] == 2 and hit["outcome"] == "require"
    assert policy.is_required(db, NET, INTERNAL) is True
    assert policy.resolve_line(db, NET, metrics=INTERNAL) == (lid, None)


def test_a_blocked_auto_rule_with_nothing_after_it_still_needs_people(db):
    """맞는 것이 없으면 정책대로 — 단, 정책이 꺼져 있어도 결재 없이 열리지 않는다(자동 승인보다 가벼워지지 않게)."""
    lid = _line(db, 3)
    policy.set_policy(db, NET, actor_id=7, required=False, default_line_id=lid)
    policy.set_rules(db, NET, NET_AUTO, actor_id=7)
    assert policy.is_required(db, NET, INTERNAL) is True
    assert policy.resolve_line(db, NET, metrics=INTERNAL) == (lid, None)
    hit = policy.evaluate(db, NET, INTERNAL)
    assert hit["rule"] == 1 and hit["outcome"] == "require" and hit["line_id"] is None
    assert BLOCKED in policy.describe_rule(NET, hit)
    # 켜진 정책 · 고정 결재선이면 그 줄 그대로
    policy.set_policy(db, NET, actor_id=7, required=True, default_line_id=lid, line_locked=True)
    assert policy.resolve_line(db, NET, metrics=INTERNAL) == (lid, None)


def test_a_request_that_does_not_carry_the_declared_value_is_blocked(db):
    policy.set_rules(db, NET, NET_AUTO, actor_id=7)
    for m in ({"targets": 1}, {"targets": 1, "internal": "true"}, {"targets": 1, "internal": None}):
        hit = policy.evaluate(db, NET, m)
        assert hit["outcome"] == "require" and hit["auto_blocked"] is True, m
        assert "내부망 대상 포함 값을 알 수 없는 요청" in hit["auto_blocked_reason"]
    assert policy.resolve_line(db, NET, metrics=PUBLIC) == (None, None)


def test_submit_does_not_auto_approve_an_internal_target(db):
    lid = _line(db, 3, 4)
    calls = []
    registry.register_action(NET, lambda p, r: calls.append(p), label="통신")
    policy.set_policy(db, NET, actor_id=7, required=True, default_line_id=lid)
    policy.set_rules(db, NET, NET_AUTO, actor_id=7)
    line, steps = policy.resolve_line(db, NET, metrics=INTERNAL)
    out = store.submit(db, requester_id=1, title="통신 허용", action_type=NET, payload={"add": ["10.0.0.5"]},
                       line_id=line, steps=steps, target_ref="wf:1", metrics=INTERNAL)
    assert out["status"] == E.PENDING and not store.is_auto_approved(out)
    assert [s["approver_id"] for s in out["steps"]] == [3, 4]
    assert calls == [], "사람이 보기 전에 적용됐다"
    d = store.rule_decision(db, out["id"])
    assert d["outcome"] == "require" and d["auto_blocked"] is True and BLOCKED in d["text"]
    # 부르는 쪽이 resolve_line 을 건너뛰고 line_id 없이 올려도 자동 승인되지 않는다
    with pytest.raises(E.ApprovalError):
        store.submit(db, requester_id=1, title="통신 허용", action_type=NET, target_ref="wf:2",
                     metrics=INTERNAL)
    assert calls == [] and all(r["status"] != E.APPROVED for r in db.t["approval_requests"])
    # 공인 대상만이면 지금처럼 자동 승인
    pub = store.submit(db, requester_id=1, title="통신 허용", action_type=NET, payload={"add": ["example.com"]},
                       target_ref="wf:3", metrics=PUBLIC)
    assert pub["status"] == E.APPROVED and store.is_auto_approved(pub) and calls == [{"add": ["example.com"]}]


def test_actions_without_the_declaration_auto_approve_as_before(db, auto):
    registry.register_action(SPEC, lambda p, r: None, label="사양")
    assert policy.auto_blocked_reason(SPEC, {}) == ""
    out = store.submit(db, requester_id=1, title="사양 변경", action_type=SPEC, metrics=SMALL)
    assert out["status"] == E.APPROVED
    pv = policy.preview(db, SPEC, SMALL)
    assert pv["auto_blocked"] is False and pv["auto_blocked_reason"] == ""


def test_preview_tells_why_it_is_not_automatic(db):
    lid = _line(db, 3)
    policy.set_policy(db, NET, actor_id=7, required=True, default_line_id=lid)
    policy.set_rules(db, NET, NET_AUTO, actor_id=7)
    pv = policy.preview(db, NET, INTERNAL)
    assert pv["outcome"] == "require" and pv["required"] is True and pv["auto_approve"] is False
    assert pv["auto_blocked"] is True and pv["auto_blocked_reason"] == BLOCKED
    assert pv["rule"] == 1 and pv["line_id"] == lid
    pub = policy.preview(db, NET, PUBLIC)
    assert pub["outcome"] == "auto_approve" and pub["auto_blocked"] is False and pub["auto_blocked_reason"] == ""
    # 자동 승인 규칙이 맞지 않으면 막을 것도 없다
    assert policy.preview(db, NET, {"targets": 9, "internal": True})["auto_blocked"] is False
    assert policy.preview(db, NET)["auto_blocked"] is False


def test_preview_with_unsaved_rules(db):
    lid = _line(db, 3)
    policy.set_policy(db, NET, actor_id=7, required=True, default_line_id=lid)
    assert policy.get_rules(db, NET) == []
    pv = policy.preview(db, NET, PUBLIC, rules=NET_AUTO)
    assert pv["outcome"] == "auto_approve" and pv["rule_label"] == "적은 대상"
    pv = policy.preview(db, NET, INTERNAL, rules=NET_AUTO)
    assert pv["outcome"] == "require" and pv["auto_blocked"] is True
    assert policy.preview(db, NET, PUBLIC, rules=[])["outcome"] == "policy", "빈 목록은 「규칙 없음」 으로 본다"
    assert policy.get_rules(db, NET) == [], "미리 보기가 규칙을 저장했다"
    # 저장된 규칙이 있어도 rules= 가 이긴다
    policy.set_rules(db, NET, NET_AUTO, actor_id=7)
    assert policy.preview(db, NET, PUBLIC, rules=[])["outcome"] == "policy"


# ── 결재 문서 · 목록이 규칙 기록을 싣는다(자동 승인 · 결재선 · 결재 필요) ──────────────


def test_a_stepped_request_carries_the_rule_that_picked_its_line(db):
    """규칙이 결재선을 정한 건도 문서가 「관리자 정책 규칙 #n 에 따라 이 결재선으로」 를 보이게 ``rule_decision`` 을 준다."""
    light = _line(db, 3, name="팀장")
    heavy = _line(db, 3, 5, name="팀장+인프라")
    policy.set_policy(db, SPEC, actor_id=7, required=True, default_line_id=light)
    policy.set_rules(db, SPEC, [{"conditions": [_cond("mem_mib", ">=", 8193)], "outcome": "line",
                                 "line_id": heavy, "label": "큰 사양"}], actor_id=7)
    big = {**SMALL, "mem_mib": 16384}
    out = store.submit(db, requester_id=1, title="큰 사양", action_type=SPEC, line_id=heavy, metrics=big)
    d = out["rule_decision"]
    assert d["outcome"] == "line" and d["rule"] == 1 and d["label"] == "큰 사양" and d["text"]
    assert store.get(db, out["id"])["rule_decision"] == d
    # 규칙과 상관없이 올라간 건은 None 그대로
    plain = store.submit(db, requester_id=1, title="작은 사양", action_type=SPEC, line_id=light, metrics=SMALL)
    assert plain["rule_decision"] is None


def test_inside_a_transaction_a_stepped_request_does_not_read_the_rule_column(db, monkeypatch):
    """칸이 없는 옛 DB 에서 트랜잭션 안의 get 이 실패한 질의로 트랜잭션을 망치지 않게 — 트랜잭션 안에서는 읽지 않는다."""
    from xgen_sdk.approval import sql

    lid = _line(db, 3)
    out = store.submit(db, requester_id=1, title="사양", action_type=SPEC, line_id=lid)
    db.old_schema = True
    asked = []
    real = store.rule_decision
    monkeypatch.setattr(store, "rule_decision", lambda app_db, rid: asked.append(rid) or real(app_db, rid))
    with sql.transaction(db) as tx:
        assert store.get(tx, out["id"])["rule_decision"] is None
    assert asked == []
    # 트랜잭션 밖이면 읽고, 칸이 없으면 None 으로 끝난다.
    assert store.get(db, out["id"])["rule_decision"] is None and asked == [out["id"]]


def test_lists_carry_the_rule_decision_so_auto_approvals_can_be_told(db, auto):
    registry.register_action(SPEC, lambda p, r: None, label="사양")
    done = store.submit(db, requester_id=1, title="작은 것", action_type=SPEC, metrics=SMALL)
    assert store.is_auto_approved(done)
    people = store.submit(db, requester_id=1, title="큰 것", action_type=SPEC, line_id=auto,
                          metrics={**SMALL, "mem_mib": 9000}, target_ref="wf:2")
    mine = {r["id"]: r for r in store.outbox(db, 1)}
    assert mine[done["id"]]["rule_decision"]["outcome"] == "auto_approve"
    assert mine[done["id"]]["rule_decision"]["label"] == "작은 사양"
    assert mine[people["id"]]["rule_decision"] is None
    rows = {r["id"]: r for r in store.involved(db, 1, store.DONE)}
    assert rows[done["id"]]["rule_decision"]["outcome"] == "auto_approve"
    # 칸이 없는 옛 DB — 규칙 기록 질의가 실패해도 목록은 그대로 나온다(칸을 더하지 않는다)
    db.old_schema = True
    plain = [{"id": done["id"], "title": "작은 것"}]
    assert store._attach_rule_decisions(db, plain) is plain
    assert len(store.outbox(db, 1)) == 2


def test_lists_in_a_transaction_or_empty_stay_as_they_are(db):
    from xgen_sdk.approval import sql

    assert store._attach_rule_decisions(db, []) == []
    with sql.transaction(db) as tx:
        rows = [{"id": 1}]
        assert store._attach_rule_decisions(tx, rows) == [{"id": 1}]
