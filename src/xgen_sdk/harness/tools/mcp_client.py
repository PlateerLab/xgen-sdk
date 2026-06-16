"""
MCP Client — xgen-mcp-station HTTP API 클라이언트

MCP 세션의 도구를 발견하고 호출.
xgen-mcp-station의 REST API를 통해 JSON-RPC를 간접 호출.

Endpoints:
- GET  /api/mcp/sessions/{session_id}/tools  → 도구 목록
- POST /api/mcp/mcp-request                  → 도구 호출 (tools/call)
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .base import Tool, ToolResult
from ..core.service_registry import get_service_url

logger = logging.getLogger("harness.tools.mcp")


# v0.26.8 — 자가호출 시 user/auth 헤더 누락으로 station 이 401/빈응답 → tools=0
# 회귀가 라이브 검증으로 발견됐다. CustomAPIToolSource 가 이미 같은 패턴으로
# 헤더를 forward 한다 — 여기도 동일한 화이트리스트로 통일.
_AUTH_HEADER_ALLOW = frozenset({
    "authorization", "cookie",
    "x-user-id", "x-user-email", "x-user-name",
    "x-user-roles", "x-user-groups", "x-workspace-id",
})


def _forward_request_headers() -> dict[str, str]:
    """현재 실행 컨텍스트(contextvar)의 요청 헤더에서 인증 관련만 추려 반환.

    엔진 ``/api/harness/tool-sources`` 핸들러가 ``use_request_headers`` 컨텍스트
    매니저로 헤더를 실어두면, downstream MCPClient 호출이 같은 user 컨텍스트로
    station 을 친다. SDK 직접 사용(엔진 API 바깥)에서는 빈 dict.
    """
    try:
        # 순환 import 방지를 위해 lazy.
        from . import get_request_headers
    except Exception:
        return {}
    try:
        hdr = get_request_headers() or {}
    except Exception:
        return {}
    return {k: v for k, v in hdr.items() if k.lower() in _AUTH_HEADER_ALLOW and v}


@dataclass
class MCPCallResult:
    """MCP 도구 호출 구조화 결과 (v0.11.24).

    기존 `call_tool` 는 문자열만 반환해 에러/성공을 prefix 매칭으로 구분해야 했고
    이식측에도 같은 fragility 가 전파되었다. `call_tool_raw` 가 이 dataclass 를 반환해
    호출부가 status 로 명확히 분기할 수 있게 한다.
    """
    ok: bool
    text: str
    status: int = 200
    error_detail: str = ""


class MCPClient:
    """xgen-mcp-station HTTP 클라이언트"""

    def __init__(self, base_url: str = "", timeout: float = 60.0):
        if not base_url:
            base_url = get_service_url("mcp") or ""
        if not base_url:
            logger.warning("MCP service not registered, MCP tools will be unavailable")
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._timeout = httpx.Timeout(timeout, connect=10.0)

    async def list_tools(self, session_id: str) -> list[dict]:
        """세션의 MCP 도구 목록 조회"""
        url = f"{self._base_url}/api/mcp/sessions/{session_id}/tools"
        headers = _forward_request_headers()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    tools = data.get("tools", data.get("data", {}).get("tools", []))
                    logger.info("[MCP] Session %s: %d tools discovered", session_id, len(tools))
                    return tools
                else:
                    logger.warning("[MCP] list_tools failed: %d %s", resp.status_code, resp.text[:200])
                    return []
        except Exception as e:
            logger.error("[MCP] list_tools error: %s", e)
            return []

    async def call_tool_raw(self, session_id: str, tool_name: str, arguments: dict) -> MCPCallResult:
        """MCP 도구 호출 — 구조화 결과 반환 (v0.11.24 신규).

        `MCPTool.execute` 처럼 성공/실패를 명확히 분기해야 하는 호출부에서 사용.
        레거시 `call_tool(str)` 은 이 위에 얇은 하위 호환 래퍼.
        """
        url = f"{self._base_url}/api/mcp/mcp-request"
        payload = {
            "session_id": session_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        headers = _forward_request_headers()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    result_data = data.get("data", data.get("result", {}))
                    content = result_data.get("content", [])
                    if isinstance(content, list):
                        texts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                texts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                texts.append(block)
                        text = "\n".join(texts) if texts else json.dumps(result_data, ensure_ascii=False)
                    else:
                        text = str(content)
                    return MCPCallResult(ok=True, text=text, status=200)
                else:
                    detail = resp.text[:300]
                    logger.warning("[MCP] call failed %d: %s", resp.status_code, detail)
                    return MCPCallResult(
                        ok=False,
                        text=f"MCP call failed ({resp.status_code}): {detail}",
                        status=resp.status_code,
                        error_detail=detail,
                    )
        except Exception as e:
            logger.error("[MCP] call error: %s", e)
            return MCPCallResult(
                ok=False,
                text=f"MCP call error: {e}",
                status=-1,
                error_detail=str(e),
            )

    async def call_tool(self, session_id: str, tool_name: str, arguments: dict) -> str:
        """MCP 도구 호출 — 하위 호환 문자열 반환 래퍼.

        이식측이 이미 str 계약을 쓰고 있어 유지. 새 코드는 `call_tool_raw` 권장.
        """
        result = await self.call_tool_raw(session_id, tool_name, arguments)
        return result.text

    async def check_session(self, session_id: str) -> bool:
        """세션 존재 여부 확인"""
        url = f"{self._base_url}/api/mcp/sessions/{session_id}"
        headers = _forward_request_headers()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.get(url, headers=headers)
                return resp.status_code == 200
        except Exception as e:
            logger.debug("check_session(%s) failed: %s", session_id, e)
            return False


class MCPTool(Tool):
    """MCP 세션 도구를 xgen-harness Tool 인터페이스로 래핑"""

    def __init__(self, session_id: str, tool_def: dict, mcp_client: MCPClient):
        self._session_id = session_id
        self._tool_def = tool_def
        self._mcp_client = mcp_client

    @property
    def name(self) -> str:
        return self._tool_def.get("name", "unknown")

    @property
    def description(self) -> str:
        return self._tool_def.get("description", "")

    @property
    def input_schema(self) -> dict:
        return self._tool_def.get("inputSchema", self._tool_def.get("input_schema", {}))

    @property
    def category(self) -> str:
        return f"mcp:{self._session_id[:8]}"

    # v0.23.0 — MCP 서버가 선언한 annotations 우선. 없으면 이름 휴리스틱 폴백.
    # MCP 표준 (2025-06-18+) 은 tools/list 응답 각 tool 에 annotations 블록을 포함.
    def _annotation(self, key: str, default: bool) -> bool:
        ann = self._tool_def.get("annotations") or {}
        if key in ann:
            return bool(ann[key])
        # legacy 호환 (일부 MCP 서버가 top-level 로 보냄)
        if key == "readOnlyHint" and "is_read_only" in self._tool_def:
            return bool(self._tool_def["is_read_only"])
        return default

    @property
    def read_only_hint(self) -> bool:
        if self._tool_def.get("annotations") is not None:
            return self._annotation("readOnlyHint", False)
        # 서버가 annotations 미제공 → 휴리스틱 (deprecated 경로)
        name_lower = self.name.lower()
        write_keywords = {"create", "update", "delete", "write", "send", "post", "put", "remove"}
        return not any(kw in name_lower for kw in write_keywords)

    @property
    def destructive_hint(self) -> bool:
        return self._annotation("destructiveHint", False)

    @property
    def idempotent_hint(self) -> bool:
        return self._annotation("idempotentHint", False)

    @property
    def open_world_hint(self) -> bool:
        return self._annotation("openWorldHint", True)  # MCP 는 원격 호출 기본

    async def execute(self, input_data: dict) -> ToolResult:
        # call_tool_raw 는 MCPCallResult 로 성공/실패를 구조화 반환 — prefix 매칭 없이 status 로 분기.
        r = await self._mcp_client.call_tool_raw(self._session_id, self.name, input_data)
        if not r.ok:
            return ToolResult.error(r.text)
        return ToolResult.success(r.text)


async def discover_mcp_tools(
    session_ids: list[str],
    mcp_client: Optional[MCPClient] = None,
) -> list[MCPTool]:
    """여러 MCP 세션에서 도구를 발견하고 MCPTool로 래핑"""
    client = mcp_client or MCPClient()
    all_tools: list[MCPTool] = []

    for session_id in session_ids:
        tool_defs = await client.list_tools(session_id)
        for td in tool_defs:
            all_tools.append(MCPTool(session_id, td, client))

    logger.info("[MCP] Total %d tools from %d sessions", len(all_tools), len(session_ids))
    return all_tools
