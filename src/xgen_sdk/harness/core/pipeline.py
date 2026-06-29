"""
Pipeline — 3-Phase 실행 엔진

Phase A (Ingress, 1회): Input → Memory → System Prompt → Tool Index
Phase B (Agentic Loop, N회): Plan → Context → LLM ↔ Execute → Validate → Decide
Phase C (Egress, 1회): Save → Complete

도구 루프(LLM↔Execute)는 Phase B 내부에서 자체 반복.
검증 재시도(Validate→Decide→retry)는 Phase B 전체를 재시작.
"""

import asyncio
import logging
import time
from typing import Optional

from .config import HarnessConfig
from .stage import Stage
from .state import PipelineState
from .state_loop import apply_state_view, record_iteration
from ..events.emitter import EventEmitter
from ..events.types import (
    StageEnterEvent,
    StageExitEvent,
    ErrorEvent,
    DoneEvent,
    MetricsEvent,
)
from ..errors import HarnessError, PipelineAbortError

logger = logging.getLogger("harness.pipeline")


# ─── Role 상수 (v0.22.0) ─────────────────────────────────────────────
# Stage.role 이름은 pipeline ↔ stage 간 계약. 리터럴을 파일 여기저기 박지 않고
# 상수로 통일해 외부 기여자가 "어떤 role 이름이 예약돼 있는가" 를 한눈에 보게 한다.
# 외부 Stage 가 여기 이름으로 role 을 선언하면 Pipeline 이 동일하게 대우한다.
ROLE_ORCHESTRATOR_PLANNER = "orchestrator_planner"  # s00_harness 류 — main_call/replan 호출 대상
ROLE_POLICY_GATE = "policy_gate"                    # Policy Gate Stage — 4 훅 경로 소유
ROLE_MAIN_ACTOR = "main_actor"                      # s07_act 류 — main_call 투입 지점
ROLE_SCORER = "scorer"                              # validation_score 를 StageExit 에 노출할 Stage


class Pipeline:
    """하네스 파이프라인 실행기"""

    def __init__(
        self,
        config: HarnessConfig,
        stages: list[Stage],
        event_emitter: Optional[EventEmitter] = None,
        *,
        doc_service: object = None,
        provider: object = None,
    ):
        self.config = config
        self.event_emitter = event_emitter or EventEmitter()
        self._all_stages = stages

        # v1.10.0 — 외부 wire (사용자가 from_config 인자로 inject) 보관.
        # run(state) 진입 시 state 가 같은 attribute 가 없으면 여기 박은 인스턴스가 주입됨.
        # cluster 측은 옛 방식대로 XgenAdapter 가 state 에 직접 박는다 — BC 충돌 없음.
        self._injected_doc_service = doc_service
        self._injected_provider = provider

        # Phase별 분류
        self.ingress_stages = [s for s in stages if s.phase == "ingress"]
        self.loop_stages = [s for s in stages if s.phase == "loop"]
        self.egress_stages = [s for s in stages if s.phase == "egress"]

        self._total_stage_count = len(stages)

    @classmethod
    def from_config(
        cls,
        config: HarnessConfig,
        event_emitter: Optional[EventEmitter] = None,
        registry: Optional["ArtifactRegistry"] = None,
        *,
        doc_service: object = None,
        provider: object = None,
    ) -> "Pipeline":
        """설정으로부터 파이프라인 생성.

        registry 미지정 시 전역 싱글톤(`_get_default_registry()`)을 사용하여
        `register_stage()` 나 entry_points 로 등록된 외부 플러그인 Stage 도
        함께 반영됩니다. 테스트/격리가 필요하면 registry 를 명시 전달하세요.

        v1.1.0 — Planner 항상 OFF 직선 흐름. harness_mode/use_planner 제거.
        s00_harness Stage 자체는 레지스트리에 남아있되 ingress 최상단 prepend 안 함.
        본문 LLM 호출(main_call)은 Phase B 에서 s00 인스턴스를 통해 그대로 호출.

        v1.10.0 — `doc_service` / `provider` 키워드 인자로 외부 인프라 주입 가능.
        cluster (xgen-workflow harness_bridge) 가 옛 방식대로 state 에 직접 박던 경로는
        그대로 유효 (BC). 외부 사용자는 from_config 인자로 inject:

            from xgen_harness.adapters import QdrantDocService, create_provider
            pipeline = Pipeline.from_config(
                config,
                doc_service=QdrantDocService(url="..."),
                provider=create_provider("openai", api_key="..."),
            )
        """
        from .registry import _get_default_registry
        reg = registry or _get_default_registry()
        stages = reg.build_pipeline_stages(config)
        return cls(
            config, stages, event_emitter,
            doc_service=doc_service, provider=provider,
        )

    async def run(self, state: PipelineState) -> PipelineState:
        """파이프라인 실행 — 3 Phase (v0.13.0 단일 provider + iterative planning)."""
        state.event_emitter = self.event_emitter
        state.config = self.config
        state.start_time = time.time()

        # v1.10.0 — Pipeline.from_config 인자로 inject 된 doc_service / provider 를
        # state 에 박음 (state 에 이미 박혀있으면 그쪽 우선 — cluster wire BC 보장).
        if self._injected_doc_service is not None and getattr(state, "doc_service", None) is None:
            state.doc_service = self._injected_doc_service
        if self._injected_provider is not None and getattr(state, "provider", None) is None:
            state.provider = self._injected_provider

        # v1.18.6 — 주입된 doc_service 를 stage 들이 실제로 읽는 경로
        # (state.metadata["services"].documents) 까지 연결한다. from_config(doc_service=) 는
        # 그동안 state.doc_service 에만 박혔는데 어떤 stage 도 그걸 읽지 않아(s04/s07 RAG 는
        # metadata["services"].documents 를 본다) standalone 컴파일 wheel 의 RAG 가 항상
        # "DocumentService is not available" 로 죽었다. cluster(XgenAdapter) 는
        # metadata["services"] 를 직접 박으므로, 이미 있으면 건드리지 않는다 (BC).
        if self._injected_doc_service is not None and state.metadata.get("services") is None:
            from .services import ServiceProvider
            state.metadata["services"] = ServiceProvider(documents=self._injected_doc_service)

        try:
            # ━━━━ v0.14.0 — LLM provider 1 회 선초기화 ━━━━
            # "한 번 설정하고 그 핸들을 쭉 재사용" — state.provider 를 Pipeline 진입부에서
            # 미리 띄워 s00_harness (Planner + main_call) / s08_decide 가 전부 **같은
            # 인스턴스** 를 재활용. ensure_provider 는 idempotent.
            from .provider_bootstrap import ensure_provider
            try:
                await ensure_provider(state, stage_id="pipeline")
            except Exception as e:
                # provider 초기화 실패해도 s01_input 등 provider 불필요 단계는 돌아야 하므로
                # 여기서 abort 하지 않는다. 실제로 provider 가 필요한 Stage 에서 재시도.
                logger.debug("[Pipeline] provider 선초기화 보류: %s", e)

            # Phase A: Ingress (1회)
            logger.info("[Pipeline] Phase A: Ingress (%d stages)", len(self.ingress_stages))
            for stage in self.ingress_stages:
                if self._planner_skips(stage, state):
                    await self._emit_bypass(stage, state, reason=self._planner_skip_reason(stage, state))
                    continue
                if stage.should_bypass(state):
                    await self._emit_bypass(stage, state)
                    continue
                await self._execute_stage(stage, state)

            # Phase B: Agentic Loop — v1.1.0 직선 흐름.
            # Planner OFF 고정으로 매 iter replan 분기 dead. 본문 LLM 호출은
            # main_actor role Stage 직전에 s00.main_call 로만 위임.
            s00_stage = self._find_loop_s00()
            # v0.17.0 — Policy Gate (role="policy_gate") 인스턴스. 없으면 훅 no-op.
            policy_stage = self._find_role_stage(ROLE_POLICY_GATE)
            # v0.22.0 — orchestrator 행동을 레지스트리 spec 으로 조회. "linear"/"plan_execute"
            # if-else 하드코딩 제거. 외부 orchestrator 도 replan_per_iter/max_iterations_override
            # 를 선언만 하면 엔진이 동일하게 존중.
            from .orchestrator_registry import get_orchestrator
            from .runtime_defaults import resolve_with_default
            orch_hint = (state.metadata.get("orchestrator_hint") or "").strip().lower()
            orch_spec = get_orchestrator(orch_hint) or get_orchestrator("iterative")
            # 정책 default 는 이식측 owns. None 일 때 엔진 안전 바닥(safety floor) 으로
            # 폴백 — 외부 플러그인이 register_runtime_default("max_iterations", N) 로 override.
            effective_max_iter = (
                orch_spec.max_iterations_override
                if orch_spec and orch_spec.max_iterations_override is not None
                else resolve_with_default(self.config.max_iterations, "max_iterations")
            )
            replan_per_iter = bool(orch_spec.replan_per_iter) if orch_spec else True
            logger.info(
                "[Pipeline] Phase B: Agentic Loop (max_iter=%d, orchestrator=%r, replan=%s)",
                effective_max_iter, (orch_spec.name if orch_spec else orch_hint) or "iterative(default)",
                replan_per_iter,
            )
            while state.loop_decision == "continue" and state.loop_iteration < effective_max_iter and not state.is_over_budget:
                state.loop_iteration += 1
                logger.info("[Pipeline] Loop iteration %d", state.loop_iteration)

                # Stateful loop(read): 외부 state 뷰를 이번 회차 컨텍스트에 갱신 주입.
                # provider 미주입이면 no-op.
                apply_state_view(state)

                # v1.1.0 — iterative replan dead code 제거 (Planner 항상 OFF).

                for stage in self.loop_stages:
                    # v0.17.0 — Policy Gate 는 loop 순서에서 skip. Pipeline 이 3 훅에 별도 호출.
                    if stage.role == ROLE_POLICY_GATE:
                        await self._emit_bypass(stage, state, reason="Policy Gate 는 훅 경로로 호출")
                        continue

                    # v0.16.6 — "main_actor" role Stage 직전에 Planner 의 main_call 주입.
                    # 이름 리터럴("s07_act") 대신 Stage.role 로 검색 → 외부 Stage 가
                    # 같은 role 로 바꿔 끼워도 자동 인식.
                    if stage.role == ROLE_MAIN_ACTOR:
                        # v0.17.0 — pre_main 훅 (입력/Plan 정책)
                        await self._invoke_policy_gate(state, policy_stage, "pre_main")
                        if state.policy_block_reason:
                            state.loop_decision = "complete"
                            break

                        if s00_stage is not None:
                            await self._invoke_main_call(state, s00_stage)

                        # v1.0.7 — main_call 직후 두 훅 독립 호출.
                        # 이전 (v0.17.0) 은 `pre_tool if pending else post_response` 로
                        # 둘 중 하나만 호출 — 도구 호출 + 응답 텍스트 동반 케이스에서
                        # POST_RESPONSE (ContentGuard 응답 검증) 가 누락. 두 훅의 의미는
                        # 독립적: POST_RESPONSE 는 last_assistant_text 검증, PRE_TOOL 은
                        # pending_tool_calls 의 도구별 선행조건 검증.
                        await self._invoke_policy_gate(state, policy_stage, "post_response")
                        if state.policy_block_reason:
                            state.loop_decision = "complete"
                            break

                        if state.pending_tool_calls:
                            await self._invoke_policy_gate(state, policy_stage, "pre_tool")
                            if state.policy_block_reason:
                                state.loop_decision = "complete"
                                break

                    if self._planner_skips(stage, state):
                        await self._emit_bypass(stage, state, reason=self._planner_skip_reason(stage, state))
                        continue
                    if stage.should_bypass(state):
                        await self._emit_bypass(stage, state)
                        continue
                    await self._execute_stage(stage, state)

                    # Decide 스테이지가 loop_decision을 설정
                    if state.loop_decision in ("complete", "abort"):
                        break

                # v0.17.0 — iter 말미 loop_boundary 훅 (예산·반복 등 누적 정책)
                await self._invoke_policy_gate(state, policy_stage, "loop_boundary")

                # Stateful loop(write): 이번 회차 과정을 외부 state 에 기록(C3).
                # recorder 미주입이면 no-op.
                record_iteration(state, state.loop_decision)

                # v0.22.0 — max_iterations_override=1 (linear 등) 이면 강제 종료.
                # 이름 리터럴 없이 spec 의 수치를 본다.
                if (
                    orch_spec
                    and orch_spec.max_iterations_override == 1
                    and state.loop_decision == "continue"
                ):
                    logger.info("[Pipeline] orchestrator %r max_iter=1 — 1회 실행 후 종료", orch_spec.name)
                    state.loop_decision = "complete"
                    break

                # retry → loop_decision을 continue로 돌리고 재시작
                if state.loop_decision == "retry":
                    state.retry_count += 1
                    if state.retry_count >= self.config.max_retries:
                        logger.warning("[Pipeline] Max retries reached (%d)", self.config.max_retries)
                        state.loop_decision = "complete"
                    else:
                        logger.info("[Pipeline] Retry %d/%d", state.retry_count, self.config.max_retries)
                        state.loop_decision = "continue"
                        # verbose: 에이전틱 루프 재시도 이벤트
                        from ..events.types import RetryEvent
                        await state.emit_verbose(RetryEvent(
                            stage_id="pipeline_loop",
                            reason="loop retry by decide",
                            attempt=state.retry_count,
                            max_attempts=self.config.max_retries,
                        ))

            # v1.11.4 (2026-05-17) — synthesis_kick 전면 폐기.
            #
            # v0.26.7 ~ v1.11.3 동안 살아있던 safeguard. 도구 호출 후 intro 짧으면
            # 도구 비활성 + 재호출로 final answer 강제. 그러나:
            #   - state.tool_definitions=[] 가 환경 강제 변경 → LLM 자율 깎음.
            #   - LLM 이 도구 결과 받고 자연스레 답변 작성하는 흐름 (도구 호출 →
            #     reasoning trace 자연 노출 → 결론) 자체를 막아 답변에서 "찾아가는
            #     과정" 이 사라짐. 사용자 라이브 적발 (5/17).
            #
            # PD 정신: LLM 이 환경 (도구 카탈로그 + 도구 결과 + history + 사용자가 박은
            # max_iter / system_prompt) 보고 100% 자율 결정. max_iter 부족하면 사용자가
            # 그 환경값을 늘릴 일이지 엔진이 강제 안전망 박을 일 아님.

            # Phase C: Egress (1회)
            logger.info("[Pipeline] Phase C: Egress (%d stages)", len(self.egress_stages))
            for stage in self.egress_stages:
                if self._planner_skips(stage, state):
                    await self._emit_bypass(stage, state, reason=self._planner_skip_reason(stage, state))
                    continue
                if stage.should_bypass(state):
                    await self._emit_bypass(stage, state)
                    continue
                await self._execute_stage(stage, state)

            # 완료 이벤트
            await self.event_emitter.emit(DoneEvent(
                final_output=state.final_output,
                success=True,
            ))

        except PipelineAbortError as e:
            logger.error("[Pipeline] Abort: %s", e)
            await self.event_emitter.emit(ErrorEvent(
                message=str(e),
                stage_id=e.stage_id,
                recoverable=False,
            ))
            await self.event_emitter.emit(DoneEvent(
                final_output=state.final_output or str(e),
                success=False,
            ))

        except Exception as e:
            logger.exception("[Pipeline] Unexpected error")
            await self.event_emitter.emit(ErrorEvent(
                message=str(e),
                stage_id="",
                recoverable=False,
            ))
            await self.event_emitter.emit(DoneEvent(
                final_output=state.final_output or str(e),
                success=False,
            ))

        return state

    async def _execute_stage(self, stage: Stage, state: PipelineState) -> dict:
        """단일 스테이지 실행 (라이프사이클 훅 + 이벤트 발행 + I/O 검증)"""
        step = self._get_step_number(stage)

        # I/O 입력 검증 (Stage 인터페이스 계약)
        if stage.input_spec:
            missing = stage.input_spec.validate(state)
            if missing:
                logger.warning("[Pipeline] Stage %s missing inputs: %s (continuing anyway)", stage.stage_id, missing)

        # on_enter + 이벤트
        await self.event_emitter.emit(StageEnterEvent(
            stage_id=stage.stage_id,
            stage_name=stage.display_name_ko,
            phase=stage.phase,
            step=step,
            total=self._total_stage_count,
        ))
        await stage.on_enter(state)

        t0 = time.time()
        try:
            result = await stage.execute(state)
            elapsed = time.time() - t0
            state.stage_timings[stage.stage_id] = elapsed * 1000

            # on_exit + 이벤트
            await stage.on_exit(result, state)
            await self.event_emitter.emit(StageExitEvent(
                stage_id=stage.stage_id,
                stage_name=stage.display_name_ko,
                output=result,
                # v0.16.6 — "scorer" role Stage 의 StageExit 에만 validation_score 노출.
                score=state.validation_score if stage.role == ROLE_SCORER else None,
                step=step,
                total=self._total_stage_count,
            ))
            return result

        except HarnessError as e:
            elapsed = time.time() - t0
            state.stage_timings[stage.stage_id] = elapsed * 1000

            recovery = await stage.on_error(e, state)
            if recovery is not None:
                logger.info("[Pipeline] Stage %s recovered from error", stage.stage_id)
                # verbose: Stage on_error 복구 시 RetryEvent
                from ..events.types import RetryEvent
                await state.emit_verbose(RetryEvent(
                    stage_id=stage.stage_id,
                    reason=f"on_error recovered: {type(e).__name__}",
                    attempt=1,
                    max_attempts=1,
                ))
                await self.event_emitter.emit(StageExitEvent(
                    stage_id=stage.stage_id,
                    stage_name=stage.display_name_ko,
                    output=recovery,
                    step=step,
                    total=self._total_stage_count,
                ))
                return recovery

            await self.event_emitter.emit(ErrorEvent(
                message=str(e),
                stage_id=stage.stage_id,
                recoverable=e.recoverable,
            ))
            raise

        except Exception as e:
            # 커스텀 Stage / 외부 플러그인이 raise 한 일반 예외도 on_error 복구 기회 제공
            try:
                recovery = await stage.on_error(e, state)
            except Exception as on_err_exc:
                logger.debug("[Pipeline] stage.on_error(%s) itself raised: %s", stage.stage_id, on_err_exc)
                recovery = None
            if recovery is not None:
                logger.info("[Pipeline] Stage %s recovered from generic error", stage.stage_id)
                await self.event_emitter.emit(StageExitEvent(
                    stage_id=stage.stage_id,
                    stage_name=stage.display_name_ko,
                    output=recovery,
                    step=step,
                    total=self._total_stage_count,
                ))
                return recovery

            await self.event_emitter.emit(ErrorEvent(
                message=str(e),
                stage_id=stage.stage_id,
                recoverable=False,
            ))
            raise PipelineAbortError(str(e), stage.stage_id)

    async def _emit_bypass(
        self,
        stage: Stage,
        state: PipelineState,
        reason: str = "조건 미충족으로 건너뜀",
    ) -> None:
        """bypass된 스테이지도 이벤트 발행 (UI에서 스킵 상태 표시).

        Planner 가 skip 한 경우 reason 에 Plan.skipped[stage_id] 가 주입되어
        "왜 이 단계를 건너뛰었는지"를 프론트가 그대로 표시할 수 있다.
        """
        step = self._get_step_number(stage)
        logger.debug("[Pipeline] Bypass: %s (%s)", stage.stage_id, reason)
        await self.event_emitter.emit(StageEnterEvent(
            stage_id=stage.stage_id,
            stage_name=stage.display_name_ko,
            phase=stage.phase,
            step=step,
            total=self._total_stage_count,
            description="bypassed",
        ))
        await self.event_emitter.emit(StageExitEvent(
            stage_id=stage.stage_id,
            stage_name=stage.display_name_ko,
            output={"bypassed": True, "reason": reason},
            step=step,
            total=self._total_stage_count,
        ))

    # ── Harness Planner 연동 (v0.12.0) ─────────────────────────────

    def _planner_skips(self, stage: Stage, state: PipelineState) -> bool:
        """Planner 가 세운 Plan 에 따라 이 Stage 를 skip 해야 하는지.

        Plan 미수립(state.metadata["harness_plan"] 없음) 또는 chosen 이 비어있는
        fallback 상태에서는 skip 하지 않음 (전체 실행 — 하위 호환).
        v0.16.6 — Planner 자신(role="orchestrator_planner") 은 skip 대상 외.
        """
        if stage.role == ROLE_ORCHESTRATOR_PLANNER:
            return False
        plan = state.metadata.get("harness_plan")
        if not isinstance(plan, dict):
            return False
        chosen = plan.get("chosen") or []
        if not chosen:
            return False  # fallback — 전체 실행
        return stage.stage_id not in chosen

    def _planner_skip_reason(self, stage: Stage, state: PipelineState) -> str:
        """Plan.skipped[stage_id] 가 있으면 그 이유, 없으면 일반 메시지."""
        plan = state.metadata.get("harness_plan") or {}
        skipped = plan.get("skipped") or {}
        reason = skipped.get(stage.stage_id)
        if reason:
            return f"Planner: {reason}"
        return "Planner 가 이번 턴에는 불필요하다고 판단"

    async def _invoke_main_call(self, state: PipelineState, planner_stage: Stage) -> None:
        """v0.14.0/v0.16.6 — Planner(role="orchestrator_planner") 의 main_call 호출.

        StageEnter/Exit 이벤트는 planner_stage.stage_id 를 그대로 사용.
        transport 선택은 `state.config.active_strategies[<stage_id>]` — 이름 리터럴 없음.
        """
        if not hasattr(planner_stage, "main_call"):
            logger.error("[Pipeline] planner_stage has no main_call — upgrade required")
            return

        sid = planner_stage.stage_id
        step = self._get_step_number(planner_stage)
        transport = "streaming"
        if state.config:
            active = state.config.active_strategies.get(sid)
            if isinstance(active, str) and active:
                transport = active
            params = state.config.stage_params.get(sid) or {}
            strat = params.get("strategy")
            if isinstance(strat, str) and strat:
                transport = strat

        await self.event_emitter.emit(StageEnterEvent(
            stage_id=sid,
            stage_name=planner_stage.display_name_ko,
            phase="loop",
            step=step,
            total=self._total_stage_count,
            description=f"main_call ({transport})",
        ))

        # v1.8.0 — fetch_pd 코드화 패턴: per-turn body injection.
        # 사용자 정합 ("코드화해서 system_prompt 안 넘어가게"). 직전 turn 의 fetch_pd 본문을
        # state.system_prompt 에 임시 추가 (이번 LLM 호출만), 호출 후 즉시 환원.
        # 매 turn LLM 이 "방금 fetch 한 본문" 만 손에 — messages 누적 X, system_prompt 누적 X.
        # provider/context 한계 무관 (모든 모델 안전).
        _pending = list(getattr(state, "fetched_pending", []) or [])
        _sp_orig = state.system_prompt
        if _pending:
            # v1.11.4 — PD 정신: fetched body 본문 자체가 환경 노출. "이번에 답변에
            # 인용하세요" 같은 행동 강제 톤 폐기. 본문은 이번 turn 만 유효한 환경
            # 슬롯이며, 활용 여부는 LLM 자율.
            _injection_lines = ["<recently_fetched>"]
            _injection_lines.append(
                "이번 turn 에 호출한 fetch_pd 본문. 다음 turn 에는 노출되지 않음."
            )
            for entry in _pending:
                _kind = entry.get("kind", "?")
                _id = entry.get("id", "?")
                _body = entry.get("body", "")
                _meta = entry.get("meta", {})
                _injection_lines.append(f"\n### [{_kind}:{_id}] meta={_meta}\n")
                _injection_lines.append(_body)
            _injection_lines.append("\n</recently_fetched>")
            state.system_prompt = (_sp_orig or "") + "\n\n" + "\n".join(_injection_lines)

        t0 = time.time()
        try:
            result = await planner_stage.main_call(state, strategy=transport)
            elapsed = time.time() - t0
            state.stage_timings[f"{sid}_main_call"] = elapsed * 1000
            await self.event_emitter.emit(StageExitEvent(
                stage_id=sid,
                stage_name=planner_stage.display_name_ko,
                output=result,
                step=step,
                total=self._total_stage_count,
            ))
        except Exception as e:
            await self.event_emitter.emit(ErrorEvent(
                message=str(e), stage_id=sid, recoverable=False,
            ))
            raise
        finally:
            # v1.8.0 — 인젝션 환원. 다음 turn 시 _pending 새로 채워짐 (또는 빈 list).
            if _pending:
                state.system_prompt = _sp_orig
                state.fetched_pending = []

    def _find_loop_s00(self) -> Optional[Stage]:
        """v0.16.6 — Planner(role="orchestrator_planner") 인스턴스 조회.

        Phase B iter 의 replan 은 같은 인스턴스를 재호출해야 Plan 이 누적·갱신.
        Planner 비활성이면 None 반환. 이름 리터럴 없이 role 로만 검색.
        """
        return self._find_role_stage(ROLE_ORCHESTRATOR_PLANNER)

    def _find_role_stage(self, role: str) -> Optional[Stage]:
        """v0.17.0 — role 이름으로 Stage 인스턴스 조회 (범용)."""
        for stage in self._all_stages:
            if stage.role == role:
                return stage
        return None

    async def _invoke_policy_gate(
        self,
        state: "PipelineState",
        policy_stage: Optional[Stage],
        hook_name: str,
    ) -> None:
        """v0.17.0 — Policy Gate Stage 의 invoke_hook 호출.

        Stage 가 없거나 예외가 나도 Pipeline 실행은 계속 (정책 검사 실패가 본 흐름을 막지 않음).
        """
        if policy_stage is None or not hasattr(policy_stage, "invoke_hook"):
            return
        try:
            await policy_stage.invoke_hook(state, hook_name)
        except Exception as e:
            logger.warning("[Pipeline] Policy gate %s 호출 실패: %s", hook_name, e)

    def _get_step_number(self, stage: Stage) -> int:
        for i, s in enumerate(self._all_stages, 1):
            if s.stage_id == stage.stage_id:
                return i
        return 0
