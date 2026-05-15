"""
워크플로우 배포 승인 절차 — state machine.

배포(deploy)는 **외부 공유 전용** 절차다. 로그인 사용자의 내부 채팅과 무관.
본 모듈의 모든 전이는 외부 URL/embed 접근의 활성화 여부만 결정한다.

전체 설계: PLAN_deployment_mode_redesign.md (프로젝트 루트) 참조.

## Stage (deployment_stage)
    idle                  → 미배포 (사용자가 토글 OFF)
    pending_admin         → 관리자 승인 대기
    pending_governance    → 거버넌스 승인 대기 (FULL_ACCEPT 모드 전용)
    deployed              → 외부 접근 활성
    rejected_admin        → 관리자 거부
    rejected_governance   → 거버넌스 거부
    revoked               → 관리자가 이전 승인을 철회

## Mode (DEPLOYMENT_MODE 환경설정)
    FREE_DEPLOY    → 사용자가 클릭하면 즉시 deployed (어떤 승인도 없음)
    ADMIN_ACCEPT   → 관리자 승인 1단계
    FULL_ACCEPT    → 관리자 + 거버넌스 2단계

## 사용 규약
모든 endpoint 는 이 모듈의 next_stage_* 함수만 호출해 stage 전이를 결정한다.
DB update 시점에 stage_to_legacy_flags() 로 호환 boolean 도 함께 update.
"""
from __future__ import annotations
from typing import Any, Dict, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Stage 상수
# ──────────────────────────────────────────────────────────────────────────────
STAGE_IDLE                = 'idle'
STAGE_PENDING_ADMIN       = 'pending_admin'
STAGE_PENDING_GOVERNANCE  = 'pending_governance'
STAGE_DEPLOYED            = 'deployed'
STAGE_REJECTED_ADMIN      = 'rejected_admin'
STAGE_REJECTED_GOVERNANCE = 'rejected_governance'
STAGE_REVOKED             = 'revoked'

ALL_STAGES = frozenset({
    STAGE_IDLE,
    STAGE_PENDING_ADMIN,
    STAGE_PENDING_GOVERNANCE,
    STAGE_DEPLOYED,
    STAGE_REJECTED_ADMIN,
    STAGE_REJECTED_GOVERNANCE,
    STAGE_REVOKED,
})

# ──────────────────────────────────────────────────────────────────────────────
# Mode 상수
# ──────────────────────────────────────────────────────────────────────────────
MODE_FREE_DEPLOY  = 'FREE_DEPLOY'
MODE_ADMIN_ACCEPT = 'ADMIN_ACCEPT'
MODE_FULL_ACCEPT  = 'FULL_ACCEPT'

ALL_MODES = frozenset({MODE_FREE_DEPLOY, MODE_ADMIN_ACCEPT, MODE_FULL_ACCEPT})

DEFAULT_MODE = MODE_FULL_ACCEPT  # 가장 보수적인 기본값


def normalize_mode(mode: Any) -> str:
    """문자열/None/잘못된 값을 받아 유효 mode 로 정규화. 기본 FULL_ACCEPT."""
    if isinstance(mode, str):
        m = mode.strip().upper()
        if m in ALL_MODES:
            return m
    return DEFAULT_MODE


# ──────────────────────────────────────────────────────────────────────────────
# 예외
# ──────────────────────────────────────────────────────────────────────────────
class InvalidStageTransition(ValueError):
    """현재 stage 에서 요청한 전이가 허용되지 않을 때 발생."""


def _ensure_stage(stage: str) -> str:
    if stage not in ALL_STAGES:
        # 알 수 없는 stage 는 안전하게 idle 로 취급 (legacy row 가 NULL/공백일 수 있음)
        return STAGE_IDLE
    return stage


# ──────────────────────────────────────────────────────────────────────────────
# 전이 함수
# ──────────────────────────────────────────────────────────────────────────────
def next_stage_on_user_deploy(
    current_stage: str,
    deployment_mode: str,
    actor_is_admin: bool = False,
) -> str:
    """사용자가 "배포" 토글을 ON 으로 했을 때의 다음 stage.

    actor_is_admin: 요청자가 admin 권한 보유자인지. ADMIN_ACCEPT 모드에서 admin 본인이
                    배포 요청하면 admin 승인 단계를 건너뛴다 (이미 admin 이므로).
                    FULL_ACCEPT 모드의 admin 도 admin 단계 skip → 바로 거버넌스 대기.
    """
    current = _ensure_stage(current_stage)
    mode = normalize_mode(deployment_mode)

    # 이미 deployed 면 동작 없음.
    if current == STAGE_DEPLOYED:
        return STAGE_DEPLOYED

    # FREE_DEPLOY: 어떤 승인도 없이 즉시 배포.
    if mode == MODE_FREE_DEPLOY:
        return STAGE_DEPLOYED

    # ADMIN_ACCEPT: admin 본인이면 즉시 deployed, 아니면 pending_admin.
    if mode == MODE_ADMIN_ACCEPT:
        return STAGE_DEPLOYED if actor_is_admin else STAGE_PENDING_ADMIN

    # FULL_ACCEPT: admin 본인이면 admin 단계 skip → governance 대기. 아니면 admin 대기.
    if mode == MODE_FULL_ACCEPT:
        return STAGE_PENDING_GOVERNANCE if actor_is_admin else STAGE_PENDING_ADMIN

    return STAGE_PENDING_ADMIN  # fallback


def next_stage_on_user_cancel(current_stage: str) -> str:
    """사용자가 "배포" 토글을 OFF 로 했을 때. 모든 진행 상태가 idle 로 돌아간다."""
    return STAGE_IDLE


def next_stage_on_admin_decision(
    current_stage: str,
    decision: str,
    deployment_mode: str,
) -> str:
    """관리자의 배포 결정 (approve / reject / revoke) 후 다음 stage.

    decision:
      'approve' — 관리자가 배포를 승인. mode 에 따라 deployed (ADMIN) / pending_governance (FULL).
      'reject'  — 관리자가 거부. stage = rejected_admin. 사용자가 다시 요청해야 한다.
      'revoke'  — 이미 deployed 인 배포의 관리자 승인을 철회. stage = revoked.
    """
    current = _ensure_stage(current_stage)
    mode = normalize_mode(deployment_mode)
    decision = (decision or '').strip().lower()

    if decision == 'approve':
        # 관리자 승인 — pending_admin 또는 rejected_admin / revoked 에서 가능.
        if current not in (STAGE_PENDING_ADMIN, STAGE_REJECTED_ADMIN, STAGE_REVOKED):
            raise InvalidStageTransition(
                f"관리자 승인은 stage={current!r} 에서 불가능합니다 (요구: pending_admin / rejected_admin / revoked)"
            )
        # FULL_ACCEPT 면 다음은 거버넌스 대기, 그 외는 즉시 deployed.
        if mode == MODE_FULL_ACCEPT:
            return STAGE_PENDING_GOVERNANCE
        return STAGE_DEPLOYED

    if decision == 'reject':
        if current not in (STAGE_PENDING_ADMIN,):
            raise InvalidStageTransition(
                f"관리자 거부는 stage={current!r} 에서 불가능합니다 (요구: pending_admin)"
            )
        return STAGE_REJECTED_ADMIN

    if decision == 'revoke':
        # 이미 deployed 또는 pending_governance 인 항목의 관리자 승인을 철회.
        if current not in (STAGE_DEPLOYED, STAGE_PENDING_GOVERNANCE):
            raise InvalidStageTransition(
                f"관리자 승인 철회는 stage={current!r} 에서 불가능합니다 (요구: deployed / pending_governance)"
            )
        return STAGE_REVOKED

    raise InvalidStageTransition(f"알 수 없는 admin decision: {decision!r}")


# ──────────────────────────────────────────────────────────────────────────────
# 거버넌스 액션 상수 — UI ↔ backend ↔ history 모두 같은 문자열을 사용한다.
# ──────────────────────────────────────────────────────────────────────────────
GOVERNANCE_ACTION_APPROVE             = 'approve'              # 승인완료 → stage=deployed
GOVERNANCE_ACTION_REJECT              = 'reject'               # 승인거절 → stage=rejected_governance
GOVERNANCE_ACTION_CONDITIONAL_APPROVE = 'conditional_approve'  # 승인보류 → stage 유지(pending_governance), 조건 사유 전달

# 'pending' 은 외부 UI / multipart review API 에서 사용하는 보류 액션의 표면 이름.
# 의미상 conditional_approve 와 동일 (= 거버넌스가 결론 보류, 추가 조건/사유 누적).
# state machine 내부에서는 conditional_approve 로 정규화된다.
GOVERNANCE_ACTION_PENDING = 'pending'

ALL_GOVERNANCE_ACTIONS = frozenset({
    GOVERNANCE_ACTION_APPROVE,
    GOVERNANCE_ACTION_REJECT,
    GOVERNANCE_ACTION_CONDITIONAL_APPROVE,
    GOVERNANCE_ACTION_PENDING,  # alias
})


def normalize_governance_action(action: Any) -> str:
    """외부 입력 액션 문자열을 정규화. 'pending' → 'conditional_approve' 로 alias 처리.

    그 외 입력은 trim + lower 만 적용 (검증은 호출자가 ALL_GOVERNANCE_ACTIONS 로 별도 수행).
    """
    if not isinstance(action, str):
        return ''
    a = action.strip().lower()
    if a == GOVERNANCE_ACTION_PENDING:
        return GOVERNANCE_ACTION_CONDITIONAL_APPROVE
    return a


def next_stage_on_governance_decision(
    current_stage: str,
    decision: str,
) -> str:
    """거버넌스의 배포 결정.

    허용 stage:
      - pending_governance — 최초 심사
      - deployed           — 사후 반려 (이미 승인된 배포를 거버넌스가 회수)

    decision:
      'approve'              — 거버넌스 승인.
                               pending_governance → deployed. deployed 에서 호출은 self-loop 라
                               의미가 없어 거부.
      'reject'               — 거버넌스 반려.
                               pending_governance → rejected_governance (초기 반려, 미배포).
                               deployed → rejected_governance (사후 반려 = 배포 즉시 회수).
                               두 경우의 next stage 는 동일하지만 호출 측이 prev_stage 를
                               audit 로그에 남겨 의미를 구분할 수 있다.
      'conditional_approve'  — 조건부 보류. pending_governance 에서만 호출, stage 유지.
                               (현재 정책상 외부 UI 에서는 비노출되지만 alias 호환 보존.)
    """
    current = _ensure_stage(current_stage)
    decision = normalize_governance_action(decision)

    ALLOWED_STAGES = (STAGE_PENDING_GOVERNANCE, STAGE_DEPLOYED)
    if current not in ALLOWED_STAGES:
        raise InvalidStageTransition(
            f"거버넌스 결정은 stage={current!r} 에서 불가능합니다 "
            f"(요구: pending_governance 또는 deployed)"
        )

    if decision == GOVERNANCE_ACTION_APPROVE:
        if current == STAGE_DEPLOYED:
            # 이미 승인된 상태를 다시 승인 — self-loop, 의미 없음.
            raise InvalidStageTransition(
                "이미 배포 승인된 워크플로우입니다 (current=deployed)."
            )
        return STAGE_DEPLOYED
    if decision == GOVERNANCE_ACTION_REJECT:
        # 두 stage 모두에서 rejected_governance 로 전이. legacy flags 동기화 시 자동으로
        # is_deployed=False 가 되어 외부 접근이 즉시 차단된다.
        return STAGE_REJECTED_GOVERNANCE
    if decision == GOVERNANCE_ACTION_CONDITIONAL_APPROVE:
        if current != STAGE_PENDING_GOVERNANCE:
            raise InvalidStageTransition(
                "조건부 보류는 pending_governance 에서만 가능합니다."
            )
        # 조건부 보류 (외부 표면 이름: 'pending') — stage 변화 없음.
        # comment/conditions 만 history 에 누적되어 사용자가 조건을 확인하고 보완하도록 한다.
        # legacy boolean 도 변하지 않는다.
        return STAGE_PENDING_GOVERNANCE

    raise InvalidStageTransition(f"알 수 없는 governance decision: {decision!r}")


# ──────────────────────────────────────────────────────────────────────────────
# stage → legacy boolean 동기화
# ──────────────────────────────────────────────────────────────────────────────
def stage_to_legacy_flags(stage: str, deployment_mode: str) -> Dict[str, bool]:
    """stage + mode 로부터 (is_deployed, inquire_deploy, is_admin_accepted, is_governance_accepted) 산출.

    chat 노출 / 외부 deploy 게이팅 등 기존 boolean 코드가 그대로 동작하도록 stage 와 동기화.
    is_accepted 는 본 함수의 책임이 아니다 (직교 토글).

    governance 승인 의미가 없는 모드(FREE_DEPLOY / ADMIN_ACCEPT)에서는 is_governance_accepted = False.
    """
    s = _ensure_stage(stage)
    mode = normalize_mode(deployment_mode)

    # 기본값 — 모두 False.
    out = {
        'is_deployed': False,
        'inquire_deploy': False,
        'is_admin_accepted': False,
        'is_governance_accepted': False,
    }

    if s == STAGE_IDLE:
        return out
    if s == STAGE_PENDING_ADMIN:
        out['inquire_deploy'] = True
        return out
    if s == STAGE_PENDING_GOVERNANCE:
        out['is_admin_accepted'] = True
        return out
    if s == STAGE_DEPLOYED:
        out['is_deployed'] = True
        out['is_admin_accepted'] = True
        # FULL_ACCEPT 만 거버넌스 단계가 실제로 존재 → 통과 T. 다른 모드는 의미 없음 → F.
        out['is_governance_accepted'] = (mode == MODE_FULL_ACCEPT)
        return out
    if s == STAGE_REJECTED_ADMIN:
        return out  # 모두 False
    if s == STAGE_REJECTED_GOVERNANCE:
        out['is_admin_accepted'] = True  # 관리자 단계는 통과했음
        return out
    if s == STAGE_REVOKED:
        # 관리자가 승인을 철회 — 외부 접근 OFF. governance 이력은 별도 컬럼 보존.
        return out

    return out


# ──────────────────────────────────────────────────────────────────────────────
# UI 노출용 그룹 — 7 stage → 3 그룹 (미배포 / 승인대기 / 배포됨).
# 메인 사용자 카드의 단순 배지 표기에 사용. 상세 stage 는 배포 설정 modal 에서 별도 노출.
# ──────────────────────────────────────────────────────────────────────────────
GROUP_NOT_DEPLOYED = 'not_deployed'   # 미배포 — idle / rejected_* / revoked
GROUP_PENDING      = 'pending'        # 승인 대기 — pending_admin / pending_governance
GROUP_DEPLOYED     = 'deployed'       # 배포됨 — deployed


def stage_to_simple_group(stage: str) -> str:
    """7 stage → 3 그룹 매핑. 사용자 노출용 단순화 배지 전용.

    UX 정책: 사용자에게는 미배포/승인대기/배포됨 3개만 보여주고, 거부/철회 등의 세부 상태는
    배포 설정 모달에서만 노출. 관리자 페이지/거버넌스 페이지는 여전히 7 stage 를 그대로 본다.
    """
    s = _ensure_stage(stage)
    if s == STAGE_DEPLOYED:
        return GROUP_DEPLOYED
    if s in (STAGE_PENDING_ADMIN, STAGE_PENDING_GOVERNANCE):
        return GROUP_PENDING
    # idle / rejected_admin / rejected_governance / revoked → 모두 '미배포'
    return GROUP_NOT_DEPLOYED


# ──────────────────────────────────────────────────────────────────────────────
# 마이그레이션 — legacy boolean → (stage, is_admin_accepted)
# ──────────────────────────────────────────────────────────────────────────────
def infer_state_from_legacy_flags(row: Dict[str, Any]) -> Tuple[str, bool]:
    """기존 row 의 (is_deployed, inquire_deploy, is_governance_accepted) 로부터 stage 추론.

    is_accepted 는 사용하지 않는다 (직교 토글).

    추론 규칙:
      - is_deployed=True                            → deployed, is_admin_accepted=True
      - inquire_deploy=True                         → pending_admin, is_admin_accepted=False
      - is_governance_accepted=True (그 외)         → pending_governance, is_admin_accepted=True
      - 그 외                                       → idle, is_admin_accepted=False

    거버넌스 거부 상태(rejected_governance)는 legacy boolean 만으로 복원 불가 → idle 로 강등.
    """
    is_deployed = bool(row.get('is_deployed'))
    inquire = bool(row.get('inquire_deploy'))
    gov_accepted = bool(row.get('is_governance_accepted'))

    if is_deployed:
        return STAGE_DEPLOYED, True
    if inquire:
        return STAGE_PENDING_ADMIN, False
    if gov_accepted:
        # 관리자는 통과했지만 deployed 가 아닌 상태 = pending_governance 로 안전 추정.
        return STAGE_PENDING_GOVERNANCE, True
    return STAGE_IDLE, False
