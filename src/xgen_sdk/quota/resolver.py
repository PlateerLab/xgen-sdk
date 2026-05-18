"""
xgen_sdk.quota.resolver — 정책 결합 규칙

평가 순서 (TOKEN_QUOTA_POLICY_PLAN.md §4.4 — 요구사항 명시):

    Step 1) 사용자가 속한 모든 역할 정책 수집 → (period, scope) 별 가장 엄격 적용
            - 다중 역할 결합: 동일 key 에 N 개면 min(token_limit) 채택
            - action 결합: 하나라도 BLOCK 이면 BLOCK 으로 격상

    Step 2) 사용자 정책 override
            - 사용자가 동일 (period, scope) 에 직접 정책 보유 시 역할 결과를 *대체*
            - 사용자 정책이 없으면 역할 결과 유지
            - 사용자 정책은 면제(높은 한도) / 완화 / 엄격화 모두 표현 가능

결과는 (period, scope) 당 1개의 effective policy 리스트.
"""
from __future__ import annotations
from dataclasses import replace
from typing import Dict, Iterable, List, Sequence, Tuple

from xgen_sdk.quota.types import (
    QuotaAction,
    QuotaPeriod,
    QuotaPolicySpec,
    QuotaScope,
    QuotaTargetType,
)


def resolve_effective_policies(
    user_id: int,
    user_role_ids: Sequence[int],
    all_active_policies: Iterable[QuotaPolicySpec],
) -> List[QuotaPolicySpec]:
    """
    Args:
        user_id: 평가 대상 사용자 id
        user_role_ids: 해당 사용자가 속한 role.id 들 (None / 빈 시퀀스 허용)
        all_active_policies: is_active=True 인 정책 전체 (또는 caller 가 사전 필터링한 부분집합)

    Returns:
        (period, scope) 별 1 개씩의 effective policy 리스트.

    NULL / 빈 입력 안전:
        - user_role_ids 가 None 또는 빈 시퀀스 → 역할 정책은 무시
        - all_active_policies 가 None 또는 빈 iterable → 빈 리스트 반환
        - is_active=False 가 섞여있어도 명시적으로 거른다 (caller 가 사전 필터 미적용이어도 안전)
        - target_type 이 enum 정의 외 값 → 해당 정책 무시 (예외 안 던짐)
    """
    role_ids = set(user_role_ids or ())

    role_active: List[QuotaPolicySpec] = []
    user_active: List[QuotaPolicySpec] = []

    for p in all_active_policies or ():
        if not getattr(p, "is_active", False):
            continue
        if p.target_type == QuotaTargetType.ROLE and p.target_id in role_ids:
            role_active.append(p)
        elif p.target_type == QuotaTargetType.USER and p.target_id == user_id:
            user_active.append(p)
        # 외 (target_type 미일치 또는 다른 user/role) — skip

    # Step 1: role 결합 (min limit, BLOCK 우선)
    role_eff: Dict[Tuple[QuotaPeriod, QuotaScope], QuotaPolicySpec] = {}
    for p in role_active:
        key = (p.period_type, p.scope)
        cur = role_eff.get(key)
        if cur is None:
            role_eff[key] = p
            continue
        # 더 엄격한 한도 채택
        if p.token_limit < cur.token_limit:
            role_eff[key] = p
            cur = p
        # action 결합: 어느 정책이라도 BLOCK 이면 BLOCK 으로 격상
        if cur.action_on_exceed != QuotaAction.BLOCK and p.action_on_exceed == QuotaAction.BLOCK:
            role_eff[key] = replace(cur, action_on_exceed=QuotaAction.BLOCK)

    # Step 2: user override (동일 key 에서 user 정책이 role 결과를 대체)
    user_eff = {(p.period_type, p.scope): p for p in user_active}
    effective: Dict[Tuple[QuotaPeriod, QuotaScope], QuotaPolicySpec] = {**role_eff, **user_eff}

    return list(effective.values())
