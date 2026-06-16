"""
StrategyResolver — Strategy 이름 → 구현체 인스턴스 매핑

geny-harness의 Stage×Strategy 이중 추상화를 Python으로 구현.
각 스테이지가 선언한 Strategy 이름을 실제 클래스로 해석.

사용:
    resolver = StrategyResolver.default()
    retry = resolver.resolve("s00_harness", "retry", "exponential_backoff")
"""

import logging
from typing import Any, Optional, Type

from ..stages.interfaces import Strategy

logger = logging.getLogger("harness.strategy_resolver")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  전역 레지스트리: (stage_id, slot_name, impl_name) → Strategy 클래스
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_REGISTRY: dict[tuple[str, str, str], Type[Strategy]] = {}
_DEFAULTS_REGISTERED = False


def register_strategy(stage_id: str, slot_name: str, impl_name: str, cls: Type[Strategy]) -> None:
    _REGISTRY[(stage_id, slot_name, impl_name)] = cls


def _ensure_defaults_registered() -> None:
    """기본 Strategy 가 한 번이라도 등록되지 않았으면 트리거.

    v0.15.3 — 엔진 내장 기본(`_register_defaults`) + 파일시스템 스캔(`scan_stage_strategies`)
    + entry_points(`_discover_plugin_strategies`) 3 경로 모두 idempotent 실행.
    외부 기여자가 Stage 디렉토리 안에 `strategies/<slot>__<impl>.py` 파일만 드롭해도
    여기서 자동 합류.
    """
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return
    _DEFAULTS_REGISTERED = True
    try:
        _register_defaults()
    except Exception as e:
        logger.debug("strategy _register_defaults 실패, 레지스트리는 빈 상태 유지: %s", e)
    # 파일시스템 기반 Stage-local Strategy 자동 등록.
    try:
        from .fs_scanner import scan_stage_strategies
        added = scan_stage_strategies()
        if added:
            logger.debug("[strategy_resolver] fs_scanner: %d Stage-local strategies", added)
    except Exception as e:
        logger.debug("strategy fs_scanner 실패: %s", e)


class StrategyResolver:
    """Strategy 이름을 구현체 인스턴스로 해석."""

    def __init__(self):
        self._cache: dict[tuple[str, str, str], Strategy] = {}

    def resolve(
        self,
        stage_id: str,
        slot_name: str,
        impl_name: str,
        config: Optional[dict[str, Any]] = None,
    ) -> Optional[Strategy]:
        """Strategy 인스턴스 반환. 캐시 사용."""
        _ensure_defaults_registered()
        key = (stage_id, slot_name, impl_name)

        # 캐시 히트
        if key in self._cache:
            return self._cache[key]

        # 레지스트리에서 클래스 조회
        cls = _REGISTRY.get(key)

        # stage_id 와일드카드 폴백 (공통 전략)
        if cls is None:
            cls = _REGISTRY.get(("*", slot_name, impl_name))

        if cls is None:
            logger.warning("Strategy not found: %s/%s/%s", stage_id, slot_name, impl_name)
            return None

        instance = cls()
        if config:
            instance.configure(config)

        self._cache[key] = instance
        return instance

    @classmethod
    def default(cls) -> "StrategyResolver":
        """기본 Strategy가 등록된 Resolver 생성."""
        _register_defaults()
        return cls()


# 모듈 레벨 편의 함수 — Stage 밖(예: s08_decide judge_then_loop)에서 slot 전략을
# 이름으로 즉시 해석할 때 사용. StrategyResolver.default().resolve(...) 와 동일하나
# 호출처 import 를 단순화한다. judge_then_loop.evaluate_response 가 이 심볼을 import
# 하는데 그동안 모듈에 없어 ImportError → EvaluationStrategy hook 이 통째로 죽고 항상
# fallback 으로 빠졌다(외부 등록 평가전략 사용 불가). 누락 API 를 채워 hook 을 복구한다.
def resolve_strategy(
    stage_id: str,
    slot_name: str,
    impl_name: str,
    config: Optional[dict[str, Any]] = None,
) -> Optional[Strategy]:
    """(stage_id, slot_name, impl_name) → Strategy 인스턴스. 기본 전략 등록 보장."""
    return StrategyResolver.default().resolve(stage_id, slot_name, impl_name, config)


def _register_defaults() -> None:
    """모든 기본 Strategy를 레지스트리에 등록."""
    if _REGISTRY:
        return  # 이미 등록됨

    from ..stages.strategies import (
        ExponentialBackoffRetry, NoRetry,
        CompositeToolRouter, MCPToolRouter, BuiltinToolRouter,
        SequentialToolExecutor,
        LLMJudgeEvaluation, RuleBasedEvaluation, NoValidation,
        WeightedScorer,
        ProgressiveDiscovery, EagerLoadDiscovery,
        TokenBudgetCompactor, SlidingWindowCompactor,
        MicrocompactCompactor, ContextCollapseOverlayCompactor,
        AutocompactLLMCompactor, CascadeCompactor,
    )
    from ..stages.strategies.tool_executor import ParallelToolExecutor

    # s04_tool — discovery
    register_strategy("s04_tool", "discovery", "progressive_3level", ProgressiveDiscovery)
    register_strategy("s04_tool", "discovery", "eager_load", EagerLoadDiscovery)

    # s06_context — compactor (v0.11.21: L3/L4/L5/cascade 4종 resolver 승격)
    register_strategy("s06_context", "compactor", "token_budget", TokenBudgetCompactor)
    register_strategy("s06_context", "compactor", "sliding_window", SlidingWindowCompactor)
    register_strategy("s06_context", "compactor", "microcompact", MicrocompactCompactor)
    register_strategy("s06_context", "compactor", "context_collapse_overlay", ContextCollapseOverlayCompactor)
    register_strategy("s06_context", "compactor", "autocompact_llm", AutocompactLLMCompactor)
    register_strategy("s06_context", "compactor", "cascade", CascadeCompactor)

    # s00_harness — transport (본문 LLM 호출; streaming 이 기본)
    from ..stages.strategies.transport import StreamingTransport, BatchTransport
    register_strategy("s00_harness", "transport", "streaming", StreamingTransport)
    register_strategy("s00_harness", "transport", "batch", BatchTransport)

    # s00_harness — retry (본문 LLM 호출 실패 재시도)
    register_strategy("s00_harness", "retry", "exponential_backoff", ExponentialBackoffRetry)
    register_strategy("s00_harness", "retry", "no_retry", NoRetry)

    # s07_act — executor, router
    register_strategy("s07_act", "executor", "sequential", SequentialToolExecutor)
    register_strategy("s07_act", "executor", "parallel", ParallelToolExecutor)
    register_strategy("s07_act", "router", "composite", CompositeToolRouter)
    register_strategy("s07_act", "router", "mcp", MCPToolRouter)
    register_strategy("s07_act", "router", "builtin", BuiltinToolRouter)

    # v1.0 — s08_judge stage 격하: evaluation 슬롯이 s08_decide 아래로 통합.
    # judge_then_loop strategy 가 이 evaluation impl 들을 호출.
    register_strategy("s08_decide", "evaluation", "llm_judge", LLMJudgeEvaluation)
    register_strategy("s08_decide", "evaluation", "rule_based", RuleBasedEvaluation)
    register_strategy("s08_decide", "evaluation", "none", NoValidation)

    # v1.0 — s09_decide → s08_decide 번호 시프트
    from ..stages.strategies._decide import ThresholdDecide, AlwaysPassDecide
    register_strategy("s08_decide", "decide", "threshold", ThresholdDecide)
    register_strategy("s08_decide", "decide", "always_pass", AlwaysPassDecide)

    # s03 — cache
    from ..stages.strategies.cache import AnthropicCacheStrategy, NoCacheStrategy
    register_strategy("s03_prompt", "cache", "anthropic_cache", AnthropicCacheStrategy)
    register_strategy("s03_prompt", "cache", "no_cache", NoCacheStrategy)

    # s00_harness — token tracker, thinking processor, response parser (본문 LLM 호출)
    from ..stages.strategies.token_tracker import DefaultTokenTracker, ModelPricingCalculator
    register_strategy("s00_harness", "token_tracker", "default", DefaultTokenTracker)
    register_strategy("s00_harness", "cost_calculator", "model_pricing", ModelPricingCalculator)

    from ..stages.strategies.thinking import DefaultThinkingProcessor, NoThinkingProcessor
    register_strategy("s00_harness", "thinking", "default", DefaultThinkingProcessor)
    register_strategy("s00_harness", "thinking", "disabled", NoThinkingProcessor)

    from ..stages.strategies.parser import AnthropicResponseParser, OpenAIResponseParser, DefaultCompletionDetector
    register_strategy("s00_harness", "parser", "anthropic", AnthropicResponseParser)
    register_strategy("s00_harness", "parser", "openai", OpenAIResponseParser)
    register_strategy("s00_harness", "completion_detector", "default", DefaultCompletionDetector)

    # 공통 — scorer (와일드카드)
    register_strategy("*", "scorer", "weighted", WeightedScorer)

    logger.debug("Registered %d strategies", len(_REGISTRY))

    # 플러그인 자동 발견 — 외부 패키지 setup.cfg 의 entry_points 지원
    _discover_plugin_strategies()


def _discover_plugin_strategies() -> None:
    """외부 패키지가 setup.cfg 에 등록한 Strategy 를 자동 발견.

    entry_point 형식: ``stage_id:slot_name:impl_name = package.module:ClassName``
    (ep.name = "stage_id:slot_name:impl_name", ep.value = 클래스 경로)
    """
    import sys
    try:
        if sys.version_info >= (3, 10):
            from importlib.metadata import entry_points
            eps = entry_points(group="xgen_harness.strategies")
        else:
            from importlib.metadata import entry_points
            eps = entry_points().get("xgen_harness.strategies", [])

        for ep in eps:
            try:
                parts = ep.name.split(":")
                if len(parts) != 3:
                    logger.warning(
                        "Strategy entry_point 이름 형식 오류 (stage_id:slot:impl 기대): %s",
                        ep.name,
                    )
                    continue
                stage_id, slot_name, impl_name = parts
                cls = ep.load()
                register_strategy(stage_id, slot_name, impl_name, cls)
                logger.info(
                    "Plugin strategy registered: %s/%s/%s (%s)",
                    stage_id, slot_name, impl_name, cls.__name__,
                )
            except Exception as e:
                logger.warning("Failed to load plugin strategy %s: %s", ep.name, e)
    except Exception as e:
        logger.debug("Plugin strategy discovery skipped (no entry_points backend): %s", e)
