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

        # ── 정책 차단 집행 (BUG-1 fix) ──────────────────────────────────
        # Policy Gate(s05)가 block severity 위반을 탐지하면 state.policy_block_reason
        # 과 state.policy_block_severity 를 세팅한다. content block 은 PipelineAbortError
        # 를 던지지 않아 egress 가 그대로 진행되므로, 여기서 최종 출력을 억제해야 위반
        # 텍스트(PII/금지패턴 포함)가 유출되지 않는다. severity=="warn" 이면 통과.
        self._enforce_policy_block(state)

        # ── 빈 출력 처리 (v1.18.3 → 합성 우선 v1.x) ──────────────────────────
        #   에이전트가 마지막 턴에 텍스트 합성 없이 도구호출로만 끝나면 final_output 이
        #   빈값이 된다(약모델 실패모드: 도구수집만 하고 최종답 합성 안 함).
        #   ① 먼저 aux LLM 로 "대화·도구결과 근거로 최종답을 지금 작성" 을 1회 합성한다
        #      (finalize 의 자연스러운 책임 — 원시 도구제출 보존보다 항상 나음).
        #   ② 합성이 실패/빈값일 때만 기존 _fallback_output(마지막 유효 응답/제출 payload)로.
        #   합성은 stage_param `finalize_synthesize`(기본 True) 로 on/off (하드코딩 아님).
        if not (state.final_output or "").strip():
            synthesized = ""
            if bool(self.get_param("finalize_synthesize", state, True)):
                synthesized = await self._synthesize_final_output(state)
            if synthesized:
                state.final_output = synthesized
                logger.info(
                    "[Finalize] 최종 출력 빈값 → aux 합성으로 최종답 생성(len=%d) — "
                    "에이전트가 텍스트 없이 도구호출로 종료됨", len(synthesized),
                )
            else:
                _fb = self._fallback_output(state)
                if _fb:
                    state.final_output = _fb
                    logger.warning(
                        "[Finalize] 최종 출력 빈값 → fallback 보존(len=%d) — "
                        "합성 미수행/실패, 마지막 유효 응답/제출 payload 사용", len(_fb),
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

    # 정책 차단 시 최종 출력을 대체할 메시지의 단 하나의 default.
    # stage_param `block_message` 로 override 가능 (매직 스트링 하드코딩 아님 —
    # 값은 config 우선, 없을 때만 이 default 사용).
    _DEFAULT_BLOCK_MESSAGE = (
        "요청하신 응답이 콘텐츠 정책에 의해 차단되었습니다."
    )

    def _enforce_policy_block(self, state: PipelineState) -> None:
        """Policy Gate 가 세팅한 block 신호를 소비해 최종 출력을 억제.

        - severity=="block" 이면: state.final_output 을 config 의 block_message 로 대체.
        - severity=="warn"/미차단이면: no-op (통과, 로그는 s05 에서 이미 남김).
        차단 메시지는 stage_param `block_message` 에서 오며, 없으면 default 하나만 사용.
        """
        reason = getattr(state, "policy_block_reason", "") or ""
        if not reason:
            return
        severity = (getattr(state, "policy_block_severity", "") or "").strip().lower()
        # severity 신호가 명시됐고 block 이 아니면 통과 (warn/info).
        if severity and severity != "block":
            return

        guard = getattr(state, "policy_block_guard", "") or ""
        # 차단 메시지 = config 우선(override 가능), 없으면 default 하나.
        block_message = self.get_param("block_message", state, self._DEFAULT_BLOCK_MESSAGE)
        if not isinstance(block_message, str) or not block_message.strip():
            block_message = self._DEFAULT_BLOCK_MESSAGE

        suppressed_len = len(state.final_output or "")
        state.final_output = block_message

        # 무엇을(reason=패턴/PII 종류) · 어느 가드 · severity · 출력 억제됨 을 명시 로그.
        logger.warning(
            "[Finalize] 정책 차단 집행 — guard=%s, severity=%s, reason=%s "
            "→ 최종 출력 억제(원본 %d자 폐기, block_message 대체)",
            guard or "?", severity or "block", reason, suppressed_len,
        )

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

    # 합성 프롬프트 default — stage_param `finalize_synthesize_instruction` 로 override 가능.
    _DEFAULT_SYNTHESIZE_INSTRUCTION = (
        "Based on the conversation and tool results above, write the complete final "
        "answer for the user now. Do not call any tools; produce the finished answer "
        "text only."
    )

    async def _synthesize_final_output(self, state: PipelineState) -> str:
        """빈 최종출력 시 aux LLM 로 최종답을 1회 합성.

        에이전트가 도구수집만 하고 텍스트 합성 없이 끝난 경우, 대화·도구결과를 근거로
        사용자에게 줄 완결된 최종답을 aux_call 로 만든다. 도구 없이 순수 텍스트 요청(재귀
        금지, 1회만). 실패/빈값이면 "" 반환 → 호출부가 기존 fallback 으로 넘어감.
        """
        try:
            from ...core.llm_call import aux_call
            from ...core.runtime_defaults import resolve_with_default

            transcript = self._render_transcript(state)
            if not transcript.strip():
                return ""
            instruction = self.get_param(
                "finalize_synthesize_instruction", state, self._DEFAULT_SYNTHESIZE_INSTRUCTION,
            )
            if not isinstance(instruction, str) or not instruction.strip():
                instruction = self._DEFAULT_SYNTHESIZE_INSTRUCTION
            prompt = f"{transcript}\n\n{instruction}"

            # 최종답은 완결 텍스트라 aux floor(500)가 아니라 본문 응답 예산(config.max_tokens)을
            # 존중한다. config sentinel(None)은 runtime default 로 폴백.
            cfg = state.config
            max_tokens = int(resolve_with_default(
                cfg.max_tokens if cfg else None, "max_tokens",
            ))

            return await aux_call(
                state,
                stage_id=f"{self.stage_id}.synthesize",
                prompt=prompt,
                max_tokens=max_tokens,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[Finalize] 최종답 합성 실패 (graceful → fallback): %s", e)
            return ""

    @staticmethod
    def _render_transcript(state: PipelineState) -> str:
        """state.messages(대화 + 도구결과)를 합성 프롬프트용 텍스트로 평탄화.

        aux_call 은 단일 prompt 문자열만 받으므로 구조화 메시지를 role 라벨 텍스트로 편다.
        assistant 텍스트 · tool_use(도구호출) · tool_result(도구결과) · user 텍스트 보존.
        """
        import json as _json

        lines: list[str] = []
        for m in getattr(state, "messages", None) or []:
            role = (m.get("role") if isinstance(m, dict) else getattr(m, "role", "")) or ""
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            if isinstance(content, str):
                if content.strip():
                    lines.append(f"{role}: {content.strip()}")
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    txt = str(block or "").strip()
                    if txt:
                        lines.append(f"{role}: {txt}")
                    continue
                btype = block.get("type")
                if btype == "text":
                    txt = str(block.get("text", "")).strip()
                    if txt:
                        lines.append(f"{role}: {txt}")
                elif btype == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", "")
                    body = inp if isinstance(inp, str) else _json.dumps(inp, ensure_ascii=False)
                    lines.append(f"{role} [tool_call {name}]: {body}")
                elif btype == "tool_result":
                    inner = block.get("content", "")
                    if isinstance(inner, list):
                        inner = "".join(
                            b.get("text", "") for b in inner
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    body = inner if isinstance(inner, str) else _json.dumps(inner, ensure_ascii=False)
                    if str(body).strip():
                        lines.append(f"[tool_result]: {str(body).strip()}")
        return "\n".join(lines)

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
