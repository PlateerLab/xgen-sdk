"""
Main LLM call helper

s00_harness 가 소유하는 "본문 LLM 호출" 로직. 재시도 / 스트리밍 / tool_use 감지 /
토큰 집계 / 컨텍스트 축약 / 비용 추정을 하나의 자유 함수 세트로 노출.
s00_harness.main_call 이 이 함수를 호출하고 Pipeline 은 loop 안에서 main_call 을 invoke.

호출 로직 자체는 Stage 와 무관한 순수 함수라 헬퍼로 두는 게 자연스럽다 — 외부에서
Provider 와 Stage 가 동일 함수를 재사용 가능.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .state import PipelineState, TokenUsage
from ..events.types import MessageEvent, ToolCallEvent, ThinkingEvent, RetryEvent
from ..errors import ProviderError, RateLimitError, OverloadError, PipelineAbortError
from ..providers.base import ProviderEventType

logger = logging.getLogger("harness.llm_call")

# 재시도 딜레이 (초)
RETRY_DELAYS = {
    "rate_limit": [10, 20, 40],
    "overload": [1, 2, 4],
    "server": [2, 4, 8],
}
DEFAULT_MAX_RETRIES = 3

# Loop budget hint — 본문 호출마다 system suffix 로 현재/최대 라운드 고지 (per-call 계산,
# 비축적 — state.system_prompt 불변). gate: stage_params["s00_harness"]["loop_budget_hint"]
# 또는 config.loop_budget_hint (기본 True). LLM-facing 텍스트라 영어.
LOOP_BUDGET_HINT_TEMPLATE = (
    "[Tool-use round {current} of {max}. If you already have enough information, "
    "stop calling tools and write the complete final answer now.]"
)
LOOP_BUDGET_HINT_FINAL_TEMPLATE = (
    "[Tool-use round {current} of {max}. This is the final round — do not call any "
    "more tools. You must write the complete final answer now.]"
)


async def call_main_llm_streaming(
    state: PipelineState,
    *,
    stage_id: str = "s00_harness",
) -> dict:
    """StreamingTransport.call 의 엔진 헬퍼. stream=True."""
    return await _call_main_llm(state, stage_id=stage_id, stream=True)


async def call_main_llm_batch(
    state: PipelineState,
    *,
    stage_id: str = "s00_harness",
) -> dict:
    """BatchTransport.call 의 엔진 헬퍼. stream=False."""
    return await _call_main_llm(state, stage_id=stage_id, stream=False)


async def _call_main_llm(
    state: PipelineState,
    *,
    stage_id: str,
    stream: bool,
) -> dict:
    """본문 LLM 호출 공통 로직. Transport Strategy 에서만 호출.

    Returns: {call_count, has_tool_calls, text_length, input_tokens, output_tokens}
    """
    from .provider_bootstrap import ensure_provider
    await ensure_provider(state, stage_id=stage_id)
    if not state.provider:
        raise PipelineAbortError("LLM provider not initialized", stage_id)

    has_tool_calls = False

    # 단일 LLM 호출. 도구 루프는 Pipeline 이 반복 (s00.main_call ↔ s07_act).
    # v0.16.2 — 죽은 변수 `call_count += 1` 제거 (UnboundLocalError 원인).
    # state.llm_call_count 가 실제 누적 카운터, 반환값의 call_count=1 은 단일 호출 표시.
    result_text, tool_calls, usage = await _call_with_retry(state, stage_id=stage_id, stream=stream)
    state.llm_call_count += 1

    # 토큰 사용량 업데이트
    state.token_usage += usage
    state.turn_usages.append(usage)
    state.cost_usd += _estimate_cost(usage, state.provider.model_name)

    state.last_assistant_text = result_text or ""

    # 도구 호출 감지
    if tool_calls:
        has_tool_calls = True
        state.pending_tool_calls = tool_calls
        content_blocks = []
        if result_text:
            content_blocks.append({"type": "text", "text": result_text})
        for tc in tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc["tool_use_id"],
                "name": tc["tool_name"],
                "input": tc["tool_input"],
            })
        state.add_message("assistant", content_blocks)
    else:
        if result_text:
            state.add_message("assistant", result_text)

    # verbose: LLM 응답 완료
    from ..events.types import StageSubstepEvent
    await state.emit_verbose(StageSubstepEvent(
        stage_id=stage_id, substep="llm_response_complete",
        meta={"has_tool_calls": has_tool_calls, "text_length": len(result_text),
              "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
              "stream": stream},
    ))

    return {
        "call_count": 1,
        "has_tool_calls": has_tool_calls,
        "text_length": len(result_text),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }


async def _call_with_retry(
    state: PipelineState,
    *,
    stage_id: str,
    stream: bool,
) -> tuple[str, list[dict], TokenUsage]:
    """재시도 로직 포함 LLM 호출."""
    config = state.config

    def _param(name: str, default):
        params = (config.stage_params.get(stage_id) if config else None) or {}
        v = params.get(name)
        return v if v is not None else default

    max_retries = int(_param("max_retries", DEFAULT_MAX_RETRIES))
    delays = {
        "rate_limit": list(_param("retry_delays_rate_limit", RETRY_DELAYS["rate_limit"])),
        "overload":   list(_param("retry_delays_overload",   RETRY_DELAYS["overload"])),
        "server":     list(_param("retry_delays_server",     RETRY_DELAYS["server"])),
    }

    def _pick(kind: str, attempt: int) -> int:
        seq = delays[kind] or RETRY_DELAYS[kind]
        return int(seq[min(attempt, len(seq) - 1)])

    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return await _single_call(state, stage_id=stage_id, stream=stream)
        except RateLimitError as e:
            last_error = e
            delay = _pick("rate_limit", attempt)
            logger.warning("[LLM] Rate limited, retry %d/%d after %ds", attempt + 1, max_retries, delay)
            await state.emit_verbose(RetryEvent(
                stage_id=stage_id,
                reason=f"RateLimitError → sleep {delay}s · {str(e)[:120]}",
                attempt=attempt + 1,
                max_attempts=max_retries,
            ))
            await asyncio.sleep(delay)
        except OverloadError as e:
            last_error = e
            delay = _pick("overload", attempt)
            logger.warning("[LLM] Overloaded, retry %d/%d after %ds", attempt + 1, max_retries, delay)
            await state.emit_verbose(RetryEvent(
                stage_id=stage_id,
                reason=f"OverloadError → sleep {delay}s · {str(e)[:120]}",
                attempt=attempt + 1,
                max_attempts=max_retries,
            ))
            await asyncio.sleep(delay)
        except ProviderError as e:
            if e.recoverable and attempt < max_retries:
                last_error = e
                delay = _pick("server", attempt)
                logger.warning("[LLM] Provider error, retry %d/%d after %ds", attempt + 1, max_retries, delay)
                await state.emit_verbose(RetryEvent(
                    stage_id=stage_id,
                    reason=f"ProviderError → sleep {delay}s · {str(e)[:120]}",
                    attempt=attempt + 1,
                    max_attempts=max_retries,
                ))
                await asyncio.sleep(delay)
            else:
                raise

    raise last_error or PipelineAbortError("LLM call failed after retries", stage_id)


def _resolve_effective_max_iterations(state: PipelineState) -> int:
    """Pipeline Phase B 의 effective_max_iter 와 동일 규칙 (orchestrator override → runtime floor)."""
    from .orchestrator_registry import get_orchestrator
    from .runtime_defaults import resolve_with_default

    orch_hint = ((state.metadata or {}).get("orchestrator_hint") or "").strip().lower()
    orch_spec = get_orchestrator(orch_hint) or get_orchestrator("iterative")
    if orch_spec and orch_spec.max_iterations_override is not None:
        return int(orch_spec.max_iterations_override)
    cfg_max = state.config.max_iterations if state.config else None
    return int(resolve_with_default(cfg_max, "max_iterations"))


def _loop_budget_hint(state: PipelineState, *, enabled: bool, tool_choice: Any) -> Optional[str]:
    """이번 호출에 붙일 loop budget hint. None=붙이지 않음 (게이트 OFF / 도구 없음 / 루프 밖)."""
    if not enabled or not state.tool_definitions:
        return None
    if tool_choice not in (None, "auto"):
        return None  # 강제 tool_choice(discovery_first 등)와 "지금 답하라" 는 모순
    current = int(getattr(state, "loop_iteration", 0) or 0)
    if current < 1:
        return None
    max_iter = _resolve_effective_max_iterations(state)
    if max_iter < 1:
        return None
    template = LOOP_BUDGET_HINT_FINAL_TEMPLATE if current >= max_iter else LOOP_BUDGET_HINT_TEMPLATE
    return template.format(current=current, max=max_iter)


async def _single_call(
    state: PipelineState,
    *,
    stage_id: str,
    stream: bool,
) -> tuple[str, list[dict], TokenUsage]:
    """단일 LLM API 호출 (streaming=True/False 공용)."""
    config = state.config
    provider = state.provider

    def _param(name: str, default):
        params = (config.stage_params.get(stage_id) if config else None) or {}
        v = params.get(name)
        return v if v is not None else default

    # v1.0.x — config 의 sentinel(None) 은 runtime_defaults 의 안전 바닥으로 폴백.
    # 외부 패키지가 register_runtime_default("max_tokens", N) 로 도메인 floor 설정.
    from .runtime_defaults import resolve_with_default
    cfg_max_tokens = resolve_with_default(config.max_tokens if config else None, "max_tokens")
    cfg_thinking_budget = resolve_with_default(
        config.thinking_budget_tokens if config else None, "thinking_budget_tokens", 10000,
    )

    max_tokens = int(_param("max_tokens", cfg_max_tokens))
    thinking_enabled = bool(_param("thinking_enabled", config.thinking_enabled if config else False))
    thinking_budget = int(_param("thinking_budget", cfg_thinking_budget))
    # temperature 는 정책 sentinel — None 이면 runtime default (0.7. 도메인이 strict 면
    # register_runtime_default('temperature', 0.0) 으로 override).
    temperature_raw = (config.temperature if config else None)
    temperature = temperature_raw if temperature_raw is not None else resolve_with_default(None, "temperature", 0.7)

    thinking = None
    if thinking_enabled and provider.supports_thinking():
        thinking = {"type": "enabled", "budget_tokens": thinking_budget}

    from ..providers import get_context_limit
    provider_name = getattr(provider, "provider_name", "") or (config.provider if config else "")
    context_limit = int(_param("context_limit", get_context_limit(provider_name)))
    state.messages = _truncate_messages_if_needed(state.messages, context_limit)

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    usage = TokenUsage()
    _output_tokens_recorded = False

    from ..events.types import StageSubstepEvent
    await state.emit_verbose(StageSubstepEvent(
        stage_id=stage_id, substep="llm_request_start",
        meta={"provider": provider.provider_name, "model": provider.model_name,
              "message_count": len(state.messages),
              "tools_count": len(state.tool_definitions or []),
              "stream": stream},
    ))

    # tool_choice circuit breaker (기존 s07 규칙 그대로)
    _tool_choice = None
    if state.tool_definitions:
        # discovery-first (4.8 tool-search-first) — 첫 main LLM 호출은 무조건 search_tools 로
        # 카탈로그를 연다. one-shot(pop): loop_iteration 은 첫 iteration 전에 1로 증가하므로
        # iteration 번호에 기대지 않고 메타 키를 한 번 소비한다. 약한 모델도 "tool search 선 →
        # 발견" 흐름을 타게 강제. 소비 후 둘째 호출부터 auto.
        _disc = (state.metadata or {}).pop("discovery_first_tool", None) if state.metadata else None
        if _disc:
            _tool_choice = _disc
            logger.info("[LLM] discovery_first → tool_choice=%s (one-shot, lead with tool search)", _disc)
        else:
            _tool_choice = (state.metadata or {}).get("force_tool_choice")
            if _tool_choice == "required" and getattr(state, "loop_iteration", 0) >= 1:
                logger.info(
                    "[LLM] force_tool_choice=required → auto (iter=%d, circuit breaker)",
                    getattr(state, "loop_iteration", 0),
                )
                _tool_choice = "auto"
    elif (state.metadata or {}).get("force_tool_choice"):
        logger.warning("[LLM] force_tool_choice set but no tool_definitions — ignored")

    # 출력 스키마 하드 강제 — 연결된 output_schema(config → stage_params.response_format)를
    # OpenAI-호환 provider 의 response_format 으로 전달. capability 로 분기(모델명 하드코딩 X):
    # 미지원 provider(예: Anthropic)는 None 으로 두어 system_prompt soft 지시로 폴백.
    _response_format = _param("response_format", None)
    if _response_format and not provider.supports_response_format():
        logger.info(
            "[LLM] response_format 설정됐으나 provider=%s 미지원 → system_prompt soft 폴백",
            getattr(provider, "provider_name", "?"),
        )
        _response_format = None

    # loop budget hint — per-call suffix, 원본 state.system_prompt 비변경 (비축적)
    _hint_gate = _param(
        "loop_budget_hint", getattr(config, "loop_budget_hint", None) if config else None,
    )
    _budget_hint = _loop_budget_hint(
        state,
        enabled=True if _hint_gate is None else bool(_hint_gate),
        tool_choice=_tool_choice,
    )
    _system = state.system_prompt or None
    if _budget_hint:
        _system = f"{_system}\n\n{_budget_hint}" if _system else _budget_hint

    async for event in provider.chat(
        messages=state.messages,
        system=_system,
        tools=state.tool_definitions or None,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=stream,
        thinking=thinking,
        tool_choice=_tool_choice,
        response_format=_response_format,
    ):
        if event.type == ProviderEventType.TEXT_DELTA:
            text_parts.append(event.text)
            if state.event_emitter:
                await state.event_emitter.emit(MessageEvent(text=event.text))

        elif event.type == ProviderEventType.THINKING_DELTA:
            if state.event_emitter:
                await state.event_emitter.emit(ThinkingEvent(text=event.text))

        elif event.type == ProviderEventType.TOOL_USE:
            tool_calls.append({
                "tool_use_id": event.tool_use_id,
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
            })
            if state.event_emitter:
                # v1.0 — tool_source 자동 채움 (state.metadata['tool_source_of'] 는 s04_tool 이 채움).
                # 사용자가 UI 에서 어떤 채널 (mcp/builtin/xgen_node/rag/외부) 의 도구인지 즉시 식별.
                _src = (state.metadata.get("tool_source_of") or {}).get(event.tool_name, "")
                await state.event_emitter.emit(ToolCallEvent(
                    tool_use_id=event.tool_use_id,
                    tool_name=event.tool_name,
                    tool_input=event.tool_input,
                    tool_source=_src,
                ))

        elif event.type == ProviderEventType.USAGE:
            usage.input_tokens += event.input_tokens
            usage.cache_creation_tokens += event.cache_creation_tokens
            usage.cache_read_tokens += event.cache_read_tokens
            if event.output_tokens and not _output_tokens_recorded:
                usage.output_tokens += event.output_tokens
                _output_tokens_recorded = True

        elif event.type == ProviderEventType.STOP:
            # v0.26.4 — batch transport (stream=False) 호환.
            # OpenAIProvider._batch_request 는 응답 text 를 STOP 이벤트의 .text 필드
            # 단일 chunk 로 전달. 이전엔 STOP 핸들러가 output_tokens 만 보고 text 를
            # 무시해서 batch 모드 실행 시 result_text 가 빈 채 → state.last_assistant_text=""
            # → state.final_output 도 빈 채로 사용자에게 도착 (라이브 검증으로 발견).
            if event.text and not text_parts:
                text_parts.append(event.text)
                if state.event_emitter:
                    await state.event_emitter.emit(MessageEvent(text=event.text))
            if event.output_tokens and not _output_tokens_recorded:
                usage.output_tokens += event.output_tokens
                _output_tokens_recorded = True

    result_text = "".join(text_parts)

    # output_tokens 보정 fallback (v0.11.22)
    if not _output_tokens_recorded and result_text:
        try:
            estimated, source = provider.count_tokens(result_text)
        except Exception as e:
            logger.warning("[LLM] provider.count_tokens fallback 실패: %s", e)
            estimated, source = 0, "failed"
        if estimated > 0:
            usage.output_tokens = estimated
            state.metadata.setdefault("output_tokens_sources", []).append(source)
            logger.info("[LLM] output_tokens=%d (source=%s, fallback)", estimated, source)

    return result_text, tool_calls, usage


def _truncate_messages_if_needed(messages: list[dict], char_limit: int) -> list[dict]:
    """메시지 총 문자 수가 한도를 초과하면 중간 메시지를 축약."""
    import json

    def _msg_chars(msg: dict) -> int:
        content = msg.get("content", "")
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(
                len(block.get("text", "")) if isinstance(block, dict) else len(str(block))
                for block in content
            )
        return len(json.dumps(content, ensure_ascii=False))

    total_chars = sum(_msg_chars(m) for m in messages)
    if total_chars <= char_limit or len(messages) <= 2:
        return messages

    logger.warning(
        "[LLM] Context size %d chars exceeds limit %d, truncating middle messages",
        total_chars, char_limit,
    )

    n = len(messages)
    keep_front = max(1, int(n * 0.4))
    keep_back = max(1, int(n * 0.4))

    if keep_front + keep_back >= n:
        return messages

    front = messages[:keep_front]
    back = messages[-keep_back:]
    removed_count = n - keep_front - keep_back

    truncation_notice = {
        "role": "user",
        "content": (
            f"[System: {removed_count} messages were truncated from the middle of the "
            f"conversation to fit within the context window. "
            f"Original total: {total_chars} chars, limit: {char_limit} chars.]"
        ),
    }

    return front + [truncation_notice] + back


def _estimate_cost(usage: TokenUsage, model: str) -> float:
    """토큰 사용량으로 비용 추정 (USD)"""
    from ..stages.strategies.token_tracker import PRICING

    pricing = PRICING.get(model)
    if not pricing:
        model_lower = model.lower()
        for key, val in PRICING.items():
            if key.lower() in model_lower or model_lower in key.lower():
                pricing = val
                break
    if not pricing:
        pricing = {"input": 3.0, "output": 15.0}

    input_cost = usage.input_tokens * pricing["input"] / 1_000_000
    output_cost = usage.output_tokens * pricing["output"] / 1_000_000

    cache_read_rate = pricing.get("cache_read", pricing["input"] * 0.1)
    cache_savings = usage.cache_read_tokens * (pricing["input"] - cache_read_rate) / 1_000_000
    return input_cost + output_cost - cache_savings


# ──────────────────────────────────────────────────────────────────────────
# Auxiliary LLM call — 보조 호출 통합 헬퍼 (v0.26.11)
# ──────────────────────────────────────────────────────────────────────────
#
# s06 compaction (L5 autocompact) / s08 judge / strategies/evaluation 등의 *보조*
# LLM 호출을 한 곳으로 모은다. 본문 호출 (_single_call) 과 분리한 이유:
#   - 보조 호출은 짧은 판정/요약이라 max_tokens 작음 (`config.aux_max_tokens`)
#   - tool_choice / 컨텍스트 truncation 회로 차단 같은 본문 정책 안 적용
#   - 단일 텍스트 응답만 필요 (tool_use 루프 없음)
#
# 일관성 보장:
#   - state.config.aux_max_tokens 단일 진실 소스 (코드에 박힌 매직넘버 0)
#   - state.llm_call_count += 1 자동 누적
#   - state.token_usage 누적 자동
#   - StageSubstepEvent (verbose) 자동 emit
#   - ensure_provider(state) 단일 lookup 경로
#
# 외부에서 행동 차이 필요 시 kwargs override:
#   max_tokens / temperature / system / response_format / model 모두 override 가능.
# ──────────────────────────────────────────────────────────────────────────


async def aux_call(
    state: PipelineState,
    *,
    stage_id: str,
    prompt: str,
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: float = 0.0,
    model: Optional[str] = None,
    provider: Optional[Any] = None,
) -> str:
    """보조 LLM 호출 — 짧은 판정/요약용. 응답 텍스트 반환.

    Parameters
    ----------
    state : PipelineState
    stage_id : str
        호출자 식별 (verbose 이벤트 + 로그 추적용). 예: "s08_decide", "s06_context.l5", "evaluation.judge_then_loop".
    prompt : str
        사용자 메시지. system 따로 받음.
    system : Optional[str]
        system_prompt override. None 이면 provider default.
    max_tokens : Optional[int]
        None 이면 ``state.config.aux_max_tokens`` 사용 (단일 진실 소스).
    temperature : float
        보조 호출은 결정성 우선이라 default 0.0.
    model : Optional[str]
        provider 기본 모델 override. None 이면 provider 의 model_name.
    provider : Optional[LLMProvider]
        v1.9.0 P0#3 — 명시 provider 인스턴스. None 이면 state.provider (본문 provider).
        judge_then_loop 이 ``resolve_judge_provider`` 로 얻은 별도 인스턴스 전달
        가능 → "Judge 가 자기 답 자기 평가" 약점 회피.
    """
    from .provider_bootstrap import ensure_provider
    from ..events.types import StageSubstepEvent

    if provider is None:
        await ensure_provider(state, stage_id=stage_id)
        provider = state.provider
    if not provider:
        raise PipelineAbortError("LLM provider not initialized", stage_id)

    config = state.config
    # aux_max_tokens is a present-but-None sentinel by default, so getattr's
    # fallback never fires; resolve None to the aux floor or int() crashes.
    from .runtime_defaults import resolve_with_default
    cfg_aux = resolve_with_default(
        getattr(config, "aux_max_tokens", None) if config else None, "aux_max_tokens", 500,
    )
    effective_max_tokens = int(max_tokens) if max_tokens is not None else int(cfg_aux)

    await state.emit_verbose(StageSubstepEvent(
        stage_id=stage_id, substep="aux_llm_request_start",
        meta={
            "provider": provider.provider_name,
            "model": model or provider.model_name,
            "max_tokens": effective_max_tokens,
            "temperature": temperature,
        },
    ))

    text_parts: list[str] = []
    usage = TokenUsage()

    chat_kwargs: dict = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": effective_max_tokens,
        "stream": False,
        "tools": None,
    }
    if system:
        chat_kwargs["system"] = system
    if model:
        chat_kwargs["model"] = model

    try:
        async for event in provider.chat(**chat_kwargs):
            if event.type == ProviderEventType.TEXT_DELTA and getattr(event, "text", None):
                text_parts.append(event.text)
            elif event.type == ProviderEventType.STOP:
                if getattr(event, "text", None):
                    text_parts.append(event.text)
                event_usage = getattr(event, "usage", None)
                if event_usage:
                    usage.input_tokens += int(event_usage.get("input_tokens", 0))
                    usage.output_tokens += int(event_usage.get("output_tokens", 0))
                    usage.cache_creation_tokens += int(event_usage.get("cache_creation_input_tokens", 0))
                    usage.cache_read_tokens += int(event_usage.get("cache_read_input_tokens", 0))
    except Exception as e:
        logger.warning("[aux_call] %s 실패: %s", stage_id, e)
        await state.emit_verbose(StageSubstepEvent(
            stage_id=stage_id, substep="aux_llm_request_failed",
            meta={"error": str(e)[:200]},
        ))
        raise

    # 누적 — 본문 호출과 같은 카운터 (예산·비용 정합)
    state.llm_call_count += 1
    if state.token_usage:
        state.token_usage.input_tokens += usage.input_tokens
        state.token_usage.output_tokens += usage.output_tokens
        state.token_usage.cache_creation_tokens += usage.cache_creation_tokens
        state.token_usage.cache_read_tokens += usage.cache_read_tokens
    if usage.output_tokens or usage.input_tokens:
        state.cost_usd += _estimate_cost(usage, provider.model_name)

    await state.emit_verbose(StageSubstepEvent(
        stage_id=stage_id, substep="aux_llm_request_end",
        meta={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "text_length": sum(len(t) for t in text_parts),
        },
    ))

    return "".join(text_parts).strip()
