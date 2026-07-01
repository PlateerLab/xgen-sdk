"""
S07 Act — 도구 실행 (v0.14.0 번호 시프트: s08_act → s07_act)

- pending_tool_calls에 있는 도구 호출을 실행 (s00.main_call 이 생성)
- read_only 도구: asyncio.gather로 병렬 실행
- write 도구: 순차 실행
- 결과를 tool_results에 적재 → messages에 user 메시지로 추가
- 50K 문자 예산 초과 시 결과 축약
"""

import asyncio
import json
import logging
import traceback
from typing import Any, Optional

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState
from ...events.types import ToolResultEvent, StageSubstepEvent
from ...errors import ToolError, ToolTimeoutError

logger = logging.getLogger("harness.stage.execute")

TOOL_TIMEOUT_DEFAULT = 60.0
RESULT_BUDGET_DEFAULT = 50_000
# dedup 캐시 항목당 저장 상한(chars) — stage_param `tool_dedup_cache_max_chars` 로 override.
DEDUP_CACHE_ENTRY_MAX_CHARS = 8_000
# dedup 제외 메타도구 — 카탈로그/PD 스토어가 런 중 변해 동일입력≠동일결과 (promote-on-search),
# fetch_pd 는 truncate 저장 시 원본 재접근 목적이 깨짐.
DEDUP_EXEMPT_TOOLS = frozenset({"search_tools", "discover_tools", "fetch_pd"})
DEDUP_CACHED_PREFIX = "(cached: identical call already executed this run)\n"


class ExecuteStage(Stage):
    """도구 실행 스테이지"""

    @property
    def stage_id(self) -> str:
        return "s07_act"

    @property
    def role(self) -> str:
        # v0.16.6 — Pipeline 이 이 Stage 직전에 본문 LLM 호출(planner.main_call)을 주입.
        # 역할 이름은 "main_actor" — 외부 Stage 가 자기를 같은 role 로 바꿔 끼우면 자동.
        return "main_actor"

    @property
    def order(self) -> int:
        return 7

    def should_bypass(self, state: PipelineState) -> bool:
        return not state.pending_tool_calls

    async def execute(self, state: PipelineState) -> dict:
        if not state.pending_tool_calls:
            return {"tools_executed": 0, "bypassed": True}

        # stage_params에서 설정 읽기 (UI 설정 > stage_config 기본값 > 하드코딩)
        self._tool_timeout = self.get_param("timeout", state, TOOL_TIMEOUT_DEFAULT)
        result_budget = self.get_param("result_budget", state, RESULT_BUDGET_DEFAULT)

        tool_calls = state.pending_tool_calls
        strategy_name = self.get_param("strategy", state, "default")

        if strategy_name == "parallel_read":
            results, total_chars = await self._execute_parallel_read(tool_calls, state, result_budget)
        else:
            results, total_chars = await self._execute_sequential(tool_calls, state, result_budget)

        # strict_no_error: 도구 1개라도 실패하면 즉시 stop_on_error 마킹.
        # 사유: 신뢰성 모드 — 부분 성공으로 LLM 이 추측 답변하는 것보다 명시 에러로
        # 사용자에게 알리는 게 낫다는 사용자 피드백.
        if strategy_name == "strict_no_error":
            failed = [r for r in results if not r.get("success")]
            if failed:
                state.metadata["s07_strict_failed"] = True
                state.metadata["s07_strict_failures"] = [
                    {"tool": r.get("tool_name"), "error": r.get("error")} for r in failed
                ]
                logger.warning(
                    "[Execute] strict_no_error: %d/%d 도구 실패 — 후속 LLM 합성 차단",
                    len(failed), len(results),
                )

        # 도구 결과를 messages에 flush
        state.flush_tool_results()
        state.tools_executed_count += len(results)

        executed_count = sum(1 for r in results if r["success"])
        error_count = sum(1 for r in results if not r["success"])

        logger.info("[Execute] %d tools executed, %d errors (strategy=%s)",
                     executed_count, error_count, strategy_name)
        return {
            "tools_executed": len(results),
            "success_count": executed_count,
            "error_count": error_count,
            "total_chars": total_chars,
            "strategy": strategy_name,
        }

    async def _execute_sequential(
        self, tool_calls: list, state: PipelineState, result_budget: int
    ) -> tuple[list[dict[str, Any]], int]:
        """순차 실행 -- 기본 전략"""
        results: list[dict[str, Any]] = []
        total_chars = 0

        for tc in tool_calls:
            r, chars = await self._execute_single(
                tc.get("tool_use_id", ""), tc.get("tool_name", ""),
                tc.get("tool_input", {}), state, result_budget, total_chars,
            )
            results.append(r)
            total_chars += chars

        return results, total_chars

    async def _execute_parallel_read(
        self, tool_calls: list, state: PipelineState, result_budget: int
    ) -> tuple[list[dict[str, Any]], int]:
        """parallel_read 전략: 읽기 전용 도구는 병렬, 쓰기 도구는 순차.

        v0.23.0 — MCP annotations.readOnlyHint 1급 필드 우선. 이름 휴리스틱 폐기
        (false positive 유발). annotations 이 전혀 없을 때만 안전 쪽(write 취급)으로
        fallback. 호출부가 명시적 선언을 안 한 경우는 순차 실행이 옳다.
        """
        tool_registry = state.metadata.get("tool_registry", {})

        read_calls = []
        write_calls = []

        for tc in tool_calls:
            tool_name = tc.get("tool_name", "")
            read_only = self._resolve_read_only_hint(tool_name, state, tool_registry)
            (read_calls if read_only else write_calls).append(tc)

        results: list[dict[str, Any]] = []
        total_chars = 0

        # 읽기 도구 -> asyncio.gather 병렬 실행
        if read_calls:
            async def _run_read(tc):
                return await self._execute_single(
                    tc.get("tool_use_id", ""), tc.get("tool_name", ""),
                    tc.get("tool_input", {}), state, result_budget, 0,
                )

            parallel_results = await asyncio.gather(
                *[_run_read(tc) for tc in read_calls],
                return_exceptions=True,
            )
            for pr in parallel_results:
                if isinstance(pr, Exception):
                    results.append({"tool_name": "unknown", "success": False, "error": str(pr)})
                else:
                    r, chars = pr
                    results.append(r)
                    total_chars += chars

            logger.info("[Execute] parallel_read: %d read tools executed in parallel", len(read_calls))

        # 쓰기 도구 -> 순차 실행 (순서 보존)
        for tc in write_calls:
            r, chars = await self._execute_single(
                tc.get("tool_use_id", ""), tc.get("tool_name", ""),
                tc.get("tool_input", {}), state, result_budget, total_chars,
            )
            results.append(r)
            total_chars += chars

        return results, total_chars

    async def _execute_single(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict,
        state: PipelineState,
        result_budget: int,
        current_chars: int,
    ) -> tuple[dict[str, Any], int]:
        """단일 도구 실행 + 결과 축약 + 이벤트 발행. (result_info, chars) 반환."""
        try:
            # v0.17.0 — Policy Gate / Guard 가 참조할 호출 이력 기록 (실행 직전).
            # 타이밍은 실행 시도 시점. 성공 여부는 별도 (is_error=True 로 뒤에 기록).
            state.tool_call_history.append({
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "tool_input": tool_input,
                "iteration": state.loop_iteration,
            })

            # Capability 기반 파라미터 자동 보강 — tool_name이 capability에 바인딩됐으면
            # ParameterResolver로 누락된 필수 파라미터를 context에서 채움
            tool_input = await self._enrich_with_capability(tool_name, tool_input, state)

            # duplicate tool-call short-circuit — 같은 런에서 완전 동일 (tool_name, 정규화
            # JSON input) 재호출이면 실행 없이 캐시 반환. 비멱등 도구도 동일입력 재호출은
            # 이중실행(예: 같은 메일 2번 발송) 방지라 오히려 안전. gate: `tool_dedup` (기본 True).
            # parallel_read 동일 배치 내 경쟁은 miss 허용 (교차 라운드 반복이 주 타깃).
            dedup_key = self._dedup_key(tool_name, tool_input, state)
            if dedup_key is not None:
                _cached = state.metadata.get("tool_dedup_cache", {}).get(dedup_key)
                if _cached is not None:
                    cached_text = DEDUP_CACHED_PREFIX + _cached
                    state.add_tool_result(tool_use_id, cached_text, is_error=False)
                    if state.event_emitter:
                        _src = (state.metadata.get("tool_source_of") or {}).get(tool_name, "")
                        await state.event_emitter.emit(ToolResultEvent(
                            tool_use_id=tool_use_id,
                            tool_name=tool_name,
                            result=cached_text[:500],
                            is_error=False,
                            tool_source=_src,
                        ))
                    await state.emit_verbose(StageSubstepEvent(
                        stage_id=self.stage_id, substep="tool_call_cached",
                        meta={"tool_name": tool_name, "chars": len(cached_text)},
                    ))
                    logger.info("[Execute] dedup hit — %s served from cache (no re-execution)", tool_name)
                    return {
                        "tool_name": tool_name, "success": True,
                        "chars": len(cached_text), "cached": True,
                    }, len(cached_text)

            await state.emit_verbose(StageSubstepEvent(
                stage_id=self.stage_id, substep="tool_call_start",
                meta={"tool_name": tool_name, "tool_use_id": tool_use_id},
            ))

            result_text = await self._execute_tool(tool_name, tool_input, state)
            original_chars = len(result_text)

            # L1 Tool Result Budget (Progressive Disclosure push-side) —
            # 개별 결과가 preview_threshold 를 초과하면 preview 만 messages 에 흘리고
            # 원본은 state.pd_stores["tool_result"][tool_use_id] 에 보존.
            # LLM 은 `fetch_pd(kind="tool_result", id=<tool_use_id>)` 로 원본 재접근.
            preview_threshold = int(self.get_param("tool_result_preview_threshold", state, None) or 0)
            preview_size = int(self.get_param("tool_result_preview_size", state, None) or 0)
            # v1.18.6 — 미설정 시 schema default 가 None → `or 0` 로 둘 다 0 이 되는데,
            # 그러면 threshold=0(모든 결과가 초과) + preview_size=0([:0]=빈 문자열) 이라
            # 모든 도구 결과가 "preview 0자" 로 잘려 LLM 에 본문이 전혀 안 간다.
            # (cluster 는 이 stage_param 을 박아서 안 걸렸고, standalone 컴파일 wheel 의
            #  RAG 가 5천자를 찾고도 0자로 전달돼 "데이터 없음" 답하던 사문 버그.)
            # → 둘 다 명시(>0)된 경우에만 preview 압축. 미설정이면 전체 본문 그대로 전달.
            if preview_threshold > 0 and preview_size > 0 and original_chars > preview_threshold:
                preview_body = result_text[:preview_size]
                hint = (
                    f"\n\n... [PD: 원본 {original_chars:,}자 — preview {preview_size:,}자만 표시. "
                    f"전체 보려면 fetch_pd(kind='tool_result', id='{tool_use_id}') 호출]"
                )
                state.pd_store(
                    kind="tool_result",
                    resource_id=tool_use_id,
                    preview=preview_body + hint,
                    full=result_text,
                    meta={
                        "tool_name": tool_name,
                        "original_chars": original_chars,
                        "preview_size": preview_size,
                    },
                )
                result_text = preview_body + hint

            # v1.8.0 — Auto-load SKILL on first call (Claude Code Skills 차용 + 우리 환경 정합).
            # 이 도구의 SKILL body 가 아직 안 박혔으면 자동 load → state.system_prompt 에
            # 즉시 패치 (s03_prompt 가 ingress phase 라 매 turn 재build 안 됨 → 직접 patch
            # 필요). 다음 LLM 호출부터 가이드 손에 → 무한 루프 방지. provider 무관.
            try:
                from ...tools.skill_registry import get_skill_body as _get_skill_body
                if hasattr(state, "tool") and hasattr(state.tool, "loaded_skills"):
                    if tool_name not in state.tool.loaded_skills:
                        _body = _get_skill_body(tool_name)
                        if _body:
                            state.tool.loaded_skills[tool_name] = _body
                            # state.system_prompt 직접 패치 — <loaded_skills> 섹션이
                            # 이미 있으면 안에 append, 없으면 신규 생성. 다음 LLM call 시
                            # provider 가 system=state.system_prompt 로 전달 → 즉시 효과.
                            _marker_open = "<loaded_skills>"
                            _marker_close = "</loaded_skills>"
                            _skill_block = (
                                f"\n\n### {tool_name}\n\n{_body}"
                            )
                            _sp = state.system_prompt or ""
                            if _marker_close in _sp:
                                _sp = _sp.replace(
                                    _marker_close, _skill_block + "\n" + _marker_close,
                                )
                            else:
                                # v1.11.4 — PD 정신 회복: 환경 노출만, 행동 강제 톤 제거.
                                # "무모하게 반복 호출하지 마세요" / "NEXT 패턴을 따르세요"
                                # 등 LLM 행동 강제 표현 폐기. skill body 가 무엇인지만
                                # 환경으로 노출하고, 활용 여부는 LLM 자율.
                                _sp = _sp + (
                                    f"\n\n{_marker_open}\n"
                                    f"이번 session 에서 호출한 도구의 자동 로드 가이드 본문."
                                    f"{_skill_block}\n{_marker_close}"
                                )
                            state.system_prompt = _sp
                            result_text = (
                                result_text +
                                f"\n\n[guide for `{tool_name}` loaded — see <loaded_skills>]"
                            )
            except Exception as _se:
                logger.debug("[Execute] auto-load skill skip for %s: %s", tool_name, _se)

            # dedup 캐시 저장 — 성공 결과만 (실패는 일시 오류 재시도 여지 + streak 가이드가 담당).
            # 누적 트림 전 시점 저장: 다른 라운드의 예산 상태가 replay 에 새지 않게.
            if dedup_key is not None:
                _cap = int(self.get_param(
                    "tool_dedup_cache_max_chars", state, DEDUP_CACHE_ENTRY_MAX_CHARS,
                ))
                state.metadata.setdefault("tool_dedup_cache", {})[dedup_key] = result_text[:_cap]

            # 누적 예산 초과 시 2 차 방어 (여러 작은 결과 합이 큰 경우) — 기존 하드 트림 유지.
            chars = len(result_text)
            if current_chars + chars > result_budget:
                remaining = max(0, result_budget - current_chars)
                result_text = result_text[:remaining] + f"\n... (축약됨, 원본 {chars}자)"
                chars = len(result_text)

            state.add_tool_result(tool_use_id, result_text, is_error=False)
            # v1.9.0 P0#1 — 성공 시 streak 리셋.
            _streak_map = state.metadata.setdefault("tool_failure_streak", {})
            if tool_name in _streak_map:
                _streak_map.pop(tool_name, None)

            if state.event_emitter:
                _src = (state.metadata.get("tool_source_of") or {}).get(tool_name, "")
                await state.event_emitter.emit(ToolResultEvent(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    result=result_text[:500],
                    is_error=False,
                    tool_source=_src,
                ))

            await state.emit_verbose(StageSubstepEvent(
                stage_id=self.stage_id, substep="tool_call_complete",
                meta={"tool_name": tool_name, "chars": chars, "ok": True},
            ))

            return {"tool_name": tool_name, "success": True, "chars": chars}, chars

        except asyncio.TimeoutError:
            error_msg = f"Tool '{tool_name}' timed out after {self._tool_timeout}s"
            error_msg = self._append_failure_streak_guidance(state, tool_name, error_msg)
            state.add_tool_result(tool_use_id, error_msg, is_error=True)
            logger.warning("[Execute] %s", error_msg)
            return {"tool_name": tool_name, "success": False, "error": "timeout"}, 0

        except Exception as e:
            error_msg = f"Tool '{tool_name}' failed: {str(e)}"
            error_msg = self._append_failure_streak_guidance(state, tool_name, error_msg)
            state.add_tool_result(tool_use_id, error_msg, is_error=True)
            logger.error("[Execute] %s\n%s", error_msg, traceback.format_exc())

            if state.event_emitter:
                _src = (state.metadata.get("tool_source_of") or {}).get(tool_name, "")
                await state.event_emitter.emit(ToolResultEvent(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    result=error_msg,
                    is_error=True,
                    tool_source=_src,
                ))

            return {"tool_name": tool_name, "success": False, "error": str(e)}, 0

    def _append_failure_streak_guidance(
        self, state: PipelineState, tool_name: str, error_msg: str,
    ) -> str:
        """v1.9.0 P0#1 — 같은 tool N 연속 실패 시 LLM 에게 graceful 가이드 추가.

        runtime_default 'tool_consecutive_failure_limit' (기본 3) 도달하면
        error_msg 끝에 "다른 도구 / 다른 query / finalize" 명령형 1 줄 추가.
        LLM 이 무한 retry 빠지지 않게.
        """
        try:
            from ...core.runtime_defaults import resolve_with_default
            limit = int(resolve_with_default(None, "tool_consecutive_failure_limit", 3))
        except Exception:
            limit = 3
        streak_map = state.metadata.setdefault("tool_failure_streak", {})
        n = int(streak_map.get(tool_name, 0)) + 1
        streak_map[tool_name] = n
        if n >= limit:
            error_msg += (
                f"\n\n[harness graceful fallback — `{tool_name}` failed {n} times in a row. "
                f"Stop calling it. Either: (a) try a different tool, "
                f"(b) reformulate the user request, or (c) answer with what you have "
                f"and note the limitation.]"
            )
        return error_msg

    def _dedup_key(
        self, tool_name: str, tool_input: dict, state: PipelineState,
    ) -> Optional[str]:
        """dedup 캐시 키 (tool_name + 정규화 JSON input). None=dedup 비대상."""
        gate = self.get_param("tool_dedup", state, None)
        if gate is None:
            gate = getattr(getattr(state, "config", None), "tool_dedup", None)
        if gate is not None and not bool(gate):
            return None
        if tool_name in DEDUP_EXEMPT_TOOLS:
            return None
        try:
            canon = json.dumps(tool_input or {}, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return None
        return f"{tool_name}\n{canon}"

    def _resolve_read_only_hint(
        self, tool_name: str, state: PipelineState, tool_registry: dict,
    ) -> bool:
        """MCP annotations.readOnlyHint 우선 — 순서 (v0.24.4 재조정):

        1. `state.tool.annotations[tool_name].readOnlyHint` (s04 가 tool_definitions
           와 분리해 저장 — payload 오염 방지)
        2. `tool_registry[name].read_only_hint` (Tool 인스턴스 속성)
        3. legacy `tool_definitions[*].annotations.readOnlyHint` (구 버전 외부 MCP
           호환 — 앞으로는 annotations 가 definitions 에 섞이지 않지만 잔존 가능성)
        4. legacy `tool_definitions[*].metadata.is_read_only`
        5. legacy `tool_registry[name].is_read_only`
        6. **fallback: False** — 명시 선언이 없으면 안전 쪽(write 취급, 순차 실행).
           이전 버전의 이름 휴리스틱은 제거 (false positive 유발).
        """
        # 1. state.tool.annotations 맵 (v0.24.4 분리 저장)
        ann = (state.tool.annotations or {}).get(tool_name) or {}
        if "readOnlyHint" in ann:
            return bool(ann["readOnlyHint"])

        # 2. Tool 인스턴스의 read_only_hint 속성
        inst = tool_registry.get(tool_name)
        if inst is not None and hasattr(inst, "read_only_hint"):
            return bool(inst.read_only_hint)

        # 3. legacy tool_definitions annotations (구 버전 호환)
        for td in state.tool_definitions:
            if td.get("name") != tool_name:
                continue
            legacy_ann = td.get("annotations") or {}
            if "readOnlyHint" in legacy_ann:
                return bool(legacy_ann["readOnlyHint"])
            break

        # 4. legacy metadata.is_read_only (tool_definitions)
        for td in state.tool_definitions:
            if td.get("name") == tool_name:
                meta = td.get("metadata") or {}
                if "is_read_only" in meta:
                    return bool(meta["is_read_only"])
                break

        # 5. legacy is_read_only (instance)
        if inst is not None and hasattr(inst, "is_read_only"):
            return bool(inst.is_read_only)

        # 6. 안전 쪽 fallback
        return False

    async def _enrich_with_capability(
        self, tool_name: str, tool_input: dict, state: PipelineState
    ) -> dict:
        """tool_name에 바인딩된 CapabilitySpec이 있으면 ParameterResolver로 args 보강.

        - capability_bindings에서 역조회 (tool_name → capability_name)
        - 못 찾으면 tool_input 그대로 반환
        - 보강 과정에서 누락 필수 파라미터가 있으면 MissingParamEvent 이벤트 발행
        """
        try:
            bindings = state.metadata.get("capability_bindings", {}) or {}
            # 역인덱스: capability_name → tool_name
            cap_name = next((c for c, t in bindings.items() if t == tool_name), None)
            if cap_name is None:
                return tool_input

            from ...capabilities import ParameterResolver, get_default_registry

            spec = get_default_registry().get(cap_name)
            if spec is None or not spec.params:
                return tool_input

            resolver = ParameterResolver(spec, state)
            result = await resolver.resolve(provided=tool_input or {})

            if result.warnings:
                logger.debug("[Execute] resolve warnings for %s: %s", tool_name, result.warnings)

            if not result.ok:
                missing_names = [p.name for p in result.missing]
                logger.warning(
                    "[Execute] %s — 필수 파라미터 누락: %s (context에서 못 찾음)",
                    tool_name, missing_names,
                )
                # 누락 상태로도 실행은 시도 — 도구가 직접 에러 반환하게 함

            return result.args or tool_input
        except Exception as e:
            logger.debug("[Execute] capability enrich skipped for %s: %s", tool_name, e)
            return tool_input

    async def _execute_tool(self, tool_name: str, tool_input: dict, state: PipelineState) -> str:
        """도구 실행 — ResourceRegistry / ToolSource / tool_registry(MCP 등) 순 디스패치"""
        return await asyncio.wait_for(
            self._dispatch_tool(tool_name, tool_input, state),
            timeout=self._tool_timeout,
        )

    async def _dispatch_tool(self, tool_name: str, tool_input: dict, state: PipelineState) -> str:
        """도구 디스패처 — ResourceRegistry → 플러그인 ToolSource → 레거시 폴백"""

        # 빌트인: discover_tools (progressive disclosure Level 2)
        if tool_name == "discover_tools":
            return self._handle_discover_tools(tool_input, state)

        # 빌트인: search_tools (progressive disclosure Level 0 — 큰 카탈로그용)
        if tool_name == "search_tools":
            search = state.metadata.get("tool_registry", {}).get("search_tools")
            if search and hasattr(search, "execute"):
                result = await search.execute(tool_input)
                return result.content if hasattr(result, "content") else str(result)
            # 폴백: 인스턴스 없으면 즉시 생성
            from ...tools.builtin import SearchToolsTool
            search = SearchToolsTool(state.tool_definitions)
            result = await search.execute(tool_input)
            return result.content if hasattr(result, "content") else str(result)

        # 빌트인: rag_search (에이전트가 직접 호출하는 RAG 검색)
        if tool_name == "rag_search":
            return await self._handle_rag_search(tool_input, state)

        # ResourceRegistry 경로 (XgenAdapter가 주입)
        registry = state.metadata.get("resource_registry")
        if registry:
            executors = registry.get_tool_executors()
            if tool_name in executors:
                return await registry.execute_tool(tool_name, tool_input)

        # 플러그인 ToolSource 경로 (register_tool_source로 등록된 소스)
        # v0.16.5 — content 가 dict/list 로 오더라도 string 으로 정규화. Anthropic
        # tool_result.content 는 string 필수 + 엔진 내부 slicing([:N]) 에서 TypeError 방지.
        from ...tools import get_tool_sources
        import json as _json
        # 전역 소스 + 상태 범위 소스 (nested subpipeline 격리). 상태 범위를 먼저
        # 검사해 같은 이름이면 nested 가 우선 — 부모 카탈로그 오염 없이 위임.
        all_sources = list(getattr(state, "extra_tool_sources", None) or []) + list(get_tool_sources())
        for source in all_sources:
            if source.has_tool(tool_name):
                result = await source.call_tool(tool_name, tool_input)
                content = result.get("content", result) if isinstance(result, dict) else result
                if not isinstance(content, str):
                    try:
                        content = _json.dumps(content, ensure_ascii=False, default=str)
                    except Exception:
                        content = str(content)
                # ToolSource 가 선언한 is_error 를 존중 — 실패로 기록돼 graceful fallback 발동.
                if isinstance(result, dict) and result.get("is_error"):
                    raise ToolError(content, tool_name)
                return content

        # 레거시 폴백: state.metadata에 직접 등록된 Tool 인스턴스
        tool_registry = state.metadata.get("tool_registry", {})
        if tool_name in tool_registry:
            tool_instance = tool_registry[tool_name]
            if hasattr(tool_instance, 'execute'):
                result = await tool_instance.execute(tool_input)
                return result.content if hasattr(result, 'content') else str(result)

        # 미등록 도구
        return f"Error: Tool '{tool_name}' is not registered. Use discover_tools to see available tools."

    async def _handle_rag_search(self, tool_input: dict, state: PipelineState) -> str:
        """RAG 검색 도구 실행 — tool_registry에서 RAGSearchTool 인스턴스를 꺼내 실행"""
        tool_registry = state.metadata.get("tool_registry", {})
        rag_tool = tool_registry.get("rag_search")
        if rag_tool and hasattr(rag_tool, "execute"):
            result = await rag_tool.execute(tool_input)
            return result.content if hasattr(result, "content") else str(result)

        # 폴백: RAGSearchTool이 registry에 없으면 직접 생성 시도
        rag_collections = state.metadata.get("rag_collections", [])
        if not rag_collections:
            return "Error: No RAG collections configured. RAG search is not available."

        from ...tools.rag_tool import RAGSearchTool
        rag_top_k = state.metadata.get("rag_top_k", 4)
        # v0.11.25 — DocumentService 주입. 없으면 RAGSearchTool 이 ToolError 반환.
        _services = state.metadata.get("services")
        _doc_service = getattr(_services, "documents", None) if _services else None
        fallback_tool = RAGSearchTool(
            collections=rag_collections,
            default_top_k=rag_top_k,
            doc_service=_doc_service,
        )
        result = await fallback_tool.execute(tool_input)
        return result.content if hasattr(result, "content") else str(result)

    def _handle_discover_tools(self, tool_input: dict, state: PipelineState) -> str:
        """Progressive Disclosure Level 2: 특정 도구의 상세 스키마 반환"""
        tool_name = tool_input.get("tool_name", "")

        if not tool_name:
            # 전체 목록
            lines = []
            for t in state.tool_index:
                lines.append(f"- {t['name']}: {t.get('description', '')}")
            return "\n".join(lines) if lines else "No tools available."

        # 특정 도구의 상세 스키마
        schema = state.tool_schemas.get(tool_name)
        if schema:
            import json
            return json.dumps(schema, indent=2, ensure_ascii=False)

        # tool_definitions에서 검색
        for td in state.tool_definitions:
            if td.get("name") == tool_name:
                import json
                return json.dumps(td, indent=2, ensure_ascii=False)

        return f"Tool '{tool_name}' not found in registry."

    def list_strategies(self) -> list[StrategyInfo]:
        # v1.4.0 — 사용자 픽 카드 hide. 도구 실행 흐름은 default (순차+에러 허용) 로 고정.
        # 코드 (default/parallel_read/strict_no_error) 보존.
        return []
