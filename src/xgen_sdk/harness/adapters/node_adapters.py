"""NodeAdapter 레지스트리 — 워크플로우 노드를 하네스 도구/자원으로 변환하는 통로.

기존 `ResourceRegistry._load_api_tools` 안에 `if func_id in (...):` 식 하드코딩 분기를
**플러그인 등록** 으로 전환. 새 xgen 노드 타입이 추가되면 `register_node_adapter()` 한 줄로 연동.

확장성:
  1. 빌트인 어댑터 2개 (api_tool, db_tool) 를 `bootstrap_default_node_adapters()` 가 등록
  2. 외부 패키지는 entry_points(group="xgen_harness.node_adapters") 로 자동 등록
  3. 런타임 직접 등록: `register_node_adapter(NodeAdapter(...))`

핵심 코드는 `get_adapter_for(func_id)` 한 번 조회. 분기 0.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .resource_registry import ResourceRegistry

logger = logging.getLogger("harness.node_adapters")


@dataclass
class NodeAdapter:
    """워크플로우 노드 → 도구/자원 변환 스펙.

    Attributes:
        name: 어댑터 식별 이름 (예: 'api_tool', 'db_tool', 'ontology_retrieval')
        function_ids: 매칭할 functionId 목록 (레거시 xgen 노드 타입)
        build: (node: dict, registry: ResourceRegistry) → None. tool_def/executor 를 registry 에 등록
        resource_type: 'api_tool' | 'db_tool' | 'custom_tool' | 'rag_source' | ...
        description: 사람 읽기용 설명
    """
    name: str
    function_ids: list[str]
    build: Callable[[dict, "ResourceRegistry"], None]
    resource_type: str = "custom_tool"
    description: str = ""


_ADAPTERS: list[NodeAdapter] = []
_BOOTSTRAPPED = False


def register_node_adapter(adapter: NodeAdapter) -> None:
    """어댑터 등록 — 공개 API. 빌트인/플러그인/런타임 등록 모두 동일 경로."""
    _ADAPTERS.append(adapter)


def get_adapter_for(func_id: str) -> Optional[NodeAdapter]:
    for a in _ADAPTERS:
        if func_id in a.function_ids:
            return a
    return None


def list_adapters() -> list[NodeAdapter]:
    return list(_ADAPTERS)


# ── Bootstrap ────────────────────────────────────────────────

def bootstrap_default_node_adapters() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    # 빌트인 2종 등록
    register_node_adapter(NodeAdapter(
        name="api_tool",
        function_ids=["api_calling_tool", "api_tool", "custom_api"],
        build=_build_api_tool,
        resource_type="api_tool",
        description="REST API 호출 도구",
    ))
    register_node_adapter(NodeAdapter(
        name="db_tool",
        function_ids=["postgresql_query", "oracle_query", "db_query", "mysql_query"],
        build=_build_db_tool,
        resource_type="db_tool",
        description="SQL 쿼리 도구 — ServiceProvider.database 경유",
    ))

    # entry_points 자동 발견 — 외부 패키지가 노드 어댑터 추가 가능
    try:
        import sys
        if sys.version_info >= (3, 10):
            from importlib.metadata import entry_points
            eps = entry_points(group="xgen_harness.node_adapters")
        else:
            from importlib.metadata import entry_points
            eps = entry_points().get("xgen_harness.node_adapters", [])
        for ep in eps:
            try:
                obj = ep.load()
                adapter = obj() if callable(obj) else obj
                if isinstance(adapter, NodeAdapter):
                    register_node_adapter(adapter)
                    logger.info("[NodeAdapter] plugin registered: %s", ep.name)
            except Exception as e:
                logger.warning("[NodeAdapter] plugin load failed %s: %s", ep.name, e)
    except Exception as e:
        logger.debug("[NodeAdapter] entry_points discovery skipped: %s", e)


# ── 빌트인 빌더 ──────────────────────────────────────────────
# 기존 resource_registry.py 의 api/db 빌드 로직을 그대로 옮김.
# 리팩터 안전성: 동작 불변, 위치만 이동.

def _build_api_tool(node: dict, registry: "ResourceRegistry") -> None:
    from .resource_registry import _APIToolRef, ResourceInfo
    nd = node.get("data", {})
    params = {p["id"]: p.get("value") for p in nd.get("parameters", []) if p.get("value")}
    tool_name = params.get("tool_name", params.get("name", ""))
    if not tool_name or tool_name in registry._tool_executors:
        return

    spec = {
        "api_url": params.get("api_endpoint", params.get("api_url", "")),
        "api_method": params.get("method", "POST"),
        "api_body": params.get("request_body", {}),
        "timeout": params.get("timeout", 30),
        # 응답 후처리 — 3 키 구조 (레거시 api_loader 호환).
        # enable_response_filtering=True 면 아래 두 키가 적용됩니다.
        #   response_filter_path   : "a.b.c" 형식 dot path 로 응답 dict 안 깊은 값 추출
        #   response_filter_fields : 결과가 list[dict] 일 때 각 dict 에서 유지할 필드 (콤마 구분 문자열 또는 list)
        # 하위 호환: 기존 단일 키 response_filter 는 response_filter_path 의 별칭으로 계속 동작.
        "enable_response_filtering": bool(params.get("enable_response_filtering", False)),
        "response_filter_path": params.get("response_filter_path", params.get("response_filter", "")),
        "response_filter_fields": params.get("response_filter_fields", ""),
    }

    desc = params.get("description", f"API tool: {tool_name}")
    input_schema = params.get("input_schema", {"type": "object", "properties": {}})
    if isinstance(input_schema, str):
        try:
            input_schema = json.loads(input_schema)
        except Exception as e:
            logger.debug("[NodeAdapter] input_schema JSON 파싱 실패 (%s), 빈 schema 사용: %s", tool_name, e)
            input_schema = {"type": "object", "properties": {}}

    registry._tool_defs.append({
        "name": tool_name,
        "description": desc,
        "input_schema": input_schema,
    })
    registry._tool_executors[tool_name] = _APIToolRef(spec=spec)
    registry._tool_infos.append(ResourceInfo(
        resource_type="api_tool", name=tool_name,
        description=desc, source=spec.get("api_url", ""),
    ))


def _build_db_tool(node: dict, registry: "ResourceRegistry") -> None:
    from .resource_registry import _DBToolRef, ResourceInfo
    nd = node.get("data", {})
    params = {p["id"]: p.get("value") for p in nd.get("parameters", []) if p.get("value")}
    tool_name = params.get("tool_name", f"db_query_{node.get('id', '')[:8]}")
    if tool_name in registry._tool_executors:
        return

    desc = params.get("description", f"Database query: {tool_name}")
    registry._tool_defs.append({
        "name": tool_name,
        "description": desc,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "SQL query"}},
            "required": ["query"],
        },
    })
    registry._tool_executors[tool_name] = _DBToolRef(
        connection_id=params.get("connection_id", ""),
        db_type=params.get("db_type", "postgresql"),
    )
    registry._tool_infos.append(ResourceInfo(
        resource_type="db_tool", name=tool_name,
        description=desc, source=params.get("db_type", "db"),
    ))
