"""워크플로우 배포 — **결재 하나로 간다.**

배포는 외부 공유 전용 절차다(로그인 사용자의 내부 채팅과 무관). 이 모듈의
모든 전이는 외부 URL/embed/API 접근의 활성화 여부만 정한다.

무엇이 바뀌었나 (2026-09)
-------------------------
예전에는 배포에 **두 겹의 승인**이 있었다: 관리자 승인(pending_admin) 뒤에
거버넌스 승인(pending_governance). 거기에 누가 그 승인을 할 수 있는가는
역할(supervisor 관계, GOVERNANCE_ADMIN 역할)이 정했다. 그래서 "누가 승인해야
하는가" 를 사람이 지정할 수 없었고, 사실상 **그 역할을 가진 아무나 한 명**이
눌렀다.

지금은 그 자리에 결재가 있다. 배포를 누르면 결재가 올라가고, 결재선에 선
사람들이 **순서대로** 승인한다. 두 사람의 승인이 필요하면 결재선에 두 명을
세우면 된다 — 그게 원래 2단계가 하려던 일이다.

    idle ──[배포]──> pending_approval ──[결재 승인]──> deployed
                            │                             │
                            ├──[결재 거절]──> rejected     └──[관리자 철회]──> revoked
                            └──[회수·토글 OFF]──> idle

결재가 **필요 없는** 설정(기본값)이면 [배포] 는 곧바로 deployed 다.

Stage
-----
    idle              미배포
    pending_approval  결재 진행 중
    deployed          외부 접근 활성
    rejected          결재에서 거절됨 (사용자가 고쳐서 다시 올린다)
    revoked           관리자가 이미 난 승인을 철회

옛 stage 이름
-------------
``pending_admin`` · ``pending_governance`` → ``pending_approval``,
``rejected_admin`` · ``rejected_governance`` → ``rejected`` 로 **읽는다**.
core 의 마이그레이션이 표를 한 번 정리하지만, 그 사이에 쓰인 행이나 백업에서
돌아온 행이 옛 이름을 들고 있을 수 있다. 읽기만 관대하고 **쓰기는 새 이름만**
한다.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Stage 상수
# ──────────────────────────────────────────────────────────────────────────────
STAGE_IDLE = 'idle'
STAGE_PENDING_APPROVAL = 'pending_approval'
STAGE_DEPLOYED = 'deployed'
STAGE_REJECTED = 'rejected'
STAGE_REVOKED = 'revoked'

ALL_STAGES = frozenset({
    STAGE_IDLE,
    STAGE_PENDING_APPROVAL,
    STAGE_DEPLOYED,
    STAGE_REJECTED,
    STAGE_REVOKED,
})

#: 옛 이름 → 지금 이름. **읽을 때만** 쓴다.
LEGACY_STAGE_ALIASES: Dict[str, str] = {
    'pending_admin': STAGE_PENDING_APPROVAL,
    'pending_governance': STAGE_PENDING_APPROVAL,
    'rejected_admin': STAGE_REJECTED,
    'rejected_governance': STAGE_REJECTED,
}


class InvalidStageTransition(Exception):
    """현재 stage 에서 요청한 전이가 허용되지 않을 때."""


def normalize_stage(stage: Any) -> str:
    """저장된 값 → 지금의 stage.

    모르는 값은 ``idle`` 로 읽는다. 배포 상태를 못 읽었을 때 "배포됨" 으로
    기울면 승인 없이 외부에 열린 것으로 취급되므로, 모르면 **닫힌 쪽**이다.
    """
    s = str(stage or '').strip()
    if s in ALL_STAGES:
        return s
    if s in LEGACY_STAGE_ALIASES:
        return LEGACY_STAGE_ALIASES[s]
    return STAGE_IDLE


#: 옛 이름. 내부 호출부 호환 — 새 코드는 :func:`normalize_stage` 를 쓴다.
_ensure_stage = normalize_stage


# ──────────────────────────────────────────────────────────────────────────────
# 전이
# ──────────────────────────────────────────────────────────────────────────────
def next_stage_on_user_deploy(current_stage: str, *, approval_required: bool) -> str:
    """사용자가 [배포] 를 켰을 때.

    ``approval_required`` 는 **호출부가 정한다** — [결재 목록 설정] 의 값이자,
    슈퍼유저 면제까지 반영한 결론이다. 이 모듈은 설정을 읽지 않는다(그래야
    규칙을 DB 없이 전부 시험할 수 있다).

    키워드 전용인 이유: 예전 시그니처는 두 번째 인자가 ``deployment_mode``
    문자열이었다. 옛 호출이 그대로 남아 있으면 ``"FREE_DEPLOY"`` 가 truthy 로
    읽혀 **결재가 필요 없는 설정인데 결재를 요구하는** 조용한 오작동이 된다.
    키워드로 막으면 그 호출은 조용히 틀리는 대신 즉시 터진다.
    """
    current = normalize_stage(current_stage)
    if current == STAGE_DEPLOYED:
        return STAGE_DEPLOYED
    return STAGE_PENDING_APPROVAL if approval_required else STAGE_DEPLOYED


def next_stage_on_user_cancel(current_stage: str) -> str:
    """[배포] 를 껐을 때 — 어느 상태에서든 미배포로 돌아간다."""
    return STAGE_IDLE


#: 결재 결과 → 배포. 결재 원장의 상태 어휘를 그대로 쓴다.
APPROVAL_APPROVED = 'approved'
APPROVAL_REJECTED = 'rejected'
APPROVAL_CANCELED = 'canceled'


def next_stage_on_approval(current_stage: str, decision: str) -> str:
    """결재가 끝났을 때의 배포 상태.

    ``canceled`` 가 ``idle`` 인 이유: 회수는 "이 요청은 없던 것" 이다.
    ``rejected`` 로 두면 화면이 "거절됨" 이라 말하는데 아무도 거절한 적이 없다.
    """
    current = normalize_stage(current_stage)
    d = str(decision or '').strip().lower()

    if current != STAGE_PENDING_APPROVAL:
        raise InvalidStageTransition(
            f"결재 결과 반영은 stage={current!r} 에서 불가능합니다 (요구: pending_approval)")

    if d == APPROVAL_APPROVED:
        return STAGE_DEPLOYED
    if d == APPROVAL_REJECTED:
        return STAGE_REJECTED
    if d == APPROVAL_CANCELED:
        return STAGE_IDLE
    raise InvalidStageTransition(f"알 수 없는 결재 결과: {decision!r}")


def next_stage_on_admin_revoke(current_stage: str) -> str:
    """관리자가 이미 난 배포를 철회한다 — 결재와 별개로 남는 **비상 정지**.

    결재로 배포된 것을 관리자가 되돌릴 수 있어야 하는가? 있어야 한다. 문제가
    발견된 배포를 멈추는 데 다시 결재선을 태울 수는 없다. 대신 이것은
    **닫는 방향으로만** 간다 — 관리자가 승인 없이 여는 길은 없다.
    """
    current = normalize_stage(current_stage)
    if current != STAGE_DEPLOYED:
        raise InvalidStageTransition(
            f"배포 철회는 stage={current!r} 에서 불가능합니다 (요구: deployed)")
    return STAGE_REVOKED


# ──────────────────────────────────────────────────────────────────────────────
# stage → legacy boolean 동기화
# ──────────────────────────────────────────────────────────────────────────────
def stage_to_legacy_flags(stage: str, deployment_mode: Any = None) -> Dict[str, bool]:
    """stage → ``is_deployed`` / ``inquire_deploy`` / ``is_admin_accepted`` /
    ``is_governance_accepted``.

    이 boolean 들을 읽는 코드가 아직 여럿이라(채팅 노출 판정·외부 게이팅·감사
    집계) stage 와 함께 맞춰 쓴다. ``is_accepted`` 는 여기 없다 — 그건 배포가
    아니라 워크플로우 활성/비활성 토글이다.

    ``is_governance_accepted`` 는 이제 **거버넌스 심사와 무관**하다. 배포가
    승인되어 열렸는가와 같은 뜻이다 — 그 컬럼을 읽던 화면들이 계속 맞게
    보이도록 남긴다.

    ``deployment_mode`` 는 받기만 하고 쓰지 않는다. 옛 호출부(다른 레포가 아직
    안 올라왔을 때)가 인자 개수로 터지지 않게 하는 자리다.
    """
    s = normalize_stage(stage)
    out = {
        'is_deployed': False,
        'inquire_deploy': False,
        'is_admin_accepted': False,
        'is_governance_accepted': False,
    }
    if s == STAGE_PENDING_APPROVAL:
        out['inquire_deploy'] = True
    elif s == STAGE_DEPLOYED:
        out['is_deployed'] = True
        out['is_admin_accepted'] = True
        out['is_governance_accepted'] = True
    # idle / rejected / revoked — 전부 False (외부 접근 닫힘)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# UI 노출용 그룹
# ──────────────────────────────────────────────────────────────────────────────
GROUP_NOT_DEPLOYED = 'not_deployed'   # idle / rejected / revoked
GROUP_PENDING = 'pending'             # pending_approval
GROUP_DEPLOYED = 'deployed'           # deployed


def stage_to_simple_group(stage: str) -> str:
    """사용자 카드의 단순 배지 — 미배포 / 결재 중 / 배포됨."""
    s = normalize_stage(stage)
    if s == STAGE_DEPLOYED:
        return GROUP_DEPLOYED
    if s == STAGE_PENDING_APPROVAL:
        return GROUP_PENDING
    return GROUP_NOT_DEPLOYED


# ──────────────────────────────────────────────────────────────────────────────
# 옛 row 읽기
# ──────────────────────────────────────────────────────────────────────────────
def infer_state_from_legacy_flags(row: Dict[str, Any]) -> Tuple[str, bool]:
    """``deployment_stage`` 가 비어 있는 행의 상태를 boolean 으로 추정.

    core 의 마이그레이션이 표를 한 번 채우므로 평소에는 쓰이지 않는다. 백업
    복원이나 아주 오래된 행을 위한 마지막 방어다.

    반환: ``(stage, is_admin_accepted)``
    """
    if bool(row.get('is_deployed')):
        return STAGE_DEPLOYED, True
    if bool(row.get('inquire_deploy')):
        return STAGE_PENDING_APPROVAL, False
    if bool(row.get('is_governance_accepted')) or bool(row.get('is_admin_accepted')):
        # 승인 흔적은 있는데 배포가 아니다 — 철회됐거나 수정으로 풀린 상태.
        return STAGE_REVOKED, True
    return STAGE_IDLE, False
