"""
xgen_sdk.quota.types — 토큰 한도 정책의 enum / dataclass

DB/HTTP 의존이 없는 순수 데이터 타입. xgen-core (정책 CRUD) 와
xgen-workflow (enforcement) 양쪽에서 동일 의미로 import 한다.

NULL / 알 수 없는 값 안전:
    각 Enum 은 `coerce()` 클래스메서드로 안전한 fallback 을 제공한다.
    DB row 의 문자열 컬럼이 NULL 이거나 enum 정의 외 값(스키마 변경 중간 상태,
    수동 SQL 수정 등)이어도 caller flow 가 throw 하지 않도록 한다.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class QuotaPeriod(str, Enum):
    """quota 적용 주기 (KST 달력 기준)."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"

    @classmethod
    def coerce(cls, value: Optional[str]) -> "QuotaPeriod":
        """알 수 없는 값은 MONTHLY 로 fallback (가장 일반적인 한도 주기)."""
        if not value:
            return cls.MONTHLY
        try:
            return cls(value)
        except ValueError:
            return cls.MONTHLY


class QuotaScope(str, Enum):
    """한도 적용 범위.

    TOTAL  — input + output 합 (tool 은 LLM 입력에 이미 포함되므로 중복합산 방지)
    INPUT  — 모델 보고 input_tokens 누적
    OUTPUT — 모델 보고 output_tokens 누적
    TOOL   — agent ReAct 루프 내 tool 호출 input/output (현재 best-effort, 0 가능).
             정확도는 추후 정교화 — 인터페이스만 1급 시민으로 마련.
    """
    TOTAL = "total"
    INPUT = "input"
    OUTPUT = "output"
    TOOL = "tool"

    @classmethod
    def coerce(cls, value: Optional[str]) -> "QuotaScope":
        """알 수 없는 값은 TOTAL 로 fallback."""
        if not value:
            return cls.TOTAL
        try:
            return cls(value)
        except ValueError:
            return cls.TOTAL


class QuotaAction(str, Enum):
    """한도 초과 시 동작."""
    BLOCK = "block"
    WARN = "warn"

    @classmethod
    def coerce(cls, value: Optional[str]) -> "QuotaAction":
        """알 수 없는 값은 BLOCK 으로 fallback (보수적 안전 기본값)."""
        if not value:
            return cls.BLOCK
        try:
            return cls(value)
        except ValueError:
            return cls.BLOCK


class QuotaTargetType(str, Enum):
    """정책 적용 대상 유형."""
    USER = "user"
    ROLE = "role"

    @classmethod
    def coerce(cls, value: Optional[str]) -> "QuotaTargetType":
        """알 수 없는 값은 USER 로 fallback."""
        if not value:
            return cls.USER
        try:
            return cls(value)
        except ValueError:
            return cls.USER


@dataclass(frozen=True)
class QuotaPolicySpec:
    """DB row (token_quota_policies) ↔ SDK 평가 함수 사이의 immutable spec.

    caller (xgen-core / xgen-workflow) 는 DB 모델을 이 spec 으로 변환하여 전달한다.
    SDK 자체는 DB 모델 클래스를 import 하지 않는다.
    """
    id: int
    target_type: QuotaTargetType
    target_id: int
    period_type: QuotaPeriod
    scope: QuotaScope
    token_limit: int
    action_on_exceed: QuotaAction
    priority: int
    is_active: bool


@dataclass(frozen=True)
class QuotaUsage:
    """단일 사용자의 한 period 누적 사용량 스냅샷.

    caller 가 DB (token_usage_daily) 에서 SUM 해 온 결과를 채워 넘긴다.
    """
    period_type: QuotaPeriod
    period_start_kst: str   # 'YYYY-MM-DD'
    period_end_kst: str     # 'YYYY-MM-DD'
    input_tokens: int = 0
    output_tokens: int = 0
    tool_tokens: int = 0
    total_tokens: int = 0   # input + output (caller 가 계산해서 채우거나 0 으로 둘 수 있음)

    def value_for(self, scope: QuotaScope) -> int:
        """scope 에 해당하는 누적 토큰을 반환. 알 수 없는 scope 는 total 로 fallback."""
        if scope == QuotaScope.INPUT:
            return self.input_tokens or 0
        if scope == QuotaScope.OUTPUT:
            return self.output_tokens or 0
        if scope == QuotaScope.TOOL:
            return self.tool_tokens or 0
        # TOTAL or unknown
        return self.total_tokens or 0


@dataclass(frozen=True)
class QuotaDecision:
    """평가 결과.

    caller 는 `allowed` 만으로 차단 여부 판단 가능. `action` 은 부가 정보
    (WARN 만 트리거된 경우 allowed=True 지만 action=WARN).
    """
    allowed: bool
    action: Optional[QuotaAction]
    triggered_policy_id: Optional[int]
    period_type: Optional[QuotaPeriod]
    scope: Optional[QuotaScope]
    used_tokens: Optional[int]
    limit_tokens: Optional[int]
    reason_key: Optional[str]   # i18n key (예: 'quota.exceeded.monthly.total')

    @classmethod
    def allow(cls) -> "QuotaDecision":
        """위반 없음 — 기본 통과 결정."""
        return cls(
            allowed=True,
            action=None,
            triggered_policy_id=None,
            period_type=None,
            scope=None,
            used_tokens=None,
            limit_tokens=None,
            reason_key=None,
        )
