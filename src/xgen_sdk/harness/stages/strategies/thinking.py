"""
Thinking strategies — Extended Thinking 처리

geny-harness s08_think 차용:
  ThinkingProcessor: thinking block을 처리

Anthropic extended thinking / OpenAI reasoning tokens 처리.
LLM 응답에서 thinking block을 추출, 가공, 이벤트 발행.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..interfaces import Strategy

logger = logging.getLogger("harness.strategy.thinking")


@dataclass
class ThinkingBlock:
    """thinking block 데이터"""
    content: str
    model: str = ""
    budget_tokens: int = 0
    actual_tokens: int = 0


class ThinkingProcessor(Strategy, ABC):
    """thinking block 처리 인터페이스"""

    @abstractmethod
    async def process(self, blocks: list[ThinkingBlock], state: Any) -> list[ThinkingBlock]:
        """raw thinking blocks → 처리된 blocks"""
        ...


class DefaultThinkingProcessor(ThinkingProcessor):
    """기본 thinking 처리 — 이벤트 발행 + 메타데이터 저장"""

    @property
    def name(self) -> str:
        return "default"

    async def process(self, blocks: list[ThinkingBlock], state: Any) -> list[ThinkingBlock]:
        if not blocks:
            return blocks

        from ...events.types import ThinkingEvent

        for block in blocks:
            # 이벤트 발행
            if hasattr(state, 'event_emitter') and state.event_emitter:
                await state.event_emitter.emit(ThinkingEvent(
                    content=block.content[:500],  # 너무 길면 잘라서 전송
                ))

            # 메타데이터에 저장
            if hasattr(state, 'metadata'):
                thinking_log = state.metadata.get('thinking_blocks', [])
                thinking_log.append({
                    'content': block.content,
                    'tokens': block.actual_tokens,
                })
                state.metadata['thinking_blocks'] = thinking_log

        total_tokens = sum(b.actual_tokens for b in blocks)
        logger.info("[Thinking] Processed %d blocks, %d tokens", len(blocks), total_tokens)
        return blocks


class NoThinkingProcessor(ThinkingProcessor):
    """thinking 비활성화 — block 무시"""

    @property
    def name(self) -> str:
        return "disabled"

    async def process(self, blocks: list[ThinkingBlock], state: Any) -> list[ThinkingBlock]:
        return []
