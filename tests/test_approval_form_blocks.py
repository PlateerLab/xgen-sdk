"""결재 양식 — 단계마다 채워야 할 칸.

무엇을 푸는가
-------------
결재는 원래 **결재선 + 사유** 뿐이었다. 그런데 실제 절차는 단계마다 하는 일이
다르다 — 기안자는 기획서를 붙이고, 2차는 그것을 보고 위험도를 평가하고, 최종은
보고 결정한다. 그 절차가 결재 밖에 따로 살아 있으면 사람은 두 곳을 오가고,
어느 쪽이 진짜인지 아무도 말할 수 없다.

여기서 고정하는 규칙 셋:
  · 양식이 없으면 예전 그대로다 — [기본 결재].
  · 승인은 **내 단계의 필수 칸**을 다 채워야 된다. 남의 단계 칸은 내 책임이 아니다.
  · **거절은 막지 않는다.** 거절에 서류를 요구하면 "이 건은 못 받는다" 는 말을
    하려고 먼저 그 건의 서류를 다 만들어야 한다 — 아무도 거절하지 못한다.
"""

import pytest

from xgen_sdk.approval import blocks
from xgen_sdk.approval.engine import ApprovalError, decide


def _req():
    return {"id": 1, "status": "pending", "current_step_order": 1}


def _steps():
    return [
        {"step_order": 1, "approver_id": 10, "status": "pending"},
        {"step_order": 2, "approver_id": 20, "status": "waiting"},
    ]


def _block(step, btype, *, required=True, data=None, label=None):
    return {"step_order": step, "block_type": btype, "required": required,
            "data": data, "label": label or btype}


# ── 채움 판정은 종류가 안다 ───────────────────────────────────────────

def test_each_type_decides_what_filled_means():
    assert not blocks.is_filled(_block(1, blocks.TEXT, data='{"text":"  "}'))
    assert blocks.is_filled(_block(1, blocks.TEXT, data='{"text":"검토함"}'))
    assert not blocks.is_filled(_block(0, blocks.ATTACHMENTS, data='{"attachment_ids":[]}'))
    assert blocks.is_filled(_block(0, blocks.ATTACHMENTS, data='{"attachment_ids":[7]}'))
    assert not blocks.is_filled(_block(0, blocks.AGENT_DEV_PLAN, data='{}'))
    assert blocks.is_filled(_block(0, blocks.AGENT_DEV_PLAN, data='{"plan_id":3}'))


#: 선택 항목(``options``)이 생기기 전의 AI 위험도 평가 값 그대로의 **다 채운** 평가.
FULL_ASSESSMENT = {
    "risk_level": "medium",
    "rationale": "고객 데이터를 읽지만 쓰지 않는다",
    "impact_scope": "EMPLOYEES",
    "item_scores": {
        "categories": [
            {"name": "합법성", "weight": "50",
             "items": [{"id": "legal-1", "name": "법 위반", "score": 3, "risk_mitigation": 2},
                       {"id": "legal-2", "name": "AI기본법", "score": 0, "risk_mitigation": 1}]},
        ],
        "risk_traits": ["PERSONAL_DATA"],
    },
}


def _with(**changes):
    import copy
    data = copy.deepcopy(FULL_ASSESSMENT)
    data.update(changes)
    return data


def test_an_evaluation_is_filled_only_when_every_part_is_there():
    """등급 하나만 있으면 채운 것으로 보던 때는 "등급만 고른 평가" 가 원장에
    섞였다. 항목 점수·판단 근거·등급을 모두 채워야 한다."""
    assert blocks.is_filled(_block(1, blocks.EVALUATION, data=FULL_ASSESSMENT))
    # 점수 0 은 **매긴 점수**다 — 빈 것과 다르다.
    assert blocks.evaluation_gaps(FULL_ASSESSMENT) == []

    assert not blocks.is_filled(_block(1, blocks.EVALUATION, data='{"risk_level":"high"}'))
    # options 가 없는 값은 옛 AI 위험도 평가라 영향 범위까지 요구한다.
    assert blocks.evaluation_gaps({"risk_level": "high"}) == ["항목 점수", "판단 근거", "영향 범위"]


def test_each_missing_part_is_named():
    """무엇이 남았는지 **이름으로** 말해야 결재자가 채운다."""
    unscored = _with()
    unscored["item_scores"]["categories"][0]["items"][1]["score"] = None
    assert blocks.evaluation_gaps(unscored) == ["항목 점수 1개"]
    assert blocks.evaluation_gaps(_with(impact_scope="")) == ["영향 범위"]
    assert blocks.evaluation_gaps(_with(rationale="   ")) == ["판단 근거"]
    assert blocks.evaluation_gaps(_with(risk_level="")) == ["등급"]
    assert blocks.evaluation_gaps("평가가 아닌 값") == ["평가"]


def test_gaps_come_in_a_fixed_order():
    """화면이 같은 순서로 보여 줄 수 있어야 한다: 항목 점수 · 판단 근거 · 등급 · 영향 범위."""
    assert blocks.evaluation_gaps({}) == ["항목 점수", "판단 근거", "등급", "영향 범위"]
    off = {"options": {"impact_scope": {"enabled": False}}}
    assert blocks.evaluation_gaps(off) == ["항목 점수", "판단 근거", "등급"]


def test_a_score_must_be_a_number_not_a_flag():
    """``True`` 는 파이썬에서 1 이지만 점수가 아니다."""
    flagged = _with()
    flagged["item_scores"]["categories"][0]["items"][0]["score"] = True
    assert blocks.evaluation_gaps(flagged) == ["항목 점수 1개"]


# ── 평가 양식의 선택 항목 ─────────────────────────────────────────────

FULL_OPTIONS = {
    "mitigation": True,
    "impact_scope": {"enabled": True, "choices": [{"code": "EMPLOYEES", "label": "임직원"}]},
    "traits": {"enabled": True, "choices": [{"code": "PERSONAL_DATA", "label": "개인정보 처리"}]},
    "attachments": True,
    "agent_risk_grade": True,
}


def test_impact_scope_is_required_when_the_form_turns_it_on():
    on = _with(options=FULL_OPTIONS, impact_scope="")
    assert blocks.evaluation_gaps(on) == ["영향 범위"]
    assert blocks.evaluation_gaps(_with(options=FULL_OPTIONS)) == []


def test_impact_scope_is_not_required_when_the_form_turns_it_off():
    """영향 범위를 끈 평가 양식에서 영향 범위를 요구하면 아무도 채울 수 없다."""
    import copy
    options = copy.deepcopy(FULL_OPTIONS)
    options["impact_scope"]["enabled"] = False
    off = _with(options=options, impact_scope="")
    assert blocks.evaluation_gaps(off) == []
    assert blocks.is_filled(_block(1, blocks.EVALUATION, data=off))


def test_a_legacy_value_without_options_still_requires_impact_scope():
    """선택 항목이 생기기 전의 값은 AI 위험도 평가(전부 켜짐) 한 벌뿐이었다."""
    legacy = _with(impact_scope="")
    assert "options" not in legacy
    assert blocks.evaluation_gaps(legacy) == ["영향 범위"]
    assert not blocks.is_filled(_block(1, blocks.EVALUATION, data=legacy))


def test_a_missing_option_key_follows_the_full_default_set():
    """options 는 있는데 impact_scope 가 빠졌으면 기본 한 벌(켜짐)을 따른다."""
    assert blocks.evaluation_gaps(_with(options={"mitigation": False}, impact_scope="")) == ["영향 범위"]


def test_traits_and_attachments_are_never_required():
    """특성 체크와 평가 첨부는 해당 없음이 정상이다 — 켜져 있어도 비워 둘 수 있다."""
    data = _with(options=FULL_OPTIONS)
    data["item_scores"]["risk_traits"] = []
    data.pop("attachment_ids", None)
    assert blocks.evaluation_gaps(data) == []


# ── 옛 종류 이름 ──────────────────────────────────────────────────────

def test_the_old_type_name_is_read_as_evaluation():
    assert blocks.LEGACY_BLOCK_TYPES == {"risk_assessment": blocks.EVALUATION}
    assert blocks.normalize_block_type("risk_assessment") == "evaluation"
    assert blocks.normalize_block_type(blocks.EVALUATION) == blocks.EVALUATION
    assert blocks.normalize_block_type("형식이_사라진_종류") == "형식이_사라진_종류"
    assert blocks.normalize_block_type(None) == ""

    assert blocks.is_known("risk_assessment")
    assert blocks.spec("risk_assessment") is blocks.spec(blocks.EVALUATION)
    assert blocks.is_filled(_block(1, "risk_assessment", data=FULL_ASSESSMENT))
    assert not blocks.is_filled(_block(1, "risk_assessment", data='{"risk_level":"high"}'))


def test_the_old_names_are_gone_from_the_public_surface():
    assert not hasattr(blocks, "RISK_ASSESSMENT")
    assert not hasattr(blocks, "assessment_gaps")


def test_the_catalog_lists_evaluation_only():
    """옛 이름은 새로 고를 이름이 아니다 — [칸 추가] 목록에 나오지 않는다."""
    entries = {c["block_type"]: c for c in blocks.catalog()}
    assert "risk_assessment" not in entries
    ev = entries[blocks.EVALUATION]
    assert ev["label"] == "평가지"
    assert ev["description"] == "평가 양식으로 항목마다 점수를 매기는 칸입니다. 등급은 점수로 정해집니다."
    assert ev["default_config"] == {"template_id": None}
    assert ev["actor"] == "approver"


def test_an_applier_is_found_under_either_name():
    def fn(app_db, req, block, data):
        return None

    try:
        blocks.register_applier(blocks.EVALUATION, fn)
        assert blocks.applier("risk_assessment") is fn
        assert blocks.has_applier("risk_assessment")
        blocks._APPLIERS.clear()

        # 옛 이름으로 꽂아도 새 이름 자리에 앉는다.
        blocks.register_applier("risk_assessment", fn)
        assert list(blocks._APPLIERS) == [blocks.EVALUATION]
        assert blocks.applier(blocks.EVALUATION) is fn
    finally:
        blocks._APPLIERS.pop(blocks.EVALUATION, None)


def test_an_unknown_type_never_blocks():
    """옛 결재가 새 코드에서 영영 멈추면 그 결재는 아무도 끝낼 수 없다."""
    assert blocks.is_filled(_block(1, "형식이_사라진_종류", data=None))


def test_broken_json_is_read_as_empty_not_as_an_error():
    """깨진 값 하나가 목록 전체를 못 읽게 만들면 안 된다."""
    assert blocks.parse_data("{이건 JSON 이 아니다") is None
    assert not blocks.is_filled(_block(1, blocks.TEXT, data="{깨짐"))


# ── 승인 게이트 ───────────────────────────────────────────────────────

def test_approval_needs_my_required_blocks_filled():
    unfilled = [_block(1, blocks.EVALUATION, label="AI 위험도 평가")]
    with pytest.raises(ApprovalError) as e:
        decide(_req(), _steps(), actor_id=10, action="approved", blocks=unfilled)
    assert "AI 위험도 평가" in str(e.value), "무엇을 채워야 하는지 이름으로 말해야 한다"


def test_approval_passes_once_they_are_filled():
    filled = [_block(1, blocks.EVALUATION, data=FULL_ASSESSMENT)]
    req, steps = decide(_req(), _steps(), actor_id=10, action="approved", blocks=filled)
    assert steps[0]["status"] == "approved"


def test_rejection_is_never_blocked_by_blocks():
    unfilled = [_block(1, blocks.EVALUATION, label="AI 위험도 평가")]
    req, steps = decide(_req(), _steps(), actor_id=10, action="rejected", blocks=unfilled)
    assert steps[0]["status"] == "rejected"
    assert req["status"] == "rejected"


def test_other_steps_blocks_are_not_my_responsibility():
    """2차가 채울 칸이 비었다고 1차가 승인하지 못하면 결재가 시작되지 않는다."""
    others = [_block(2, blocks.EVALUATION, label="AI 위험도 평가")]
    req, steps = decide(_req(), _steps(), actor_id=10, action="approved", blocks=others)
    assert steps[0]["status"] == "approved"


def test_optional_blocks_do_not_block():
    optional = [_block(1, blocks.ATTACHMENTS, required=False, label="기타 첨부")]
    req, steps = decide(_req(), _steps(), actor_id=10, action="approved", blocks=optional)
    assert steps[0]["status"] == "approved"


def test_no_form_means_the_old_behaviour():
    """양식이 없으면 지금까지와 똑같이 동작한다."""
    req, steps = decide(_req(), _steps(), actor_id=10, action="approved")
    assert steps[0]["status"] == "approved"
