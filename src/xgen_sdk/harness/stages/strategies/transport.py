"""
Transport Strategy 구현 — s00_harness.main_call 의 본문 LLM 호출 전송 방식.

v0.14.0: "streaming" / "batch" 을 Strategy 인스턴스로 분리. Pipeline/Stage 는
이름 리터럴을 모른 채 Strategy 에 전적 위임. 외부 플러그인이 신규 Transport
(websocket, caching_proxy 등) 를 entry_points 로 얹어도 코드 변경 0.

각 구현체는 TransportStrategy.call(state) 하나만 구현하면 된다. 내부 세부
(retries / 축약 / 이벤트 방출 / tool_use) 는 core/llm_call.py 헬퍼에 위임.
"""

from __future__ import annotations

from typing import Any

from ..interfaces import TransportStrategy


class StreamingTransport(TransportStrategy):
    """SSE 스트리밍 + 재시도 + 모델 폴백 + Prompt Caching."""

    @property
    def name(self) -> str:
        return "streaming"

    @property
    def description(self) -> str:
        return "SSE 스트리밍 + 재시도(429→10/20/40s, 529→1/2/4s) + 모델 폴백"

    async def call(self, state: Any) -> dict:
        from ...core.llm_call import call_main_llm_streaming
        return await call_main_llm_streaming(state, stage_id="s00_harness")


class BatchTransport(TransportStrategy):
    """비스트리밍 단일 호출. 스트리밍 미지원 환경 / 디버깅 / 짧은 응답용."""

    @property
    def name(self) -> str:
        return "batch"

    @property
    def description(self) -> str:
        return "비스트리밍 단일 호출 (스트리밍 미지원 환경, 짧은 응답, 디버깅)"

    async def call(self, state: Any) -> dict:
        from ...core.llm_call import call_main_llm_batch
        return await call_main_llm_batch(state, stage_id="s00_harness")
