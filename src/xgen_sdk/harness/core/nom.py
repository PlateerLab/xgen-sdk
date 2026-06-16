"""
NOM — Node Object Model (v0.16.0).

철학:
  하네스 생태계의 모든 실행 단위 — Stage / Strategy / Tool / MCP server / xgen-workflow Node —
  를 **단일 IR(Intermediate Representation)** 로 통일. 컴파일러 / 샌드박스 /
  갤러리 / Tool Synthesis 가 모두 이 IR 을 주고받아 상호운용한다.

왜 필요한가:
  - 지금까지는 Stage/Tool/MCP 가 각기 다른 데이터 모양(dataclass, dict, Protocol)이었다.
  - 갤러리에 배포하거나 MCP 로 변환할 때마다 변환 코드가 중복.
  - NOM 한 번 쓰면 `to_mcp() / to_wheel() / to_sandbox_payload()` 한 곳에서 확정.

참고:
  LangGraph `RunnableConfig`, LangChain `Runnable`, MCP `Tool` 스키마, OpenAPI 3 을
  종합한 **최소 교집합** 형태. "호출 가능한 함수 덩어리" 단일 추상.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class NOMKind(Enum):
    """NOM 노드 종류 — 하네스 실행 그래프를 구성하는 5 가지 실체."""
    STAGE = "stage"
    STRATEGY = "strategy"
    TOOL = "tool"
    MCP_SERVER = "mcp_server"
    NODE = "node"  # xgen-workflow 레거시 노드 (마이그레이션 브리지)


@dataclass
class NOMParam:
    """노드 입력 파라미터 스펙. OpenAPI 3 subset."""
    name: str
    type: str = "string"  # string | integer | number | boolean | object | array
    description: str = ""
    required: bool = False
    default: Any = None
    enum: Optional[list[Any]] = None


@dataclass
class NOMOutput:
    """노드 출력 스펙."""
    name: str
    type: str = "string"
    description: str = ""


@dataclass
class NOMNode:
    """하네스 IR 단일 노드.

    컴파일러 / 샌드박스 / 갤러리 가 공통으로 주고받는 구조. 이 dataclass 한 곳만 알면
    Stage / Strategy / Tool / MCP server / workflow Node 모두 취급 가능.

    Attributes
    ----------
    id : str
        전역 식별자. 예: "xgen.stages.s08_decide", "xgen.tools.rag_search",
        "plugin.bedrock.provider", "lotte.stages.s04_tool_lotte".
    kind : NOMKind
        실체 구분.
    name : str
        사람용 이름.
    description : str
        요약.
    source_file : str
        원본 소스 경로 (카탈로그 source_file 과 동일 의미).
    entry : str
        동적 로딩 경로. Python: "module:callable", MCP: stdio command, Node: import spec.
    kind_meta : dict
        kind 별 추가 메타 (예: Stage 는 phase/order, Tool 은 scope, MCP 는 transport).
    inputs / outputs : list
        파라미터/출력 스펙.
    tags : list[str]
        검색/필터용.
    version : str
        semver. 갤러리 업로드 시 필수.
    plugin_package : str
        소속 pip 패키지 이름 ("", "xgen-harness", "xgen-stage-judge-core" 등).
    """
    id: str
    kind: NOMKind
    name: str = ""
    description: str = ""
    source_file: str = ""
    entry: str = ""
    kind_meta: dict = field(default_factory=dict)
    inputs: list[NOMParam] = field(default_factory=list)
    outputs: list[NOMOutput] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: str = "0.0.0"
    plugin_package: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "NOMNode":
        kind_val = d.get("kind", "stage")
        kind = kind_val if isinstance(kind_val, NOMKind) else NOMKind(kind_val)
        inputs = [NOMParam(**p) if isinstance(p, dict) else p for p in d.get("inputs", [])]
        outputs = [NOMOutput(**o) if isinstance(o, dict) else o for o in d.get("outputs", [])]
        return cls(
            id=d["id"],
            kind=kind,
            name=d.get("name", ""),
            description=d.get("description", ""),
            source_file=d.get("source_file", ""),
            entry=d.get("entry", ""),
            kind_meta=d.get("kind_meta", {}),
            inputs=inputs,
            outputs=outputs,
            tags=d.get("tags", []),
            version=d.get("version", "0.0.0"),
            plugin_package=d.get("plugin_package", ""),
        )


@dataclass
class NOMGraph:
    """여러 NOMNode 를 묶은 실행 그래프.

    compile/ 에서 워크플로우 스냅샷 → NOMGraph → wheel/MCP/Gallery 로 역직렬화.

    v0.21.0 — `to_mcp_schema()` / `to_sandbox_payload()` / `to_wheel_snapshot()` 3 변환이
    주석의 약속대로 실체화. 외부 NOM (LLM 이 생성한 도구 그래프, 외부 플러그인 노드) 도
    같은 wheel/MCP/sandbox 파이프라인을 쓸 수 있게 한다.
    """
    nodes: list[NOMNode] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)  # {source, target, label}
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": list(self.edges),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NOMGraph":
        return cls(
            nodes=[NOMNode.from_dict(n) for n in d.get("nodes", [])],
            edges=list(d.get("edges", [])),
            metadata=dict(d.get("metadata", {})),
        )

    # ─────────────────────────────────────────────
    # v0.21.0 Phase C — 3 변환 (NOM IR 허브의 본체)
    # ─────────────────────────────────────────────

    def to_mcp_schema(
        self,
        *,
        include_kinds: Optional[list[NOMKind]] = None,
        name_strategy: str = "last_segment",
    ) -> list[dict]:
        """NOM → MCP `tools/list` 응답의 `tools` 배열 스키마.

        Claude Desktop / Cursor / 임의 MCP 클라이언트가 그대로 읽을 수 있는 JSON.
        NOMNode 의 ``inputs`` → JSON Schema ``properties`` 로 변환.

        Parameters
        ----------
        include_kinds : list[NOMKind] | None
            포함할 kind 필터. 기본 [TOOL, MCP_SERVER] — 실제 도구 호출 단위.
        name_strategy : str
            도구 이름 규칙. ``last_segment`` (기본): id 의 마지막 `.` 뒤 부분,
            ``full_id``: id 전체 그대로.

        Returns
        -------
        list[dict]  각 dict 는 ``{"name": str, "description": str, "inputSchema": {...}}``.

        Example
        -------
        >>> graph = NOMGraph(nodes=[
        ...     NOMNode(id="x.tools.search", kind=NOMKind.TOOL, description="웹 검색",
        ...             inputs=[NOMParam(name="q", type="string", required=True)]),
        ... ])
        >>> graph.to_mcp_schema()[0]["name"]
        'search'
        """
        if include_kinds is None:
            include_kinds = [NOMKind.TOOL, NOMKind.MCP_SERVER]
        allowed = set(include_kinds)
        out: list[dict] = []
        for n in self.nodes:
            if n.kind not in allowed:
                continue
            tool_name = n.id.rsplit(".", 1)[-1] if name_strategy == "last_segment" else n.id
            properties: dict[str, dict] = {}
            required: list[str] = []
            for p in n.inputs:
                spec: dict[str, Any] = {"type": p.type}
                if p.description:
                    spec["description"] = p.description
                if p.enum:
                    spec["enum"] = list(p.enum)
                if p.default is not None:
                    spec["default"] = p.default
                properties[p.name] = spec
                if p.required:
                    required.append(p.name)
            input_schema: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                input_schema["required"] = required
            out.append({
                "name": tool_name,
                "description": n.description or n.name or tool_name,
                "inputSchema": input_schema,
            })
        return out

    def to_sandbox_payload(self, node_id: str, input: dict) -> dict:
        """특정 NOMNode 를 `Sandbox.run_nom_tool` 이 받는 payload 로 직렬화.

        `core/sandbox.py` 의 ``Sandbox.run_nom_tool(entry, input_payload)`` 와
        조합하면 NOM 노드 하나를 격리 환경에서 시연 가능.

        Raises
        ------
        KeyError
            ``node_id`` 가 그래프에 없을 때.
        ValueError
            노드의 ``entry`` 가 비어있을 때 (동적 로드 불가).
        """
        node = next((n for n in self.nodes if n.id == node_id), None)
        if node is None:
            raise KeyError(f"node '{node_id}' not in graph")
        if not node.entry:
            raise ValueError(
                f"node '{node_id}' has no entry (kind={node.kind.value}); "
                "sandbox execution requires 'module:callable' entry"
            )
        return {
            "entry": node.entry,
            "input": dict(input or {}),
            "metadata": {
                "node_id": node.id,
                "kind": node.kind.value,
                "name": node.name,
                "version": node.version,
                "plugin_package": node.plugin_package,
            },
        }

    def to_wheel_snapshot(
        self,
        *,
        gallery_name: str,
        gallery_version: str = "0.1.0",
        harness_config: Optional[Any] = None,
        extra_metadata: Optional[dict] = None,
    ) -> Any:
        """NOM → `WorkflowSnapshot` (기존 `build_npm_package` 에 그대로 전달 가능).

        NOM 그래프를 ``workflow_data`` 의 nodes/edges 로 직렬화 + ``metadata.from_nom=True``.
        harness_config 미지정 시 빈 dict 로 기본값 생성 — 실행이 필요 없는 도구 묶음
        (Tool Synthesis 결과, 외부 플러그인 세트) 을 그대로 wheel 로 쌀 수 있다.

        Import 지연: compile.snapshot.WorkflowSnapshot 가 실제로는 compile 서브모듈에만
        존재하므로 여기서 지연 import.
        """
        from ..compile.snapshot import WorkflowSnapshot
        config_dict = harness_config
        if config_dict is None:
            config_dict = {}
        if not isinstance(config_dict, dict) and hasattr(config_dict, "to_dict"):
            config_dict = config_dict.to_dict()
        workflow_data = {
            "workflow_type": "nom",
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": list(self.edges),
        }
        meta = {"from_nom": True, "nom_metadata": dict(self.metadata)}
        if extra_metadata:
            meta.update(extra_metadata)
        return WorkflowSnapshot.from_config(
            harness_config=config_dict,
            workflow_data=workflow_data,
            gallery_name=gallery_name,
            gallery_version=gallery_version,
            extra_metadata=meta,
        )


# ───────────────────────────────────────────────────────────────
#  현재 하네스 레지스트리 → NOMGraph 덤프 헬퍼
# ───────────────────────────────────────────────────────────────

def snapshot_current_registry_as_nom() -> NOMGraph:
    """현재 엔진 상태(Stage·Strategy·Tool·Orchestrator·Provider 레지스트리)를 NOM 으로 덤프.

    디버깅 / 갤러리 업로드 / 샌드박스 복원 시 이 함수 하나로 현 상태 얻는다.
    이 함수 자체에 Stage/Strategy/Tool 이름 리터럴 0 — 전부 레지스트리에서 런타임 조회.
    """
    graph = NOMGraph(metadata={"snapshot_of": "xgen_harness_runtime"})

    # Stage
    try:
        from .registry import ArtifactRegistry
        for entry in ArtifactRegistry.default().describe_all():
            graph.nodes.append(NOMNode(
                id=f"xgen.stages.{entry['stage_id']}",
                kind=NOMKind.STAGE,
                name=entry.get("display_name", entry["stage_id"]),
                description=(entry.get("config") or {}).get("description_ko", "")
                            if entry.get("config") else "",
                source_file=entry.get("source_file", ""),
                entry="",  # dynamic; engine handles directly
                kind_meta={
                    "phase": entry.get("phase", ""),
                    "order": entry.get("order", 0),
                    "required": entry.get("required", False),
                    "artifacts": entry.get("artifacts", []),
                },
                tags=["stage", entry.get("phase", "")],
                plugin_package="xgen-harness",
            ))
    except Exception:
        pass

    # Strategy
    try:
        from .strategy_resolver import _REGISTRY as _SR, _ensure_defaults_registered
        _ensure_defaults_registered()
        for (sid, slot, impl), cls in _SR.items():
            graph.nodes.append(NOMNode(
                id=f"xgen.strategies.{sid}.{slot}.{impl}",
                kind=NOMKind.STRATEGY,
                name=impl,
                description=getattr(cls(), "description", "") if callable(cls) else "",
                entry=f"{cls.__module__}:{cls.__name__}",
                kind_meta={"stage_id": sid, "slot": slot, "impl": impl},
                tags=["strategy", slot],
                plugin_package=cls.__module__.split(".")[0] if cls.__module__ else "",
            ))
    except Exception:
        pass

    # Orchestrator
    try:
        from .orchestrator_registry import get_orchestrator_specs
        for spec in get_orchestrator_specs():
            graph.nodes.append(NOMNode(
                id=f"xgen.orchestrators.{spec['name']}",
                kind=NOMKind.STRATEGY,  # Orchestrator 도 실행 전략의 한 종류
                name=spec["name"],
                description=spec.get("description", ""),
                kind_meta={"dispatch_key": spec.get("dispatch_key", "")},
                tags=["orchestrator"],
                plugin_package="xgen-harness",
            ))
    except Exception:
        pass

    # Provider
    try:
        from ..providers import list_providers, get_default_model, PROVIDER_CONTEXT_LIMITS
        for name in list_providers():
            graph.nodes.append(NOMNode(
                id=f"xgen.providers.{name}",
                kind=NOMKind.STRATEGY,
                name=name,
                description=f"LLM Provider ({name})",
                kind_meta={
                    "default_model": get_default_model(name),
                    "context_limit": PROVIDER_CONTEXT_LIMITS.get(name, 0),
                },
                tags=["provider"],
            ))
    except Exception:
        pass

    return graph
