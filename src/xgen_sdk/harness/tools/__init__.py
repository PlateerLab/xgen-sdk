"""
xgen-harness 도구 패키지 — ToolSource 레지스트리 (단일 도구 공급 채널).

v0.25.0 이후 엔진은 도구를 **`ToolSource` 한 경로로만` 얻는다. MCP 세션, Custom
API, xgen 워크플로우 노드 등 모든 공급원이 이 Protocol 뒤에 감춰진다. 엔진 특수
분기 0, 이식/외부 플러그인이 자기 소스를 ``register_tool_source()`` + entry_points
로 합류시킨다. (v1.0.5: synthesized tool 인프라는 dead trigger 라 제거됨.)

Usage (이식/외부)::

    from xgen_harness.tools import ToolSource, register_tool_source

    class MySource:
        source_id = "my-source"
        display_name = "My Tools"
        display_name_ko = "내 도구"
        description = "외부 API 도구 모음"
        icon = "🛠"
        category = "api"

        # 필터 스키마 — 프론트가 이 소스 Box 안에 sub-UI 로 렌더한다.
        filter_schema = {
            "tags": {
                "type": "multi_select",
                "options_source": "my-tags",   # 이식측 OptionSource 이름
                "description": "포함할 태그",
            }
        }

        async def list_tools(self, filters=None): ...
        async def call_tool(self, name, args): ...
        def has_tool(self, name): ...

    register_tool_source(MySource())

entry_points (외부 wheel)::

    [project.entry-points."xgen_harness.tool_sources"]
    my_source = "my_pkg:MySource"

엔진 수정 0 으로 하네스 s04 UI 에 "My Tools" Box 가 자동 등장하며, s07_act 가
``call_tool`` 로 dispatch. ``/api/harness/tool-sources`` 엔드포인트가 소스 목록 +
각 소스의 ``list_tools()`` 결과를 프론트에 내려준다.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any, Iterator, Optional, Protocol, runtime_checkable

logger = logging.getLogger("harness.tools")

_TOOL_SOURCES: list = []
_ENTRY_POINTS_DISCOVERED = False

# v0.25.0 — 현재 실행 컨텍스트의 HTTP 요청 헤더. ToolSource 가 user/auth 관련
# 헤더를 downstream 호출에 전파할 수 있게 한다. 엔진 ``/api/harness/tool-sources``
# 엔드포인트가 요청 헤더를 이 contextvar 에 실은 뒤 레지스트리를 호출한다.
_REQUEST_HEADERS_CTX: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "xgen_harness_request_headers", default={},
)


# ─── Protocol ────────────────────────────────────────────────────────────────

@runtime_checkable
class ToolSource(Protocol):
    """도구 소스 프로토콜 — 이 인터페이스만 구현하면 등록 가능.

    필수 메서드 3 개. 추가 메타 속성(source_id/display_name/filter_schema 등)은
    선택적 — 없으면 엔진이 안전한 폴백을 사용한다 (``describe_tool_source`` 참조).
    """

    async def list_tools(self, filters: Optional[dict] = None) -> list[dict]:
        """사용 가능한 도구 목록. ``filters`` 는 소스별 자유 해석 (태그/검색어 등).

        각 dict 는 최소 ``{"name", "description"}``. ``input_schema``/``annotations``
        ``tags`` 같은 선택 필드는 엔진이 그대로 전파한다.
        """
        ...

    async def call_tool(self, name: str, args: dict) -> dict:
        """도구 실행. 반환 dict 는 최소 ``{"content": str}``."""
        ...

    def has_tool(self, name: str) -> bool:
        """해당 이름의 도구를 이 소스가 가지고 있는지."""
        ...


# ─── Registry ───────────────────────────────────────────────────────────────

def register_tool_source(source: ToolSource) -> None:
    """도구 소스를 전역 레지스트리에 등록.

    같은 ``source_id`` 로 재등록하면 덮어쓴다 (hot-reload 시나리오 대응).
    """
    if not isinstance(source, ToolSource):
        raise TypeError(
            "Tool source must implement ToolSource protocol "
            "(list_tools, call_tool, has_tool). "
            f"Got: {type(source).__name__}"
        )
    sid = getattr(source, "source_id", None) or type(source).__name__
    # source_id 가 이미 있는 슬롯은 교체
    for i, existing in enumerate(_TOOL_SOURCES):
        existing_sid = getattr(existing, "source_id", None) or type(existing).__name__
        if existing_sid == sid:
            _TOOL_SOURCES[i] = source
            logger.info("[tools] tool source replaced: %s", sid)
            return
    _TOOL_SOURCES.append(source)
    logger.info("[tools] tool source registered: %s", sid)


def get_tool_sources() -> list:
    """등록된 모든 도구 소스 반환 (복사본).

    첫 호출 시 ``xgen_harness.tool_sources`` entry_points 자동 스캔.
    """
    _discover_from_entry_points_once()
    return list(_TOOL_SOURCES)


def clear_tool_sources() -> None:
    """테스트용: 등록된 도구 소스 초기화."""
    global _ENTRY_POINTS_DISCOVERED
    _TOOL_SOURCES.clear()
    _ENTRY_POINTS_DISCOVERED = False


# ─── UI / API 메타 헬퍼 ────────────────────────────────────────────────────

def describe_tool_source(source: Any) -> dict:
    """프론트 s04 UI 가 소스 Box 를 렌더할 때 필요한 메타.

    속성 누락 시 안전한 폴백 — 외부 작업자가 최소 Protocol 3 메서드만 만족해도
    UI 에 등장한다. 단 Box 라벨이 ``type(source).__name__`` 이 되므로 실사용
    시 ``source_id`` + ``display_name`` 만큼은 권장.
    """
    sid = getattr(source, "source_id", None) or type(source).__name__
    display_en = getattr(source, "display_name", None) or sid
    display_ko = getattr(source, "display_name_ko", None) or display_en
    desc = {
        "source_id": sid,
        "display_name": display_en,
        "display_name_ko": display_ko,
        "description": getattr(source, "description", "") or "",
        "icon": getattr(source, "icon", "") or "",
        "category": getattr(source, "category", "tools") or "tools",
        "filter_schema": dict(getattr(source, "filter_schema", None) or {}),
    }
    return desc


def describe_all_sources() -> list[dict]:
    """등록된 모든 소스의 메타. 엔진 ``/api/harness/tool-sources`` 응답 기반."""
    return [describe_tool_source(s) for s in get_tool_sources()]


async def list_all_tools(
    filters_by_source: Optional[dict[str, dict]] = None,
) -> dict[str, list[dict]]:
    """모든 소스의 ``list_tools()`` 결과를 병합한 ``{source_id: [...]}``.

    ``filters_by_source`` 는 source_id → filter params. 소스가 ``list_tools``
    에 ``filters`` 인자를 받지 않는 구형 구현이면 TypeError 를 흡수하고
    인자 없이 재호출한다 (backwards compatibility).
    """
    filters_by_source = filters_by_source or {}
    out: dict[str, list[dict]] = {}
    for src in get_tool_sources():
        sid = getattr(src, "source_id", None) or type(src).__name__
        filters = filters_by_source.get(sid)
        try:
            if filters is None:
                try:
                    listed = await src.list_tools()
                except TypeError:
                    listed = await src.list_tools(None)
            else:
                try:
                    listed = await src.list_tools(filters)
                except TypeError:
                    # 구형 소스 — filters 무시
                    listed = await src.list_tools()
        except Exception as e:
            logger.debug("[tools] list_tools failed for %s: %s", sid, e)
            listed = []
        out[sid] = list(listed or [])
    return out


def get_request_headers() -> dict[str, str]:
    """현재 실행 컨텍스트의 HTTP 요청 헤더를 ``{name: value}`` 로 반환.

    ToolSource 가 self-loopback HTTP 호출 시 ``Authorization`` / ``x-user-*``
    같은 인증 헤더를 그대로 전파할 수 있게 한다. 엔진 API 바깥 (직접 sdk 사용)
    에서는 빈 dict.
    """
    try:
        return dict(_REQUEST_HEADERS_CTX.get() or {})
    except Exception:
        return {}


@contextlib.contextmanager
def use_request_headers(headers: dict[str, str] | None) -> Iterator[None]:
    """요청 헤더를 contextvar 에 설정하는 컨텍스트 매니저.

    엔진 API 엔드포인트가 ToolSource 를 호출하기 직전에 사용한다::

        with use_request_headers(dict(request.headers)):
            sources = await list_all_tools()
    """
    token = _REQUEST_HEADERS_CTX.set(dict(headers or {}))
    try:
        yield
    finally:
        _REQUEST_HEADERS_CTX.reset(token)


def source_of(tool_name: str) -> Optional[str]:
    """도구 이름 → 그 도구를 가진 소스의 source_id (없으면 None).

    s07_act 가 dispatch 직전에 쓰면 **누가 실행할지** 명확해진다. 같은 이름을
    여러 소스가 가지면 가장 먼저 등록된 소스가 승자 (확정성).
    """
    for src in get_tool_sources():
        try:
            if src.has_tool(tool_name):
                return getattr(src, "source_id", None) or type(src).__name__
        except Exception:
            continue
    return None


# ─── Entry points + manifest preload (내부) ───────────────────────────────

def _discover_from_entry_points_once() -> None:
    """외부 패키지의 ``xgen_harness.tool_sources`` entry_points 자동 스캔.

    entry_point 반환값 허용 형태:
      - ToolSource 인스턴스
      - 0 인자 factory (callable → 호출 결과가 ToolSource)
      - iterable of ToolSource
    """
    global _ENTRY_POINTS_DISCOVERED
    if _ENTRY_POINTS_DISCOVERED:
        return
    _ENTRY_POINTS_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.tool_sources"
        if hasattr(eps, "select"):
            items = eps.select(group=group)
        else:
            items = eps.get(group, [])
        for ep in items:
            try:
                loaded = ep.load()
                result = loaded() if callable(loaded) else loaded
                _register_entry_point_result(result)
            except Exception as e:
                logger.debug("[tools] entry_point %s load failed: %s", ep.name, e)
    except Exception as e:
        logger.debug("[tools] entry_points scan failed: %s", e)


def _register_entry_point_result(result: Any) -> None:
    """entry_point 결과를 허용 형태에 따라 register_tool_source."""
    if result is None:
        return
    if isinstance(result, ToolSource):
        register_tool_source(result)
        return
    if hasattr(result, "__iter__") and not isinstance(result, (str, bytes)):
        for item in result:
            if isinstance(item, ToolSource):
                register_tool_source(item)


# ──────────────────────────────────────────────────────────────────────
# Term Expander re-exports (v0.26.20+) — search_tools query 확장 메커니즘.
# 엔진은 빈 dict + Protocol + entry_points 만. 도메인 alias (한국어 등) 는
# 외부 plug 가 ``register_search_alias`` 또는 ``register_term_expander`` 로 주입.
# ──────────────────────────────────────────────────────────────────────
from .builtin import (
    TermExpander,
    register_term_expander,
    register_search_alias,
    list_term_expanders,
    list_search_aliases,
)
