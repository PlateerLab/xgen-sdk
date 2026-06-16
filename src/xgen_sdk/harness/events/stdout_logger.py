"""stdout 이벤트 로거 — pipe 1 줄 박으면 모든 이벤트 stream.

v1.10.4+ 트리거: transpile 산출물의 `build_pipeline(enable_logging=True)` 가
편의 helper 였지만, 사용자가 커스텀 provider / 커스텀 ToolSource 를 직접 주입
하느라 `Pipeline.from_config` 를 직접 호출하는 경로에서는 로깅 helper 가 닿지
않음. PD 메타 도구 / 서브에이전트 / judge 동작은 로그 스트리밍이 핵심인데
manual-wire 경로가 이 신호를 못 받으면 실전 디버깅이 막힘.

해결: 엔진 본체에 1 줄 helper 박음. 어떤 방식으로 Pipeline 인스턴스를 받든
`enable_stdout_logging(pipe)` 한 줄이면 stdout 으로 모든 이벤트가 흘러나옴.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from .emitter import EventEmitter


# unsubscribe callback — 사용자가 추후 정리 가능.
Unsubscribe = Callable[[], None]


def enable_stdout_logging(
    target: Any,
    *,
    max_text_len: int = 80,
) -> Unsubscribe:
    """Pipeline / EventEmitter 의 모든 이벤트를 stdout 으로 한 줄씩 출력.

    Args:
        target: ``Pipeline`` (``.event_emitter`` 자동 추출) 또는 ``EventEmitter``.
            Pipeline 인스턴스를 그대로 넘기는 게 일반적 사용.
        max_text_len: 이벤트의 ``content`` / ``text`` 필드를 stdout 으로 출력할 때
            잘라낼 최대 길이. 너무 길면 가독성 손상. 0 이면 무제한.

    Returns:
        ``unsubscribe()`` 콜러블. long-running 환경에서 명시적 정리할 때 호출.

    Output format:
        ``[EventClass] stage=<id> tool=<name> text=<...>``

        - stage: ``StageEnterEvent`` / ``StageExitEvent`` 등에 박힌 stage id.
        - tool: ``ToolCallEvent`` / ``ToolResultEvent`` 의 tool 이름.
        - text: ``MessageEvent.content`` 또는 ``ThinkingEvent.text`` 등 (잘려 표시).

    Example:
        >>> from xgen_harness import Pipeline, PipelineState, enable_stdout_logging
        >>> pipe = Pipeline.from_config(config, provider=my_vllm_provider)
        >>> enable_stdout_logging(pipe)
        >>> result = await pipe.run(PipelineState(user_input="..."))
        # → stdout 에 [StageEnterEvent] stage=s06_context ... 같은 라인이 흘러나옴.
    """
    emitter = _resolve_emitter(target)

    async def _log(event: Any) -> None:
        name = type(event).__name__
        stage = getattr(event, "stage_id", "") or ""
        tool = getattr(event, "tool_name", "") or ""
        # content > text 순서로 우선 노출.
        text = getattr(event, "content", None) or getattr(event, "text", None)
        parts: list[str] = []
        if stage:
            parts.append(f"stage={stage}")
        if tool:
            parts.append(f"tool={tool}")
        if text:
            text_str = str(text)
            if max_text_len and len(text_str) > max_text_len:
                text_str = text_str[:max_text_len] + "…"
            parts.append(f"text={text_str!r}")
        suffix = (" " + " ".join(parts)) if parts else ""
        print(f"[{name}]{suffix}", flush=True)

    emitter.subscribe(_log)

    def _unsubscribe() -> None:
        try:
            unsub = getattr(emitter, "unsubscribe", None)
            if callable(unsub):
                unsub(_log)
        except Exception:
            pass

    return _unsubscribe


def _resolve_emitter(target: Any) -> EventEmitter:
    """Pipeline 또는 EventEmitter 모두 수용."""
    if isinstance(target, EventEmitter):
        return target
    emitter = getattr(target, "event_emitter", None)
    if isinstance(emitter, EventEmitter):
        return emitter
    raise TypeError(
        "enable_stdout_logging 의 target 은 Pipeline 또는 EventEmitter 여야 합니다. "
        f"받은 타입: {type(target).__name__}"
    )
