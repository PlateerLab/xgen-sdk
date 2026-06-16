"""MultiAgentPlannerStage — 's00_harness' 슬롯의 'multi_agent' strategy (v1.0).

흐름:
1. ComplexityDetector 가 escalate 판정.
2. escalate=False 면 즉시 bypass → 단일 에이전트 흐름 그대로.
3. escalate=True 면 RAG 컬렉션 / 인텐트 절 별로 SubAgent N 개를 만들고
   기존 DAGOrchestrator 로 병렬 실행.
4. 각 sub-agent 출력을 한 system_prompt 부록으로 묶어서 state 에 주입.
   → 본 파이프라인의 s00_harness.main_call 이 자연스럽게 종합 답변을 만든다.

v1.0 — 슬롯 이전: 구 's05_strategy' (삭제됨) → 's00_harness' 의 multi_agent strategy.
       Planner 가 multi-agent 가 필요하다고 판단하면 s00 의 strategy 카드 픽으로 활성.

캔버스 데이터 의존 0. harness_config + state 만으로 동작.
"""

from __future__ import annotations

import dataclasses
import logging

from ..core.config import HarnessConfig
from ..core.stage import Stage, StrategyInfo
from ..core.state import PipelineState
from ..events.emitter import EventEmitter
from ..events.types import StageSubstepEvent
from .complexity import ComplexityDetector
from .dag import AgentNode, DAGEdge, DAGOrchestrator

# sub-agent 의 system_prompt 템플릿 — stage_params['sub_agent_prompt_template'] 로 override.
# {col} = 컬렉션 이름 placeholder. 외부 작업자가 자기 톤으로 바꿀 수 있게 분리.
DEFAULT_SUB_PROMPT_TEMPLATE = (
    "너는 '{col}' 컬렉션에 대해서만 답하는 전문 sub-agent. "
    "주어진 사용자 질의에서 이 컬렉션과 관련된 부분만 응답. 1500자 이내."
)

# 하위 슬롯 stage_id 상수 — 문자열 리터럴 반복 제거.
PLAN_SLOT = "s00_harness"  # v1.0 — 구 "s05_strategy" 슬롯 삭제됨, multi_agent strategy 가 s00 의 한 변형
TOOL_INDEX_SLOT = "s04_tool"
CONTEXT_SLOT = "s06_context"

logger = logging.getLogger("harness.orchestrator.planner")


class MultiAgentPlannerStage(Stage):
    """s00_harness 슬롯의 multi_agent strategy (v1.0) — 자동 분기 + 종합."""

    @property
    def stage_id(self) -> str:
        return PLAN_SLOT

    @property
    def order(self) -> int:
        return 0

    def should_bypass(self, state: PipelineState) -> bool:
        return state.loop_iteration > 1

    async def execute(self, state: PipelineState) -> dict:
        config = state.config
        rag_collections: list[str] = self._collect_rag_collections(state)
        capabilities: list[str] = list(getattr(config, "capabilities", []) or []) if config else []
        tool_count = len(state.tool_definitions or [])

        detector = ComplexityDetector(
            long_input_chars=int(self.get_param("long_input_chars", state, 280)),
            rag_threshold=int(self.get_param("rag_threshold", state, 2)),
            capability_threshold=int(self.get_param("capability_threshold", state, 2)),
            tool_threshold=int(self.get_param("tool_threshold", state, 8)),
            score_to_escalate=int(self.get_param("score_to_escalate", state, 2)),
        )
        verdict = detector.evaluate(
            user_input=state.user_input or "",
            rag_collections=rag_collections,
            capabilities=capabilities,
            tool_count=tool_count,
        )

        await state.emit_verbose(StageSubstepEvent(
            stage_id=self.stage_id,
            substep="complexity_evaluated",
            meta={
                "escalate": verdict.escalate,
                "score": verdict.score,
                "signals": verdict.signals,
                "reasons": verdict.reasons,
            },
        ))

        if not verdict.escalate:
            return {
                "multi_agent": False,
                "complexity_score": verdict.score,
                "reasons": verdict.reasons,
            }

        # ── escalate: sub-agent DAG 구성 ──
        sub_agents = self._build_sub_agents(state, rag_collections, config)
        if not sub_agents:
            return {
                "multi_agent": False,
                "complexity_score": verdict.score,
                "skipped": "no_subtasks",
            }

        emitter = state.event_emitter or EventEmitter()
        orchestrator = DAGOrchestrator(emitter)
        for node in sub_agents:
            orchestrator.add_node(node)
        # 모든 sub-agent → 가상 aggregator 단계는 만들지 않음 (병렬 fan-out only).
        # 의존성 추가가 필요하면 fan_out_strategy 별로 add_edge.

        await state.emit_verbose(StageSubstepEvent(
            stage_id=self.stage_id,
            substep="multi_agent_run_start",
            meta={"sub_agent_count": len(sub_agents)},
        ))

        result = await orchestrator.run(state.user_input or "")

        # 결과 종합: 본 파이프라인의 system_prompt 에 sub-agent 결과를 부록으로 추가.
        # s00_harness.main_call 이 이걸 컨텍스트로 종합 답변을 생성.
        appendix_parts = ["\n\n<sub_agent_results>"]
        for node in sub_agents:
            r = result.results.get(node.node_id)
            if not r:
                continue
            appendix_parts.append(
                f"<agent name=\"{node.name}\" success=\"{r.success}\">"
                f"{r.output[:4000]}"
                f"</agent>"
            )
        appendix_parts.append("</sub_agent_results>")
        state.system_prompt = (state.system_prompt or "") + "\n".join(appendix_parts)

        await state.emit_verbose(StageSubstepEvent(
            stage_id=self.stage_id,
            substep="multi_agent_run_complete",
            meta={
                "duration_ms": result.total_duration_ms,
                "tokens": result.total_tokens,
                "cost_usd": result.total_cost_usd,
                "success": result.success,
            },
        ))

        logger.info(
            "[MultiAgentPlanner] escalated: %d sub-agents, %dms, %d tokens",
            len(sub_agents), result.total_duration_ms, result.total_tokens,
        )
        return {
            "multi_agent": True,
            "complexity_score": verdict.score,
            "reasons": verdict.reasons,
            "sub_agent_count": len(sub_agents),
            "duration_ms": result.total_duration_ms,
            "tokens": result.total_tokens,
            "cost_usd": result.total_cost_usd,
        }

    def _collect_rag_collections(self, state: PipelineState) -> list[str]:
        """RAG 컬렉션은 s04_tool 와 s06_context 두 곳의 stage_params 에 흩어짐.
        둘 다 모아서 dedupe. dict/str 형태 모두 허용."""
        config = state.config
        seen: list[str] = []
        if not config:
            return seen
        for sid in (TOOL_INDEX_SLOT, CONTEXT_SLOT):
            params = (config.stage_params or {}).get(sid, {}) or {}
            for c in params.get("rag_collections", []) or []:
                name = c if isinstance(c, str) else (c.get("collection") if isinstance(c, dict) else None)
                if name and name not in seen:
                    seen.append(name)
        # state.metadata fallback (Builder 흐름)
        for c in (state.metadata or {}).get("rag_collections", []) or []:
            name = c if isinstance(c, str) else (c.get("collection") if isinstance(c, dict) else None)
            if name and name not in seen:
                seen.append(name)
        return seen

    def _build_sub_agents(
        self,
        state: PipelineState,
        rag_collections: list[str],
        base_config: HarnessConfig | None,
    ) -> list[AgentNode]:
        """fan-out 전략 디스패치 — `_FAN_OUT_STRATEGIES` 레지스트리.

        외부 작업자가 `register_fan_out_strategy('per_intent', fn)` 한 줄로 추가 가능.
        """
        if not base_config:
            return []
        strategy = self.get_param("fan_out", state, "per_rag_collection")
        builder = _FAN_OUT_STRATEGIES.get(strategy)
        if builder is None:
            logger.warning("[MultiAgentPlanner] unknown fan_out strategy '%s', skipping", strategy)
            return []
        prompt_template = self.get_param(
            "sub_agent_prompt_template", state, DEFAULT_SUB_PROMPT_TEMPLATE,
        )
        return builder(
            base_config=base_config,
            rag_collections=rag_collections,
            prompt_template=prompt_template,
        )

    def list_strategies(self) -> list[StrategyInfo]:
        return [
            StrategyInfo(name, info["desc"], is_default=info.get("default", False))
            for name, info in _FAN_OUT_STRATEGIES_META.items()
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fan-out 전략 레지스트리 — 외부 확장 통로
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_FAN_OUT_STRATEGIES: dict = {}
_FAN_OUT_STRATEGIES_META: dict = {}


def register_fan_out_strategy(
    name: str, builder, description: str, is_default: bool = False,
) -> None:
    """외부 작업자가 새 fan-out 전략을 한 줄로 등록.

    builder 시그니처: (base_config, rag_collections, prompt_template) -> list[AgentNode]
    """
    _FAN_OUT_STRATEGIES[name] = builder
    _FAN_OUT_STRATEGIES_META[name] = {"desc": description, "default": is_default}


_FAN_OUT_DISCOVERED = False


def _discover_fan_out_from_entry_points() -> None:
    """entry_points 그룹 ``xgen_harness.fan_out_strategies`` 자동 발견. idempotent.

    외부 패키지가 노출:
      [project.entry-points."xgen_harness.fan_out_strategies"]
      per_intent = "my_pkg.orchestrator:per_intent_builder"

    entry_point 가 builder callable 을 직접 반환하거나
    {"name?", "builder", "description?", "is_default?"} dict 반환 모두 허용.
    """
    global _FAN_OUT_DISCOVERED
    if _FAN_OUT_DISCOVERED:
        return
    _FAN_OUT_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.fan_out_strategies"
        items = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])  # type: ignore[arg-type]
        for ep in items:
            try:
                produced = ep.load()
                if callable(produced) and not isinstance(produced, dict):
                    register_fan_out_strategy(ep.name, produced, description=f"(entry_points: {ep.value})")
                elif isinstance(produced, dict) and produced.get("builder"):
                    register_fan_out_strategy(
                        produced.get("name", ep.name),
                        produced["builder"],
                        description=produced.get("description", ""),
                        is_default=bool(produced.get("is_default", False)),
                    )
            except Exception as e:
                logger.warning("[fan_out] entry_point %s 로드 실패: %s", ep.name, e)
    except Exception as e:
        logger.debug("[fan_out] entry_points discovery 실패: %s", e)


_discover_fan_out_from_entry_points()


def _clone_config_for_sub(
    base_config: HarnessConfig,
    *,
    system_prompt: str,
    rag_collection: str,
) -> HarnessConfig:
    """base_config 의 모든 필드를 복제 + sub-agent 용으로 일부만 override.

    재진입 방지: artifacts[s00_harness] = 'default' 강제 (multi_agent 카드 무한 재귀 차단).
    """
    base_dict = dataclasses.asdict(base_config)
    # disabled_stages 는 set 인데 asdict 가 list 로 변환 → 다시 set 화 필요
    base_dict["disabled_stages"] = set(base_dict.get("disabled_stages", []))
    base_dict["system_prompt"] = system_prompt
    sub_artifacts = dict(base_dict.get("artifacts", {}) or {})
    sub_artifacts[PLAN_SLOT] = "default"
    base_dict["artifacts"] = sub_artifacts
    sub_params = dict(base_dict.get("stage_params", {}) or {})
    sub_params[TOOL_INDEX_SLOT] = {
        **(sub_params.get(TOOL_INDEX_SLOT) or {}),
        "rag_collections": [rag_collection],
    }
    sub_params[CONTEXT_SLOT] = {
        **(sub_params.get(CONTEXT_SLOT) or {}),
        "rag_collections": [rag_collection],
    }
    base_dict["stage_params"] = sub_params
    return HarnessConfig(**base_dict)


def _per_rag_collection(
    *, base_config: HarnessConfig, rag_collections: list[str], prompt_template: str,
) -> list[AgentNode]:
    if not rag_collections:
        return []
    nodes: list[AgentNode] = []
    for col in rag_collections:
        sub_cfg = _clone_config_for_sub(
            base_config,
            system_prompt=prompt_template.format(col=col),
            rag_collection=col,
        )
        nodes.append(AgentNode(
            node_id=f"sub_{col}", name=f"RAG[{col}]", config=sub_cfg,
        ))
    return nodes


# 기본 전략 등록
register_fan_out_strategy(
    "per_rag_collection", _per_rag_collection,
    "RAG 컬렉션 별로 sub-agent fan-out (문서 RAG 가 주 use case)",
    is_default=True,
)
