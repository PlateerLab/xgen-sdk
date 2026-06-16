"""
Gallery Tool Loader — xgen-gallery 도구 패키지 자동 로드

갤러리 도구 패키지 표준:
1. TOOL_DEFINITIONS 리스트 (정적 도구 스키마)
2. call_tool(name, args) 함수 (단일 디스패처)
3. pyproject.toml에 xgen-harness entry point 등록 (선택)

사용:
    # 패키지에서 직접 로드
    tools = load_tool_package("document_adapter")

    # entry point에서 자동 발견
    tools = discover_gallery_tools()

    # 하네스 파이프라인에 바인딩
    for tool in tools:
        state.tool_definitions.append(tool.to_api_format())
        state.metadata["tool_registry"][tool.name] = tool

갤러리 도구 개발자는 아래 ToolPackageSpec을 따르면 됩니다.
"""

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .base import Tool, ToolResult

logger = logging.getLogger("harness.tools.gallery")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. ToolPackageSpec — 갤러리 도구 패키지 명세
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class ToolPackageSpec:
    """갤러리에 등록할 도구 패키지 명세.

    개발자가 이 형식을 따르면 하네스가 자동으로 로드할 수 있다.

    최소 요구사항:
        - name: 패키지 이름
        - tool_definitions: 도구 스키마 리스트
        - call_tool: 도구 실행 디스패처

    선택사항:
        - version, author, description
        - categories: 도구 카테고리별 그룹핑
        - setup: 초기화 함수 (API 키 설정 등)
        - teardown: 정리 함수
    """
    name: str
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    tool_definitions: list[dict[str, Any]] = field(default_factory=list)
    call_tool: Optional[Callable] = None
    categories: dict[str, list[str]] = field(default_factory=dict)
    setup: Optional[Callable] = None
    teardown: Optional[Callable] = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. GalleryTool — 갤러리 도구를 하네스 Tool ABC로 래핑
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GalleryTool(Tool):
    """갤러리 도구 패키지의 개별 도구를 하네스 Tool로 래핑.

    call_tool() 디스패처를 통해 실행.
    """

    def __init__(
        self,
        tool_def: dict[str, Any],
        dispatcher: Callable,
        package_name: str = "",
    ):
        self._name = tool_def["name"]
        self._description = tool_def.get("description", "")
        self._input_schema = tool_def.get("input_schema", tool_def.get("inputSchema", {}))
        self._category = tool_def.get("category", package_name)
        self._dispatcher = dispatcher
        self._package = package_name

        # v0.23.0 — annotations 우선 + legacy is_read_only 폴백
        ann = tool_def.get("annotations") or {}
        self._read_only = bool(ann.get("readOnlyHint", tool_def.get("is_read_only", False)))
        self._destructive = bool(ann.get("destructiveHint", False))
        self._idempotent = bool(ann.get("idempotentHint", False))
        self._open_world = bool(ann.get("openWorldHint", True))

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict:
        return self._input_schema

    @property
    def category(self) -> str:
        return self._category

    @property
    def read_only_hint(self) -> bool:
        return self._read_only

    @property
    def destructive_hint(self) -> bool:
        return self._destructive

    @property
    def idempotent_hint(self) -> bool:
        return self._idempotent

    @property
    def open_world_hint(self) -> bool:
        return self._open_world

    async def execute(self, input_data: dict) -> ToolResult:
        try:
            result = self._dispatcher(self._name, input_data)
            # async 디스패처 지원
            if hasattr(result, '__await__'):
                result = await result

            if isinstance(result, dict):
                content = result.get("content", result.get("text", str(result)))
                is_error = result.get("is_error", result.get("isError", False))
                return ToolResult(content=str(content), is_error=is_error)
            return ToolResult(content=str(result))
        except Exception as e:
            return ToolResult(content=f"[{self._package}:{self._name}] Error: {e}", is_error=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. 로더 함수
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_tool_package(module_name: str) -> list[GalleryTool]:
    """Python 패키지에서 도구 로드.

    패키지에서 찾는 것:
    1. TOOL_DEFINITIONS (리스트)
    2. call_tool (함수)
    3. 또는 get_tool_spec() → ToolPackageSpec

    Args:
        module_name: 파이썬 모듈 경로 (예: "document_adapter", "synaptic_memory")

    Returns:
        GalleryTool 리스트
    """
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        logger.warning("[Gallery] 패키지 %s 로드 실패: %s", module_name, e)
        return []

    # 방법 1: get_tool_spec() 함수가 있으면 사용
    if hasattr(mod, "get_tool_spec"):
        spec = mod.get_tool_spec()
        if isinstance(spec, ToolPackageSpec):
            return _spec_to_tools(spec)

    # 방법 2: TOOL_DEFINITIONS + call_tool
    tool_defs = getattr(mod, "TOOL_DEFINITIONS", None)
    dispatcher = getattr(mod, "call_tool", None)

    if not tool_defs:
        # 서브모듈에서 찾기 (document_adapter.tools 등)
        for sub in ["tools", "tool_definitions", "mcp_tools"]:
            try:
                sub_mod = importlib.import_module(f"{module_name}.{sub}")
                tool_defs = getattr(sub_mod, "TOOL_DEFINITIONS", None)
                dispatcher = dispatcher or getattr(sub_mod, "call_tool", None)
                if tool_defs:
                    break
            except ImportError:
                continue

    if not tool_defs:
        logger.warning("[Gallery] %s에서 TOOL_DEFINITIONS를 찾을 수 없음", module_name)
        return []

    if not dispatcher:
        logger.warning("[Gallery] %s에서 call_tool을 찾을 수 없음", module_name)
        return []

    tools = []
    for td in tool_defs:
        if isinstance(td, dict) and td.get("name"):
            tools.append(GalleryTool(td, dispatcher, package_name=module_name))

    logger.info("[Gallery] %s: %d tools loaded", module_name, len(tools))
    return tools


def discover_gallery_tools() -> list[GalleryTool]:
    """entry_points에서 xgen-harness 도구 자동 발견.

    pyproject.toml 예시:
        [project.entry-points."xgen_harness.tools"]
        document_adapter = "document_adapter:get_tool_spec"
        synaptic_memory = "synaptic_memory:get_tool_spec"
    """
    tools = []
    try:
        if hasattr(importlib.metadata, "entry_points"):
            eps = importlib.metadata.entry_points()
            # Python 3.12+
            harness_eps = eps.select(group="xgen_harness.tools") if hasattr(eps, "select") else eps.get("xgen_harness.tools", [])
            for ep in harness_eps:
                try:
                    factory = ep.load()
                    spec = factory()
                    if isinstance(spec, ToolPackageSpec):
                        loaded = _spec_to_tools(spec)
                        tools.extend(loaded)
                        logger.info("[Gallery] entry_point %s: %d tools", ep.name, len(loaded))
                except Exception as e:
                    logger.warning("[Gallery] entry_point %s 로드 실패: %s", ep.name, e)
    except Exception as e:
        logger.debug("[Gallery] entry_points 스캔 실패: %s", e)

    return tools


def _spec_to_tools(spec: ToolPackageSpec) -> list[GalleryTool]:
    """ToolPackageSpec → GalleryTool 리스트."""
    if not spec.call_tool:
        logger.warning("[Gallery] %s에 call_tool 없음", spec.name)
        return []

    tools = []
    for td in spec.tool_definitions:
        if isinstance(td, dict) and td.get("name"):
            tools.append(GalleryTool(td, spec.call_tool, package_name=spec.name))
    return tools
