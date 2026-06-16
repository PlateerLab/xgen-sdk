"""Progressive Disclosure 기반 context compactor 들.

state/pd_stores/provider 에 접근이 필요한 압축 전략(L3 Microcompact · L4 Context
Collapse · L5 Autocompact · Cascade)을 별도 모듈로 분리. 기존 stateless compactor
(TokenBudget/SlidingWindow)는 `compactor.py` 에 남아 인터페이스 분리 유지.

v0.11.24 — audit 지적 "Stage 비공개 헬퍼 (_try_*) 호출" 해소.
Stage 에 공개된 public 계약 `try_microcompact` / `try_context_collapse` /
`try_autocompact` / `try_cascade` 만 의존하도록 교체. 외부 기여자는 같은
공개 API 를 재사용하거나, state.messages / state.pd_stores 를 자체 로직으로
조작해 완전히 다른 압축 전략을 구현할 수 있다.

외부 기여자 훅:
    새 AdvancedContextCompactor 구현체를 만들고
    `register_strategy("s06_context", "compactor", "<name>", MyCls)` 로 등록하면
    사용자가 `stage_params.s06_context.strategy="<name>"` 로 활성화할 수 있다.
"""

from abc import ABC, abstractmethod
from typing import Any

from ..interfaces import Strategy


class AdvancedContextCompactor(Strategy, ABC):
    """state 전체를 받아 pd_stores·provider 를 조작하는 compactor.

    기존 `ContextCompactor` 는 (messages, system_prompt, budget, max_tokens) → tuple
    시그니처라 L3/L4/L5 처럼 pd_stores 에 원본 보존하거나 child LLM 을 호출하는
    전략을 표현 못 한다. Advanced 는 state + stage 를 그대로 받아 side-effect 를 일으킨다.
    """

    @abstractmethod
    async def apply(
        self,
        *,
        state: Any,
        stage: Any,
        budget_used: float,
        results: dict,
    ) -> None:
        """state 를 in-place 로 수정하고 results dict 에 요약 필드를 기록."""
        ...


class MicrocompactCompactor(AdvancedContextCompactor):
    """L3 — 오래된 tool_result 블록만 placeholder 로 교체 (비파괴)."""

    @property
    def name(self) -> str:
        return "microcompact"

    @property
    def description(self) -> str:
        return "L3 — 오래된 tool_result 만 placeholder 로 교체 (pd_stores 원본 보존)"

    async def apply(self, *, state, stage, budget_used, results) -> None:
        stage.try_microcompact(state, budget_used, results)


class ContextCollapseOverlayCompactor(AdvancedContextCompactor):
    """L4 — 중간 메시지를 overlay 로 접고 원본은 pd_stores['history'] 에 보존."""

    @property
    def name(self) -> str:
        return "context_collapse_overlay"

    @property
    def description(self) -> str:
        return "L4 — 중간 메시지 overlay 압축 (비파괴, fetch_pd 로 복원 가능)"

    async def apply(self, *, state, stage, budget_used, results) -> None:
        stage.try_context_collapse(state, budget_used, results)


class AutocompactLLMCompactor(AdvancedContextCompactor):
    """L5 — child LLM 9-section 요약으로 교체 (비파괴, 회로 차단)."""

    @property
    def name(self) -> str:
        return "autocompact_llm"

    @property
    def description(self) -> str:
        return "L5 — child LLM 9-section 요약 (비파괴, 실패 3회 시 circuit breaker)"

    async def apply(self, *, state, stage, budget_used, results) -> None:
        await stage.try_autocompact(state, budget_used, results)


class CascadeCompactor(AdvancedContextCompactor):
    """Claude Code cascade — 임계별 L3→L4→L5 자동 에스컬레이션."""

    @property
    def name(self) -> str:
        return "cascade"

    @property
    def description(self) -> str:
        return "임계별 L3→L4→L5 자동 에스컬레이션 (Claude Code 패턴, 한 턴 1 전략)"

    async def apply(self, *, state, stage, budget_used, results) -> None:
        await stage.try_cascade(state, budget_used, results)
