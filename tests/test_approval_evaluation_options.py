"""평가지 선택 항목은 **서버가 정한다** (2.3.0).

평가지 값의 ``options`` 를 보낸 대로 믿으면 결재자가 API 로 필수 영향 범위를
끄거나(``impact_scope=false``) 위험 등급 원장에서 빠질 수 있다
(``agent_risk_grade=false``). 상신과 칸 채우기는 모두 ``store`` 를 지나므로 거기서
양식 버전의 선택 항목으로 덮어쓴다.

패리티 벡터 표는 core ``evaluation_forms`` · 프론트 ``api-client`` 정규화와 **같은 표**다.
"""
from __future__ import annotations

import copy
import json
import logging

import pytest

from xgen_sdk.approval import blocks, engine as E, evaluation, registry, store, templates
from xgen_sdk.approval.evaluation import FULL_OPTIONS, normalize_options, resolve_options
from tests.test_approval_store import FakeDB


# ── 패리티 벡터 (SDK · 프론트 공유) ──────────────────────────────────

MISSING = object()

#: (raw 값, flag 기대값, choice.enabled 기대값)
PARITY_VECTORS = [
    (MISSING, True, True),
    (True, True, True),
    (False, False, False),
    (None, False, False),
    (0, False, False),
    (1, True, True),
    ("", False, False),
    ("x", True, True),
    ([], False, False),
    ([1], True, True),
    ({}, True, True),
    ({"enabled": False}, False, False),
    ({"enabled": None}, False, False),
    ({"enabled": "x"}, True, True),
]

#: options 자체가 dict 가 아니면 전부 켠 한 벌.
NON_DICT_OPTIONS = [None, [], "x"]


def _raw_options(key, raw):
    return {} if raw is MISSING else {key: raw}


@pytest.mark.parametrize("raw, flag, choice", PARITY_VECTORS)
def test_parity_vector_normalize_options(raw, flag, choice):
    flag_out = normalize_options({"options": _raw_options("mitigation", raw)})
    choice_out = normalize_options({"options": _raw_options("impact_scope", raw)})
    assert flag_out["mitigation"] is flag
    assert choice_out["impact_scope"]["enabled"] is choice
    assert choice_out["impact_scope"]["choices"] == evaluation.IMPACT_SCOPE_CHOICES


@pytest.mark.parametrize("raw, flag, choice", PARITY_VECTORS)
def test_parity_vector_option_on_matches_normalize_options(raw, flag, choice):
    """채움 판정(``blocks._option_on``)과 정본이 **같은 답**이어야 화면과 서버가 어긋나지 않는다."""
    for key in evaluation.FLAG_OPTIONS:
        options = _raw_options(key, raw)
        assert blocks._option_on(options, key) is normalize_options({"options": options})[key] is flag
    for key in evaluation.CHOICE_OPTIONS:
        options = _raw_options(key, raw)
        normalized = normalize_options({"options": options})[key]["enabled"]
        assert blocks._option_on(options, key) is normalized is choice


@pytest.mark.parametrize("options", NON_DICT_OPTIONS)
def test_parity_non_dict_options_are_full(options):
    assert normalize_options({"options": options}) == FULL_OPTIONS
    for key in evaluation.FLAG_OPTIONS + evaluation.CHOICE_OPTIONS:
        assert blocks._option_on(options, key) is True


@pytest.mark.parametrize("policy_data", [None, [], "x", {}, {"categories": []}])
def test_policy_data_without_options_is_full(policy_data):
    out = normalize_options(policy_data)
    assert out == FULL_OPTIONS
    out["impact_scope"]["choices"].append({"code": "X", "label": "X"})
    assert len(FULL_OPTIONS["impact_scope"]["choices"]) == 4, "돌려준 값을 고쳐도 정본은 그대로"


def test_choices_are_cleaned_like_core():
    out = normalize_options({"options": {"traits": {"choices": [
        {"code": " A ", "label": ""}, {"code": "A", "label": "중복"}, {"code": ""}, "x",
        {"code": "B", "label": " 비 "},
    ]}, "impact_scope": {"enabled": True, "choices": []}}})
    assert out["traits"] == {"enabled": True, "choices": [{"code": "A", "label": "A"},
                                                         {"code": "B", "label": "비"}]}
    assert out["impact_scope"]["choices"] == evaluation.IMPACT_SCOPE_CHOICES


# ── 평가 양식 표를 가진 원장 ──────────────────────────────────────────

LITE_OPTIONS = {"mitigation": False, "impact_scope": False, "traits": False,
                "attachments": False, "agent_risk_grade": False}


class EvalDB(FakeDB):
    """core 의 평가 양식 표(``risk_assessment_templates`` / ``risk_assessment_policies``)까지 아는 원장."""

    def __init__(self):
        super().__init__()
        self.templates = []
        self.policies = []
        self.broken = False
        self.reads = []

    def add_template(self, template_id, *, is_default=False):
        self.templates.append({"id": len(self.templates) + 1, "template_id": template_id,
                               "is_default": is_default})

    def add_policy(self, template_id, version, options=None, *, is_active=True, raw=None):
        pid = len(self.policies) + 1
        data = {"categories": []}
        if options is not None:
            data["options"] = options
        self.policies.append({"id": pid, "template_id": template_id, "version": version,
                              "policy_data": raw if raw is not None else json.dumps(data),
                              "is_active": is_active})
        return pid

    def _run(self, s, p):
        if "risk_assessment_" in s:
            self.reads.append(s)
            if self.broken:
                raise RuntimeError('relation "risk_assessment_templates" does not exist')
            if s == "SELECT template_id, is_default FROM risk_assessment_templates ORDER BY id":
                return [dict(t) for t in sorted(self.templates, key=lambda t: t["id"])]
            head = "SELECT id, template_id, version, policy_data, is_active FROM risk_assessment_policies"
            if s == head + " WHERE id = %s":
                return [dict(r) for r in self.policies if r["id"] == p[0]]
            if s == head + " WHERE template_id = %s":
                return [dict(r) for r in self.policies if r["template_id"] == p[0]]
            raise AssertionError(f"모르는 질의: {s}")
        return super()._run(s, p)


@pytest.fixture
def db():
    d = EvalDB()
    for uid, name in ((1, "기안자"), (3, "팀장"), (4, "부장")):
        d.add_user(uid, name, name)
    d.add_template("ai-risk", is_default=True)
    d.add_template("lite")
    d.full_v1 = d.add_policy("ai-risk", 1, FULL_OPTIONS)
    d.lite_v1 = d.add_policy("lite", 1, LITE_OPTIONS)
    return d


def _assessed(**extra):
    value = {
        "risk_level": "medium", "rationale": "근거", "impact_scope": "EMPLOYEES",
        "item_scores": {"categories": [{"name": "합법성", "weight": "100",
                                        "items": [{"id": "a", "name": "법", "score": 3,
                                                   "risk_mitigation": 1}]}],
                        "risk_traits": []},
    }
    value.update(extra)
    return value


def _deploy_request(db):
    """[AI Agent 배포 결재] — 2차에 평가지 칸(설정 ``template_id: ai-risk``)."""
    t = templates.by_name("AI Agent 배포 결재")
    form_id = store.create_form(db, name=t["name"], description=t["description"], owner_id=None,
                                steps=t["steps"], blocks=t["blocks"], is_builtin=True)
    line_id = store.create_line(db, name="배포 결재선", description="", owner_id=1, is_shared=True,
                                steps=[{"approver_id": 3, "step_order": 1},
                                       {"approver_id": 4, "step_order": 2}])["id"]
    store.set_line_form(db, line_id, form_id)
    rid = store.submit(db, requester_id=1, title="배포", line_id=line_id,
                       block_values=[{"sort_order": 1, "data": {"plan_id": 5}}])["id"]
    block = next(b for b in store.get(db, rid)["blocks"] if b["block_type"] == blocks.EVALUATION)
    return rid, block


def _stored(db, block_id):
    row = next(r for r in db.t["approval_request_blocks"] if r["id"] == block_id)
    return blocks.parse_data(row["data"])


def _set_block_config(db, block_id, config):
    row = next(r for r in db.t["approval_request_blocks"] if r["id"] == block_id)
    row["config"] = json.dumps(config)


# ── 칸 채우기 ────────────────────────────────────────────────────────

def test_forged_options_cannot_skip_the_required_impact_scope(db):
    """버전이 영향 범위를 켰으면, 끈 척 보내도 저장은 켜진 것으로 되고 승인은 막힌다."""
    registry.register_action("generic", lambda *a, **k: None)
    rid, block = _deploy_request(db)
    forged = _assessed(impact_scope="", policy_id=db.full_v1,
                       options={"impact_scope": False, "agent_risk_grade": False})
    store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3, data=forged)

    stored = _stored(db, block["id"])
    assert stored["options"] == FULL_OPTIONS
    assert blocks.evaluation_gaps(stored) == ["영향 범위"]
    with pytest.raises(E.ApprovalError):
        store.decide(db, rid, actor_id=3, action="approved")

    store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3,
                     data=_assessed(policy_id=db.full_v1, options={"impact_scope": False}))
    store.decide(db, rid, actor_id=3, action="approved")


def test_agent_risk_grade_is_forced_by_the_active_version_without_policy_id(db):
    """``policy_id`` 없이 ``agent_risk_grade=false`` 를 보내도 AI 위험도 평가지면 원장에 남는다."""
    rid, block = _deploy_request(db)
    store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3,
                     data=_assessed(options={"agent_risk_grade": False, "mitigation": False}))
    stored = _stored(db, block["id"])
    assert stored["options"]["agent_risk_grade"] is True
    assert stored["options"] == FULL_OPTIONS


def test_client_options_are_added_when_missing(db):
    rid, block = _deploy_request(db)
    store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3, data=_assessed())
    assert _stored(db, block["id"]) == _assessed(options=FULL_OPTIONS)


def test_a_version_that_turns_options_off_is_honoured(db):
    """칸이 가벼운 양식을 쓰면 서버도 그 양식의 선택 항목을 쓴다 — 보낸 값이 켜도 꺼진다."""
    registry.register_action("generic", lambda *a, **k: None)
    rid, block = _deploy_request(db)
    _set_block_config(db, block["id"], {"template_id": "lite"})
    store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3,
                     data=_assessed(impact_scope="", policy_id=str(db.lite_v1), options=FULL_OPTIONS))
    stored = _stored(db, block["id"])
    assert stored["options"]["agent_risk_grade"] is False
    assert stored["options"]["impact_scope"] == {"enabled": False,
                                                 "choices": evaluation.IMPACT_SCOPE_CHOICES}
    assert blocks.evaluation_gaps(stored) == []
    store.decide(db, rid, actor_id=3, action="approved")


def test_a_version_of_another_form_is_refused(db):
    rid, block = _deploy_request(db)
    with pytest.raises(E.EvaluationVersionMismatch) as e:
        store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3,
                         data=_assessed(policy_id=db.lite_v1))
    assert isinstance(e.value, E.ApprovalError)
    assert str(e.value) == "이 칸에 지정된 평가 양식이 아니니 평가지를 다시 열어 평가해 주세요."
    assert e.value.code == "EVALUATION_VERSION_MISMATCH"
    assert _stored(db, block["id"]) is None, "거절된 값은 저장하지 않는다"


@pytest.mark.parametrize("policy_id", [999, "999", "abc", "1.0", -1, True])
def test_a_missing_version_is_refused(db, policy_id):
    rid, block = _deploy_request(db)
    with pytest.raises(E.EvaluationVersionNotFound) as e:
        store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3,
                         data=_assessed(policy_id=policy_id))
    assert isinstance(e.value, E.ApprovalError)
    assert str(e.value) == "평가한 양식 버전을 찾을 수 없으니 평가지를 다시 열어 평가해 주세요."
    assert e.value.code == "EVALUATION_VERSION_NOT_FOUND"
    assert _stored(db, block["id"]) is None


def test_a_read_failure_falls_back_to_full_and_warns(db, caplog):
    """표가 없거나 DB 가 넘어지면 가장 엄격한 한 벌 — 결재는 막지 않고 건너뛰게도 두지 않는다."""
    rid, block = _deploy_request(db)
    db.broken = True
    with caplog.at_level(logging.WARNING, logger="approval-evaluation"):
        store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3,
                         data=_assessed(policy_id=999, options=LITE_OPTIONS))
    assert _stored(db, block["id"])["options"] == FULL_OPTIONS
    assert any("전부 켠 한 벌" in r.getMessage() for r in caplog.records)


def test_empty_values_are_left_alone(db):
    rid, block = _deploy_request(db)
    for empty in (None, {}):
        store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3, data=empty)
        assert _stored(db, block["id"]) == empty, "빈 값에 선택 항목을 덧붙이지 않는다"
    assert db.reads == [], "빈 값에는 양식을 읽을 일이 없다"


def test_other_block_types_are_untouched(db):
    rid, _ = _deploy_request(db)
    plan = next(b for b in store.get(db, rid)["blocks"] if b["block_type"] == blocks.AGENT_DEV_PLAN)
    store.fill_block(db, request_id=rid, block_id=plan["id"], actor_id=1,
                     data={"plan_id": 7, "options": {"x": 1}})
    assert _stored(db, plan["id"]) == {"plan_id": 7, "options": {"x": 1}}


def test_the_legacy_block_name_is_covered(db):
    """옛 이름(``risk_assessment``)이 남은 진행 중 결재도 같은 규칙을 탄다."""
    rid, block = _deploy_request(db)
    row = next(r for r in db.t["approval_request_blocks"] if r["id"] == block["id"])
    row["block_type"] = "risk_assessment"
    store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3,
                     data=_assessed(options={"agent_risk_grade": False}))
    assert _stored(db, block["id"])["options"] == FULL_OPTIONS
    with pytest.raises(E.EvaluationVersionMismatch):
        store.fill_block(db, request_id=rid, block_id=block["id"], actor_id=3,
                         data=_assessed(policy_id=db.lite_v1))


# ── 상신과 함께 온 기안 칸 ────────────────────────────────────────────

def _draft_evaluation_line(db, config):
    form_id = store.create_form(db, name="기안 평가", owner_id=1, steps=[
        {"step_index": 0, "title": "기안"}, {"step_index": 1, "title": "검토"},
    ], blocks=[
        {"step_index": 0, "block_type": blocks.EVALUATION, "label": "평가", "required": True,
         "config": config},
    ])
    line_id = store.create_line(db, name="한 명", description="", owner_id=1, is_shared=True,
                                steps=[{"approver_id": 3, "step_order": 1}])["id"]
    store.set_line_form(db, line_id, form_id)
    return line_id


def test_submit_overwrites_forged_draft_options(db):
    line_id = _draft_evaluation_line(db, {"template_id": "ai-risk"})
    req = store.submit(db, requester_id=1, title="평가", line_id=line_id, block_values=[
        {"sort_order": 1, "data": _assessed(impact_scope="", options={"impact_scope": False})}])
    block = next(b for b in req["blocks"] if b["block_type"] == blocks.EVALUATION)
    stored = _stored(db, block["id"])
    assert stored["options"] == FULL_OPTIONS
    assert blocks.evaluation_gaps(stored) == ["영향 범위"]


def test_submit_refuses_a_version_of_another_form(db):
    line_id = _draft_evaluation_line(db, {"template_id": "ai-risk"})
    with pytest.raises(E.EvaluationVersionMismatch):
        store.submit(db, requester_id=1, title="평가", line_id=line_id, block_values=[
            {"sort_order": 1, "data": _assessed(policy_id=db.lite_v1)}])


def test_submit_with_an_empty_draft_value_is_left_alone(db):
    line_id = _draft_evaluation_line(db, {"template_id": "lite"})
    req = store.submit(db, requester_id=1, title="평가", line_id=line_id,
                       block_values=[{"sort_order": 1, "data": None}])
    block = next(b for b in req["blocks"] if b["block_type"] == blocks.EVALUATION)
    assert _stored(db, block["id"]) is None


# ── 칸의 양식 고르기 · 활성 버전 ─────────────────────────────────────

def _block(config=None):
    return {"block_type": blocks.EVALUATION, "config": config}


def test_unknown_template_in_config_uses_the_default_template(db):
    db.templates[0]["is_default"] = False
    db.templates[1]["is_default"] = True
    assert resolve_options(db, _block({"template_id": "gone"}), {"risk_level": "low"}) == \
        normalize_options({"options": LITE_OPTIONS})
    assert resolve_options(db, _block('{"template_id": "lite"}'), {"policy_id": db.lite_v1}) == \
        normalize_options({"options": LITE_OPTIONS})
    with pytest.raises(E.EvaluationVersionMismatch):
        resolve_options(db, _block(None), {"policy_id": db.full_v1})


def test_no_default_template_falls_back_to_the_builtin(db):
    db.templates[0]["is_default"] = False
    assert resolve_options(db, _block({"template_id": ""}), {"policy_id": db.full_v1}) == FULL_OPTIONS
    with pytest.raises(E.EvaluationVersionMismatch):
        resolve_options(db, _block({}), {"policy_id": db.lite_v1})


def test_the_highest_active_version_wins_and_no_active_version_is_full(db):
    db.add_policy("lite", 2, {"agent_risk_grade": True, "impact_scope": False}, is_active=True)
    db.add_policy("lite", 3, {"agent_risk_grade": True}, is_active=False)
    out = resolve_options(db, _block({"template_id": "lite"}), {"risk_level": "low"})
    assert out["agent_risk_grade"] is True and out["impact_scope"]["enabled"] is False

    for r in db.policies:
        r["is_active"] = False
    assert resolve_options(db, _block({"template_id": "lite"}), {"risk_level": "low"}) == FULL_OPTIONS


def test_an_inactive_but_existing_version_is_still_accepted(db):
    """평가를 연 뒤 새 버전이 활성화돼도 그 버전으로 채점한 값은 그 버전의 선택 항목을 쓴다."""
    old = db.policies[db.lite_v1 - 1]
    old["is_active"] = False
    db.add_policy("lite", 2, FULL_OPTIONS)
    assert resolve_options(db, _block({"template_id": "lite"}), {"policy_id": db.lite_v1}) == \
        normalize_options({"options": LITE_OPTIONS})


@pytest.mark.parametrize("policy_id", [None, "", "  "])
def test_blank_policy_id_means_the_active_version(db, policy_id):
    out = resolve_options(db, _block({"template_id": "lite"}), {"policy_id": policy_id})
    assert out == normalize_options({"options": LITE_OPTIONS})


def test_a_version_row_without_template_id_belongs_to_the_legacy_form(db):
    db.add_template("default")
    legacy = db.add_policy(None, 1, LITE_OPTIONS)
    assert resolve_options(db, _block({"template_id": "default"}), {"policy_id": legacy}) == \
        normalize_options({"options": LITE_OPTIONS})
    with pytest.raises(E.EvaluationVersionMismatch):
        resolve_options(db, _block({"template_id": "ai-risk"}), {"policy_id": legacy})


@pytest.mark.parametrize("raw", ["", "{broken", "[]", json.dumps({"options": None})])
def test_unreadable_policy_data_is_full(db, raw):
    pid = db.add_policy("lite", 9, raw=raw)
    assert resolve_options(db, _block({"template_id": "lite"}), {"policy_id": pid}) == FULL_OPTIONS


def test_the_result_is_a_fresh_copy(db):
    out = resolve_options(db, _block({"template_id": "ai-risk"}), {"risk_level": "low"})
    out["impact_scope"]["choices"].clear()
    db.broken = True
    again = resolve_options(db, _block({"template_id": "ai-risk"}), {"risk_level": "low"})
    again["traits"]["choices"].clear()
    assert FULL_OPTIONS == copy.deepcopy(evaluation.FULL_OPTIONS)
    assert len(evaluation.FULL_OPTIONS["impact_scope"]["choices"]) == 4
    assert len(evaluation.FULL_OPTIONS["traits"]["choices"]) == 2


def test_public_names_are_exported():
    from xgen_sdk import approval
    assert approval.evaluation is evaluation
    assert approval.EvaluationVersionNotFound is E.EvaluationVersionNotFound
    assert approval.EvaluationVersionMismatch is E.EvaluationVersionMismatch
    assert evaluation.EvaluationVersionNotFound is E.EvaluationVersionNotFound
    for name in ("IMPACT_SCOPE_CHOICES", "TRAIT_CHOICES", "FULL_OPTIONS", "FLAG_OPTIONS",
                 "CHOICE_OPTIONS", "normalize_options", "resolve_options"):
        assert hasattr(evaluation, name), name
