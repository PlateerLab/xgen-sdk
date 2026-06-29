"""Stateful loop seam — 엔진=중립 메커니즘, 이식=정책.

루프가 매 iteration 마다 (1) 외부 state 를 LLM 컨텍스트로 주입하고(read),
(2) 그 iteration 의 과정을 외부에 기록(write)할 수 있는 두 훅을 제공한다.
구현체(StateProvider/StateRecorder)는 이식측이 `state.metadata` 로 주입하며,
미주입이면 전부 no-op — 기존 동작 무변. 어떤 예외도 런을 깨지 않는다.
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("harness.state_loop")

STATE_VIEW_OPEN = "<state_view>"
STATE_VIEW_CLOSE = "</state_view>"

PROVIDER_KEY = "state_provider"
RECORDER_KEY = "state_recorder"


@runtime_checkable
class StateProvider(Protocol):
    """매 iteration LLM 이 볼 state 뷰(정제기억·작업기억·spine)를 합성해 반환."""

    def get_state_view(self, state) -> Optional[str]: ...


@runtime_checkable
class StateRecorder(Protocol):
    """iteration 경계에서 그 회차 과정을 외부(활동현황·정제기억·spine)에 기록."""

    def record_iteration(self, state, decision: str) -> None: ...


def apply_state_view(state) -> None:
    """provider 가 있으면 state 뷰를 system_prompt 의 관리 블록으로 갱신 주입(idempotent)."""
    provider = (getattr(state, "metadata", None) or {}).get(PROVIDER_KEY)
    if provider is None:
        return
    try:
        view = provider.get_state_view(state)
    except Exception as e:
        logger.debug("[state_loop] get_state_view skipped: %s", e)
        return
    sp = state.system_prompt or ""
    sp = _strip_block(sp)
    if view and view.strip():
        block = f"{STATE_VIEW_OPEN}\n{view.strip()}\n{STATE_VIEW_CLOSE}"
        sp = (sp + ("\n\n" if sp else "") + block)
    state.system_prompt = sp


def record_iteration(state, decision: str) -> None:
    """recorder 가 있으면 이 iteration 의 과정을 기록(C3). 실패는 무음."""
    recorder = (getattr(state, "metadata", None) or {}).get(RECORDER_KEY)
    if recorder is None:
        return
    try:
        recorder.record_iteration(state, decision)
    except Exception as e:
        logger.debug("[state_loop] record_iteration skipped: %s", e)


def _strip_block(text: str) -> str:
    """이전 회차의 <state_view> 관리 블록 제거 — 누적 방지, 최신만 유지."""
    if STATE_VIEW_OPEN not in text:
        return text
    start = text.find(STATE_VIEW_OPEN)
    end = text.find(STATE_VIEW_CLOSE)
    if end < 0:
        return text[:start].rstrip()
    return (text[:start] + text[end + len(STATE_VIEW_CLOSE):]).strip()
