"""
S06 Context — scope 선언 + 컨텍스트 윈도우 관리 (v1.9.0 Option C 라디칼)

역할 (v1.9.0):
1. scope 선언 — rag_collections / ontology_collections / folders / files / db_connections
   를 metadata 에 노출. 실제 search 는 LLM 이 s07 에서 rag_search / query_graph /
   갤러리 search tool 등을 자율 호출 (단일 경로).
2. folders 자동 확장 — folder_id 에 속한 collection 들을 rag_collections 에 합류
3. DB 스키마 요약 주입 — DocumentService.database.get_schema_summary 위임 (경량, search X)
4. Intent routing — user_input → metadata_filter 자동 결정 (rag_search 의 default filter
   로 합류, BC)
5. 토큰 예산 관리 + cascade (L3/L4/L5) compaction

v1.9.0 BREAKING (이전 동작 vs):
- ❌ doc_service.search 자동 호출 제거 (rag_tool_mode='context'/'both' 모두 폐기)
- ❌ doc_service.ontology_query 자동 호출 제거 (ontology_tool_mode='context'/'both' 폐기)
- ❌ system_prompt 에 RAG/GraphRAG 결과 통째 주입 제거
- ✅ 옛 stage_params (top_k / score_threshold / metadata_filter / files / reranker /
   rerank_top_k / rag_pd_mode / rag_pd_snippet_size / rag_ingestion_mode) 는 s04 가
   RAGSearchTool / QueryGraphTool 생성 시 default args 로 자동 이주 (UI 사용자 의도
   보존). 첫 응답 1 turn 지연 + max_iter 압박 감수.

근거: feedback_recursive_autonomy — 재귀적 자율주행. s06 이 강제 fetch 박으면 LLM
자율 판단 빼앗김. 모든 자원이 "도구로 등가" — RAG / Ontology / Gallery / MCP isomorphic.
사용자 호소 (2026-05-12): "고결하게 로직짜야할거같아서 이경우엔 뭐 저경우엔 뭐하는
식으로 짜면안됨". 즉 단일 경로.
"""

import logging

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState
from ...core.service_registry import get_service_url
from ...core.runtime_defaults import resolve_with_default
from .cascade import CascadeCompactionMixin
from .intent import IntentRoutingMixin

logger = logging.getLogger("harness.stage.context")

CHARS_PER_TOKEN = 3  # 평균 추정 (영어 ≈ 4, 한국어 ≈ 1.5~2). stage_param chars_per_token 으로 override.


def _pct_threshold(value, runtime_default_key: str) -> float:
    """percent stage_param → 0.0~1.0 비율 변환.

    사용자가 stage_param 에 0~100 percentage 로 박으면 ratio 로 변환.
    None 이면 runtime_defaults 의 floor 사용 (외부 register_runtime_default
    로 override 가능). floor 도 없으면 0 → 해당 strategy 비활성.
    """
    pct = resolve_with_default(value, runtime_default_key, 0)
    try:
        return float(pct) / 100.0
    except (TypeError, ValueError):
        return 0.0


class ContextStage(CascadeCompactionMixin, IntentRoutingMixin, Stage):
    """RAG / DB / 컨텍스트 압축 — 책임은 Mixin 두 개에 분산.

    - `CascadeCompactionMixin`: L3 microcompact / L4 collapse / L5 autocompact + cascade dispatcher
    - `IntentRoutingMixin`: stage_param `intent_rules` → `auto_metadata_filter` 자동 결정
    - 본 클래스: 위 책임을 호출하는 dispatcher + RAG/DB fetch + token budget compaction
    """


    @property
    def stage_id(self) -> str:
        return "s06_context"

    @property
    def order(self) -> int:
        return 6

    def should_bypass(self, state: PipelineState) -> bool:
        # v1.9.0 — 첫 loop = scope/intent/DB 스키마 + budget. 이후 loop = budget 만.
        # 옛 "첫 루프만 RAG search" 의미는 폐기 (Option C — search 자체 안 함).
        return state.loop_iteration > 1

    async def execute(self, state: PipelineState) -> dict:
        config = state.config
        results = {
            "rag_collections": 0, "db_results": 0, "compacted": False,
            "ontology_collections": 0, "folders_expanded": 0,
            "intent_routed": False,
        }

        # ── 0. Intent Routing (v1.0 흡수 from 구 s05_strategy) ──
        # 사용자 입력 → metadata_filter 자동 결정 (auto_metadata_filter).
        # stage_params.metadata_filter 가 비어있을 때만 fallback 으로 사용.
        await self._apply_intent_routing(state)
        if state.metadata.get("auto_metadata_filter"):
            results["intent_routed"] = True

        # ── 1. RAG 컬렉션 + folders 확장 ──
        rag_collections: list[str] = list(self.get_param("rag_collections", state, []) or [])
        # v1.1.0 — harness_mode 제거에 따른 폴백 단순화.
        # stage_params 의 rag_collections 가 비어있으면 항상 config.rag_collections 폴백.
        # 사용자가 UI 에서 명시 비워 저장하면 stage_params 가 빈 list 로 들어와도
        # config.rag_collections 자체가 비어있을 것 (양쪽이 같은 store 필드를 공유).
        if not rag_collections and hasattr(config, 'rag_collections'):
            rag_collections = list(getattr(config, 'rag_collections', []) or [])

        # folders → 해당 폴더 안의 컬렉션 자동 추가 (DocumentService.list_collections 위임)
        folders: list[str] = list(self.get_param("folders", state, []) or [])
        if folders:
            services = state.metadata.get("services")
            doc_service = getattr(services, "documents", None) if services else None
            if doc_service and hasattr(doc_service, "list_collections"):
                try:
                    all_cols = await doc_service.list_collections() or []
                    expanded = [
                        c.get("collection_name") for c in all_cols
                        if isinstance(c, dict)
                        and (c.get("folder_id") in folders or str(c.get("folder_id")) in folders)
                        and c.get("collection_name")
                    ]
                    for col in expanded:
                        if col not in rag_collections:
                            rag_collections.append(col)
                    results["folders_expanded"] = len(expanded)
                except Exception as e:
                    logger.warning("[Context] folder expansion failed: %s", e)

        # ── 1.5 v1.9.0 Option C — scope 노출만, 자동 search 폐기 ──
        # 옛 동작 (v1.8.x 이하): rag_tool_mode='context'/'both' 면 s06 가 doc_service.search
        # 직접 호출 → system_prompt 박음. ontology 도 동일.
        # 신 동작 (v1.9.0): s06 은 자동 search 안 함. rag_collections / ontology_collections
        # 는 metadata 에만 노출, s04 가 RAGSearchTool / QueryGraphTool 등록 → s07 에서 LLM
        # 이 자율 호출. 9 stage_params (top_k / score_threshold / metadata_filter / files /
        # reranker / rerank_top_k / rag_pd_mode / rag_pd_snippet_size / rag_ingestion_mode)
        # 는 s04 가 도구 default args 로 자동 이주 — 사용자 의도 보존.
        #
        # 사용자 호소 (2026-05-12): "이경우엔 뭐 저경우엔 뭐 짜면안됨" → 단일 경로.
        # 옛 stage_params 박힌 워크플로우 = s04 가 read 해서 도구 default args 로 박음.
        # s03 의 <active_resources> 명령형 지시 → LLM 이 자율 도구 호출 (BC 완화).
        if rag_collections:
            state.metadata["rag_collections"] = list(rag_collections)
        ontology_collections: list[str] = list(self.get_param("ontology_collections", state, []) or [])
        if ontology_collections:
            state.metadata["ontology_collections"] = list(ontology_collections)
        results["rag_collections"] = len(rag_collections)
        results["rag_collection_names"] = list(rag_collections)
        results["ontology_collections"] = len(ontology_collections)
        results["ontology_collection_names"] = list(ontology_collections)
        # files 도 metadata 에 노출 — s04 가 RAGSearchTool.default_filter.file_name 로 합류.
        _files_for_metadata = list(self.get_param("files", state, []) or [])
        if _files_for_metadata:
            state.metadata["files"] = _files_for_metadata
        if rag_collections or ontology_collections:
            logger.info(
                "[Context] scope: rag=%d cols, ontology=%d cols (search 는 s07 도구 호출 시점)",
                len(rag_collections), len(ontology_collections),
            )

        # ── 2. DB 연결 — services.database.get_schema_summary 로 위임 ──
        # 라이브러리는 connection_name 같은 추상 식별자만 다루고,
        # 실제 SQL/엔진별 해석은 ServiceProvider.database 구현체 책임.
        db_connections: list[str] = self.get_param("db_connections", state, [])
        if db_connections:
            services = state.metadata.get("services")
            if services and getattr(services, "database", None):
                summaries: list[str] = []
                for conn in db_connections:
                    try:
                        summary = await services.database.get_schema_summary(conn)
                        if summary:
                            summaries.append(summary)
                    except Exception as e:
                        logger.warning("[Context] DB schema 조회 실패 (%s): %s", conn, e)
                if summaries:
                    state.system_prompt += "\n\n<db_context>\n" + "\n".join(summaries) + "\n</db_context>"
                    results["db_results"] = len(summaries)
                    logger.info("[Context] DB context injected: %d connections", len(summaries))
                else:
                    results["db_results"] = 0
                    logger.info("[Context] db_connections: %d selected, schema 조회 결과 없음", len(db_connections))
            else:
                results["db_results"] = 0
                logger.info(
                    "[Context] db_connections: %d selected, but ServiceProvider.database 가 주입되지 않음",
                    len(db_connections),
                )

        # ── 3. 토큰 예산 관리 ──
        # v1.0.x — getattr default 는 attribute 부재일 때만 발동. config 에 None 으로
        # 박혀있으면 None 흘러감 → 산술 TypeError. resolve_with_default 로 sentinel 폴백
        # 명시. context_window / max_tokens / thinking_budget_tokens 는 runtime_defaults
        # 레지스트리에 등록되어 있어 외부 패키지가 register_runtime_default(...) 로 override.
        context_window = resolve_with_default(
            self.get_param("context_window", state, None) or getattr(config, "context_window", None),
            "context_window",
        )
        max_tokens = resolve_with_default(
            getattr(config, "max_tokens", None) if config else None,
            "max_tokens",
        )
        available_tokens = int(context_window) - int(max_tokens)

        if config and getattr(config, "thinking_enabled", False):
            thinking_budget = resolve_with_default(
                getattr(config, "thinking_budget_tokens", None),
                "thinking_budget_tokens",
            )
            available_tokens -= int(thinking_budget)

        total_chars = len(state.system_prompt)
        for msg in state.messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total_chars += len(str(block.get("text", "")))
                        total_chars += len(str(block.get("content", "")))

        # chars_per_token override 가능 (영어/한국어 토큰 비율이 달라서).
        chars_per_token = int(self.get_param("chars_per_token", state, CHARS_PER_TOKEN))
        if chars_per_token < 1:
            chars_per_token = CHARS_PER_TOKEN
        estimated_tokens = total_chars // chars_per_token
        budget_used = estimated_tokens / available_tokens if available_tokens > 0 else 1.0

        # 압축 전략 디스패치
        #   token_budget (기본):  first + last_3 유지. 파괴적 (원본 소실).
        #   sliding_window:        최근 N개 유지. 파괴적.
        #   microcompact (L3):     오래된 tool_result 만 placeholder 교체. 비파괴 (pd_stores 복원).
        #   context_collapse_overlay (L4): 중간 메시지 overlay 교체. 비파괴.
        #   autocompact_llm (L5):  child LLM 9-section 요약으로 교체. 비파괴.
        #   cascade (v0.11.15+):   임계별 L3 → L4 → L5 순 자동 전환. 현재 turn 에 1개만 발동.
        strategy_name = self.get_param("strategy", state, "token_budget")
        compaction_threshold = _pct_threshold(
            self.get_param("compaction_threshold", state, None),
            "compaction_threshold_pct",
        )

        # v0.11.21 — 외부 기여자가 register_strategy("s06_context","compactor",...) 로
        # 교체할 수 있도록 resolver 경로를 선행 조회. AdvancedContextCompactor 인스턴스가
        # 반환되면 자체 apply() 에 전적으로 위임하고 inline if/elif 는 건너뛴다.
        handled_by_strategy = False
        try:
            from ...core.strategy_resolver import StrategyResolver
            from ..strategies.compactor import AdvancedContextCompactor
            resolver = StrategyResolver.default()
            compactor = resolver.resolve("s06_context", "compactor", strategy_name)
            if isinstance(compactor, AdvancedContextCompactor):
                await compactor.apply(state=state, stage=self, budget_used=budget_used, results=results)
                handled_by_strategy = True
        except Exception as e:
            logger.warning("[Context] strategy resolver dispatch 실패 (%s): %s", strategy_name, e)

        if handled_by_strategy:
            pass  # AdvancedContextCompactor 가 results 에 이미 반영
        elif strategy_name == "sliding_window":
            # 슬라이딩 윈도우: 최근 N개 메시지만 유지 (심플하지만 채팅에 효과적)
            window_size = int(self.get_param("window_size", state, None) or 0)
            if len(state.messages) > window_size:
                state.messages = state.messages[-window_size:]
                results["compacted"] = True
                logger.info("[Context] SlidingWindow: kept last %d messages", window_size)
        elif strategy_name == "microcompact":
            self.try_microcompact(state, budget_used, results)
        elif strategy_name == "autocompact_llm":
            await self.try_autocompact(state, budget_used, results)
        elif strategy_name == "context_collapse_overlay":
            self.try_context_collapse(state, budget_used, results)
        elif strategy_name == "cascade":
            await self.try_cascade(state, budget_used, results)
        elif budget_used > compaction_threshold and len(state.messages) > 4:
            # token_budget(기본, 파괴적): first + last 3
            state.messages = [state.messages[0]] + state.messages[-3:]
            results["compacted"] = True
            logger.info("[Context] Compacted: kept first + last 3 messages")

        results["estimated_tokens"] = estimated_tokens
        results["budget_used"] = round(budget_used, 2)

        logger.info("[Context] tokens=%d, budget=%.0f%%, rag=%d cols",
                    estimated_tokens, budget_used * 100, len(rag_collections))
        return results

    # v1.12.2 — try_cascade / try_microcompact / try_context_collapse / try_autocompact /
    # _autocompact_summarize / _apply_intent_routing 메서드는 본 클래스에서 제거. 동일 본문이
    # CascadeCompactionMixin (cascade.py) + IntentRoutingMixin (intent.py) 에 박혀있어 MRO 로
    # 자동 위임. 단일 진실 소스 = Mixin.

    def list_strategies(self) -> list[StrategyInfo]:
        # v1.4.0 — 사용자 픽 카드 1개 (cascade) 만 노출. 압력별 L3→L4→L5 자동 에스컬레이션이
        # 가장 안전한 default. 다른 5 strategy (token_budget/sliding_window/microcompact/
        # context_collapse_overlay/autocompact_llm) 의 dispatcher 코드는 보존되어 외부 plugin
        # 또는 active_strategies 직접 셋으로 강제 가능. 사용자 UI 표면만 단순화.
        return [
            StrategyInfo("cascade", "압력별 L3→L4→L5 자동 압축", is_default=True),
        ]
