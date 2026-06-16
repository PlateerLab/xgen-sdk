"""Stateless ContextCompactor 구현체 (messages/system_prompt 만 입출력).

state/pd_stores/provider 접근이 필요한 PD 기반 compactor 는 `compactor_pd.py` 로 이주.
이 모듈은 순수 (messages, system_prompt, budget, max_tokens) → tuple 시그니처만 담는다.
"""

import logging

from ..interfaces import ContextCompactor

# Advanced* 는 compactor_pd 로 이주했지만 기존 import 경로 유지 위해 re-export.
# 외부 코드가 `from xgen_harness.stages.strategies.compactor import CascadeCompactor` 쓰던 경우
# 그대로 동작하도록 하위 호환.
from .compactor_pd import (  # noqa: F401 — re-export for backward compat
    AdvancedContextCompactor,
    MicrocompactCompactor,
    ContextCollapseOverlayCompactor,
    AutocompactLLMCompactor,
    CascadeCompactor,
)

logger = logging.getLogger("harness.strategy.compactor")

CHARS_PER_TOKEN = 3


class TokenBudgetCompactor(ContextCompactor):
    """토큰 예산 기반 3단계 압축 — 기본 전략."""

    @property
    def name(self) -> str:
        return "token_budget"

    @property
    def description(self) -> str:
        return "토큰 예산 기반 3단계 압축"

    async def compact(
        self,
        messages: list[dict],
        system_prompt: str,
        budget_tokens: int,
        max_tokens: int,
    ) -> tuple[list[dict], str, bool]:
        available = budget_tokens - max_tokens
        total_chars = len(system_prompt)
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total_chars += len(str(block.get("text", "")))
                        total_chars += len(str(block.get("content", "")))

        estimated = total_chars // CHARS_PER_TOKEN
        budget_used = estimated / available if available > 0 else 1.0
        compacted = False

        # 1단계: 오래된 메시지 제거
        if budget_used > 0.8 and len(messages) > 4:
            messages = [messages[0]] + messages[-3:]
            compacted = True
            logger.info("[Compactor] Kept first + last 3 messages")

        # 2단계: 저우선순위 시스템 프롬프트 섹션 제거
        if budget_used > 0.9 and "<previous_results>" in system_prompt:
            start = system_prompt.find("<previous_results>")
            end = system_prompt.find("</previous_results>")
            if start >= 0 and end >= 0:
                system_prompt = system_prompt[:start] + system_prompt[end + len("</previous_results>"):]
                compacted = True
                logger.info("[Compactor] Removed previous_results section")

        return messages, system_prompt, compacted


class SlidingWindowCompactor(ContextCompactor):
    """슬라이딩 윈도우 — 최근 N개 메시지만 유지."""

    def __init__(self, window_size: int = 10):
        self._window = window_size

    @property
    def name(self) -> str:
        return "sliding_window"

    @property
    def description(self) -> str:
        return f"슬라이딩 윈도우 (최근 {self._window}개 메시지)"

    def configure(self, config: dict) -> None:
        self._window = config.get("window_size", self._window)

    async def compact(
        self,
        messages: list[dict],
        system_prompt: str,
        budget_tokens: int,
        max_tokens: int,
    ) -> tuple[list[dict], str, bool]:
        if len(messages) <= self._window:
            return messages, system_prompt, False
        messages = messages[-self._window:]
        return messages, system_prompt, True
