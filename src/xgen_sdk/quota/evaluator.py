"""
xgen_sdk.quota.evaluator — 사용량 vs 한도 평가

순수 함수. 캐시·DB·HTTP 의존 없음.
caller 가 effective policy 와 (period, usage) 매핑을 미리 채워 전달한다.
"""
from __future__ import annotations
from typing import Iterable, Mapping, Optional, Tuple

from xgen_sdk.quota.types import (
    QuotaAction,
    QuotaDecision,
    QuotaPeriod,
    QuotaPolicySpec,
    QuotaUsage,
)


def evaluate_quota(
    effective_policies: Iterable[QuotaPolicySpec],
    usage_by_period: Mapping[QuotaPeriod, QuotaUsage],
) -> QuotaDecision:
    """
    Args:
        effective_policies: resolve_effective_policies() 의 결과
        usage_by_period:    { period: QuotaUsage } — caller 가 DB 에서 SUM 해온 현재 사용량

    Returns:
        QuotaDecision. 우선순위:
            1. BLOCK 위반 1 건 이상 → allowed=False, action=BLOCK
            2. BLOCK 위반 없고 WARN 위반만 → allowed=True, action=WARN
            3. 아무 위반 없음 → allowed=True, action=None  (QuotaDecision.allow())

    NULL / 이상값 안전:
        - effective_policies 가 None 또는 빈 → allow()
        - usage_by_period 가 None → 빈 mapping 으로 취급
        - usage_by_period 에 해당 period 가 없으면 그 정책 skip (위반 아님으로 처리)
        - token_limit <= 0 인 정책은 의미 모호 → skip (caller validation 권장)
    """
    block_hit: Optional[Tuple[QuotaPolicySpec, int]] = None
    warn_hit: Optional[Tuple[QuotaPolicySpec, int]] = None

    safe_usage: Mapping[QuotaPeriod, QuotaUsage] = usage_by_period or {}

    for p in effective_policies or ():
        usage = safe_usage.get(p.period_type)
        if usage is None:
            continue
        if p.token_limit is None or p.token_limit <= 0:
            continue

        used = usage.value_for(p.scope)
        if used < p.token_limit:
            continue

        # 위반 — action 별로 분기
        if p.action_on_exceed == QuotaAction.BLOCK and block_hit is None:
            block_hit = (p, used)
        elif p.action_on_exceed == QuotaAction.WARN and warn_hit is None:
            warn_hit = (p, used)

    if block_hit is not None:
        p, used = block_hit
        return QuotaDecision(
            allowed=False,
            action=QuotaAction.BLOCK,
            triggered_policy_id=p.id,
            period_type=p.period_type,
            scope=p.scope,
            used_tokens=used,
            limit_tokens=p.token_limit,
            reason_key=f"quota.exceeded.{p.period_type.value}.{p.scope.value}",
        )

    if warn_hit is not None:
        p, used = warn_hit
        return QuotaDecision(
            allowed=True,
            action=QuotaAction.WARN,
            triggered_policy_id=p.id,
            period_type=p.period_type,
            scope=p.scope,
            used_tokens=used,
            limit_tokens=p.token_limit,
            reason_key=f"quota.warning.{p.period_type.value}.{p.scope.value}",
        )

    return QuotaDecision.allow()
