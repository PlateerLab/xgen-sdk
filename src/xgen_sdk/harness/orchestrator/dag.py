"""
DAG Orchestrator — 멀티 에이전트 DAG 실행

워크플로우 간 움직임이 많아서 오케스트레이터가 필수.
각 노드 = 하네스 파이프라인 1개 (단일 에이전트).
엣지 = 이전 에이전트 출력 → 다음 에이전트 입력.

기존 xgen-workflow의 DAG 토폴로지 정렬 + 병렬 실행 패턴 참고.
AsyncWorkflowExecutor의 workflow_tracker.py 패턴 차용.

구조:
- AgentNode: 하네스 파이프라인 1개의 설정
- DAGEdge: 노드 간 데이터 흐름
- DAGOrchestrator: 토폴로지 정렬 → 레벨별 병렬 실행
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core.config import HarnessConfig
from ..core.pipeline import Pipeline
from ..core.state import PipelineState
from ..events.emitter import EventEmitter
from ..events.types import (
    StageEnterEvent,
    StageExitEvent,
    DoneEvent,
    ErrorEvent,
    MetricsEvent,
)

logger = logging.getLogger("harness.orchestrator")


class DAGCycleError(ValueError):
    """DAG 에 사이클이 존재 — `_topological_levels` 에서 감지 시 발생 (v0.11.27)."""


@dataclass
class AgentNode:
    """DAG의 노드 — 하네스 파이프라인 1개"""
    node_id: str
    name: str
    config: HarnessConfig
    system_prompt: str = ""
    # 입력 변환 함수: 이전 노드 출력들 → 이 노드의 user_input
    input_transformer: Optional[Callable[[dict[str, str]], str]] = None
    # 추가 컨텍스트 (RAG, 파일 등)
    rag_context: str = ""
    tool_definitions: list[dict] = field(default_factory=list)


@dataclass
class DAGEdge:
    """노드 간 연결"""
    source: str      # source node_id
    target: str      # target node_id
    label: str = ""  # 엣지 설명 (선택)


@dataclass
class NodeResult:
    """개별 노드 실행 결과"""
    node_id: str
    output: str
    success: bool
    duration_ms: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""


@dataclass
class DAGResult:
    """전체 DAG 실행 결과"""
    results: dict[str, NodeResult] = field(default_factory=dict)
    execution_order: list[list[str]] = field(default_factory=list)  # 레벨별 실행 순서
    total_duration_ms: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    success: bool = True
    final_output: str = ""  # 마지막 노드의 출력

    def get_output(self, node_id: str) -> str:
        """특정 노드의 출력 가져오기"""
        r = self.results.get(node_id)
        return r.output if r else ""


class DAGOrchestrator:
    """
    DAG 기반 멀티 에이전트 오케스트레이터 — **DAG 실행의 단일 진입점** (v0.11.24).

    엔진 내 DAG 실행 경로는 다음 3 진입점이 **모두 이 클래스의 `run()`** 으로 수렴한다:

    1. `DAGOrchestrator.run()` 직접 호출 — 저수준 API (외부 기여자가 노드/엣지를 직접 구성)
    2. `orchestrator.multi_agent.MultiAgentExecutor` — `workflow_data` JSON 을 파싱해 DAG 구성
       후 `DAGOrchestrator.run()` 에 위임 (워크플로우 캔버스 → DAG 변환 전담)
    3. `stages.multi_agent_planner.MultiAgentPlannerStage` — s05 Strategy 슬롯. 복잡도 감지 시
       sub-agent 를 build 해서 `DAGOrchestrator.run()` 으로 escalate (런타임 fan-out 전담)

    세 entry 가 모두 이 `run()` 에 수렴하므로 실행·병렬화·재시도·에러 복구 로직은 여기 한
    곳에서만 관리한다. 한 곳 수정이 누락되면 세 경로 모두 같이 영향을 받는다.

    사용법:
        orch = DAGOrchestrator()
        orch.add_node(AgentNode(node_id="researcher", ...))
        orch.add_node(AgentNode(node_id="writer", ...))
        orch.add_edge(DAGEdge(source="researcher", target="writer"))
        result = await orch.run("사용자 질문")
    """

    def __init__(self, event_emitter: Optional[EventEmitter] = None):
        self._nodes: dict[str, AgentNode] = {}
        self._edges: list[DAGEdge] = []
        self._event_emitter = event_emitter or EventEmitter()

    def add_node(self, node: AgentNode) -> "DAGOrchestrator":
        self._nodes[node.node_id] = node
        return self

    def add_edge(self, edge: DAGEdge) -> "DAGOrchestrator":
        self._edges.append(edge)
        return self

    async def run(self, initial_input: str) -> DAGResult:
        """DAG 실행 — 토폴로지 정렬 → 레벨별 병렬 실행"""
        start = time.time()
        dag_result = DAGResult()

        # 1. 토폴로지 정렬 — 레벨별 그룹화
        levels = self._topological_levels()
        dag_result.execution_order = levels

        logger.info("[Orchestrator] DAG: %d nodes, %d levels", len(self._nodes), len(levels))
        for i, level in enumerate(levels):
            logger.info("[Orchestrator] Level %d: %s", i, level)

        # 2. 레벨별 실행 (같은 레벨은 병렬)
        node_outputs: dict[str, str] = {}

        for level_idx, level_nodes in enumerate(levels):
            # 오케스트레이터 이벤트
            await self._event_emitter.emit(StageEnterEvent(
                stage_id=f"orchestrator_level_{level_idx}",
                stage_name=f"레벨 {level_idx} 실행 ({', '.join(level_nodes)})",
                phase="orchestrator",
                step=level_idx + 1,
                total=len(levels),
            ))

            # 같은 레벨의 노드들은 병렬 실행
            tasks = []
            for node_id in level_nodes:
                node = self._nodes[node_id]
                # 입력 결정: 루트 노드 → initial_input, 나머지 → 이전 노드 출력
                predecessors = self._get_predecessors(node_id)
                if not predecessors:
                    user_input = initial_input
                elif node.input_transformer:
                    pred_outputs = {p: node_outputs.get(p, "") for p in predecessors}
                    user_input = node.input_transformer(pred_outputs)
                else:
                    # 기본: 이전 노드 출력을 합쳐서 전달
                    parts = []
                    for p in predecessors:
                        output = node_outputs.get(p, "")
                        if output:
                            parts.append(f"[{self._nodes[p].name}의 결과]\n{output}")
                    user_input = "\n\n---\n\n".join(parts) if parts else initial_input

                tasks.append(self._run_node(node, user_input))

            # 병렬 실행
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for node_id, result in zip(level_nodes, results):
                if isinstance(result, Exception):
                    nr = NodeResult(
                        node_id=node_id,
                        output="",
                        success=False,
                        error=str(result),
                    )
                    dag_result.success = False
                    logger.error("[Orchestrator] Node %s failed: %s", node_id, result)
                else:
                    nr = result
                    node_outputs[node_id] = nr.output

                dag_result.results[node_id] = nr
                dag_result.total_tokens += nr.tokens
                dag_result.total_cost_usd += nr.cost_usd

            await self._event_emitter.emit(StageExitEvent(
                stage_id=f"orchestrator_level_{level_idx}",
                stage_name=f"레벨 {level_idx} 완료",
                output={nid: dag_result.results[nid].success for nid in level_nodes},
                step=level_idx + 1,
                total=len(levels),
            ))

        # 3. 최종 결과 = 마지막 레벨의 출력 합치기
        if levels:
            final_parts = []
            for node_id in levels[-1]:
                output = node_outputs.get(node_id, "")
                if output:
                    if len(levels[-1]) > 1:
                        final_parts.append(f"[{self._nodes[node_id].name}]\n{output}")
                    else:
                        final_parts.append(output)
            dag_result.final_output = "\n\n".join(final_parts)

        dag_result.total_duration_ms = int((time.time() - start) * 1000)

        # 메트릭스 이벤트
        await self._event_emitter.emit(MetricsEvent(
            duration_ms=dag_result.total_duration_ms,
            total_tokens=dag_result.total_tokens,
            cost_usd=dag_result.total_cost_usd,
            model="orchestrator",
        ))

        await self._event_emitter.emit(DoneEvent(
            final_output=dag_result.final_output,
            success=dag_result.success,
        ))

        logger.info(
            "[Orchestrator] Done: %dms, %d tokens, $%.4f, success=%s",
            dag_result.total_duration_ms,
            dag_result.total_tokens,
            dag_result.total_cost_usd,
            dag_result.success,
        )
        return dag_result

    async def _run_node(self, node: AgentNode, user_input: str) -> NodeResult:
        """단일 에이전트 노드 실행"""
        t0 = time.time()
        logger.info("[Orchestrator] Running node: %s (%s)", node.node_id, node.name)

        config = node.config
        if node.system_prompt:
            config.system_prompt = node.system_prompt

        emitter = EventEmitter()
        pipeline = Pipeline.from_config(config, emitter)

        # v0.26.6 — PipelineState 가 v0.11.22+ 에서 tool 을 ToolGroup 으로 도메인
        # 그룹화 (tool_definitions 는 property shim). dataclass __init__ kwarg 로
        # tool_definitions 직접 못 넘김 → TypeError. setter 로 박는다.
        # 라이브 검증 발견: DAG 멀티 하네스 100% "PipelineState.__init__() got an
        # unexpected keyword argument 'tool_definitions'" 에러.
        state = PipelineState(
            user_input=user_input,
            rag_context=node.rag_context,
        )
        if node.tool_definitions:
            state.tool_definitions = node.tool_definitions
        # sub-pipeline 의 모든 Stage 가 verbose 이벤트를 발행하도록 emitter 직접 주입.
        # 이게 없으면 sub-agent 의 stage_enter / substep / metrics 가 메인 SSE 에 안 흐름.
        state.event_emitter = emitter
        state.config = config       # ← stage_params / artifacts 가 sub 에서도 적용되게

        async def _forward_events():
            try:
                async for event in emitter.stream():
                    # sub Pipeline 의 DoneEvent 는 *그 노드 종료* 신호일 뿐 DAG 전체 종료가 아니다.
                    # forward 하면 외부 stream() 이 첫 노드 DoneEvent 에서 자동 break (emitter.py
                    # line 102) — 두 번째 노드 이벤트가 외부 클라이언트에 도달 못 함.
                    # DAG 전체 DoneEvent 는 run() 마지막에 별도로 emit 한다.
                    if isinstance(event, DoneEvent):
                        continue
                    if hasattr(event, "stage_name"):
                        event.stage_name = f"[{node.name}] {event.stage_name}"
                    await self._event_emitter.emit(event)
            except Exception as e:
                logger.warning("[Orchestrator] forward stopped: %s", e)

        forward_task = asyncio.create_task(_forward_events())
        try:
            await pipeline.run(state)
        finally:
            # 반드시 sub-emitter 를 닫아야 forward_task 가 stream() 루프를 빠져나옴.
            try:
                await emitter.close()
            except Exception as e:
                logger.debug("[DAG] sub-emitter close suppressed: %s", e)
        try:
            await asyncio.wait_for(forward_task, timeout=2.0)
        except asyncio.TimeoutError:
            forward_task.cancel()

        elapsed = int((time.time() - t0) * 1000)
        return NodeResult(
            node_id=node.node_id,
            output=state.final_output or state.last_assistant_text,
            success=True,
            duration_ms=elapsed,
            tokens=state.token_usage.total,
            cost_usd=state.cost_usd,
        )

    def _topological_levels(self) -> list[list[str]]:
        """토폴로지 정렬 — 레벨별 그룹화 (Kahn's algorithm).

        v0.11.27 — 사이클 감지 추가. Kahn 이 처리한 노드 수가 전체 노드 수보다 적으면
        사이클이 존재한다는 뜻. 이전에는 빈 level 을 조용히 반환해 사이클 노드가
        실행에서 누락됐다. 이제 `DAGCycleError` 로 명시적 실패.
        """
        in_degree: dict[str, int] = defaultdict(int)
        adj: dict[str, list[str]] = defaultdict(list)

        for node_id in self._nodes:
            in_degree[node_id] = 0

        for edge in self._edges:
            adj[edge.source].append(edge.target)
            in_degree[edge.target] += 1

        # BFS
        queue = deque([n for n in self._nodes if in_degree[n] == 0])
        levels: list[list[str]] = []
        processed = 0

        while queue:
            level = list(queue)
            levels.append(level)
            processed += len(level)
            next_queue: deque[str] = deque()
            for node_id in level:
                for neighbor in adj[node_id]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        next_queue.append(neighbor)
            queue = next_queue

        if processed != len(self._nodes):
            unresolved = [n for n in self._nodes if in_degree[n] > 0]
            raise DAGCycleError(
                f"DAG 에 사이클이 존재합니다: 미해결 노드 {unresolved} "
                f"(총 {len(self._nodes)}개 중 {processed}개만 위상정렬 가능). "
                "edges 를 점검하거나 orchestrator.remove_edge() 로 순환을 끊어주세요."
            )

        return levels

    def _get_predecessors(self, node_id: str) -> list[str]:
        """노드의 선행 노드 목록"""
        return [e.source for e in self._edges if e.target == node_id]
