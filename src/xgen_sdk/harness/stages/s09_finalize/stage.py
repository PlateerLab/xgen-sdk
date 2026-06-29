"""
S09 Finalize — 최종 출력 포맷팅 + 메트릭스 + 저장 (v1.0)

v1.0 통합:
  - 구 s11_finalize → s09_finalize (번호 −2 시프트)
  - 구 s10_save stage 삭제 → 'persist' strategy 로 격하 흡수
  - 출력 포맷터는 외부 등록 가능 (박제 0): register_output_formatter()

Strategy:
  default  — 메트릭스 수집 + state.final_output 확정 (저장 X)
  persist  — default + DB 저장 (구 s10_save)
  noop     — 메트릭스만 발행, 출력 변형 X (디버깅)
"""

from __future__ import annotations

import logging
from typing import Callable

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState
from ...events.types import MetricsEvent

logger = logging.getLogger("harness.stage.finalize")


# ─── 출력 포맷터 레지스트리 (박제 풀기) ───────────────────────────────
# 키 = 포맷 이름, 값 = (state) -> str.
# stage_params.output_format 또는 active_strategies.s09_finalize 가
# 이 dict 의 키와 매칭되면 해당 포맷터 사용.

OutputFormatter = Callable[[PipelineState], str]


def _format_text(state: PipelineState) -> str:
    return state.final_output or state.last_assistant_text or ""


def _format_json(state: PipelineState) -> str:
    if not state.final_output:
        return ""
    import json as _json
    return _json.dumps(
        {
            "content": state.final_output,
            "model": state.provider.model_name if state.provider else "",
            "tokens": state.token_usage.total,
        },
        ensure_ascii=False,
        indent=2,
    )


def _format_markdown(state: PipelineState) -> str:
    if not state.final_output:
        return ""
    model = state.provider.model_name if state.provider else "unknown"
    return (
        f"## Response\n\n{state.final_output}\n\n---\n"
        f"*Model: {model} | Tokens: {state.token_usage.total}*"
    )


OUTPUT_FORMATTERS: dict[str, OutputFormatter] = {
    "text": _format_text,
    "json": _format_json,
    "markdown": _format_markdown,
}


def register_output_formatter(name: str, formatter: OutputFormatter) -> None:
    """출력 포맷터 등록. 외부 작업자가 자기 도메인 포맷 추가 가능.

    예) register_output_formatter("xml", lambda s: f"<r>{s.final_output}</r>")
        → stage_params.output_format = "xml" 로 선택.
    """
    OUTPUT_FORMATTERS[name] = formatter


_OUTPUT_FORMATTERS_DISCOVERED = False


def _discover_output_formatters_from_entry_points() -> None:
    """entry_points 그룹 ``xgen_harness.output_formatters`` 자동 발견. idempotent."""
    global _OUTPUT_FORMATTERS_DISCOVERED
    if _OUTPUT_FORMATTERS_DISCOVERED:
        return
    _OUTPUT_FORMATTERS_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.output_formatters"
        items = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])  # type: ignore[arg-type]
        for ep in items:
            try:
                fn = ep.load()
                if callable(fn):
                    register_output_formatter(ep.name, fn)
            except Exception as e:
                logger.warning("[output_formatters] entry_point %s 로드 실패: %s", ep.name, e)
    except Exception as e:
        logger.debug("[output_formatters] entry_points discovery 실패: %s", e)


_discover_output_formatters_from_entry_points()


class FinalizeStage(Stage):
    """최종 출력 포맷팅 + 메트릭스 + 선택적 DB 저장 (v1.0)."""

    @property
    def stage_id(self) -> str:
        return "s09_finalize"

    @property
    def order(self) -> int:
        return 9

    async def execute(self, state: PipelineState) -> dict:
        # 1. 최종 출력 확정 — 포맷터 결정
        # 우선순위: stage_params.output_format > active_strategies(이름이 포맷이면)
        strategy_name = (self.get_param("strategy", state, None) or "").strip().lower()
        fmt_name = self.get_param("output_format", state, "text")
        if not isinstance(fmt_name, str) or not fmt_name:
            fmt_name = "text"
        formatter = OUTPUT_FORMATTERS.get(fmt_name, OUTPUT_FORMATTERS["text"])

        if strategy_name == "noop":
            # 변형 없이 last_assistant_text 그대로 (디버깅 모드)
            state.final_output = state.last_assistant_text or ""
        else:
            state.final_output = formatter(state)

        # ── 빈 출력 fallback (v1.18.3) — retry 소진/마지막 턴 실패(provider 에러 등)로
        #   last_assistant_text 까지 비면 그대로 "" 를 확정해 다운스트림이 빈값을 받았다.
        #   런 중 만들어진 마지막 유효 산출물(직전 assistant 텍스트 → 마지막 도구 제출
        #   payload)로 폴백해, 부분 결과라도 보존한다. 유효 출력이 있으면 동작 불변.
        if not (state.final_output or "").strip():
            _fb = self._fallback_output(state)
            if _fb:
                state.final_output = _fb
                logger.warning(
                    "[Finalize] 최종 출력 빈값 → fallback 보존(len=%d) — "
                    "마지막 유효 응답/제출 payload 사용", len(_fb),
                )

        # 2. 메트릭스 이벤트
        metrics = self._build_metrics(state)
        if state.event_emitter:
            await state.event_emitter.emit(MetricsEvent(**metrics))
        logger.info(
            "[Finalize] %dms, %d tokens, $%.4f, %d LLM calls, %d tools, %d iterations",
            metrics["duration_ms"],
            metrics["total_tokens"],
            metrics["cost_usd"],
            metrics["llm_calls"],
            metrics["tools_executed"],
            metrics["iterations"],
        )

        result: dict = {
            "output_length": len(state.final_output),
            "format": fmt_name,
            "usage": {
                "input_tokens": state.token_usage.input_tokens,
                "output_tokens": state.token_usage.output_tokens,
            },
            **metrics,
        }

        # 3. persist strategy — DB 저장 (구 s10_save 격하 흡수)
        if strategy_name == "persist" or self.get_param("save_enabled", state, False):
            from .strategies.persist import persist_execution_record
            persist_result = await persist_execution_record(state, self.get_param)
            result["persisted"] = persist_result

        # 4. 기억 추출 (HP3) — persist 전략과 무관. 판정·저장은 등록된 콜백(이식)이 책임.
        if self.get_param("memory_extract", state, False):
            result["memory_extracted"] = await self._extract_memory(state)

        return result

    async def _extract_memory(self, state):
        try:
            import inspect
            from ...memory.memory_store import get_memory_extractor
            fn = get_memory_extractor()
            if fn is None:
                return None
            res = fn(state)
            if inspect.isawaitable(res):
                res = await res
            return int(res) if isinstance(res, (int, bool)) else None
        except Exception as e:
            logger.warning("[Finalize] memory_extract 실패 (graceful skip): %s", e)
            return None

    @staticmethod
    def _fallback_output(state: PipelineState) -> str:
        """빈 최종출력 폴백 — 런 중 마지막 유효 산출물을 찾는다.
        ① messages 역순: 마지막 비어있지 않은 assistant 텍스트(직전 turn 들의 답).
        ② tool_call_history 역순: 마지막 도구 호출의 input payload(submit 류 우선)
           — 제출은 이미 외부(예: Redis)에 반영됐으므로 그 내용을 텍스트로 보존.
        둘 다 없으면 ""(기존 동작)."""
        try:
            for m in reversed(getattr(state, "messages", None) or []):
                role = (m.get("role") if isinstance(m, dict) else getattr(m, "role", "")) or ""
                if str(role) != "assistant":
                    continue
                content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
                if isinstance(content, list):
                    text = "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = str(content or "")
                if text.strip():
                    return text.strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            import json as _json
            history = list(getattr(state, "tool_call_history", None) or [])
            # submit 류 우선, 없으면 마지막 호출
            ordered = (
                [h for h in reversed(history) if "submit" in str((h or {}).get("tool_name", ""))]
                + list(reversed(history))
            )
            for h in ordered:
                if not isinstance(h, dict):
                    continue
                ti = h.get("tool_input")
                if not ti:
                    continue
                body = ti if isinstance(ti, str) else _json.dumps(ti, ensure_ascii=False)
                if body.strip():
                    return (
                        f"[finalize-fallback] 최종 답변 누락 — 마지막 도구 제출 내용 보존 "
                        f"({h.get('tool_name')}):\n{body[:8000]}"
                    )
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _build_metrics(self, state: PipelineState) -> dict:
        return {
            "duration_ms": state.elapsed_ms,
            "total_tokens": state.token_usage.total,
            "input_tokens": state.token_usage.input_tokens,
            "output_tokens": state.token_usage.output_tokens,
            "cost_usd": round(state.cost_usd, 6),
            "llm_calls": state.llm_call_count,
            "tools_executed": state.tools_executed_count,
            "iterations": state.loop_iteration,
            "model": state.provider.model_name if state.provider else "",
        }

    def list_strategies(self) -> list[StrategyInfo]:
        return [
            StrategyInfo("default", "메트릭스 수집 + 출력 포맷팅", is_default=True),
            StrategyInfo("persist", "default + DB 저장 (구 s10_save 흡수)"),
            StrategyInfo("noop", "메트릭스만, 출력 변형 X (디버깅)"),
        ]


# 하위 호환 — 외부 import 보호
CompleteStage = FinalizeStage
