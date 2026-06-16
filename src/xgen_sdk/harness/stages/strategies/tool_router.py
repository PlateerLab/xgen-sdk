"""ToolRouter 구현체들

xgen-workflow 이식 시:
- MCPToolRouter: MCP 서비스 HTTP 호출
- BuiltinToolRouter: discover_tools 등 내장 도구
- CompositeToolRouter: 여러 라우터를 체이닝 (우선순위 순서로 탐색)

왜 CompositeToolRouter인가:
  단일 라우터로 모든 도구 유형을 처리하면 하드코딩이 된다.
  Composite 패턴으로 MCP/Builtin/NodeBridge를 독립적으로 추가/제거.
  xgen-workflow에서 Node Bridge 추가 시 CompositeToolRouter에 끼우면 끝.
"""

import logging
from typing import Any

from ..interfaces import ToolRouter, ToolResult

logger = logging.getLogger("harness.strategy.tool_router")


class BuiltinToolRouter(ToolRouter):
    """빌트인 도구 라우터 — discover_tools 등"""

    def __init__(self, tool_registry: dict[str, Any] = None):
        self._registry = tool_registry or {}

    @property
    def name(self) -> str:
        return "builtin"

    @property
    def description(self) -> str:
        return "빌트인 도구 (discover_tools, calculator 등)"

    async def route(self, tool_name: str, tool_input: dict) -> ToolResult:
        tool = self._registry.get(tool_name)
        if not tool:
            raise KeyError(f"Builtin tool '{tool_name}' not found")
        result = await tool.execute(tool_input)
        return ToolResult(content=result.content, is_error=result.is_error)

    async def list_available(self) -> list[dict[str, str]]:
        return [
            {"name": name, "description": getattr(t, "description", "")}
            for name, t in self._registry.items()
        ]


class MCPToolRouter(ToolRouter):
    """MCP 도구 라우터 — MCP 서비스 HTTP 호출.

    xgen-workflow 이식 후에도 동일 인터페이스.
    MCP_STATION_URL만 환경변수로 바꾸면 됨.
    """

    def __init__(self, mcp_tool_mapping: dict[str, str] = None):
        # tool_name → session_id 매핑
        self._mapping = mcp_tool_mapping or {}

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def description(self) -> str:
        return "MCP 도구 (MCP 서비스 경유)"

    async def route(self, tool_name: str, tool_input: dict) -> ToolResult:
        session_id = self._mapping.get(tool_name)
        if not session_id:
            raise KeyError(f"MCP tool '{tool_name}' not mapped to session")
        from ...tools.mcp_client import MCPClient
        client = MCPClient()
        try:
            result_text = await client.call_tool(session_id, tool_name, tool_input)
            return ToolResult(content=result_text)
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)

    async def list_available(self) -> list[dict[str, str]]:
        return [{"name": name, "description": f"MCP session: {sid}"} for name, sid in self._mapping.items()]


class CompositeToolRouter(ToolRouter):
    """복합 라우터 — 여러 라우터를 우선순위 순서로 체이닝.

    route() 시 등록된 라우터를 순서대로 탐색.
    첫 번째로 해당 도구를 가진 라우터가 실행.

    왜 이게 필요한가:
    - Builtin 도구(discover_tools)는 항상 있어야 함
    - MCP 도구는 세션에 따라 동적
    - Node Bridge 도구는 xgen-workflow에서만 존재
    - 각각 독립 라우터로 만들고 Composite로 합치면 확장성 확보
    """

    def __init__(self, routers: list[ToolRouter] = None):
        self._routers = routers or []
        self._name_to_router: dict[str, ToolRouter] = {}

    @property
    def name(self) -> str:
        return "composite"

    @property
    def description(self) -> str:
        names = [r.name for r in self._routers]
        return f"복합 라우터 ({' → '.join(names)})"

    def add_router(self, router: ToolRouter) -> "CompositeToolRouter":
        """라우터 추가 (fluent)"""
        self._routers.append(router)
        return self

    async def build_index(self) -> None:
        """등록된 라우터들의 도구 목록을 캐싱"""
        self._name_to_router.clear()
        for router in self._routers:
            try:
                tools = await router.list_available()
                for t in tools:
                    if t["name"] not in self._name_to_router:
                        self._name_to_router[t["name"]] = router
            except Exception as e:
                logger.warning("Router %s list_available failed: %s", router.name, e)

    async def route(self, tool_name: str, tool_input: dict) -> ToolResult:
        # 캐시된 매핑에서 탐색
        if tool_name in self._name_to_router:
            return await self._name_to_router[tool_name].route(tool_name, tool_input)

        # 캐시 미스 → 순서대로 시도
        for router in self._routers:
            try:
                return await router.route(tool_name, tool_input)
            except KeyError:
                continue

        return ToolResult(
            content=f"Error: Tool '{tool_name}' not found in any router. Use discover_tools to see available tools.",
            is_error=True,
        )

    async def list_available(self) -> list[dict[str, str]]:
        all_tools: list[dict[str, str]] = []
        seen = set()
        for router in self._routers:
            try:
                tools = await router.list_available()
                for t in tools:
                    if t["name"] not in seen:
                        all_tools.append(t)
                        seen.add(t["name"])
            except Exception as e:
                # v0.11.21 — 한 라우터 실패가 다른 라우터 발견을 막으면 안 됨.
                # pass 대신 로그 남겨 디버깅 가능성 확보.
                logger.warning(
                    "[CompositeToolRouter] %s.list_available 실패, 해당 라우터 건너뜀: %s",
                    type(router).__name__, e,
                )
        return all_tools
