"""
Token tracking + Cost calculation strategies

geny-harness s07_token 차용:
  TokenTracker: API 응답에서 토큰 사용량 추적
  CostCalculator: 프로바이더/모델별 비용 계산

기존 s00_harness.main_call 에 하드코딩되어 있던 토큰/비용 로직을 Strategy로 분리.
프로바이더별 가격 모델이 다를 때 유연하게 대응.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..interfaces import Strategy

logger = logging.getLogger("harness.strategy.token")


@dataclass
class TokenUsageRecord:
    """토큰 사용 기록"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class TokenTracker(Strategy, ABC):
    """토큰 사용량 추적 인터페이스"""

    @abstractmethod
    def track(self, api_response: dict, state: Any) -> TokenUsageRecord:
        """API 응답에서 토큰 사용량 추출"""
        ...


class CostCalculator(Strategy, ABC):
    """비용 계산 인터페이스"""

    @abstractmethod
    def calculate(self, usage: TokenUsageRecord, model: str) -> float:
        """토큰 사용량 → USD 비용"""
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  구현체
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DefaultTokenTracker(TokenTracker):
    """기본 토큰 추적 — Anthropic/OpenAI 공통"""

    @property
    def name(self) -> str:
        return "default"

    def track(self, api_response: dict, state: Any) -> TokenUsageRecord:
        usage = api_response.get("usage", {})
        return TokenUsageRecord(
            input_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            output_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        )


# 가격 테이블 (USD per 1M tokens) — 빌트인 default.
# 외부 모델 (vLLM 자체 호스팅 / 사내 모델 / 신규 GPT 모델) 은
# `register_model_pricing()` 또는 entry_points "xgen_harness.model_pricing" 로 추가.
PRICING: dict[str, dict] = {
    # Anthropic
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-haiku-4-20250414": {"input": 0.80, "output": 4.0, "cache_write": 1.0, "cache_read": 0.08},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "o3-mini": {"input": 1.10, "output": 4.40},
    # Google
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
}


def register_model_pricing(
    name: str,
    input: float,
    output: float,
    cache_read: float | None = None,
    cache_write: float | None = None,
) -> None:
    """모델 가격 등록 (USD per 1M tokens). idempotent — 같은 이름 재등록 시 덮어쓴다.

    예) self-hosted vLLM Qwen3.5-27b 가격 0 으로 등록:
      register_model_pricing("Qwen3.5-27b", input=0.0, output=0.0)

    cache_* 미지정 시 빌트인 fallback (input × 1.25 / × 0.1) 적용.
    """
    spec: dict = {"input": float(input), "output": float(output)}
    if cache_read is not None:
        spec["cache_read"] = float(cache_read)
    if cache_write is not None:
        spec["cache_write"] = float(cache_write)
    PRICING[name] = spec


_PRICING_DISCOVERED = False


def _discover_pricing_from_entry_points() -> None:
    """entry_points 그룹 ``xgen_harness.model_pricing`` 자동 발견. idempotent.

    외부 패키지 등록 예:
      [project.entry-points."xgen_harness.model_pricing"]
      qwen3-27b = "my_pkg.pricing:get_qwen3"   # () -> {input, output, cache_read?, cache_write?}

    entry_point 가 dict 또는 dict list 반환 모두 허용. dict 키 ``name`` 미지정 시 ep.name 사용.
    """
    global _PRICING_DISCOVERED
    if _PRICING_DISCOVERED:
        return
    _PRICING_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.model_pricing"
        items = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])  # type: ignore[arg-type]
        for ep in items:
            try:
                produced = ep.load()
                if callable(produced):
                    produced = produced()
                if isinstance(produced, dict):
                    register_model_pricing(
                        produced.get("name", ep.name),
                        input=float(produced["input"]),
                        output=float(produced["output"]),
                        cache_read=produced.get("cache_read"),
                        cache_write=produced.get("cache_write"),
                    )
                elif isinstance(produced, list):
                    for item in produced:
                        if isinstance(item, dict) and item.get("name"):
                            register_model_pricing(
                                item["name"],
                                input=float(item["input"]),
                                output=float(item["output"]),
                                cache_read=item.get("cache_read"),
                                cache_write=item.get("cache_write"),
                            )
            except Exception as e:
                logger.warning("[model_pricing] entry_point %s 로드 실패: %s", ep.name, e)
    except Exception as e:
        logger.debug("[model_pricing] entry_points discovery 실패: %s", e)


_discover_pricing_from_entry_points()


class ModelPricingCalculator(CostCalculator):
    """모델별 가격 테이블 기반 비용 계산"""

    @property
    def name(self) -> str:
        return "model_pricing"

    def calculate(self, usage: TokenUsageRecord, model: str) -> float:
        pricing = PRICING.get(model)
        if not pricing:
            # 알 수 없는 모델 — 보수적 추정 ($3/1M input, $15/1M output)
            pricing = {"input": 3.0, "output": 15.0}

        cost = 0.0
        cost += (usage.input_tokens / 1_000_000) * pricing.get("input", 3.0)
        cost += (usage.output_tokens / 1_000_000) * pricing.get("output", 15.0)
        cost += (usage.cache_creation_tokens / 1_000_000) * pricing.get("cache_write", pricing.get("input", 3.0) * 1.25)
        cost += (usage.cache_read_tokens / 1_000_000) * pricing.get("cache_read", pricing.get("input", 3.0) * 0.1)

        return cost
