"""
S00 Harness — 메타 스테이지 (본문 LLM 호출 진입점)

v1.1.0 — Planner OFF 고정 직선 흐름. 과거 LLM 계획자(HarnessPlanner)로
Stage/파라미터/Strategy 를 런타임 조립하던 경로는 제거됨.

현재 책임:
  - execute() 는 사실상 noop — 호환용으로 빈 `HarnessPlan.off_mode()` 만 박음.
    (Pipeline 이 이 Stage 를 ingress 최상단 prepend 하지 않으므로 실제 호출 X)
  - main_call() 만 살아있어 Phase B main_actor 직전 본문 LLM 호출 담당.
    Transport Strategy (streaming/batch/…) 를 StrategyResolver 로 해석해 위임.

비담당 (명시):
  - 실제 Stage 실행은 Pipeline 이 함
  - provider 초기화는 `core.provider_bootstrap` 에 위임 (s07 과 공용)
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState

logger = logging.getLogger("harness.stage.harness")


class HarnessStage(Stage):
    """메타 스테이지 — Planner 가 조립 결정을 내리는 단일 지점."""

    @property
    def stage_id(self) -> str:
        return "s00_harness"

    @property
    def role(self) -> str:
        # v0.16.6 — Pipeline 이 이 Stage 를 ingress 최상단 prepend + bypass 금지 +
        # Phase B replan 대상으로 특수 처리. Pipeline 은 이름 "s00_harness" 대신
        # 이 role 을 본다.
        return "orchestrator_planner"

    @property
    def order(self) -> int:
        # s01 보다 먼저. Stage.phase 기본 계산은 order<=4 면 ingress 라 문제 없음.
        return 0

    @property
    def phase(self) -> str:
        return "ingress"

    def list_strategies(self) -> list[StrategyInfo]:
        # v0.14.0: Transport Strategy 를 자기서술 — StrategyResolver 에 등록된 모든
        # impl 을 동적으로 노출. 리터럴 목록 하드코딩 금지 (재귀적 자율주행).
        from ...core.strategy_resolver import _REGISTRY, _ensure_defaults_registered
        _ensure_defaults_registered()
        entries: list[StrategyInfo] = []
        for (sid, slot, impl), cls in sorted(_REGISTRY.items()):
            if sid != "s00_harness" or slot != "transport":
                continue
            try:
                inst = cls()
                entries.append(StrategyInfo(
                    name=impl,
                    description=getattr(inst, "description", "") or "",
                    is_default=(impl == "streaming"),
                ))
            except Exception:
                entries.append(StrategyInfo(name=impl, description="", is_default=(impl == "streaming")))
        if not entries:
            # 레지스트리 비어있을 때의 최후 폴백 — 기본 두 개 이름만.
            entries = [
                StrategyInfo(name="streaming", description="SSE 스트리밍", is_default=True),
                StrategyInfo(name="batch", description="비스트리밍 단일 호출"),
            ]
        return entries

    async def execute(self, state: PipelineState) -> dict:
        """v1.1.0 — Planner OFF 고정. Plan 생성 분기 dead code 제거.

        Pipeline 은 ingress 최상단 prepend 안 함 → 이 execute 는 사실상 호출되지
        않음. main_call 만 살아있어 Phase B main_actor 직전 본문 LLM 호출 담당.
        호환을 위해 빈 off-mode Plan 만 반환 (구 row 가 직접 호출하는 경우 대비).
        """
        from ...core.planner import HarnessPlan

        plan = HarnessPlan.off_mode("planner_disabled_v1_1")
        state.metadata["harness_plan"] = plan.to_dict()
        return {
            "planner_source": plan.source,
            "chosen_count": 0,
            "skipped_count": 0,
        }

    async def main_call(self, state: PipelineState, *, strategy: str = "streaming") -> dict:
        """v0.14.0 — 본문 LLM 호출 진입점. Pipeline Phase B 루프에서 호출.

        과거 s07_llm.execute 가 하던 역할. Transport Strategy (streaming/batch/…)
        를 StrategyResolver 에서 이름으로 해석해 위임. 리터럴 분기 없음.
        """
        from ...stages.interfaces import TransportStrategy
        from ...core.strategy_resolver import StrategyResolver

        resolver = StrategyResolver.default()
        transport = resolver.resolve("s00_harness", "transport", strategy)
        if transport is None or not isinstance(transport, TransportStrategy):
            # 레지스트리에 없는 이름 — 기본값 streaming 으로 재시도
            transport = resolver.resolve("s00_harness", "transport", "streaming")
        if transport is None:
            raise RuntimeError(
                f"Transport strategy '{strategy}' not registered for s00_harness"
            )
        return await transport.call(state)

    # ── helpers ─────────────────────────────────────────────────────

    def _merge_plan_into_config(self, state: PipelineState, plan: "HarnessPlan") -> None:  # type: ignore  # noqa: F821
        """Plan 의 params/strategies/max_iterations 를 `state.config` 에 반영한다.

        v1.24 — "환경만 주어주고 알아서 조립" 의 실전 구현. 단 ungated 직접 변이가
        아니라 **gated RuntimeConfigMutator 경유**로 바뀌었다 (자가설정 노드 seam 부활):
          - mode = config.runtime_self_govern (기본 "off") → 기본은 no-op (동작 변화 0).
          - "observe" → 적용 없이 제안만 기록, "act" → legality 검증·inverse 저널 후 적용.
        활성화 정책은 이식 노드 파라미터가 opt-in 한다 (엔진=메커니즘, 이식=정책).
        """
        config = state.config
        if not config:
            return

        mutator = state.get_config_mutator()
        applied = mutator.apply_plan(plan)
        if applied:
            logger.info(
                "[Harness] Plan 반영 mode=%s applied=%d diff=%s",
                mutator.mode, applied, mutator.diff(),
            )
        # mutator 핸들을 노출 — 이식측 SSE 중계가 config_diff 이벤트로 캔버스에 push.
        state.metadata["config_mutator"] = mutator

        # orchestrator_hint 는 엔진에서는 메타데이터로만 기록.
        # 이식측 dispatcher / 프론트 PlanningCard 가 해석.
        hint = getattr(plan, "orchestrator_hint", "") or ""
        if hint:
            state.metadata["orchestrator_hint"] = hint
