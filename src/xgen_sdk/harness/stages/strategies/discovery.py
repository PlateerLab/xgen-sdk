"""ToolDiscoveryStrategy 구현체들 — v1.2.0 Claude Code 정합 deferred tools.

v1.2.0 기준 책임 분담:
  s04_tool: ToolSource 들 listing → selected_tools 화이트리스트로 eager/deferred 분리
            eager: state.tool_definitions (Anthropic API tools= 인자에 박힘)
            deferred: state.deferred_tools (이름+1줄 desc 만 노출)
            schema 캐시: state.tool_schemas (모든 도구의 full schema, ToolSearch 가 조회)

  ProgressiveDiscovery: s04 가 분리해놓은 결과 + 빌트인 (search/discover/fetch_pd/ToolSearch)
  EagerLoadDiscovery:   사용자가 명시적으로 eager_load strategy 선택 시 — deferred 무시
                         하고 모든 도구 schema 를 eager 로 환원 (백워드 호환).
"""

from typing import Any
from ..interfaces import ToolDiscoveryStrategy


# Level 1 description 최대 길이 (~40 tokens ≈ 120 chars)
_MAX_DESC_CHARS = 120

# 카탈로그가 이 크기 이상이면 search_tools 빌트인을 함께 등록.
# 작은 워크플로우는 discover_tools 만으로 충분 — 검색 도구를 늘려봐야 컨텍스트만 먹음.
# 공개 상수 — stage config API / UI 배지 가 import 해서 "검색 모드 전환 임계치" 로 표시.
SEARCH_TOOLS_THRESHOLD = 12
_SEARCH_TOOLS_THRESHOLD = SEARCH_TOOLS_THRESHOLD  # 내부 호환


def get_progressive_threshold() -> int:
    """현재 Progressive Disclosure search_tools 임계치. UI 배지/로그가 참조."""
    return SEARCH_TOOLS_THRESHOLD


# v1.8.0 — default tool 노출 모드 (s04_tool 의 selected_tools 명시 X 시 동작).
#   "strict" (v1.12.1 신 default): 사용자 명시 안 한 도구는 카탈로그 완전 제외 —
#                                   search_tools 검색 결과에서도 안 보임. 메타 도구
#                                   (search_tools/discover_tools/ToolSearch) 와 자원
#                                   매칭 도구 (rag_search/query_graph) 만 자동 노출.
#                                   진정한 PD = LLM 이 사용자가 박은 환경만 본다.
#   "deferred_default" (v1.8.0~v1.12.0 default): 사용자 박은 빌트인 노드 도구 = deferred,
#                                                 LLM 이 search_tools / ToolSearch 로 능동 발견
#   "eager_all" (옛 default, v1.7 이전): 모든 도구 즉시 eager (백워드 호환)
# 호스트가 register_default_tool_strategy(...) 로 변경 가능.
_DEFAULT_TOOL_STRATEGY: str = "strict"


def register_default_tool_strategy(mode: str) -> None:
    """v1.8.0 — selected_tools 명시 X 시 default 동작 외부 override.

    Args:
        mode:
          - "strict" (v1.12.1 default): 사용자 명시 도구 + 자원 매칭 + 메타만 노출.
            search_tools 도 이 화이트리스트 안에서만 검색.
          - "deferred_default" (v1.8~v1.12.0): 사용자 박은 도구 = deferred (search 로 발견)
          - "eager_all" (v1.7 이전): 모든 도구 즉시 eager

    호스트 (xgen-workflow / 외부 wheel) 가 호출. 옛 워크플로우 호환 위해 임시 환원,
    또는 도메인 특화 정책 (예: "사내 도구 카탈로그 작아 항상 eager").
    """
    if mode not in ("strict", "deferred_default", "eager_all"):
        raise ValueError(
            f"Invalid mode: {mode!r}. Must be 'strict' / 'deferred_default' / 'eager_all'."
        )
    global _DEFAULT_TOOL_STRATEGY
    _DEFAULT_TOOL_STRATEGY = mode


def _get_default_tool_strategy() -> str:
    """현재 default tool 노출 모드 (s04_tool 가 read)."""
    return _DEFAULT_TOOL_STRATEGY


def get_default_tool_strategy() -> str:
    """공개 alias — 디버그/UI 용."""
    return _DEFAULT_TOOL_STRATEGY


class ProgressiveDiscovery(ToolDiscoveryStrategy):
    """Progressive Disclosure 3단계 — 기본 전략 (v1.2.0 Claude Code 정합).

    Level 0 (search_tools): 전체 카탈로그 ≥ 12 시 키워드 검색 빌트인 추가
    Level 1 (tool_index):    eager 도구 + 빌트인 메타데이터 → system_prompt
    Level 2 (ToolSearch):    deferred 도구 schema 를 names 명시로 eager 승격
    Level 2'(discover_tools): 특정 도구 상세 input_schema 조회
    Level 3 (s07_act):       실제 도구 실행

    s04 가 selected_tools 화이트리스트로 분리해 놓은 state.tool_definitions (eager)
    + state.deferred_tools (이름만) 두 채널을 그대로 받아 augmented (Anthropic tools=
    인자) 와 tool_index (system_prompt 메타) 를 만든다.
    """

    @property
    def name(self) -> str:
        return "progressive_3level"

    @property
    def description(self) -> str:
        return "3단계 점진적 디스커버리 (eager+빌트인 → ToolSearch 승격 → 실행)"

    async def discover(
        self,
        tool_definitions: list[dict],
        state: Any,
    ) -> tuple[list[dict], list[dict]]:
        # tool_definitions 는 s04 가 selected_tools 로 추린 eager 도구만.
        # Level 1 메타 (~40 tokens/tool) — eager 만 system_prompt 에 schema 까지 박힘 안내.
        tool_index: list[dict] = []
        for td in tool_definitions:
            name = td.get("name", "unknown")
            raw_desc = td.get("description", "") or ""
            short_desc = raw_desc[:_MAX_DESC_CHARS]
            if len(raw_desc) > _MAX_DESC_CHARS:
                short_desc = short_desc.rsplit(" ", 1)[0] + "..."
            tool_index.append({
                "name": name,
                "description": short_desc,
                "category": td.get("category", "tool"),
            })

        augmented = list(tool_definitions)

        # 빌트인 등록 — search_tools / discover_tools / ToolSearch / fetch_pd.
        # search_tools / discover_tools 는 deferred 도구도 함께 검색·조회 가능하도록
        # state.tool_schemas (캐시) 의 모든 도구를 후보로 본다.
        from ...tools.builtin import DiscoverToolsTool, SearchToolsTool, FetchPDTool, ToolSearchTool

        all_known = list((state.tool_schemas or {}).values())
        if not all_known:
            all_known = list(tool_definitions)

        discover = DiscoverToolsTool(all_known)
        augmented.append(discover.to_api_format())
        if hasattr(state, "metadata"):
            state.metadata.setdefault("tool_registry", {})["discover_tools"] = discover

        if len(all_known) >= _SEARCH_TOOLS_THRESHOLD:
            # state_ref 전달 — execute 시 state.tool_schemas(live)를 보게 해, discover 이후
            # s04 가 등록하는 연결 자원(rag_search/query_graph)도 search_tools 가 발견.
            search = SearchToolsTool(all_known, state_ref=state)
            augmented.append(search.to_api_format())
            tool_index.append({
                "name": search.name,
                "description": search.description[:_MAX_DESC_CHARS],
                "category": "system",
            })
            if hasattr(state, "metadata"):
                state.metadata.setdefault("tool_registry", {})["search_tools"] = search

        # v1.2.0 — ToolSearch 빌트인. deferred 도구가 있으면 등록 (없으면 무의미).
        deferred = getattr(state, "deferred_tools", None) or []
        if deferred:
            tool_search = ToolSearchTool(state)
            augmented.append(tool_search.to_api_format())
            tool_index.append({
                "name": tool_search.name,
                "description": tool_search.description[:_MAX_DESC_CHARS],
                "category": "system",
            })
            if hasattr(state, "metadata"):
                state.metadata.setdefault("tool_registry", {})["ToolSearch"] = tool_search

        # fetch_pd — Progressive Disclosure 원본 조회 (PD chunks / tool_result preview).
        fetch_pd = FetchPDTool(state)
        augmented.append(fetch_pd.to_api_format())
        tool_index.append({
            "name": fetch_pd.name,
            "description": fetch_pd.description[:_MAX_DESC_CHARS],
            "category": "system",
        })
        if hasattr(state, "metadata"):
            state.metadata.setdefault("tool_registry", {})["fetch_pd"] = fetch_pd

        # state.tool_schemas 는 s04 가 모든 도구 (eager+deferred) full schema 를 미리 채움.
        # 비어있으면 fallback 으로 eager 만이라도 캐시 — 호환성 안전망.
        if hasattr(state, "tool_schemas") and not state.tool_schemas:
            state.tool_schemas = {td.get("name"): td for td in tool_definitions if td.get("name")}

        return tool_index, augmented


class EagerLoadDiscovery(ToolDiscoveryStrategy):
    """모든 도구 schema 를 즉시 로드 — 사용자가 명시적으로 ``eager_load`` 픽 시.

    s04 가 selected_tools 로 일부만 eager 로 분리했더라도, 이 strategy 는 그
    분리를 무시하고 deferred 까지 모두 eager 로 환원한다 (백워드 호환).
    """

    @property
    def name(self) -> str:
        return "eager_load"

    @property
    def description(self) -> str:
        return "모든 도구 스키마를 즉시 로드 (deferred 환원)"

    async def discover(
        self,
        tool_definitions: list[dict],
        state: Any,
    ) -> tuple[list[dict], list[dict]]:
        # deferred 까지 모두 eager 로 끌어올린다.
        eager = list(tool_definitions)
        eager_names = {td.get("name") for td in eager if td.get("name")}
        deferred = getattr(state, "deferred_tools", None) or []
        schemas = getattr(state, "tool_schemas", None) or {}

        for d in deferred:
            nm = d.get("name") if isinstance(d, dict) else None
            if not nm or nm in eager_names:
                continue
            schema = schemas.get(nm)
            if schema:
                eager.append(schema)
                eager_names.add(nm)
                if hasattr(state, "tool"):
                    state.tool.loaded_names.add(nm)

        # deferred 비워 — eager 환원 후 system_prompt 의 [deferred] 섹션도 사라짐.
        if hasattr(state, "deferred_tools"):
            state.deferred_tools = []

        tool_index = []
        for td in eager:
            tool_index.append({
                "name": td.get("name", "unknown"),
                "description": td.get("description", ""),
                "category": "tool",
                "schema": td.get("input_schema"),
            })
        return tool_index, eager
