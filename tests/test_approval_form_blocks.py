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


def test_a_risk_assessment_without_a_grade_is_not_filled():
    """항목 점수만 있고 등급이 없으면 읽는 사람은 결론을 모른다."""
    scored = '{"item_scores":{"a":3,"b":5}}'
    assert not blocks.is_filled(_block(1, blocks.RISK_ASSESSMENT, data=scored))
    assert blocks.is_filled(_block(1, blocks.RISK_ASSESSMENT, data='{"risk_level":"high"}'))


def test_an_unknown_type_never_blocks():
    """옛 결재가 새 코드에서 영영 멈추면 그 결재는 아무도 끝낼 수 없다."""
    assert blocks.is_filled(_block(1, "형식이_사라진_종류", data=None))


def test_broken_json_is_read_as_empty_not_as_an_error():
    """깨진 값 하나가 목록 전체를 못 읽게 만들면 안 된다."""
    assert blocks.parse_data("{이건 JSON 이 아니다") is None
    assert not blocks.is_filled(_block(1, blocks.TEXT, data="{깨짐"))


# ── 승인 게이트 ───────────────────────────────────────────────────────

def test_approval_needs_my_required_blocks_filled():
    unfilled = [_block(1, blocks.RISK_ASSESSMENT, label="AI 위험도 평가")]
    with pytest.raises(ApprovalError) as e:
        decide(_req(), _steps(), actor_id=10, action="approved", blocks=unfilled)
    assert "AI 위험도 평가" in str(e.value), "무엇을 채워야 하는지 이름으로 말해야 한다"


def test_approval_passes_once_they_are_filled():
    filled = [_block(1, blocks.RISK_ASSESSMENT, data='{"risk_level":"medium"}')]
    req, steps = decide(_req(), _steps(), actor_id=10, action="approved", blocks=filled)
    assert steps[0]["status"] == "approved"


def test_rejection_is_never_blocked_by_blocks():
    unfilled = [_block(1, blocks.RISK_ASSESSMENT, label="AI 위험도 평가")]
    req, steps = decide(_req(), _steps(), actor_id=10, action="rejected", blocks=unfilled)
    assert steps[0]["status"] == "rejected"
    assert req["status"] == "rejected"


def test_other_steps_blocks_are_not_my_responsibility():
    """2차가 채울 칸이 비었다고 1차가 승인하지 못하면 결재가 시작되지 않는다."""
    others = [_block(2, blocks.RISK_ASSESSMENT, label="AI 위험도 평가")]
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
