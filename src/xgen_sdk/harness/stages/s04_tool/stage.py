"""
S04 Tool Index — Progressive 도구 디스커버리 (v0.25.0 재설계)

## 책임
- 등록된 모든 ``ToolSource`` 를 순회하여 도구 정의를 수집한다.
- 사용자가 UI 에서 고른 필터 / 도구 화이트리스트를 적용한다.
- Strategy (progressive_3level / eager_load / none) 으로 LLM 프롬프트용 색인을 만든다.
- RAG 컬렉션 / capability / builtin 도구를 별도 경로로 병합한다.

## 변경점 (v0.24 → v0.25)
- ``mcp_sessions`` / ``custom_tools`` / ``node_tags`` / ``cli_skills`` stage_param **제거**.
- 단일 stage_param ``selected_tools: dict[str, list[str]]`` (source_id → 허용 도구 이름).
  - 키 없음 = 해당 소스의 모든 도구 포함.
  - 빈 리스트 = 해당 소스 비활성.
  - 이름 리스트 = 그 이름만 포함.
- 단일 stage_param ``tool_source_filters: dict[str, dict]`` (source_id → list_tools filter).
  - 각 ToolSource 는 자기 ``filter_schema`` 를 선언하고, 프론트가 sub-UI 로 렌더한다.
  - 예: MCP 세션 소스는 ``session_ids`` 필터, xgen 노드 소스는 ``tags`` 필터.
- 단일 stage_param ``tool_description_overrides: dict[str, str]`` (tool_name → 설명).
  - 사용자가 UI 에서 도구 설명을 직접 고치면 LLM 이 그 텍스트로 도구를 고른다.
  - 빈 값/미설정 = 소스 원본 description. eager/deferred/schema 캐시 일괄 적용.
- MCP/Custom/xgenNode 특수 분기 전부 이식측 ToolSource 구현으로 이관 (엔진 무지식).

## Progressive Disclosure (3 레벨)
- L1: ``state.tool_index`` (이름/설명만) → 시스템 프롬프트 삽입
- L2: ``discover_tools`` 빌트인 도구로 상세 스키마 조회
- L3: 실제 실행 (s07_act)
"""

from __future__ import annotations

import logging
from typing import Optional

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState
from ...events.types import StageSubstepEvent
from ...tools import get_tool_sources
from ...tools.rag_tool import RAGSearchTool
from ...tools.ontology_tool import QueryGraphTool

logger = logging.getLogger("harness.stage.tool_index")

# ─── Capability Discovery 기본값 (박제 풀기, v1.0 흡수 from 구 s05_strategy) ───
# 자연어 intent 로 capability 자동 발견 시 사용. stage_params 또는
# register_capability_discovery_defaults() 로 override 가능.
CAPABILITY_DISCOVERY_DEFAULTS: dict[str, float | int] = {
    "top_k": 3,
    "min_score": 0.4,
}


def register_capability_discovery_defaults(*, top_k: int | None = None,
                                            min_score: float | None = None) -> None:
    """capability discovery 기본 임계값 override. 외부 작업자가 자기 도메인에 맞춰 조정."""
    if top_k is not None:
        CAPABILITY_DISCOVERY_DEFAULTS["top_k"] = int(top_k)
    if min_score is not None:
        CAPABILITY_DISCOVERY_DEFAULTS["min_score"] = float(min_score)


class ToolIndexStage(Stage):
    """도구 색인 + progressive disclosure 설정 (v0.25.0)."""

    when_to_use = "도구/RAG/capability/MCP 세션 중 하나라도 쓰려는 요청"
    when_to_skip = "순수 텍스트 답변만 — 외부 호출 0"
    cost_hint = "low"

    @property
    def stage_id(self) -> str:
        return "s04_tool"

    @property
    def order(self) -> int:
        # v1.2.0 — s03_prompt 보다 먼저 실행되어 도구 카탈로그를 먼저 채운다.
        # 그래야 s03 의 <available_tools> 섹션이 진짜로 렌더되고, eager/deferred 분리
        # 메타가 system_prompt 에 반영된다 (Claude Code 스타일 deferred tools).
        return 3

    async def execute(self, state: PipelineState) -> dict:
        # v0.26.0 — strategy="none" 분기 (D6 fix).
        # 사용자가 도구 인덱싱을 명시적으로 비활성. should_bypass 의 자동 감지와 달리
        # 도구·RAG·capability 가 *있어도* 강제 skip. 디버깅·도구 무관 단발 답변에 유용.
        strategy_name = (self.get_param("strategy", state, None) or "").strip().lower()
        if strategy_name == "none":
            logger.info("[Tool Index] strategy=none, discovery skipped")
            return {"strategy": "none", "tools_indexed": 0, "definitions_bound": 0}

        # ─── stage_params ──────────────────────────────────────────────
        # selected_tools 입력 형태 두 가지 모두 허용:
        #   1) dict {source_id: [tool_name, ...]}  — source 별 화이트리스트
        #   2) list [tool_name, ...]               — 글로벌 화이트리스트 (모든 source)
        # 사유: 사용자가 "이 도구 1개만 쓰고 싶다" 할 때 source_id 를 알 필요 없게.
        # 이전 버전은 list 받으면 .get(sid) 호출에서 AttributeError → silently fallback
        # → 모든 도구 노출 → 비용 폭증 (BUG-B 실측: selected=[tavily] 인데 brave 도 호출).
        _sel_raw = self.get_param("selected_tools", state, {})
        selected_tools_by_source: dict[str, list[str]] = {}
        global_allow: Optional[set[str]] = None
        if isinstance(_sel_raw, dict):
            selected_tools_by_source = _sel_raw
        elif isinstance(_sel_raw, list):
            global_allow = {str(n) for n in _sel_raw if n}
        # 그 외 타입은 무시 (전부 허용으로 폴백).

        tool_source_filters: dict[str, dict] = (
            self.get_param("tool_source_filters", state, {}) or {}
        )
        # 도구별 description 오버라이드 — 사용자가 s04 UI 에서 도구 설명을 직접 고침.
        # { tool_name: "설명" }. LLM 이 도구를 고를 때 읽는 텍스트를 사람이 손본다.
        # 빈 값/미설정이면 소스가 준 원본 description 을 그대로 쓴다. eager / deferred /
        # schema 캐시 전부 아래 td 빌드 한 곳에서 파생되므로 여기 한 번만 먹이면 일괄 적용.
        tool_description_overrides: dict[str, str] = (
            self.get_param("tool_description_overrides", state, {}) or {}
        )
        if not isinstance(tool_description_overrides, dict):
            tool_description_overrides = {}
        rag_collections: list[str] = self.get_param("rag_collections", state, []) or []
        # v1.18.6 — 미설정/0 이면 0 으로 두지 말 것. limit=0 검색은 Qdrant 가 빈 결과를
        # 돌려줘 RAG 가 조용히 사문된다(standalone 컴파일 wheel 은 연결노드 top_k 가 없어
        # 항상 여기로 떨어졌다). runtime floor("rag_top_k"=4) 로 폴백.
        from ...core.runtime_defaults import resolve_with_default
        _rtk_raw = self.get_param("rag_top_k", state, None)
        try:
            _rtk = int(_rtk_raw) if _rtk_raw is not None else 0
        except (TypeError, ValueError):
            _rtk = 0
        rag_top_k: int = _rtk if _rtk > 0 else int(resolve_with_default(None, "rag_top_k", 4))

        # ─── 0.5 Capability 바인딩 ────────────────────────────────────
        # (a) config.capabilities 명시 선언 → materialize
        cap_result = self._bind_capabilities(state)
        if cap_result.get("_events"):
            from ...events.types import CapabilityBindEvent
            for ev in cap_result["_events"]:
                await state.emit_verbose(CapabilityBindEvent(
                    name=ev["name"], source=ev["source"], stage_id=self.stage_id,
                ))

        # (b) v1.0 — 자연어 intent → capability 자동 발견 (구 s05_strategy 흡수).
        # active_strategy=="capability_auto" 이거나 stage_params.capability_discovery=True 일 때.
        if self._is_capability_discovery_enabled(state):
            disc_result = await self._discover_and_bind_capabilities(state)
            cap_result["discovery"] = disc_result

        # ─── 1. ToolSource 수집 — eager/deferred 분리 (v1.2.0 Claude Code 정합) ─
        # 사용자가 명시한 selected_tools 화이트리스트 안의 도구만 eager (full schema 가
        # tools= 인자에 박힘). 나머지는 deferred (이름+1줄 desc 만 system_prompt 노출 +
        # schema 는 state.tool_schemas 캐시). LLM 이 ToolSearch(names=[...]) 로 승격
        # 호출하면 다음 llm_call 의 tools= 에 자동 합류.
        # selected_tools 가 전혀 명시되지 않은 경우 = 모든 도구 eager (회귀 0).
        existing_names = {td.get("name") for td in state.tool_definitions}
        source_tool_map: dict[str, str] = state.metadata.setdefault("tool_source_of", {})
        collected = 0          # eager 합류 + deferred 합류 합계
        eager_added = 0
        deferred_added = 0
        sources_used: list[str] = []

        # 명시적 화이트리스트 존재 여부. 없으면 모든 도구 eager (백워드 컴팻).
        has_explicit_selection = bool(selected_tools_by_source) or (global_allow is not None)

        # 전역 등록 소스 + 이 실행에만 주입된 상태 범위 소스 (nested subpipeline 격리).
        sources = list(get_tool_sources()) + list(getattr(state, "extra_tool_sources", None) or [])
        await state.emit_verbose(StageSubstepEvent(
            stage_id=self.stage_id, substep="sources_discover_start",
            meta={"source_count": len(sources), "explicit_selection": has_explicit_selection},
        ))

        for src in sources:
            sid = getattr(src, "source_id", None) or type(src).__name__
            sub_allow = selected_tools_by_source.get(sid)
            # 빈 리스트 = 그 source 자체를 카탈로그에서 완전 제외 (eager 도 deferred 도 X)
            if sub_allow is not None and len(sub_allow) == 0:
                continue
            filter_params = tool_source_filters.get(sid)

            try:
                listed = await self._invoke_list_tools(src, filter_params)
            except Exception as e:
                logger.debug("[Tool Index] list_tools failed for %s: %s", sid, e)
                continue

            before = collected
            for t in listed or []:
                if not isinstance(t, dict):
                    continue
                nm = t.get("name")
                if not nm or nm in existing_names:
                    continue
                _override_desc = tool_description_overrides.get(nm)
                _has_override = isinstance(_override_desc, str) and _override_desc.strip() != ""
                td = {
                    "name": nm,
                    "description": (
                        _override_desc.strip() if _has_override
                        else (t.get("description", "") or "")
                    ),
                    "input_schema": t.get("input_schema") or {"type": "object"},
                }
                # annotations 는 payload 분리 (v0.24.4 — Anthropic 400 방지)
                ann = t.get("annotations")
                if ann:
                    state.tool.annotations[nm] = dict(ann)
                # tags 는 metadata 로 보존 (Planner catalog / UI 필터용)
                tags = t.get("tags")
                if tags:
                    td.setdefault("metadata", {})["tags"] = list(tags)

                # eager / deferred / skip 결정. v1.12.1 — strict 모드 추가.
                #
                # 모드별 동작 (selected_tools 명시 X 워크플로우 기준):
                #   - "strict" (v1.12.1 default): 사용자 명시 도구 + 자원 매칭 + 메타
                #     외 모든 도구 카탈로그 완전 제외 (deferred 도 X). search_tools
                #     검색 결과에서도 안 보임. 진정한 PD.
                #   - "deferred_default" (v1.8~v1.12.0): 사용자 박은 도구 = deferred.
                #     LLM 이 search_tools / ToolSearch 로 발견·승격 가능.
                #   - "eager_all" (옛 default): 모든 도구 eager.
                # selected_tools 명시 워크플로우는 영향 X — 명시한 것 eager / 나머지 deferred.
                #
                # 메타 도구 / 자원 매칭 도구 식별 — tags 기반:
                #   - "builtin" / "pd" / "system" tag → 메타 도구 (search_tools 등)
                #   - 자원 매칭 도구 (rag_search / query_graph) 는 위 ToolSource 합집합엔
                #     보통 안 들어옴 (s04 가 별도 등록). 여기서는 메타만 항상 통과.
                from ..strategies.discovery import _get_default_tool_strategy
                _default_mode = _get_default_tool_strategy()
                _is_meta_tool = bool(set(tags or []) & {"builtin", "pd", "system"})

                if has_explicit_selection:
                    # 명시 워크플로우 — 명시한 것만 eager / 나머지 deferred.
                    is_eager = False
                    if global_allow is not None and nm in global_allow:
                        is_eager = True
                    if sub_allow is not None and nm in sub_allow:
                        is_eager = True
                    skip_completely = False
                elif _default_mode == "eager_all":
                    is_eager = True
                    skip_completely = False
                elif _default_mode == "strict":
                    # 메타 도구만 eager, 그 외 카탈로그 완전 제외.
                    is_eager = _is_meta_tool
                    skip_completely = not _is_meta_tool
                else:  # "deferred_default"
                    is_eager = False
                    skip_completely = False

                if skip_completely:
                    # 카탈로그 자체에서 제외 — schema 캐시도 안 채움. search_tools /
                    # ToolSearch 가 발견 못 함. PD strict.
                    continue

                # schema 캐시는 항상 채움 — ToolSearch / discover_tools 가 조회.
                state.tool_schemas[nm] = td

                if is_eager:
                    state.tool_definitions.append(td)
                    eager_added += 1
                else:
                    short_desc = (td["description"] or "")[:120]
                    if len(td["description"] or "") > 120:
                        short_desc = short_desc.rsplit(" ", 1)[0] + "..."
                    state.deferred_tools.append({
                        "name": nm,
                        "description": short_desc,
                        "category": "deferred",
                    })
                    deferred_added += 1

                source_tool_map[nm] = sid
                existing_names.add(nm)
                collected += 1
            if collected > before:
                sources_used.append(sid)

        await state.emit_verbose(StageSubstepEvent(
            stage_id=self.stage_id, substep="sources_discover_complete",
            meta={
                "tools_collected": collected,
                "eager_count": eager_added,
                "deferred_count": deferred_added,
                "sources_used": sources_used,
            },
        ))

        # v1.2.0 / v1.5.3 — 분류 결과 ToolDeferredEvent 로 외부 보고.
        # 사용자 디버깅 needs 정합 — 도구 선택 결과는 selected_tools 비어있어도 항상 emit.
        # has_explicit_selection 조건 제거 — eager 든 deferred 든 도구 발견 했으면 EventLog 에.
        if eager_added or deferred_added:
            from ...events.types import ToolDeferredEvent
            _deferred_names = [
                t.get("name") for t in (state.deferred_tools or []) if t.get("name")
            ]
            await state.emit_verbose(ToolDeferredEvent(
                eager_count=eager_added,
                deferred_count=deferred_added,
                eager_names=[td.get("name") for td in state.tool_definitions if td.get("name")],
                deferred_names=_deferred_names,
            ))

        if collected:
            # v1.8.0 — backend log 도 도구 이름 list 박음 (사용자 디버깅 정합).
            # 카운트만으론 "어떤 도구 박혔는지" 추적 불가. 너무 길면 첫 N개 + 나머지 카운트.
            _eager_n = [td.get("name") for td in state.tool_definitions if td.get("name")]
            _deferred_n = [t.get("name") for t in (state.deferred_tools or []) if t.get("name")]
            _trim = lambda lst, k=10: (lst[:k] + [f"...+{len(lst)-k}"]) if len(lst) > k else lst
            logger.info(
                "[Tool Index] tool_sources: %d tool(s) from %d source(s) — "
                "eager=%d %s · deferred=%d %s",
                collected, len(sources_used),
                eager_added, _trim(_eager_n),
                deferred_added, _trim(_deferred_n),
            )

        # ─── 2. Strategy 디스패치 (progressive_3level / eager_load / none) ─
        selected_builtins: list[str] = self.get_param(
            "builtin_tools", state, ["discover_tools"],
        )
        strategy = self.resolve_strategy("discovery", state, "progressive_3level")
        if not strategy:
            from ..strategies.discovery import ProgressiveDiscovery
            strategy = ProgressiveDiscovery()
        tool_index, augmented_defs = await strategy.discover(
            state.tool_definitions, state,
        )

        # discover_tools 빌트인은 선택된 경우만 유지
        if "discover_tools" not in selected_builtins:
            augmented_defs = [td for td in augmented_defs if td.get("name") != "discover_tools"]
            tool_index = [ti for ti in tool_index if ti.get("name") != "discover_tools"]
            logger.info("[Tool Index] discover_tools excluded (not in builtin_tools)")

        # v1.1.1 — fetch_pd 는 strategy 무관 항상 등록.
        # v1.6 — check_policy / discover_prompt 도 strategy 무관 항상 등록 (사용자 정신:
        # 시스템 빌트인 = 1단계 메타 도구 = LLM 항상 손에). discover_collection 은
        # rag_collections 박혔을 때만 (조건부, Section 3 영역에서 등록).
        from ...tools.builtin import (
            FetchPDTool, CheckPolicyTool, DiscoverPromptTool, SkillTool,
            FetchSynthesizeTool,
        )
        _STRATEGY_AGNOSTIC = [
            ("fetch_pd", FetchPDTool, True),       # state_ref 필요
            ("check_policy", CheckPolicyTool, True),
            ("discover_prompt", DiscoverPromptTool, False),
            # v1.8.0 — Skill (Claude Code Skills 패턴 — body lazy + session 고정)
            ("Skill", SkillTool, True),
            # v1.8.0 — fetch_synthesize (sub-agent 패턴 — 본문 main context 격리)
            ("fetch_synthesize", FetchSynthesizeTool, True),
        ]
        for tname, cls, needs_state in _STRATEGY_AGNOSTIC:
            if any(td.get("name") == tname for td in augmented_defs):
                continue
            inst = cls(state) if needs_state else cls()
            augmented_defs.append(inst.to_api_format())
            tool_index.append({
                "name": inst.name,
                "description": (inst.description[:120] if hasattr(inst, "description") else ""),
                "category": getattr(inst, "category", "system"),
            })
            if hasattr(state, "metadata"):
                state.metadata.setdefault("tool_registry", {})[tname] = inst
            logger.info("[Tool Index] %s builtin registered (strategy-agnostic)", tname)

        # v1.19.1 — Recall Workspace 빌트인 (opt-in). builtin_tools 에 명시될 때만 등록.
        # 긴 작업 하네스가 "작업기억 보존(working memory)" 이 필요할 때 활성화.
        # 비검색 하네스는 selected_builtins 에 없으므로 영향 0 (도메인 agnostic 유지).
        from ...tools.builtin import KeepTool, CheckTool, RecallTool
        _RECALL_BUILTINS = [
            ("keep", KeepTool),
            ("check", CheckTool),
            ("recall", RecallTool),
        ]
        for tname, cls in _RECALL_BUILTINS:
            if tname not in selected_builtins:
                continue
            if any(td.get("name") == tname for td in augmented_defs):
                continue
            inst = cls(state)
            augmented_defs.append(inst.to_api_format())
            tool_index.append({
                "name": inst.name,
                "description": inst.description[:120],
                "category": getattr(inst, "category", "recall"),
            })
            if hasattr(state, "metadata"):
                state.metadata.setdefault("tool_registry", {})[tname] = inst
            logger.info("[Tool Index] %s recall builtin registered (opt-in)", tname)

        state.tool_definitions = augmented_defs
        state.tool_index = tool_index

        # ─── 3. RAG 설정 (ToolSource 가 아닌 별도 경로) ───────────────
        if rag_collections:
            state.metadata["rag_collections"] = rag_collections
            state.metadata["rag_top_k"] = rag_top_k
            logger.info("[Tool Index] RAG collections: %s (top_k=%d)",
                        rag_collections, rag_top_k)

            # v1.7 — ResourceProvider 패턴으로 메타 자동 인식.
            # 등록된 모든 ResourceProvider 호출 → state.metadata['{kind}_meta'] 자동 cache.
            # 빌트인 = RAG / Ontology (DocumentService 기반). 외부 wheel 이 DB / Files / MCP /
            # 다른 자원 종 추가 = register_resource_provider() 또는 entry_points 1줄 등록.
            # hardcoded fetch 코드 추가 X — 자원 종이 자기 메타 fetch 책임.
            try:
                from ...core.resource_providers import fetch_all_resource_meta
                fetched = await fetch_all_resource_meta(state)
                if fetched:
                    logger.info(
                        "[Tool Index] resource_providers fetched: %s",
                        ", ".join(f"{k}={len(v)}" for k, v in fetched.items()),
                    )
            except Exception as e:
                logger.warning("[Tool Index] fetch_all_resource_meta failed: %s", e)

            # v1.6 — discover_collection 빌트인 (rag_collections 박혔을 때만 의미). progressive_4level
            # 의 컬렉션 isomorphic — L2 sample documents lazy load.
            from ...tools.builtin import DiscoverCollectionTool
            if not any(td.get("name") == "discover_collection" for td in state.tool_definitions):
                _disc = DiscoverCollectionTool(state)
                state.tool_definitions.append(_disc.to_api_format())
                state.tool_index.append({
                    "name": _disc.name,
                    "description": (_disc.description[:120] if hasattr(_disc, "description") else ""),
                    "category": getattr(_disc, "category", "retrieval"),
                })
                state.metadata.setdefault("tool_registry", {})["discover_collection"] = _disc
                logger.info("[Tool Index] discover_collection builtin registered (rag_collections present)")

            # v1.9.0 — Option C 라디칼: rag_tool_mode 분기 폐기. rag_collections 박혀
            # 있으면 항상 rag_search 도구로 등록. s06 의 옛 9 stage_params 가 도구
            # default args 로 자동 이주 (사용자 의도 보존).
            _services = state.metadata.get("services")
            _doc_service = getattr(_services, "documents", None) if _services else None
            rag_pd_mode_for_tool = str(
                self.get_param("rag_pd_mode", state, "progressive") or "progressive"
            ).strip().lower()
            rag_pd_snippet_for_tool = int(
                self.get_param("rag_pd_snippet_size", state, 120) or 120
            )
            # v1.9.0 — s06 의 stage_params 를 cross-stage 로 read (BC).
            # s04 가 자기 stage 의 동명 param 우선 → 없으면 s06 의 동명 param fallback.
            _s06_params = (
                state.config.stage_params.get("s06_context", {})
                if state.config and getattr(state.config, "stage_params", None)
                else {}
            )

            def _from_s06(key, fallback=None):
                # s04 stage_params 우선 → s06 stage_params fallback → fallback
                v = self.get_param(key, state, None)
                if v is None:
                    v = _s06_params.get(key, fallback)
                return v

            _score_th = _from_s06("score_threshold", None)
            _raw_filter = _from_s06("metadata_filter", None)
            _files_default = _from_s06("files", None) or []
            _reranker = _from_s06("reranker", None)
            _rerank_topk = _from_s06("rerank_top_k", None)
            # metadata_filter 가 JSON 문자열로 들어올 수도 있어 파싱.
            if isinstance(_raw_filter, str) and _raw_filter.strip():
                try:
                    import json as _json
                    _raw_filter = _json.loads(_raw_filter)
                    if not isinstance(_raw_filter, dict):
                        _raw_filter = None
                except Exception:
                    _raw_filter = None
            if not isinstance(_raw_filter, dict):
                _raw_filter = None
            # bool 문자열 정규화 ("False"/"True" → False/True)
            if isinstance(_reranker, str):
                _reranker = _reranker.strip().lower() == "true"
            try:
                _rerank_topk_int = int(_rerank_topk) if _rerank_topk is not None else None
            except (TypeError, ValueError):
                _rerank_topk_int = None
            try:
                _score_th_float = float(_score_th) if _score_th is not None else None
            except (TypeError, ValueError):
                _score_th_float = None
            rag_tool = RAGSearchTool(
                collections=rag_collections,
                default_top_k=rag_top_k,
                doc_service=_doc_service,
                state_ref=state,
                progressive=(rag_pd_mode_for_tool == "progressive"),
                snippet_size=rag_pd_snippet_for_tool,
                default_score_threshold=_score_th_float,
                default_filter=_raw_filter,
                default_reranker=_reranker,
                default_rerank_top_k=_rerank_topk_int,
                default_file_names=list(_files_default) if _files_default else None,
            )
            _rag_already = any(td.get("name") == "rag_search" for td in state.tool_definitions) \
                or any(t.get("name") == "rag_search" for t in (state.deferred_tools or []))
            if not _rag_already:
                _rag_api = rag_tool.to_api_format()
                state.metadata.setdefault("tool_registry", {})["rag_search"] = rag_tool
                if rag_pd_mode_for_tool == "progressive":
                    # 4.8 Tool search 패턴(백과사전 + Claude MCP Tool Search / OpenAI tool search):
                    # 연결된 RAG 를 eager 로 박지 않고 deferred 카탈로그(안내데스크)에 둔다. LLM 이
                    # search_tools 로 "RAG 연결됨" 발견 → ToolSearch 승격 → rag_search 호출 — "연결
                    # 노드를 tool 로 인식해 검색·사용" 사고 흐름이 지도 타고 이어진다. eager 면 그냥
                    # 박혀 발견 단계가 사라짐(도구 폭발 4.6). 검색 자체는 rag_collections 가 s06/
                    # active_resources 로 노출돼 에이전트가 "무엇이 연결됐는지" 는 안다.
                    state.tool_schemas["rag_search"] = _rag_api
                    state.deferred_tools.append({
                        # category != "deferred" → s03 가 익명 대량 카탈로그(MCP)와 달리 이름으로
                        # 노출(사용자가 명시 연결한 자원이라 발견 단서가 보여야 약한 모델도 탄다).
                        "name": "rag_search",
                        "description": (_rag_api.get("description") or "")[:120],
                        "category": "retrieval",
                    })
                    logger.info(
                        "[Tool Index] rag_search DEFERRED (progressive PD — search_tools/ToolSearch 로 발견) "
                        "collections=%s score_th=%s", rag_collections, _score_th_float,
                    )
                else:
                    state.tool_definitions.append(_rag_api)
                    tool_index.append(rag_tool.to_index_entry())
                    logger.info(
                        "[Tool Index] rag_search registered (eager) "
                        "defaults: score_th=%s files=%d filter=%s rerank=%s rerank_top_k=%s",
                        _score_th_float, len(_files_default) if _files_default else 0,
                        "set" if _raw_filter else "none", _reranker, _rerank_topk_int,
                    )

        # ─── 3.5 Ontology / GraphRAG 도구 등록 (v1.9.0 Option C) ────────
        # 사용자가 박은 ontology_collections 를 query_graph 빌트인 도구로 노출.
        # v1.9.0 — ontology_tool_mode 분기 폐기. ontology_collections 박혀있으면
        # 항상 query_graph 도구로 등록 (RAG / Ontology / Gallery / MCP isomorphic 단일 경로).
        # s06 자동 호출은 v1.9.0 에서 폐기 (Option C).
        ontology_collections: list[str] = self.get_param("ontology_collections", state, []) or []
        if ontology_collections:
            # v1.5.4 — s03_prompt 가 cross-stage 로 인지하도록 metadata cache (rag 와 isomorphic).
            state.metadata["ontology_collections"] = ontology_collections
            _services_o = state.metadata.get("services")
            _doc_service_o = getattr(_services_o, "documents", None) if _services_o else None
            if _doc_service_o is not None and hasattr(_doc_service_o, "ontology_query"):
                graph_tool = QueryGraphTool(
                    collections=list(ontology_collections),
                    doc_service=_doc_service_o,
                    state_ref=state,
                    progressive=True,
                    snippet_size=int(self.get_param("rag_pd_snippet_size", state, 200) or 200),
                )
                if not any(td.get("name") == "query_graph" for td in state.tool_definitions):
                    state.tool_definitions.append(graph_tool.to_api_format())
                    tool_index.append(graph_tool.to_index_entry())
                    state.metadata.setdefault("tool_registry", {})["query_graph"] = graph_tool
                    logger.info(
                        "[Tool Index] query_graph registered (v1.9.0 Option C — single tool path) "
                        "collections=%s",
                        ontology_collections,
                    )
            else:
                logger.info(
                    "[Tool Index] ontology_collections present (%s) but DocumentService.ontology_query unavailable — query_graph not registered",
                    ontology_collections,
                )

        # ─── 4. force_tool_use (v0.11.19) ──────────────────────────────
        force_tool_use = bool(self.get_param("force_tool_use", state, False))
        if force_tool_use and state.tool_definitions:
            state.metadata["force_tool_choice"] = "required"
            logger.info("[Tool Index] force_tool_use=True → tool_choice=required")

        # ─── 4b. discovery-first (tool-search-first, 백과사전 4.8) ──────────
        # 연결된 자원이 deferred(안내데스크 뒤)에 있으면, 첫 턴 행동을 search_tools 로 강제한다.
        # 약한 모델이 카탈로그를 안 열고 바로 답/엉뚱한 도구로 가는 걸 막고 "tool search 를
        # 선으로 → 거기서 도구 발견" 흐름을 보장(하네스가 일을 해준다). 첫 턴만(circuit breaker
        # 는 llm_call 이 loop_iteration 으로 처리). opt-out: discovery_first 파라미터 False.
        _has_deferred = bool(getattr(state, "deferred_tools", None))
        _has_search = "search_tools" in (state.metadata.get("tool_registry") or {})
        _disc_first = bool(self.get_param("discovery_first", state, True))
        if _disc_first and _has_deferred and _has_search and not force_tool_use:
            state.metadata["discovery_first_tool"] = "search_tools"
            logger.info("[Tool Index] discovery_first → 첫 턴 search_tools 강제 (tool-search-first)")

        logger.info("[Tool Index] %d tools indexed, %d definitions bound",
                    len(tool_index), len(state.tool_definitions))
        return {
            "tools_count": len(tool_index),
            "tools_bound": len(state.tool_definitions),
            "sources_used": sources_used,
            "rag_collections": len(rag_collections),
            "capabilities_declared": cap_result.get("declared", 0),
            "capabilities_resolved": cap_result.get("resolved", 0),
            "capabilities_unknown": cap_result.get("unknown", 0),
            "force_tool_use": force_tool_use,
        }

    # ─── helpers ────────────────────────────────────────────────────────

    @staticmethod
    async def _invoke_list_tools(src, filter_params):
        """``ToolSource.list_tools`` 호출 — filters 인자 미지원 소스 호환."""
        if filter_params is None:
            try:
                return await src.list_tools()
            except TypeError:
                return await src.list_tools(None)
        try:
            return await src.list_tools(filter_params)
        except TypeError:
            # 구형 소스 — filters 무시
            return await src.list_tools()

    def _bind_capabilities(self, state: PipelineState) -> dict:
        """config.capabilities 를 Tool 인스턴스로 materialize 후 state 반영."""
        config = state.config
        if config is None or not getattr(config, "capabilities", None):
            return {"declared": 0, "resolved": 0, "unknown": 0}

        from ...capabilities import materialize_capabilities, merge_into_state

        declared_names = list(config.capabilities)
        report = materialize_capabilities(
            declared_names,
            capability_params=getattr(config, "capability_params", None),
        )
        added = merge_into_state(report, state)

        logger.info(
            "[Tool Index] capabilities: declared=%d, resolved=%d, added=%d, "
            "unknown=%d, no_factory=%d",
            len(declared_names), len(report.resolved), added,
            len(report.unknown), len(report.no_factory),
        )
        if report.unknown:
            logger.warning("[Tool Index] unknown capabilities: %s", report.unknown)
        if report.no_factory:
            logger.warning("[Tool Index] capabilities without tool_factory: %s",
                           report.no_factory)

        return {
            "declared": len(declared_names),
            "resolved": len(report.resolved),
            "unknown": len(report.unknown),
            "_events": [
                {"name": n, "source": "declaration"} for n in report.resolved
            ],
        }

    # ---------- Capability Auto-Discovery (v1.0 흡수 from 구 s05_strategy) ----------

    def _is_capability_discovery_enabled(self, state: PipelineState) -> bool:
        """active_strategy=="capability_auto" 또는 stage_params.capability_discovery=True 면 활성."""
        active = ""
        if state.config:
            picked = (state.config.active_strategies or {}).get(self.stage_id)
            if isinstance(picked, str):
                active = picked.strip()
        if active == "capability_auto":
            return True
        return bool(self.get_param("capability_discovery", state, False))

    async def _discover_and_bind_capabilities(self, state: PipelineState) -> dict:
        """자연어 intent(user_input) → capability 후보 매칭 + state 바인딩.

        - 이미 config.capabilities 에 선언된 것은 _bind_capabilities 가 처리 → 중복 회피
        - 매칭된 것 중 아직 안 된 것만 materialize
        - 임계값(top_k / min_score) 은 stage_params 또는 모듈 상수로 override
        """
        if state.config is None:
            return {"suggestions": 0, "bound": 0}

        intent = state.user_input or ""
        if not intent.strip():
            return {"suggestions": 0, "bound": 0}

        from ...capabilities import (
            CapabilityMatcher,
            MatchStrategy,
            get_default_registry,
            materialize_capabilities,
            merge_into_state,
        )

        already_bound = set(state.metadata.get("capability_bindings", {}).keys())
        already_declared = set(getattr(state.config, "capabilities", []) or [])
        skip = already_bound | already_declared

        top_k = int(self.get_param(
            "capability_top_k", state, CAPABILITY_DISCOVERY_DEFAULTS["top_k"]))
        min_score = float(self.get_param(
            "capability_min_score", state, CAPABILITY_DISCOVERY_DEFAULTS["min_score"]))

        registry = get_default_registry()
        matcher = CapabilityMatcher(registry, min_score=min_score)
        matches = matcher.match(intent, limit=top_k * 2, strategy=MatchStrategy.AUTO)

        suggested = [m for m in matches if m.spec.name not in skip][:top_k]
        if not suggested:
            logger.info("[Tool Index] capability discovery: no new matches (intent=%r)", intent[:80])
            return {"suggestions": 0, "bound": 0}

        names = [m.spec.name for m in suggested]
        state.metadata.setdefault("suggested_capabilities", []).extend(
            [{"name": m.spec.name, "score": m.score, "strategy": m.strategy} for m in suggested]
        )

        report = materialize_capabilities(
            names,
            registry=registry,
            capability_params=getattr(state.config, "capability_params", None),
        )
        added = merge_into_state(report, state)

        logger.info(
            "[Tool Index] capability discovery: suggestions=%s, bound=%d, unknown=%d, no_factory=%d",
            names, added, len(report.unknown), len(report.no_factory),
        )

        from ...events.types import CapabilityBindEvent
        for m in suggested:
            if m.spec.name in report.resolved:
                await state.emit_verbose(CapabilityBindEvent(
                    name=m.spec.name, source="discovery", stage_id=self.stage_id,
                ))

        return {"suggestions": len(names), "bound": added, "names": names}

    def should_bypass(self, state: PipelineState) -> bool:
        # 도구/RAG/capability/builtin 중 하나라도 있으면 실행.
        # ToolSource 가 등록됐으면 공급자 있음 → 실행.
        has_tools = bool(state.tool_definitions)
        has_rag = bool(self.get_param("rag_collections", state, []))
        has_sources = bool(get_tool_sources())
        has_caps = bool(state.config and getattr(state.config, "capabilities", None))
        has_builtins = bool(self.get_param("builtin_tools", state, []))
        return not (has_tools or has_rag or has_sources or has_caps or has_builtins)

    def list_strategies(self) -> list[StrategyInfo]:
        # v1.4.0 — 사용자 픽 카드 hide. progressive_3level + ToolSearch 가 default.
        # 코드 (progressive_3level/eager_load/capability_auto/none) 보존 — active_strategies 직접 셋 가능.
        return []
