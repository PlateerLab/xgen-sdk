"""
MultiAgentExecutor — 워크플로우 기반 멀티 에이전트 실행

xgen-workflow의 워크플로우 데이터(nodes, edges)에서
에이전트 노드를 추출하여 DAG 오케스트레이터로 실행.

기존 AsyncWorkflowExecutor가 모든 노드를 DAG로 실행했다면,
이건 에이전트 노드만 추출하여 하네스 파이프라인으로 실행.
비에이전트 노드(RAG, MCP, Input 등)는 에이전트의 설정으로 흡수.
"""

import logging
from typing import Any, Optional

from ..core.config import HarnessConfig
from ..events.emitter import EventEmitter
from .dag import AgentNode, DAGEdge, DAGOrchestrator, DAGResult

logger = logging.getLogger("harness.orchestrator.multi_agent")

AGENT_NODE_PREFIXES = {"agents/"}
RAG_NODE_IDS = {"document_loaders/", "VectorDB"}
MCP_NODE_PREFIX = "mcp/"
TOOL_NODE_IDS = {"input_string", "input_int", "input_files", "local_cli_tool", "print_any"}


class MultiAgentExecutor:
    """
    워크플로우 데이터에서 멀티 에이전트 DAG를 자동 구성하고 실행.

    사용법:
        executor = MultiAgentExecutor(workflow_data, event_emitter)
        result = await executor.run("사용자 입력")
    """

    def __init__(
        self,
        workflow_data: dict[str, Any],
        event_emitter: Optional[EventEmitter] = None,
        default_provider: str = "",  # "" → providers.get_default_provider() 로 해석
        default_model: str = "",  # 비어있으면 PROVIDER_DEFAULT_MODEL 에서 해석
    ):
        from ..providers import get_default_provider, get_default_model
        self._workflow_data = workflow_data
        self._emitter = event_emitter or EventEmitter()
        self._default_provider = default_provider or get_default_provider()
        self._default_model = default_model or get_default_model(self._default_provider)

    async def run(self, initial_input: str) -> DAGResult:
        """워크플로우에서 에이전트 DAG 구성 → 실행"""
        orchestrator = self._build_dag()

        if not orchestrator._nodes:
            logger.warning("[MultiAgent] No agent nodes found in workflow")
            # 단일 에이전트 폴백
            orchestrator.add_node(AgentNode(
                node_id="default_agent",
                name="Agent",
                config=HarnessConfig(
                    provider=self._default_provider,
                    model=self._default_model,
                ),
            ))

        return await orchestrator.run(initial_input)

    def _build_dag(self) -> DAGOrchestrator:
        """워크플로우 데이터에서 DAG 구성"""
        orchestrator = DAGOrchestrator(self._emitter)

        nodes = self._workflow_data.get("nodes", [])
        edges = self._workflow_data.get("edges", [])

        # 1. 에이전트 노드 추출
        agent_node_map: dict[str, AgentNode] = {}  # react_flow_id → AgentNode
        react_to_agent: dict[str, str] = {}  # react_flow_id → agent_node_id

        for node in nodes:
            rf_id = node.get("id", "")
            data = node.get("data", {})
            node_type = data.get("id", "")

            if not any(node_type.startswith(p) for p in AGENT_NODE_PREFIXES):
                continue

            parameters = data.get("parameters", [])
            agent_id = f"agent_{rf_id}"

            # 에이전트 설정 추출
            config = self._extract_config(parameters)
            system_prompt = self._get_param(parameters, "system_prompt") or ""

            agent_node = AgentNode(
                node_id=agent_id,
                name=data.get("nodeName", node_type),
                config=config,
                system_prompt=system_prompt,
            )

            # 연결된 RAG/MCP/도구 노드에서 설정 흡수
            self._absorb_connected_nodes(agent_node, rf_id, nodes, edges)

            agent_node_map[rf_id] = agent_node
            react_to_agent[rf_id] = agent_id
            orchestrator.add_node(agent_node)

        # 2. 에이전트 간 엣지 추출
        for edge in edges:
            source_rf = edge.get("source", "")
            target_rf = edge.get("target", "")

            # 직접 연결
            if source_rf in react_to_agent and target_rf in react_to_agent:
                orchestrator.add_edge(DAGEdge(
                    source=react_to_agent[source_rf],
                    target=react_to_agent[target_rf],
                ))
                continue

            # 간접 연결 (비에이전트 노드를 건너뛰기)
            if source_rf in react_to_agent:
                downstream_agents = self._find_downstream_agents(
                    target_rf, edges, react_to_agent,
                )
                for da_id in downstream_agents:
                    orchestrator.add_edge(DAGEdge(
                        source=react_to_agent[source_rf],
                        target=da_id,
                    ))

        return orchestrator

    def _extract_config(self, parameters: list) -> HarnessConfig:
        """노드 파라미터에서 HarnessConfig 생성"""
        provider = self._get_param(parameters, "provider") or self._default_provider
        model = self._get_param(parameters, "model") or ""

        if not model:
            model_field = f"{provider}_model"
            model = self._get_param(parameters, model_field) or self._default_model

        temperature = float(self._get_param(parameters, "temperature") or "0.7")

        return HarnessConfig(
            preset="standard",
            provider=provider,
            model=model,
            temperature=temperature,
        )

    def _absorb_connected_nodes(
        self,
        agent_node: AgentNode,
        rf_id: str,
        nodes: list,
        edges: list,
    ) -> None:
        """에이전트에 연결된 RAG/MCP/도구 노드의 설정을 흡수"""
        connected_rf_ids = set()
        for edge in edges:
            if edge.get("target") == rf_id:
                connected_rf_ids.add(edge.get("source", ""))
            if edge.get("source") == rf_id:
                connected_rf_ids.add(edge.get("target", ""))

        for node in nodes:
            nid = node.get("id", "")
            if nid not in connected_rf_ids:
                continue

            data = node.get("data", {})
            node_type = data.get("id", "")
            params = data.get("parameters", [])

            # MCP 세션 → tool_definitions에 추가할 것 (실행 시점에 MCP discover)
            if node_type.startswith(MCP_NODE_PREFIX):
                session_id = self._get_param(params, "session_id")
                if session_id and session_id != "Select Session":
                    agent_node.config.system_prompt = (
                        (agent_node.config.system_prompt or "") +
                        f"\n[MCP Session: {session_id}]"
                    )

            # RAG 노드 → rag_context는 실행 시점에 채움
            elif any(node_type.startswith(p) for p in RAG_NODE_IDS if isinstance(p, str)):
                collection = self._get_param(params, "collection_name")
                if collection:
                    agent_node.config.system_prompt = (
                        (agent_node.config.system_prompt or "") +
                        f"\n[RAG Collection: {collection}]"
                    )

    def _find_downstream_agents(
        self,
        start_rf_id: str,
        edges: list,
        react_to_agent: dict,
        visited: set = None,
    ) -> list[str]:
        """비에이전트 노드를 건너뛰고 다음 에이전트 노드를 찾기"""
        if visited is None:
            visited = set()
        if start_rf_id in visited:
            return []
        visited.add(start_rf_id)

        if start_rf_id in react_to_agent:
            return [react_to_agent[start_rf_id]]

        result = []
        for edge in edges:
            if edge.get("source") == start_rf_id:
                target = edge.get("target", "")
                result.extend(self._find_downstream_agents(target, edges, react_to_agent, visited))
        return result

    @staticmethod
    def _get_param(parameters: list, param_id: str) -> str:
        for p in parameters:
            if p.get("id") == param_id:
                val = p.get("value")
                return str(val) if val is not None else ""
        return ""
