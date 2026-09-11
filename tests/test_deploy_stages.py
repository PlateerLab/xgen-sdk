"""배포 상태기계 — **결재 하나로 간다.**

예전에는 관리자 승인 + 거버넌스 심사 2단계였고, 누가 승인할 수 있는가는
역할이 정했다(사실상 그 역할을 가진 아무나 한 명). 지금 그 자리에는 결재가
있고, 두 사람의 승인이 필요하면 결재선에 두 명을 세운다.

여기서 지키는 것:
  1. 결재가 필요 없으면(기본) [배포] 는 곧바로 열린다.
  2. 필요하면 결재 중 상태로 가고, **결재 결과만이** 그것을 연다.
  3. 관리자는 닫는 방향으로만 개입할 수 있다(철회). 여는 길은 결재뿐이다.
  4. 옛 stage 이름을 읽을 수 있다 — 모르는 값은 **닫힌 쪽**으로 읽는다.
"""
from __future__ import annotations

import pytest

from xgen_sdk import deploy_state_machine as M


# ── 1. 기본은 막지 않는다 ─────────────────────────────────────────────


def test_deploying_without_a_gate_opens_it_right_away():
    assert M.next_stage_on_user_deploy(M.STAGE_IDLE, approval_required=False) == M.STAGE_DEPLOYED


def test_deploying_with_a_gate_waits_for_the_approval():
    assert (M.next_stage_on_user_deploy(M.STAGE_IDLE, approval_required=True)
            == M.STAGE_PENDING_APPROVAL)


def test_redeploying_something_already_deployed_changes_nothing():
    assert M.next_stage_on_user_deploy(M.STAGE_DEPLOYED, approval_required=True) == M.STAGE_DEPLOYED


def test_a_rejected_one_can_be_sent_up_again():
    """고쳐서 다시 올릴 수 없으면 거절은 영구 사망이다."""
    assert (M.next_stage_on_user_deploy(M.STAGE_REJECTED, approval_required=True)
            == M.STAGE_PENDING_APPROVAL)


def test_the_old_mode_string_cannot_sneak_in_as_the_gate_flag():
    """옛 시그니처는 두 번째 인자가 ``deployment_mode`` 문자열이었다.

    그대로 두면 ``"FREE_DEPLOY"`` 가 truthy 로 읽혀 **결재가 필요 없는 설정인데
    결재를 요구하는** 조용한 오작동이 된다. 키워드 전용이라 즉시 터진다.
    """
    with pytest.raises(TypeError):
        M.next_stage_on_user_deploy(M.STAGE_IDLE, "FREE_DEPLOY")   # type: ignore[misc]


def test_turning_the_toggle_off_goes_back_to_idle():
    for stage in M.ALL_STAGES:
        assert M.next_stage_on_user_cancel(stage) == M.STAGE_IDLE


# ── 2. 여는 것은 결재뿐 ───────────────────────────────────────────────


def test_an_approval_opens_it():
    assert (M.next_stage_on_approval(M.STAGE_PENDING_APPROVAL, "approved")
            == M.STAGE_DEPLOYED)


def test_a_rejection_marks_it_rejected():
    assert (M.next_stage_on_approval(M.STAGE_PENDING_APPROVAL, "rejected")
            == M.STAGE_REJECTED)


def test_a_withdrawal_is_not_a_rejection():
    """회수는 "이 요청은 없던 것" 이다. rejected 로 두면 화면이 "거절됨" 이라
    말하는데 아무도 거절한 적이 없다."""
    assert (M.next_stage_on_approval(M.STAGE_PENDING_APPROVAL, "canceled")
            == M.STAGE_IDLE)


@pytest.mark.parametrize("stage", ["idle", "deployed", "rejected", "revoked"])
def test_an_approval_result_only_lands_on_something_waiting_for_it(stage):
    """결재를 기다리지 않던 것이 결재 결과로 열리면, 그 배포는 아무도 요청한
    적이 없는 배포다."""
    with pytest.raises(M.InvalidStageTransition):
        M.next_stage_on_approval(stage, "approved")


def test_an_unknown_result_is_refused():
    with pytest.raises(M.InvalidStageTransition):
        M.next_stage_on_approval(M.STAGE_PENDING_APPROVAL, "maybe")


# ── 3. 관리자는 닫기만 ────────────────────────────────────────────────


def test_an_admin_can_stop_a_live_deployment():
    """문제가 발견된 배포를 멈추는 데 다시 결재선을 태울 수는 없다."""
    assert M.next_stage_on_admin_revoke(M.STAGE_DEPLOYED) == M.STAGE_REVOKED


@pytest.mark.parametrize("stage", ["idle", "pending_approval", "rejected", "revoked"])
def test_an_admin_cannot_open_anything(stage):
    """관리자가 승인 없이 여는 길은 없다 — 있으면 결재는 우회 가능한 절차다."""
    with pytest.raises(M.InvalidStageTransition):
        M.next_stage_on_admin_revoke(stage)


def test_a_revoked_one_needs_a_fresh_approval():
    assert (M.next_stage_on_user_deploy(M.STAGE_REVOKED, approval_required=True)
            == M.STAGE_PENDING_APPROVAL)


# ── 4. 옛 이름 읽기 ───────────────────────────────────────────────────


@pytest.mark.parametrize("old,expected", [
    ("pending_admin", M.STAGE_PENDING_APPROVAL),
    ("pending_governance", M.STAGE_PENDING_APPROVAL),
    ("rejected_admin", M.STAGE_REJECTED),
    ("rejected_governance", M.STAGE_REJECTED),
    ("deployed", M.STAGE_DEPLOYED),
    ("idle", M.STAGE_IDLE),
])
def test_old_stage_names_are_still_readable(old, expected):
    assert M.normalize_stage(old) == expected


@pytest.mark.parametrize("bad", [None, "", "   ", "나중에생긴값", 7])
def test_an_unreadable_stage_is_treated_as_closed(bad):
    """모르면 '배포됨' 이 아니라 '미배포' 다 — 모르는 값이 외부를 열면 안 된다."""
    assert M.normalize_stage(bad) == M.STAGE_IDLE


# ── 5. 옛 boolean 과 맞물리기 ─────────────────────────────────────────


def test_only_deployed_opens_the_outside():
    for stage in M.ALL_STAGES:
        flags = M.stage_to_legacy_flags(stage)
        assert flags["is_deployed"] is (stage == M.STAGE_DEPLOYED)


def test_waiting_shows_up_as_inquire_deploy():
    """옛 화면들이 '승인 대기' 를 이 칸으로 읽는다."""
    assert M.stage_to_legacy_flags(M.STAGE_PENDING_APPROVAL)["inquire_deploy"] is True


def test_a_deployed_one_reads_as_fully_accepted():
    """is_governance_accepted 는 이제 거버넌스 심사와 무관하다 — '승인되어
    열렸는가' 와 같은 뜻이다. 그 칸을 읽던 화면이 계속 맞게 보이도록."""
    flags = M.stage_to_legacy_flags(M.STAGE_DEPLOYED)
    assert flags["is_admin_accepted"] and flags["is_governance_accepted"]


def test_revoked_and_rejected_close_everything():
    for stage in (M.STAGE_REJECTED, M.STAGE_REVOKED, M.STAGE_IDLE):
        assert not any(M.stage_to_legacy_flags(stage).values())


def test_the_old_two_argument_call_still_works():
    """다른 레포가 아직 안 올라왔을 때 인자 개수로 터지지 않게."""
    assert M.stage_to_legacy_flags(M.STAGE_DEPLOYED, "FULL_ACCEPT")["is_deployed"] is True


# ── 6. 배지 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage,group", [
    (M.STAGE_DEPLOYED, M.GROUP_DEPLOYED),
    (M.STAGE_PENDING_APPROVAL, M.GROUP_PENDING),
    (M.STAGE_IDLE, M.GROUP_NOT_DEPLOYED),
    (M.STAGE_REJECTED, M.GROUP_NOT_DEPLOYED),
    (M.STAGE_REVOKED, M.GROUP_NOT_DEPLOYED),
    ("pending_governance", M.GROUP_PENDING),
])
def test_simple_group(stage, group):
    assert M.stage_to_simple_group(stage) == group


# ── 7. stage 가 비어 있는 옛 행 ───────────────────────────────────────


def test_a_deployed_legacy_row_is_read_as_deployed():
    assert M.infer_state_from_legacy_flags({"is_deployed": True}) == (M.STAGE_DEPLOYED, True)


def test_a_waiting_legacy_row_is_read_as_waiting():
    assert (M.infer_state_from_legacy_flags({"inquire_deploy": True})
            == (M.STAGE_PENDING_APPROVAL, False))


def test_an_approved_but_closed_legacy_row_is_read_as_revoked():
    """승인 흔적은 있는데 배포가 아니다 — 철회됐거나 수정으로 풀린 상태다.
    idle 로 읽으면 "승인받은 적 없는 것" 이 되어 이력이 거짓이 된다."""
    assert (M.infer_state_from_legacy_flags({"is_governance_accepted": True})
            == (M.STAGE_REVOKED, True))


def test_an_empty_legacy_row_is_idle():
    assert M.infer_state_from_legacy_flags({}) == (M.STAGE_IDLE, False)
