"""
xgen_sdk.quota — 토큰 한도(quota) 정책 평가 모듈

DB·HTTP 의존이 없는 순수 dataclass / enum / 평가 함수.
xgen-core (정책 CRUD) 와 xgen-workflow (enforcement) 양쪽에서
동일 의미로 import 한다.

전체 설계: TOKEN_QUOTA_POLICY_PLAN.md §4 참조.

Quick start:
    from xgen_sdk.quota import (
        QuotaPolicySpec, QuotaPeriod, QuotaScope, QuotaUsage,
        QuotaAction, QuotaTargetType, QuotaDecision,
        resolve_effective_policies, evaluate_quota,
        period_bounds_kst, today_kst,
    )

    # 1) 활성 정책 + 사용자 역할 로드 후 결합
    effective = resolve_effective_policies(user_id, role_ids, policies)

    # 2) 각 effective period 에 대해 DB SUM 으로 usage 채움
    usage_by_period = { ... }  # caller 책임

    # 3) 평가
    decision = evaluate_quota(effective, usage_by_period)
    if not decision.allowed:
        ...  # block 처리
"""
from xgen_sdk.quota.types import (
    QuotaAction,
    QuotaDecision,
    QuotaPeriod,
    QuotaPolicySpec,
    QuotaScope,
    QuotaTargetType,
    QuotaUsage,
)
from xgen_sdk.quota.period import (
    period_bounds_kst,
    today_kst,
)
from xgen_sdk.quota.resolver import resolve_effective_policies
from xgen_sdk.quota.evaluator import evaluate_quota

__all__ = [
    # types
    "QuotaAction",
    "QuotaDecision",
    "QuotaPeriod",
    "QuotaPolicySpec",
    "QuotaScope",
    "QuotaTargetType",
    "QuotaUsage",
    # period
    "period_bounds_kst",
    "today_kst",
    # resolver / evaluator
    "resolve_effective_policies",
    "evaluate_quota",
]
