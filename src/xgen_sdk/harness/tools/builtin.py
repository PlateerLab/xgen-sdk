"""
Built-in Tools — 하네스 기본 제공 도구

진정한 Progressive Disclosure (Anthropic 스타일):
  Level 0: search_tools(query)       — 키워드로 도구 검색 (큰 카탈로그용)
  Level 1: discover_tools()          — 메타 목록 (이름+설명)
  Level 2: discover_tools(tool_name) — 상세 input_schema
  Level 3: 실제 도구 호출

도구 카탈로그가 작으면 Level 1 만으로 충분, 100+ 개면 search_tools 부터 시작.
"""

import logging
import re
from typing import Callable

from .base import Tool, ToolResult

# v1.0.9 — Term expander 인프라는 term_expansion.py 단일 정의로 분리됨 (god-class 정리).
# 본 모듈에서는 하위 호환을 위한 re-export 만 유지. 외부 호출자는 신규 모듈 또는
# tools 패키지 (`from xgen_harness.tools import register_term_expander`) 사용 권장.
from .term_expansion import (
    TermExpander,
    register_term_expander,
    register_search_alias,
    list_term_expanders,
    list_search_aliases,
    expand_query_terms,
    _expand_query_terms,  # 구 private alias 호환
)
from .skill_registry import (
    get_skill_body,
    list_skill_names,
    register_skill_body,
)

logger = logging.getLogger("harness.tools.search")


class DiscoverToolsTool(Tool):
    """에이전트가 도구의 상세 스키마를 조회하는 빌트인 도구.

    Progressive Disclosure:
    - Level 1: 시스템 프롬프트에 이름+설명만 포함 (~40 tokens/tool)
    - Level 2: 이 도구로 상세 input_schema 조회
    - Level 3: 실제 도구 실행
    """

    def __init__(self, tool_definitions: list[dict]):
        self._tool_defs = {t["name"]: t for t in tool_definitions}

    @property
    def name(self) -> str:
        return "discover_tools"

    @property
    def description(self) -> str:
        # v1.11.4 — PD 정신: 도구 메타만, 사용 순서 강제 톤 폐기.
        return (
            "List eager tools, or get full schema for a specific tool (tool_name='X')."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Name of the tool to get details for. Omit to list all.",
                },
            },
        }

    @property
    def category(self) -> str:
        return "system"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False  # 내부 카탈로그만 읽음

    async def execute(self, input_data: dict) -> ToolResult:
        tool_name = input_data.get("tool_name", "")

        if not tool_name:
            # 전체 목록
            lines = []
            for name, td in self._tool_defs.items():
                desc = td.get("description", "")[:100]
                lines.append(f"- {name}: {desc}")
            return ToolResult.success("\n".join(lines) if lines else "No tools available.")

        td = self._tool_defs.get(tool_name)
        if not td:
            return ToolResult.error(f"Tool '{tool_name}' not found.")

        import json
        return ToolResult.success(json.dumps(td, indent=2, ensure_ascii=False))


# ── BM25F 도구검색 헬퍼 (Anthropic Tool Search 정합: BM25 over name + description +
#    argument names + argument descriptions). 이전 ad-hoc 가중치(+3/+5/+1)는 업계 어디서도
#    안 쓰는 임시방편이라 정식 BM25 로 교체. 필드 가중치는 BM25F(원리), title boost 는
#    synaptic-memory(BM25 FTS title 3x) 참조. 토큰 부분일치로 한국어 조사 변형 흡수. ──
_BM25_K1 = 1.5
_BM25_B = 0.75
_BM25_FIELD_WEIGHTS = {"name": 3.0, "argn": 2.0, "desc": 1.0, "argd": 1.0}


def _st_tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zA-Z가-힣_]+", (text or "").lower()) if t]


def _st_field_tokens(td: dict) -> dict:
    """도구 dict → 4 필드 토큰. input_schema.properties 에서 인자명·인자설명까지."""
    schema = td.get("input_schema") or td.get("inputSchema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    argn: list[str] = []
    argd: list[str] = []
    if isinstance(props, dict):
        for k, v in props.items():
            argn.append(str(k))
            if isinstance(v, dict) and v.get("description"):
                argd.append(str(v.get("description")))
    return {
        "name": _st_tokenize(td.get("name") or ""),
        "argn": _st_tokenize(" ".join(argn)),
        "desc": _st_tokenize(td.get("description") or ""),
        "argd": _st_tokenize(" ".join(argd)),
    }


def _st_tf(term: str, toks: list[str]) -> int:
    # 토큰 부분일치 — '컬렉션' ⊂ '컬렉션에'(조사) 대응. 순수 BM25 는 정확토큰이나 한국어
    # 형태소 미적용 환경에서 재현율 위해 substring 허용(synaptic 의 substring fallback 참조).
    return sum(1 for tok in toks if term in tok)


class SearchToolsTool(Tool):
    """도구 카탈로그를 BM25 로 검색 — Progressive Disclosure Level 0.

    카탈로그가 큰 환경(100+ 도구) 에서 첫 호출에 모든 메타를 system_prompt 에
    싣는 비효율을 막음. Anthropic 의 sandbox 도구 패턴 차용 — 환경만 주고
    에이전트가 필요할 때 검색.

    호출 예:
      search_tools(query="email", limit=5)
      → [{"name":"mcp_gmail_send","description":"..."}, ...]
    """

    def __init__(self, tool_definitions: list[dict], state_ref=None):
        self._tools = list(tool_definitions)
        # live 카탈로그 — state.tool_schemas 가 있으면 그것을 본다. 순서버그 fix:
        # SearchToolsTool 생성(discovery 전략) 이후 s04 가 등록하는 연결 자원
        # (rag_search/query_graph)도 검색 대상에 포함되게 한다(스냅샷이면 영영 누락).
        self._state = state_ref

    def _catalog(self) -> list[dict]:
        st = self._state
        if st is not None:
            sch = getattr(st, "tool_schemas", None)
            if sch:
                return list(sch.values())
        return self._tools

    @property
    def name(self) -> str:
        return "search_tools"

    @property
    def description(self) -> str:
        # v1.11.4 — PD 정신: 도구 메타만, 사용 순서 강제 톤 폐기.
        return (
            "Search the tool catalog by keyword (eager + deferred)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword(s); BM25-ranked over tool name, description, and argument names/descriptions."},
                "limit": {"type": "integer", "description": "Max results (default 8).", "default": 8},
                "category": {"type": "string", "description": "Filter by category (optional)."},
            },
            "required": ["query"],
        }

    @property
    def category(self) -> str:
        return "system"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False

    async def execute(self, input_data: dict) -> ToolResult:
        q = (input_data.get("query") or "").strip().lower()
        limit = int(input_data.get("limit") or 8)
        category_filter = (input_data.get("category") or "").strip().lower()
        if not q:
            return ToolResult.error("'query' is required.")

        # v0.26.19 — 한국어 query 가 영문 도구명/description 과 매칭 안 되던 결함 fix.
        # raw terms 1차 매칭 후 0건이면 한국어→영문 alias 로 expand 후 2차 매칭.
        # 그래도 0건이면 LLM 이 "도구 없음" 결론 안 내도록 카테고리 hint + 전체
        # discover 안내. 라이브 사례: "네이버 뉴스" → mcp_naver_news_mcp 매치 실패.
        raw_terms = [t for t in re.split(r"\s+", q) if t]
        expanded_terms = _expand_query_terms(raw_terms)
        scored = self._score_terms(expanded_terms, category_filter)
        # category 필터가 0건이면 풀고 재검색 — 카탈로그(to_api_format)엔 category 메타가
        # 없어 LLM 이 박은 필터가 전부 제외시키는 회귀 방지(0건 침묵 대신 BM25 결과 노출).
        if not scored and category_filter:
            scored = self._score_terms(expanded_terms, "")

        scored.sort(key=lambda x: -x[0])
        top = scored[:limit]
        if not top:
            return ToolResult.success(self._empty_match_hint(q, category_filter, limit))

        used_aliases = [t for t in expanded_terms if t not in raw_terms]
        header = f"Matched {len(top)} of {len(scored)} tools"
        if used_aliases:
            header += f" (expanded with: {', '.join(used_aliases)})"
        lines = [f"{header}:"]
        for s, td in top:
            n = td.get("name", "?")
            d = (td.get("description") or "")[:120]
            lines.append(f"- {n} (score={s}): {d}")
        # v1.11.4 — PD 정신: 결과 자체가 환경 노출. 다음 행동 권유 폐기.
        return ToolResult.success("\n".join(lines))

    def _score_terms(self, terms: list[str], category_filter: str) -> list[tuple[float, dict]]:
        """BM25F over {name, arg-names, description, arg-descriptions}.

        IDF(희소어 가중) · TF saturation(k1) · 길이정규화(b) · 필드가중치(BM25F).
        이전 +3/+5/+1 ad-hoc 점수 대비: 희귀어 우대, 긴 설명 편향 제거, 인자 필드 포함.
        """
        import math
        docs: list[tuple[dict, dict, float]] = []
        for td in self._catalog():
            cat = (td.get("category") or td.get("metadata", {}).get("category") or "").lower()
            if category_filter and category_filter not in cat:
                continue
            ftoks = _st_field_tokens(td)
            dl = sum(_BM25_FIELD_WEIGHTS[f] * len(toks) for f, toks in ftoks.items())
            docs.append((td, ftoks, dl))
        if not docs:
            return []
        N = len(docs)
        avgdl = (sum(dl for _, _, dl in docs) / N) or 1.0
        idf: dict[str, float] = {}
        for t in terms:
            dfq = sum(1 for _, ftoks, _ in docs if any(_st_tf(t, toks) for toks in ftoks.values()))
            if dfq > 0:
                idf[t] = math.log(1 + (N - dfq + 0.5) / (dfq + 0.5))
        scored: list[tuple[float, dict]] = []
        for td, ftoks, dl in docs:
            s = 0.0
            for t in terms:
                w = idf.get(t)
                if not w:
                    continue
                wtf = sum(_BM25_FIELD_WEIGHTS[f] * _st_tf(t, toks) for f, toks in ftoks.items())
                if wtf <= 0:
                    continue
                s += w * (wtf * (_BM25_K1 + 1)) / (wtf + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl))
            if s > 0:
                scored.append((round(s, 3), td))
        return scored

    def _empty_match_hint(self, q: str, category_filter: str, limit: int) -> str:
        """매칭 0건일 때 LLM 이 즉시 포기하지 않도록 카테고리 후보 + 권유.

        총 도구 수 적으면 (≤ 20) 그냥 전체 목록 제공. 많으면 카테고리별 top 몇 개씩
        샘플링. 마지막에 명확한 다음 액션 (discover_tools / 다른 query) 안내.
        """
        catalog = self._catalog()
        if not catalog:
            return f"No tools available."

        from collections import defaultdict
        by_cat: dict[str, list[dict]] = defaultdict(list)
        for td in catalog:
            cat = (td.get("category") or td.get("metadata", {}).get("category") or "uncategorized").lower()
            by_cat[cat].append(td)

        lines = [f"No exact match for '{q}'. Showing available tools by category:"]
        # 카테고리당 limit/카테고리수 정도로 sample (최소 1, 최대 limit)
        per_cat = max(1, min(limit, 5))
        for cat in sorted(by_cat.keys()):
            if category_filter and category_filter not in cat:
                continue
            samples = by_cat[cat][:per_cat]
            lines.append(f"\n[{cat}] ({len(by_cat[cat])} tools)")
            for td in samples:
                n = td.get("name", "?")
                d = (td.get("description") or "")[:80]
                lines.append(f"- {n}: {d}")
            if len(by_cat[cat]) > per_cat:
                lines.append(f"  ... +{len(by_cat[cat]) - per_cat} more")
        # v1.11.4 — PD 정신: 환경 상태 (카테고리별 도구 샘플) 만 노출. 다음 행동
        # 강제 권유 (e.g. 'naver' / 'news' 같은 keyword 예시 박기) 폐기.
        return "\n".join(lines)


class ToolSearchTool(Tool):
    """v1.2.0 — Claude Code 스타일 deferred tools 승격 빌트인.

    s04_tool 이 selected_tools 화이트리스트 외 도구를 모두 deferred 로 보내고,
    full schema 는 ``state.tool_schemas`` 캐시에만 둔다. LLM 은 system_prompt
    의 [deferred] 섹션에서 도구 이름을 보고, 필요한 도구만 이 빌트인으로 명시
    승격해 호출 가능 상태로 만든다.

    호출 패턴:
      ToolSearch(names=["mcp_notion_search", "brave_web_search"])
        → state.tool_schemas[name] 을 state.tool_definitions 에 합류
        → 다음 llm_call 의 tools= 인자에 자동 누적
        → LLM 이 그 도구를 직접 호출 가능

    keyword 검색은 별도 search_tools 빌트인 (token-grep) 이 담당.
    """

    def __init__(self, state_ref):
        # state_ref: PipelineState 인스턴스. 매 턴 같은 인스턴스라 schemas / definitions
        # 가 live 하게 공유된다 (FetchPDTool 와 같은 패턴).
        self._state = state_ref

    @property
    def name(self) -> str:
        return "ToolSearch"

    @property
    def description(self) -> str:
        # v1.8.0 — RESTRICTIONS_ONLY 톤.
        return (
            "Load deferred tool schemas to make callable. "
            "Pass exact names. Tool callable next turn."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact tool names to load (from the [deferred] list).",
                },
            },
            "required": ["names"],
        }

    @property
    def category(self) -> str:
        return "system"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False  # 캐시(state.tool_schemas) 만 읽음

    async def execute(self, input_data: dict) -> ToolResult:
        names_in = input_data.get("names") or []
        if isinstance(names_in, str):
            # "select:a,b" 형태 또는 단일 문자열도 허용
            raw = names_in.split(":", 1)[1] if names_in.startswith("select:") else names_in
            names_in = [n.strip() for n in raw.split(",") if n.strip()]
        if not isinstance(names_in, list) or not names_in:
            return ToolResult.error("'names' must be a non-empty array of tool names.")

        schemas = self._state.tool_schemas or {}
        existing = {td.get("name") for td in self._state.tool_definitions}
        loaded: list[str] = []
        not_found: list[str] = []
        already: list[str] = []

        for nm in names_in:
            if not isinstance(nm, str) or not nm.strip():
                continue
            nm = nm.strip()
            if nm in existing:
                already.append(nm)
                continue
            schema = schemas.get(nm)
            if not schema:
                not_found.append(nm)
                continue
            self._state.tool_definitions.append(schema)
            self._state.tool.loaded_names.add(nm)
            loaded.append(nm)

        # ToolLoadedEvent emit (best-effort).
        try:
            from ..events.types import ToolLoadedEvent
            await self._state.emit_verbose(ToolLoadedEvent(
                names=loaded,
                total_loaded=len(self._state.tool.loaded_names),
            ))
        except Exception as _e:
            logger.debug("[ToolSearch] event emit failed: %s", _e)

        lines = []
        if loaded:
            lines.append(f"Loaded {len(loaded)} tool(s): {', '.join(loaded)}")
            lines.append("These tools are now callable in your next turn.")
        if already:
            lines.append(f"Already loaded: {', '.join(already)}")
        if not_found:
            available = list(schemas.keys())[:20]
            lines.append(
                f"Not found: {', '.join(not_found)}. "
                f"Available deferred tools (first 20): {', '.join(available) or '(none)'}"
            )
        if not lines:
            lines.append("Nothing to load.")
        return ToolResult.success("\n".join(lines))


class FetchPDTool(Tool):
    """Progressive Disclosure 원본 조회 — messages 에 preview 만 노출한 리소스의 전체 내용 반환.

    Push-side 압축 (L1 tool result budget, L3 microcompact) 또는 pull-side 출력 지연
    (RAG 청크, DB 스키마 등) 으로 원본이 state.pd_stores 에 보관된 경우, 에이전트가
    이 도구로 id 를 명시해 원본을 당겨올 수 있습니다.

    호출 예:
      fetch_pd(kind="tool_result", id="toolu_01abc...")  → 50KB+ 도구 결과 원본
      fetch_pd(kind="rag",          id="0")              → 첫 번째 RAG 청크 본문
      fetch_pd(kind="db_schema",    id="products")       → 특정 테이블 스키마 상세

    종류 목록만 궁금하면 `kind` 만 주고 id 를 생략해 id 리스트를 받습니다.
    """

    def __init__(self, state_ref):
        # state_ref: PipelineState 인스턴스. s04 가 주입.
        # 매 턴마다 s04 가 FetchPDTool 을 재생성하거나, __init__ 에서 받은 ref 가
        # 같은 PipelineState 인스턴스이므로 pd_stores 는 live 하게 공유됩니다.
        self._state = state_ref

    @property
    def name(self) -> str:
        return "fetch_pd"

    @property
    def description(self) -> str:
        # v1.8.0 — fetch_pd 의존도 낮춤. search 결과 (snippet) 만으로 합성이 default.
        return (
            "Fetch full body by (kind, id). RARELY needed — synthesize from search "
            "snippets first. Use only if a specific chunk is clearly insufficient. "
            "Kinds: rag, tool_result, history, db_schema, gallery, graph."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Resource type (tool_result | rag | history | db_schema | gallery | ...)",
                },
                "id": {
                    "type": "string",
                    "description": "Resource id within the kind. Omit to list available ids.",
                },
            },
            "required": ["kind"],
        }

    @property
    def category(self) -> str:
        return "system"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False  # pd_stores 는 프로세스 내부 state

    async def execute(self, input_data: dict) -> ToolResult:
        kind = (input_data.get("kind") or "").strip()
        rid = input_data.get("id")
        if not kind:
            return ToolResult.error("'kind' is required.")
        if rid is None or rid == "":
            ids = self._state.pd_list(kind)
            if not ids:
                return ToolResult.success(f"No stored resources for kind={kind!r}.")
            return ToolResult.success(
                f"kind={kind!r} has {len(ids)} resources:\n" +
                "\n".join(f"- {i}" for i in ids[:50]) +
                ("\n..." if len(ids) > 50 else "")
            )
        entry = self._state.pd_fetch(kind, str(rid))
        if entry is None:
            return ToolResult.error(
                f"No resource for kind={kind!r} id={rid!r}. "
                f"Available ids: {self._state.pd_list(kind)[:10]}"
            )
        full = entry.get("full", "")
        meta = entry.get("meta", {})
        header = f"[pd:{kind}:{rid}] meta={meta}" if meta else f"[pd:{kind}:{rid}]"
        # v1.8.0 — 청크 원문 본문 들이붓기 차단. 사용자 명시: "청크 원문 박는 걸 막으라".
        # rag/tool_result kind 는 본문 cap 1500자 (snippet 600 + 약간 더). 큰 본문 들이붓기 X.
        # tool_result/db_schema/gallery/graph/history 도 동일 cap. context 보호.
        # 더 깊이 필요하면 → 다른 query / 다른 search 도구 시도 (fetch_pd 들이붓기 X).
        _CAP = 1500
        if len(full) > _CAP:
            full = (
                full[:_CAP] +
                f"\n\n[body capped at {_CAP} — original {len(full):,} chars. "
                f"Don't fetch more chunks. Synthesize from this preview, "
                f"or run a different search query, or stop.]"
            )
        return ToolResult.success(f"{header}\n\n{full}")


# ────────────────────────────────────────────────────────────────────────────
# v1.8.0 — FetchSynthesizeTool: sub-agent 패턴 (본문 main context 격리)
# ────────────────────────────────────────────────────────────────────────────


class FetchSynthesizeTool(Tool):
    """v1.9.0 — Claude Code 서브에이전트 패턴. body 를 sub-LLM system prompt 에 박지
    않음. sub-agent 가 자기 도구로 본문 chunk 자율 탐색 → done() 으로 main 복귀.

    main agent 가 fetch_synthesize(kind, id, query) 호출:
      1. 코드가 state.pd_stores 에서 본문 read (sub-agent context 박지 X)
      2. sub-agent 가 자기 컨텍스트 안에서 ReAct 루프:
         - read_chunk(start, end) 로 본문 chunk lazy 탐색
         - search_in_body(keyword) 로 키워드 위치 발견
         - done(synthesis, citations[], missing_topics[]) 로 main 복귀
      3. main 엔 done() 의 synthesis 텍스트만 박힘 — body / 중간 read 폐기

    v1.9.0 핵심 (vs v1.8.0):
    - body 가 sub-agent system prompt 에 통째 박히던 → 도구로 lazy 탐색 (vLLM 안전)
    - 합성 응답에 verbatim citation 발췌 + missing_topics 강제 → 누락 위험 ↓
    - 작은 본문 (< 2000자) raw passthrough — sub-agent 호출 자체 skip (비용/속도 ↑)
    - 작은 본문 raw passthrough 임계는 register_runtime_default('synth_raw_threshold', N)
      으로 호스트 override.
    """

    # Raw passthrough 임계 (chars). 본문이 이보다 작으면 sub-agent 호출 안 함.
    # 호스트가 register_runtime_default('synth_raw_threshold', N) 으로 override.
    _RAW_PASSTHROUGH_THRESHOLD_DEFAULT = 2000

    # Sub-agent ReAct 최대 turn (안전망 — 무한 루프 차단).
    _SUB_MAX_TURNS_DEFAULT = 8

    def __init__(self, state_ref):
        self._state = state_ref

    @property
    def name(self) -> str:
        return "fetch_synthesize"

    @property
    def description(self) -> str:
        return (
            "Fetch a resource body and synthesize an answer in an isolated sub-agent loop. "
            "The body is never loaded into your context — the sub-agent reads chunks via "
            "its own tools and returns only the synthesis + verbatim citations. "
            "Use when you need to extract specific info from a large body without context bloat. "
            "Pass kind, id (from search index), and query (what to extract)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Resource type (rag | tool_result | history | db_schema | gallery | graph)",
                },
                "id": {
                    "type": "string",
                    "description": "Resource id within kind (from search index, e.g. 'assort#1').",
                },
                "query": {
                    "type": "string",
                    "description": "What to extract/synthesize from the body (natural language).",
                },
            },
            "required": ["kind", "id", "query"],
        }

    @property
    def category(self) -> str:
        return "system"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return False  # sub-LLM 호출 — 같은 입력도 미세 다른 합성 가능

    @property
    def open_world_hint(self) -> bool:
        return True   # LLM provider 외부 호출

    @staticmethod
    def _resolve_raw_threshold() -> int:
        try:
            from ..core.runtime_defaults import resolve_with_default
            v = resolve_with_default(None, "synth_raw_threshold",
                                     FetchSynthesizeTool._RAW_PASSTHROUGH_THRESHOLD_DEFAULT)
            return int(v)
        except Exception:
            return FetchSynthesizeTool._RAW_PASSTHROUGH_THRESHOLD_DEFAULT

    @staticmethod
    def _resolve_max_turns() -> int:
        try:
            from ..core.runtime_defaults import resolve_with_default
            v = resolve_with_default(None, "synth_sub_max_turns",
                                     FetchSynthesizeTool._SUB_MAX_TURNS_DEFAULT)
            return int(v)
        except Exception:
            return FetchSynthesizeTool._SUB_MAX_TURNS_DEFAULT

    async def execute(self, input_data: dict) -> ToolResult:
        kind = (input_data.get("kind") or "").strip()
        rid = input_data.get("id")
        query = (input_data.get("query") or "").strip()
        if not kind or rid is None or rid == "" or not query:
            return ToolResult.error("'kind', 'id', 'query' all required.")

        entry = self._state.pd_fetch(kind, str(rid))
        if entry is None:
            return ToolResult.error(
                f"No resource for kind={kind!r} id={rid!r}. "
                f"Available ids: {self._state.pd_list(kind)[:10]}"
            )
        body = entry.get("full", "")
        if not body:
            return ToolResult.success(
                f"Empty body for kind={kind!r} id={rid!r}. Nothing to synthesize."
            )

        body_chars = len(body)
        raw_threshold = self._resolve_raw_threshold()

        # ── Raw passthrough — 작은 본문은 sub-agent 호출 skip ──
        # main 에 본문 통째 박아도 컨텍스트 안전. 합성/압축 없음 = 누락 0.
        if body_chars <= raw_threshold:
            try:
                from ..events.types import StageSubstepEvent
                await self._state.emit_verbose(StageSubstepEvent(
                    stage_id="s07_act",
                    substep="sub_agent_raw_passthrough",
                    meta={
                        "tool": "fetch_synthesize",
                        "kind": kind, "id": str(rid), "query": query,
                        "body_chars": body_chars,
                        "threshold": raw_threshold,
                    },
                ))
            except Exception:
                pass
            return ToolResult.success(
                f"[raw passthrough — body={body_chars} chars ≤ threshold={raw_threshold}, "
                f"no compression; missing risk = 0]\n\n{body}"
            )

        # ── Sub-agent ReAct 루프 ──
        try:
            from ..core.provider_bootstrap import ensure_provider
            provider = await ensure_provider(self._state)
        except Exception as e:
            return ToolResult.error(
                f"sub-agent provider unavailable: {e}. "
                f"Synthesize from search snippets you already have."
            )
        if provider is None:
            return ToolResult.error("sub-agent provider not bootstrapped.")

        # 본문은 sub-agent 의 closure 변수로만 (system prompt 박지 X).
        # sub-agent 의 read_chunk / search_in_body 가 호출 시점에 lazy load.
        body_ref = body  # closure 캡처

        # ── Sub-agent 의 도구 정의 ──
        sub_tool_defs = [
            {
                "name": "read_chunk",
                "description": (
                    "Read a slice of the resource body. Use this to inspect specific "
                    "parts of the body that you want to cite verbatim. "
                    f"Body is {body_chars} chars total. "
                    "Pass start (0-indexed char position) and end (exclusive)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "integer", "description": "Start char index (0-indexed)."},
                        "end": {"type": "integer", "description": "End char index (exclusive). Max 1500 chars per call."},
                    },
                    "required": ["start", "end"],
                },
            },
            {
                "name": "search_in_body",
                "description": (
                    "Find a keyword's positions in the body. Returns up to 10 char "
                    "indices where the keyword first appears in each match. "
                    "Use to locate relevant sections quickly without reading sequentially."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "Keyword to find (case-insensitive)."},
                    },
                    "required": ["keyword"],
                },
            },
            {
                "name": "done",
                "description": (
                    "Return the final synthesis to the caller and exit. "
                    "MUST be called once you have enough info. "
                    "synthesis: 1-5 sentence answer. citations: list of verbatim excerpts "
                    "you read (for grounding). missing_topics: topics in the body NOT "
                    "covered by your synthesis (so the caller can decide to re-query)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "synthesis": {"type": "string", "description": "Final synthesis answer."},
                        "citations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Verbatim excerpts from the body that support the synthesis.",
                        },
                        "missing_topics": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Body topics not covered (helps caller decide to re-query).",
                        },
                    },
                    "required": ["synthesis"],
                },
            },
        ]

        # Helper: keyword 검색
        def _search_positions(haystack: str, needle: str, limit: int = 10) -> list[int]:
            positions: list[int] = []
            if not needle:
                return positions
            hay_lc = haystack.lower()
            ned_lc = needle.lower()
            idx = 0
            while True:
                p = hay_lc.find(ned_lc, idx)
                if p < 0 or len(positions) >= limit:
                    break
                positions.append(p)
                idx = p + max(1, len(ned_lc))
            return positions

        sub_system = (
            "You are a sub-agent assisting a main LLM by extracting facts from one "
            "isolated resource body. The body is NOT in your context — read it via the "
            "`read_chunk` and `search_in_body` tools.\n\n"
            "Workflow:\n"
            "1. Optionally call `search_in_body(keyword)` to locate relevant sections.\n"
            "2. Call `read_chunk(start, end)` to read the surrounding context (max ~1500 "
            "chars per call). Repeat until you have enough.\n"
            "3. Call `done(synthesis, citations, missing_topics)` to return.\n\n"
            "Rules:\n"
            "- Quote verbatim in `citations` — never paraphrase the body inside citations.\n"
            "- Be honest in `missing_topics` — if you skipped a section, list its theme.\n"
            "- Do NOT speculate beyond what you read in chunks.\n"
            f"- Finish within {self._resolve_max_turns()} tool turns."
        )
        sub_messages: list[dict] = [{
            "role": "user",
            "content": (
                f"Resource: kind={kind}, id={rid}, body={body_chars} chars (not in context).\n"
                f"Query: {query}\n\n"
                f"Explore the body via tools, then call `done`."
            ),
        }]

        try:
            from ..events.types import StageSubstepEvent
            await self._state.emit_verbose(StageSubstepEvent(
                stage_id="s07_act",
                substep="sub_agent_call_start",
                meta={
                    "tool": "fetch_synthesize",
                    "kind": kind, "id": str(rid), "query": query,
                    "body_chars": body_chars,
                    "mode": "react_subagent",
                    "max_turns": self._resolve_max_turns(),
                },
            ))
        except Exception:
            pass

        # ── ReAct 루프 ──
        max_turns = self._resolve_max_turns()
        done_output: dict | None = None
        last_error: str | None = None
        for turn_idx in range(max_turns):
            tool_uses: list[dict] = []
            text_acc = ""
            try:
                from ..providers.base import ProviderEventType
                async for event in provider.chat(
                    messages=sub_messages,
                    system=sub_system,
                    tools=sub_tool_defs,
                    temperature=0.2,
                    max_tokens=800,
                    stream=True,
                    tool_choice=None,
                ):
                    if event.type == ProviderEventType.TEXT_DELTA:
                        text_acc += event.text
                    elif event.type == ProviderEventType.TOOL_USE:
                        # 도구 호출 capture — ProviderEvent: tool_use_id / tool_name / tool_input
                        tool_uses.append({
                            "id": event.tool_use_id or f"sub_tu_{turn_idx}",
                            "name": event.tool_name,
                            "input": event.tool_input or {},
                        })
                    elif event.type == ProviderEventType.ERROR:
                        last_error = getattr(event, "text", "") or "sub-agent error"
                        break
            except Exception as e:
                last_error = str(e)
                break
            if last_error:
                break

            # assistant turn 박음 (text + tool_use 모두)
            assistant_content: list[dict] = []
            if text_acc.strip():
                assistant_content.append({"type": "text", "text": text_acc})
            for tu in tool_uses:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tu["id"] or f"sub_tu_{turn_idx}_{len(assistant_content)}",
                    "name": tu["name"],
                    "input": tu["input"],
                })
            if not assistant_content:
                # 빈 응답 — 모델이 멈춤. fallback synthesis 추출.
                last_error = "sub-agent emitted no tool_use and no text"
                break
            sub_messages.append({"role": "assistant", "content": assistant_content})

            # done 호출 검사
            done_call = next((tu for tu in tool_uses if tu["name"] == "done"), None)
            if done_call:
                done_output = done_call["input"] if isinstance(done_call["input"], dict) else {}
                break

            # 도구 호출 처리 → tool_result 추가
            tool_results: list[dict] = []
            for tu in tool_uses:
                inp = tu["input"] if isinstance(tu["input"], dict) else {}
                tu_id = tu["id"] or f"sub_tu_{turn_idx}"
                tu_name = tu["name"]
                if tu_name == "read_chunk":
                    try:
                        s = max(0, int(inp.get("start", 0)))
                        e = min(body_chars, int(inp.get("end", s)))
                        if e <= s:
                            payload = f"[read_chunk error: end ({e}) <= start ({s})]"
                        elif e - s > 1500:
                            payload = (
                                f"[read_chunk error: range too large "
                                f"({e - s} chars > 1500). Narrow start/end.]"
                            )
                        else:
                            chunk = body_ref[s:e]
                            payload = f"[chunk start={s} end={e} chars={len(chunk)}]\n{chunk}"
                    except Exception as ce:
                        payload = f"[read_chunk error: {ce}]"
                elif tu_name == "search_in_body":
                    kw = (inp.get("keyword") or "").strip()
                    if not kw:
                        payload = "[search_in_body error: keyword required]"
                    else:
                        positions = _search_positions(body_ref, kw, limit=10)
                        if not positions:
                            payload = f"[search_in_body keyword={kw!r}: 0 matches]"
                        else:
                            payload = (
                                f"[search_in_body keyword={kw!r}: {len(positions)} matches at "
                                f"positions {positions}]"
                            )
                else:
                    # 알 수 없는 도구 호출 — sub-agent 가 schema 따랐을 텐데 …
                    payload = (
                        f"[unknown tool: {tu_name!r}. Available: read_chunk, "
                        f"search_in_body, done]"
                    )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": payload,
                })
            sub_messages.append({"role": "user", "content": tool_results})

        # ── 결과 처리 ──
        try:
            from ..events.types import StageSubstepEvent
            await self._state.emit_verbose(StageSubstepEvent(
                stage_id="s07_act",
                substep="sub_agent_call_complete",
                meta={
                    "tool": "fetch_synthesize",
                    "kind": kind, "id": str(rid),
                    "mode": "react_subagent",
                    "turns_used": (turn_idx + 1) if not last_error else (turn_idx + 1),
                    "done_called": done_output is not None,
                    "error": last_error,
                },
            ))
        except Exception:
            pass

        if last_error and done_output is None:
            return ToolResult.error(
                f"sub-agent failed: {last_error}. Body={body_chars} chars isolated. "
                f"Synthesize from search snippets instead."
            )
        if done_output is None:
            return ToolResult.success(
                f"sub-agent exhausted max_turns={max_turns} without calling done. "
                f"Try a more focused query or re-call later."
            )

        synthesis = (done_output.get("synthesis") or "").strip()
        citations = done_output.get("citations") or []
        missing = done_output.get("missing_topics") or []
        if not synthesis:
            return ToolResult.success(
                f"sub-agent returned empty synthesis for kind={kind!r} id={rid!r} "
                f"query={query!r}. Try a different chunk or different query."
            )
        # main agent context — synthesis + citations (verbatim 발췌) + missing_topics.
        # citations 는 body 일부지만 사용자가 보고 검증하도록 main 에 박음.
        parts: list[str] = [
            f"[synthesized from {kind}:{rid}, body={body_chars} chars isolated]",
            "",
            synthesis,
        ]
        if citations:
            cap_cits = citations[:5] if isinstance(citations, list) else []
            if cap_cits:
                parts.append("")
                parts.append("Citations (verbatim from body):")
                for c in cap_cits:
                    s = str(c).strip()
                    if len(s) > 400:
                        s = s[:400] + "…"
                    parts.append(f"- {s}")
        if missing:
            cap_miss = missing[:5] if isinstance(missing, list) else []
            if cap_miss:
                parts.append("")
                parts.append("Missing topics (body areas not covered):")
                for m in cap_miss:
                    parts.append(f"- {str(m).strip()}")
                parts.append(
                    "  (re-call fetch_synthesize with a different query if you need these)"
                )
        return ToolResult.success("\n".join(parts))


class CompactTool(Tool):
    """대화 이력·도구 결과를 LLM 이 스스로 축약해 컨텍스트 비용을 줄임.

    자동 threshold(s07_act 의 50KB per-tool-result 컷) 는 **개별 결과** 만 자름.
    여러 턴·여러 도구 결과가 누적해 부풀면 자동으로 못 잡는다. CompactTool 은
    LLM 이 "지금 무엇을 버려도 되는지" 판단하고 직접 호출해 버림 (Anthropic·
    Cursor 류 long-running agent 표준 패턴).

    scope:
      - `tool_results_before:N` — N번째 이전 turn 들의 도구 결과 메시지 요약
      - `history_before:N`      — N번째 이전 대화 턴 요약
      - `pd_store:<kind>`       — pd_stores 의 특정 kind 원본을 요약으로 대체

    summary_hint:
      LLM 이 "어떤 정보를 남길지" 힌트 (예: "주문번호와 금액만", "에러 스택만").
      비워두면 일반 요약.

    summarizer 주입 방식:
      s04_tool 이 Tool 인스턴스 생성 시 state 와 summarizer(콜러블) 를 바인딩.
      summarizer 는 `(texts: list[str], hint: str) -> str` 시그니처.
      기본 summarizer 는 state.provider 로 짧은 LLM 호출 수행 — provider 없으면
      length-based truncate 로 폴백 (의미론 손실 있지만 동작 유지).

    파괴적 호출 — `destructive_hint=True`. HITLGuard 가 트리거 대상으로 잡으면
    사용자 승인 후 실행. 프로덕션에선 일반적으로 `trigger_destructive=True`
    기본이라 compact 도 한 번 확인을 거치게 됨. dev 환경은 auto-approve 권장.
    """

    def __init__(self, state_ref, summarizer=None):
        self._state = state_ref
        self._summarizer = summarizer   # async callable or None

    @property
    def name(self) -> str:
        return "compact"

    @property
    def description(self) -> str:
        # v1.8.0 — RESTRICTIONS_ONLY 톤.
        return (
            "Compact older context to free budget. scope: tool_results_before:N | "
            "history_before:N | pd_store:<kind>. Destructive."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": (
                        "Target to compact. One of: "
                        "'tool_results_before:<N>' | 'history_before:<N>' | 'pd_store:<kind>'"
                    ),
                },
                "summary_hint": {
                    "type": "string",
                    "description": "Optional. What to preserve in the summary "
                                   "(e.g. 'order numbers and amounts only').",
                },
            },
            "required": ["scope"],
        }

    @property
    def category(self) -> str:
        return "system"

    @property
    def read_only_hint(self) -> bool:
        return False   # 메시지 치환 — state 변경

    @property
    def destructive_hint(self) -> bool:
        return True    # 원본 문자열 손실 (요약 치환)

    @property
    def idempotent_hint(self) -> bool:
        return False   # 두 번 부르면 요약의 요약

    @property
    def open_world_hint(self) -> bool:
        return False   # 프로세스 내부 state 만

    async def execute(self, input_data: dict) -> ToolResult:
        scope = (input_data.get("scope") or "").strip()
        hint = (input_data.get("summary_hint") or "").strip()
        if not scope:
            return ToolResult.error("'scope' is required.")

        kind, _, arg = scope.partition(":")
        kind = kind.strip()
        arg = arg.strip()

        if kind == "tool_results_before":
            return await self._compact_tool_results_before(arg, hint)
        if kind == "history_before":
            return await self._compact_history_before(arg, hint)
        if kind == "pd_store":
            return await self._compact_pd_store(arg, hint)
        return ToolResult.error(
            f"Unknown scope kind {kind!r}. Supported: "
            "tool_results_before:<N> | history_before:<N> | pd_store:<kind>"
        )

    async def _compact_tool_results_before(self, arg: str, hint: str) -> ToolResult:
        try:
            n = int(arg)
        except Exception:
            return ToolResult.error(f"tool_results_before:<N> expects integer, got {arg!r}")

        # messages 에서 user 역할의 tool_result content block 을 가진 메시지를 역순 스캔
        targets_idx: list[int] = []
        messages = self._state.messages or []
        for i, m in enumerate(messages):
            content = m.get("content") if isinstance(m, dict) else None
            if not isinstance(content, list):
                continue
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                targets_idx.append(i)

        if len(targets_idx) <= n:
            return ToolResult.success(
                f"only {len(targets_idx)} tool_result messages present; nothing before index {n}."
            )

        victims = targets_idx[:-n] if n > 0 else targets_idx
        texts: list[str] = []
        for i in victims:
            content = messages[i]["content"]
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, str):
                        texts.append(c)
                    elif isinstance(c, list):
                        for sub in c:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                texts.append(sub.get("text", ""))

        summary = await self._summarize(texts, hint or "preserve key facts for later reference")
        replacement = f"[compacted {len(victims)} tool_result messages — hint: {hint or '-'}]\n\n{summary}"

        # 첫 victim 위치에 요약 메시지 하나로 치환, 나머지 삭제.
        first = victims[0]
        new_msg = {"role": "user", "content": replacement}
        new_messages = []
        replaced = False
        victim_set = set(victims)
        for i, m in enumerate(messages):
            if i in victim_set:
                if not replaced:
                    new_messages.append(new_msg)
                    replaced = True
                continue
            new_messages.append(m)
        self._state.messages = new_messages

        return ToolResult.success(
            f"compacted {len(victims)} tool_result messages → 1 summary "
            f"(~{len(replacement):,} chars)",
            victims=len(victims),
            chars=len(replacement),
        )

    async def _compact_history_before(self, arg: str, hint: str) -> ToolResult:
        try:
            n = int(arg)
        except Exception:
            return ToolResult.error(f"history_before:<N> expects integer, got {arg!r}")

        # 전체 messages 를 두 구간으로 분리: 보존(마지막 N 턴) + 희생(그 이전)
        msgs = list(self._state.messages or [])
        if len(msgs) <= n:
            return ToolResult.success(
                f"only {len(msgs)} messages present; nothing before index {n}."
            )
        victims = msgs[:-n] if n > 0 else msgs
        keep = msgs[-n:] if n > 0 else []

        texts: list[str] = []
        for m in victims:
            content = m.get("content") if isinstance(m, dict) else m
            if isinstance(content, str):
                texts.append(f"[{m.get('role','?')}] {content}")
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        t = b.get("text") or b.get("content") or ""
                        if isinstance(t, str) and t:
                            texts.append(f"[{m.get('role','?')}] {t}")

        summary = await self._summarize(texts, hint or "preserve intent, decisions, and key facts")
        summary_msg = {"role": "user", "content": f"[compacted history — {hint or '-'}]\n\n{summary}"}
        self._state.messages = [summary_msg] + keep

        return ToolResult.success(
            f"compacted {len(victims)} messages → 1 summary "
            f"(kept last {len(keep)})",
            victims=len(victims),
            kept=len(keep),
        )

    async def _compact_pd_store(self, kind: str, hint: str) -> ToolResult:
        bucket = self._state.pd_stores.get(kind) if hasattr(self._state, "pd_stores") else None
        if not bucket:
            return ToolResult.error(f"pd_store kind {kind!r} empty or missing.")

        ids = list(bucket.keys())
        texts = [bucket[rid].get("full", "") for rid in ids]
        summary = await self._summarize(texts, hint or f"summarize {kind} entries")

        # 각 entry 의 full 을 요약 한 줄로 대체 (preview 는 유지).
        for rid in ids:
            bucket[rid]["full"] = f"[compacted — hint: {hint or '-'}]\n{summary}"
            bucket[rid].setdefault("meta", {})["compacted"] = True

        return ToolResult.success(
            f"compacted pd_store[{kind}] {len(ids)} entries",
            entries=len(ids),
        )

    async def _summarize(self, texts: list[str], hint: str) -> str:
        """summarizer 주입 우선, 없으면 길이 기반 폴백."""
        joined = "\n\n---\n\n".join(t for t in texts if t)
        if not joined:
            return "(empty)"

        if self._summarizer is not None:
            try:
                result = self._summarizer(joined, hint)
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, str) and result.strip():
                    return result.strip()
            except Exception as e:
                import logging as _logging
                _logging.getLogger("harness.tools.compact").warning(
                    "summarizer failed, falling back to truncate: %s", e,
                )

        # 폴백: 앞부분 N자만 유지. 의미론 손실 있지만 동작 유지.
        limit = 2000
        if len(joined) <= limit:
            return joined
        return joined[:limit] + f"\n... [truncated from {len(joined):,} chars — no summarizer available]"


# ────────────────────────────────────────────────────────────────────────────
# v1.6 신규 빌트인 — Pack 3 (사용자 PD 정신: policy / prompt / collection 도구화)
# ────────────────────────────────────────────────────────────────────────────


class CheckPolicyTool(Tool):
    """v1.6 — Policy self-check 빌트인.

    LLM 이 민감 도구 호출 전 정책 (가드 / 예산 / 콘텐츠) 통과 여부 사전 검증.
    s05_policy guards 와 동일 검증 로직 재사용 — registry 의 Guard 인스턴스에
    같은 인터페이스로 위임.

    호출 예:
      check_policy(action="mcp_database_loader", args={"connection": "prod_postgres"})
        → {"allowed": true, "reasons": []}  또는  {"allowed": false, "reasons": ["..."]}
    """

    def __init__(self, state_ref):
        self._state = state_ref

    @property
    def name(self) -> str:
        return "check_policy"

    @property
    def description(self) -> str:
        # v1.8.0 — RESTRICTIONS_ONLY 톤.
        return (
            "Pre-check policies before sensitive tool call (DB write, external API, "
            "destructive). If blocked → tell user, don't retry."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "호출하려는 도구 이름"},
                "args": {"type": "object", "description": "호출 인자 (정책 검증용)"},
            },
            "required": ["action"],
        }

    @property
    def category(self) -> str:
        return "policy"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False

    async def execute(self, input_data: dict) -> ToolResult:
        action = input_data.get("action", "")
        args = input_data.get("args") or {}

        # state.config 의 stage_params.s05_policy.guards 받아 PRE_TOOL hook 시뮬레이션.
        # Guard 의 진짜 인터페이스 = check(state, context) → GuardResult.
        # PRE_TOOL hook 지원하는 Guard 만 평가 (TokenBudget / Iteration LOOP_BOUNDARY 만 = skip).
        import logging as _logging
        _log = _logging.getLogger("harness.tools.check_policy")

        config = getattr(self._state, "config", None)
        if not config or not hasattr(config, "stage_params"):
            return ToolResult.success(
                "no policy configured", allowed=True, reasons=[],
            )
        guards_cfg = (config.stage_params or {}).get("s05_policy", {}).get("guards") or []
        guard_names = [g.get("name") for g in guards_cfg if isinstance(g, dict) and g.get("name")]
        if not guard_names:
            return ToolResult.success(
                "no guards active", allowed=True, reasons=[],
            )

        try:
            from ..stages.strategies.guard import (
                _GUARD_REGISTRY, GuardChain, HookPoint,
            )
        except Exception as e:
            return ToolResult.success(
                f"guard registry unavailable: {e}",
                allowed=True, reasons=[],
            )

        # Guard 인스턴스 빌드 — params 박음 + configure() 호출
        instances = []
        for g in guards_cfg:
            if not isinstance(g, dict):
                continue
            gname = g.get("name")
            if not gname:
                continue
            cls = _GUARD_REGISTRY.get(gname)
            if not cls:
                continue
            try:
                params = g.get("params") or {}
                # 빌트인 Guard 들은 keyword args 또는 빈 ctor — 양쪽 시도
                try:
                    inst = cls(**params) if isinstance(params, dict) and params else cls()
                except TypeError:
                    inst = cls()
                # Strategy.configure() 표준 hook
                if hasattr(inst, "configure"):
                    try:
                        inst.configure(params if isinstance(params, dict) else {})
                    except Exception:
                        pass
                instances.append(inst)
            except Exception as e:
                _log.warning("guard %s instantiate failed: %s", gname, e)
                continue

        if not instances:
            return ToolResult.success(
                "no instantiable guards", allowed=True, reasons=[],
                active_guards=guard_names,
            )

        # PRE_TOOL hook + pending_tool_call 빌드 → GuardChain.invoke (모든 결과 수집)
        chain = GuardChain(instances)
        pending_tool_call = {"name": action, "input": args}
        try:
            results = chain.invoke(
                HookPoint.PRE_TOOL, self._state,
                pending_tool_call=pending_tool_call,
                short_circuit=False,  # 모든 reason 수집
            )
        except Exception as e:
            return ToolResult.success(
                f"guard chain invoke failed: {e}",
                allowed=True, reasons=[],
                active_guards=guard_names,
            )

        reasons: list[str] = []
        for r in results:
            if not r.passed and r.severity == "block":
                reasons.append(f"{r.guard_name}: {r.reason or 'blocked'}")

        return ToolResult.success(
            f"policy check: {len(results)} guard(s) evaluated at PRE_TOOL hook",
            allowed=not reasons,
            reasons=reasons,
            active_guards=guard_names,
            evaluated=len(results),
        )


class DiscoverPromptTool(Tool):
    """v1.6 — Prompt template lazy load 빌트인.

    s03_prompt 의 DEFAULT_IDENTITIES / DEFAULT_RULES / THINKING_MODE_TEMPLATES 외
    register_*() / entry_points 로 등록된 외부 prompt template 의 본문 + 메타를
    LLM 이 lazy load 가능. progressive_3level 의 prompt isomorphic.

    호출 예:
      discover_prompt(template_type="identity", name="legal_advisor")
        → {"name": ..., "template": "..."}
    """

    @property
    def name(self) -> str:
        return "discover_prompt"

    @property
    def description(self) -> str:
        # v1.8.0 — RESTRICTIONS_ONLY 톤.
        return (
            "Browse/fetch prompt templates (identity/rules/thinking_mode). "
            "Reference material — not callable."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "template_type": {
                    "type": "string",
                    "enum": ["identity", "rules", "thinking_mode"],
                    "description": "어떤 종류의 template",
                },
                "name": {"type": "string", "description": "template 이름. 비워두면 list."},
            },
            "required": ["template_type"],
        }

    @property
    def category(self) -> str:
        return "prompt"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False

    async def execute(self, input_data: dict) -> ToolResult:
        ttype = (input_data.get("template_type") or "").strip()
        name = (input_data.get("name") or "").strip()

        try:
            from ..stages.s03_prompt.stage import (
                DEFAULT_IDENTITIES, DEFAULT_RULES, THINKING_MODE_TEMPLATES,
                _discover_prompt_templates_from_entry_points,
            )
            _discover_prompt_templates_from_entry_points()
        except Exception as e:
            return ToolResult.error(f"prompt registry import failed: {e}")

        registry_map = {
            "identity": DEFAULT_IDENTITIES,
            "rules": DEFAULT_RULES,
            "thinking_mode": THINKING_MODE_TEMPLATES,
        }
        reg = registry_map.get(ttype)
        if reg is None:
            return ToolResult.error(
                f"unknown template_type: {ttype}. "
                f"valid: {list(registry_map.keys())}"
            )

        if not name:
            # v1.7.5 — list 모드 정보량 보강. 이름만 X → name + description (본문 첫 줄,
            # 120자) + length. system_prompt 의 <available_prompt_templates> 섹션과
            # isomorphic — LLM 이 "어떤 게 적합한지" 자율 판단할 정보 박음.
            entries = []
            for k, v in reg.items():
                first_line = ""
                if isinstance(v, str):
                    s = v.strip()
                    if s:
                        first_line = s.split("\n", 1)[0][:120]
                entries.append({
                    "name": k,
                    "description": first_line,
                    "length": len(v) if isinstance(v, str) else 0,
                })
            return ToolResult.success(
                f"{len(reg)} {ttype} templates — discover_prompt(template_type='{ttype}', "
                f"name=...) 로 본문 fetch",
                templates=entries,
            )

        if name not in reg:
            return ToolResult.error(
                f"template not found: {ttype}/{name}. "
                f"available: {list(reg.keys())}"
            )

        return ToolResult.success(
            f"loaded {ttype}/{name}",
            name=name,
            template_type=ttype,
            content=reg[name],
        )


class DiscoverCollectionTool(Tool):
    """v1.6 — RAG 컬렉션 sample documents / metadata lazy fetch 빌트인.

    progressive_4level 의 컬렉션 isomorphic — 도구의 progressive_3level 정신을
    자원 (컬렉션) 에 그대로:
    - L1: <reference_resources> 의 메타 (name + description + total_documents)
    - L2: discover_collection(name) → sample documents + 통계 (이 도구!)
    - L3: rag_search(collection, query) → 인덱스 + snippet
    - L4: fetch_pd(kind='rag', id=...) → 본문

    호출 예:
      discover_collection(name="assort", sample_size=3)
        → {"name": ..., "description": ..., "total": ..., "samples": [...]}
    """

    def __init__(self, state_ref):
        self._state = state_ref

    @property
    def name(self) -> str:
        return "discover_collection"

    @property
    def description(self) -> str:
        # v1.8.0 — RESTRICTIONS_ONLY 톤 + STOP 명령형 (v1.7.5 정합).
        return (
            "Sample RAG collection. Don't call same name twice. "
            "If empty: rag_search directly, or another collection, or STOP."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "컬렉션 이름"},
                "sample_size": {
                    "type": "integer",
                    "description": "sample 개수 (default 3, max 10)",
                    "default": 3,
                },
            },
            "required": ["name"],
        }

    @property
    def category(self) -> str:
        return "retrieval"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False

    async def execute(self, input_data: dict) -> ToolResult:
        name = (input_data.get("name") or "").strip()
        if not name:
            return ToolResult.error("name 필수")
        sample_size = max(1, min(int(input_data.get("sample_size") or 3), 10))

        services = self._state.metadata.get("services") if hasattr(self._state, "metadata") else None
        doc_service = getattr(services, "documents", None) if services else None
        if not doc_service:
            return ToolResult.error("DocumentService 미주입")

        # 메타 cache 우선
        meta = (self._state.metadata.get("rag_collections_meta") or {}).get(name) if hasattr(self._state, "metadata") else None
        result_meta = dict(meta) if meta else {"name": name}
        result_meta["name"] = name

        # sample 검색 — 빈 query 또는 generic word 로 top_k=sample_size
        try:
            samples = await doc_service.search(
                "", name, limit=sample_size, score_threshold=0.0,
            ) or []
        except Exception as e:
            # v1.8.0 — RESTRICTIONS_ONLY 톤.
            return ToolResult.success(
                f"sample fetch failed for '{name}'. Don't retry. "
                f"Use rag_search(query=..., collection_name='{name}') directly, "
                f"or try another collection, or STOP.",
                meta=result_meta,
                samples_error=str(e)[:120],
            )

        sample_summaries = []
        for r in samples[:sample_size]:
            if not isinstance(r, dict):
                continue
            text = r.get("chunk_text") or r.get("text") or ""
            sample_summaries.append({
                "source": r.get("file_name") or r.get("source", ""),
                "preview": text[:200],
            })

        # v1.8.0 — RESTRICTIONS_ONLY 톤.
        if not sample_summaries:
            return ToolResult.success(
                f"empty for '{name}'. Don't retry discover_collection. "
                f"Use rag_search(query=..., collection_name='{name}'), "
                f"or query_graph for relationships, or try another collection, or STOP.",
                meta=result_meta,
                samples=[],
            )
        return ToolResult.success(
            f"collection {name}: {len(sample_summaries)} samples. "
            f"Next: rag_search if relevant.",
            meta=result_meta,
            samples=sample_summaries,
        )


# ────────────────────────────────────────────────────────────────────────────
# v1.8.0 — Skill (Claude Code Skills 패턴 — body lazy + session 고정)
# ────────────────────────────────────────────────────────────────────────────


class SkillTool(Tool):
    """Claude Code Skills 패턴 — 메타 도구의 사용 가이드 markdown body lazy load.

    description (frontmatter 격) 은 짧게 유지하고, 자세한 사용법 / 예시 / 흔한 실수 /
    NEXT path 는 markdown body 로 분리. LLM 이 `Skill(name="rag_search")` 호출 시
    body 가져옴 + state.loaded_skills 에 박힘. s03_prompt 의 <loaded_skills> 섹션이
    매 turn 그 body 들 포함 (session 고정).

    Claude Code 의 SKILL.md frontmatter+body 분리 정합. 한 번 load 한 skill 은
    재호출 X — 이미 system_prompt 에 박혀있음.
    """

    def __init__(self, state_ref):
        self._state = state_ref

    @property
    def name(self) -> str:
        return "Skill"

    @property
    def description(self) -> str:
        # v1.8.0 — RESTRICTIONS_ONLY 톤.
        return (
            "Load detailed guide for a meta tool. Don't load same skill twice. "
            "Body stays in context for session."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Exact skill name. Omit to list all available skill names."
                    ),
                },
            },
        }

    @property
    def category(self) -> str:
        return "system"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False

    async def execute(self, input_data: dict) -> ToolResult:
        nm = (input_data.get("name") or "").strip()
        if not nm:
            names = list_skill_names()
            return ToolResult.success(
                f"{len(names)} skills available — call Skill(name='X') to load body:\n"
                + "\n".join(f"- {n}" for n in names)
            )
        body = get_skill_body(nm)
        if body is None:
            available = list_skill_names()
            return ToolResult.error(
                f"Unknown skill: {nm!r}. Available: {', '.join(available)}"
            )
        # session 고정 — state.loaded_skills 에 박음. s03_prompt 가 매 turn read.
        try:
            self._state.tool.loaded_skills[nm] = body
        except Exception as e:
            logger.debug("[Skill] loaded_skills update skip: %s", e)
        return ToolResult.success(
            f"Loaded skill '{nm}' ({len(body)} chars). "
            f"Body now in <loaded_skills> from this turn forward — act on it.\n\n"
            f"{body}"
        )


# ────────────────────────────────────────────────────────────────────────────
# v1.19.1 — Recall Workspace 빌트인 (작업기억 보존소)
#   policy(LLM) 는 keep / check / recall 로 *의미 결정* 만 emit.
#   엔진은 우선순위 랭킹·dedup·cap·확인기록·step-out 렌더를 관리 (memory/recall).
#   PD 정합: 전체 본문은 pd_stores["recall"] → fetch_pd step-in, render → step-out.
#   opt-in: s04 builtin_tools 에 명시될 때만 등록 (긴 작업 하네스만).
# ────────────────────────────────────────────────────────────────────────────

# pd_stores kind + state.metadata 키 — 한 곳에서만 정의 (오타 회귀 방지).
RECALL_PD_KIND = "recall"
_RECALL_STATE_KEY = "recall_set"


def _get_recall_set(state_ref):
    """state.metadata 의 RecallSet 을 조회/생성. cap 은 config.recall_cap 존중.

    memory/recall 은 state 를 모르는 순수 모듈이라 state 바인딩은 여기(툴 층)가 한다.
    """
    from ..memory.recall import RecallSet, DEFAULT_RECALL_CAP

    meta = getattr(state_ref, "metadata", None)
    if meta is None:
        return RecallSet()
    rs = meta.get(_RECALL_STATE_KEY)
    if not isinstance(rs, RecallSet):
        cap = DEFAULT_RECALL_CAP
        cfg = getattr(state_ref, "config", None)
        if cfg is not None:
            try:
                cap = int(getattr(cfg, "recall_cap", DEFAULT_RECALL_CAP) or DEFAULT_RECALL_CAP)
            except (TypeError, ValueError):
                cap = DEFAULT_RECALL_CAP
        rs = RecallSet(cap=cap)
        meta[_RECALL_STATE_KEY] = rs
    return rs


def _sync_recall_pd(state_ref, item) -> None:
    """항목 전체 본문을 pd_stores["recall"] 로 — fetch_pd("recall", id) step-in 지원.

    eviction 으로 set 에서 빠진 항목의 pd 잔재는 무해(조회는 set 기준 id 로만 안내)."""
    if not hasattr(state_ref, "pd_store"):
        return
    preview = (item.content or "")[:200]
    state_ref.pd_store(
        RECALL_PD_KIND, item.id,
        preview=preview, full=item.content or "",
        meta={
            "source": item.source,
            "priority": item.priority.value,
            "checked": item.checked,
            "score": item.score,
        },
    )


class KeepTool(Tool):
    """중요한 정보를 작업기억에 보존 — 긴 작업의 핵심 기억.

    검색/도구로 찾은 것 중 "남길 가치 있는 것" 을 LLM 이 직접 골라 priority 와 함께
    저장한다. 같은 id/내용은 dedup 갱신, cap 초과 시 최저 우선순위부터 자동 정리.
    저장된 항목은 세션 압축에도 살아남고 recall 로 step-out, fetch_pd 로 step-in.
    """

    def __init__(self, state_ref):
        self._state = state_ref

    @property
    def name(self) -> str:
        return "keep"

    @property
    def description(self) -> str:
        return (
            "Save a key piece of information to your persistent working memory "
            "(survives context compaction). Pass a stable id, the content, and "
            "priority (critical|high|normal|low). Re-keeping the same id/content updates it."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Stable id (e.g. doc/source/chunk id)."},
                "content": {"type": "string", "description": "The text to retain."},
                "source": {"type": "string", "description": "Where it came from (file/url/collection)."},
                "priority": {
                    "type": "string",
                    "enum": ["critical", "high", "normal", "low"],
                    "description": "How important this is. Default normal.",
                },
                "score": {"type": "number", "description": "Optional retrieval/confidence score."},
            },
            "required": ["id", "content"],
        }

    @property
    def category(self) -> str:
        return "recall"

    @property
    def read_only_hint(self) -> bool:
        return False  # state(working memory) 변경

    @property
    def idempotent_hint(self) -> bool:
        return True   # 같은 id/내용 재호출 = 갱신 (안전)

    @property
    def open_world_hint(self) -> bool:
        return False  # 프로세스 내부 state 만

    async def execute(self, input_data: dict) -> ToolResult:
        rid = (input_data.get("id") or "").strip()
        content = input_data.get("content") or ""
        if not rid or not content.strip():
            return ToolResult.error("'id' and 'content' are required.")
        rs = _get_recall_set(self._state)
        turn = int(getattr(self._state, "loop_iteration", 0) or 0)
        item = rs.keep(
            id=rid, content=content,
            source=(input_data.get("source") or "").strip(),
            priority=input_data.get("priority", "normal"),
            score=float(input_data.get("score") or 0.0),
            turn=turn,
        )
        _sync_recall_pd(self._state, item)
        return ToolResult.success(
            f"Kept '{item.id}' ({item.priority.value}). "
            f"Working memory now has {len(rs.items)} item(s) (cap={rs.cap}). "
            f"List with recall; read full body with fetch_pd(kind='recall', id='{item.id}').",
            recall_count=len(rs.items),
        )


class CheckTool(Tool):
    """보존 항목의 확인 결과를 기록 — 재확인 방지.

    LLM 이 한 항목을 (예: 다른 출처와 대조해) 확인/반증했을 때 그 결과를 작업기억에
    남긴다. 같은 항목을 두 번 확인하지 않도록 결과가 보존된다(엔진이 기록 관리)."""

    def __init__(self, state_ref):
        self._state = state_ref

    @property
    def name(self) -> str:
        return "check"

    @property
    def description(self) -> str:
        return (
            "Record a check result on a kept item (ok=true/false + optional note). "
            "Prevents re-checking the same item. Keep it first."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Id of a kept item."},
                "ok": {"type": "boolean", "description": "True if it holds up, false if refuted."},
                "note": {"type": "string", "description": "Why it passed/failed (optional)."},
            },
            "required": ["id", "ok"],
        }

    @property
    def category(self) -> str:
        return "recall"

    @property
    def read_only_hint(self) -> bool:
        return False

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False

    async def execute(self, input_data: dict) -> ToolResult:
        rid = (input_data.get("id") or "").strip()
        if not rid or "ok" not in input_data:
            return ToolResult.error("'id' and 'ok' are required.")
        rs = _get_recall_set(self._state)
        item = rs.check(rid, bool(input_data.get("ok")), (input_data.get("note") or "").strip())
        if item is None:
            return ToolResult.error(
                f"No kept item with id={rid!r}. Keep it first, then check."
            )
        _sync_recall_pd(self._state, item)
        return ToolResult.success(
            f"Recorded check('{rid}') = {item.checked}"
            + (f": {item.note}" if item.note else "")
        )


class RecallTool(Tool):
    """작업기억을 우선순위 순 compact 뷰로 — step-out.

    전체 본문이 아니라 한 줄 요약만. 특정 항목 본문이 필요하면 fetch_pd(kind='recall',
    id=...) 로 step-in. 누적 transcript 를 다시 읽지 않고 "지금까지 모은 것" 을 회수."""

    def __init__(self, state_ref):
        self._state = state_ref

    @property
    def name(self) -> str:
        return "recall"

    @property
    def description(self) -> str:
        return (
            "List your kept items (priority-ranked, compact). "
            "Read a full body with fetch_pd(kind='recall', id=...)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "max_items": {"type": "integer", "description": "Cap rows returned (0 = all)."},
            },
        }

    @property
    def category(self) -> str:
        return "recall"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True

    @property
    def open_world_hint(self) -> bool:
        return False

    async def execute(self, input_data: dict) -> ToolResult:
        rs = _get_recall_set(self._state)
        try:
            max_items = int(input_data.get("max_items") or 0)
        except (TypeError, ValueError):
            max_items = 0
        return ToolResult.success(rs.render(max_items=max_items))


# ────────────────────────────────────────────────────────────────────────────
# v1.6 — collection description enricher registry (default OFF)
# ────────────────────────────────────────────────────────────────────────────

_COLLECTION_ENRICHERS: list = []


def register_collection_enricher(fn):
    """컬렉션 description 빈 칸일 때 자동 생성하는 enricher 등록.

    fn 시그니처: async def enrich(name: str, sample_docs: list[str]) -> str | None
    여러 enricher 등록 가능 — 첫 비빈 결과 반환.

    entry_points group: ``xgen_harness.collection_enrichers``
    default OFF — 사용자가 명시 ON 시만 발동 (config.enrich_empty_descriptions=True
    또는 register 호출).
    """
    if fn not in _COLLECTION_ENRICHERS:
        _COLLECTION_ENRICHERS.append(fn)


async def enrich_collection_description(name: str, sample_docs: list[str]) -> str | None:
    """등록된 enricher 들 순서대로 호출. 첫 비빈 description 반환."""
    for fn in _COLLECTION_ENRICHERS:
        try:
            result = fn(name, sample_docs)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, str) and result.strip():
                return result.strip()
        except Exception:
            continue
    return None
