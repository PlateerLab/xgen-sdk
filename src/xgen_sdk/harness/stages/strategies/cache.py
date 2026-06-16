"""
Cache strategies — Prompt Caching

geny-harness s05_cache 차용:
  CacheStrategy: 메시지에 cache_control 마커 적용

Anthropic prompt caching:
  시스템 프롬프트에 cache_control 마커를 붙이면
  동일 프롬프트 재사용 시 비용 90% 절감.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from ..interfaces import Strategy

logger = logging.getLogger("harness.strategy.cache")


class CacheStrategy(Strategy, ABC):
    """Prompt caching 마커 적용 인터페이스"""

    @abstractmethod
    def apply_cache_markers(self, state: Any) -> None:
        """state의 메시지/시스템 프롬프트에 cache_control 마커 적용"""
        ...


class AnthropicCacheStrategy(CacheStrategy):
    """Anthropic prompt caching 적용

    시스템 프롬프트의 마지막 블록에 cache_control: ephemeral 추가.
    동일 프롬프트 반복 시 TTL 5분 내 캐시 히트.
    """

    @property
    def name(self) -> str:
        return "anthropic_cache"

    def apply_cache_markers(self, state: Any) -> None:
        if not hasattr(state, 'messages') or not state.messages:
            return

        # 시스템 프롬프트에 cache_control 적용
        if hasattr(state, 'system_prompt') and state.system_prompt:
            # Anthropic API: system이 list of content blocks인 경우
            # 마지막 블록에 cache_control 추가
            if not hasattr(state, '_cache_markers_applied'):
                state.metadata['cache_control'] = {
                    'type': 'ephemeral',
                    'applied_to': 'system_prompt',
                }
                state._cache_markers_applied = True
                logger.info("[Cache] Anthropic cache markers applied to system prompt")

        # 도구 정의에도 cache_control 적용 (도구 수 많을 때 효과적)
        if hasattr(state, 'tool_definitions') and len(state.tool_definitions) > 3:
            state.metadata['tools_cache_control'] = {'type': 'ephemeral'}
            logger.info("[Cache] Cache markers applied to %d tool definitions", len(state.tool_definitions))


class NoCacheStrategy(CacheStrategy):
    """캐싱 비활성화"""

    @property
    def name(self) -> str:
        return "no_cache"

    def apply_cache_markers(self, state: Any) -> None:
        pass
