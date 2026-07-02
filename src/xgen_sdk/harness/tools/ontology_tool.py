"""Ontology / GraphRAG query tool — v1.5.0 R3 정합.

s06_context 가 자동 사전 호출하던 ontology_query 를 LLM 손에 도구로 노출.
RAG (rag_search) 와 isomorphic — selected_tools 화이트리스트 기준 eager/deferred,
progressive PD 로 결과 본문은 pd_stores["graph"] 보관, LLM 에는 인덱스+snippet 만.

배경:
  ontology / GraphRAG 는 빌드 완료된 컬렉션 (Fuseki triple store + SCS + SQL) 만
  쿼리 가능. 사용자가 매 턴 박은 ontology_collections 에 대해 자동 호출하면
    - 이중 LLM 호출 (multi_turn_rag 가 child ReAct 로 멀티턴 LLM)
    - 매 턴 무조건 호출 (질문이 그래프 무관해도)
    - 결과 통째 system_prompt 박힘 (PD 0)
  세 함정. v1.5.0 R3 = LLM 이 query_graph 도구 호출 결정 (자율) → progressive PD.
"""

import logging
from typing import Any, Optional

from .base import Tool, ToolResult

logger = logging.getLogger("harness.tools.ontology")


class QueryGraphTool(Tool):
    """온톨로지 / GraphRAG 쿼리 — LLM 이 도구로 호출 (R3, v1.5.0).

    백엔드 multi_turn_rag.query() 위임 — ReAct 패턴 멀티턴 그래프 탐색
    (SPARQL + SCS + SQL). 결과는 progressive PD — 본문은 pd_stores["graph"] 보관,
    LLM 에는 인덱스 + 짧은 요약 (snippet) 만. LLM 은 fetch_pd(kind="graph",
    id="<collection>") 로 본문 lazy fetch.

    빌드 안 된 컬렉션 → 백엔드가 빈 결과 또는 unavailable 반환 → LLM 이 처리.
    """

    def __init__(
        self,
        collections: list[str],
        doc_service: Optional[Any] = None,
        state_ref: Optional[Any] = None,
        progressive: bool = True,
        snippet_size: int = 200,
    ):
        self._collections = collections
        self._doc_service = doc_service
        self._state = state_ref
        self._progressive = progressive
        self._snippet_size = snippet_size

    @property
    def name(self) -> str:
        return "query_graph"

    @property
    def description(self) -> str:
        cols = ", ".join(self._collections)
        # v1.8.0 — frontmatter. 자세한 사용법은 Skill('query_graph').
        return (
            f"GraphRAG / ontology search (multi-turn ReAct over SPARQL + chunks + SQL) "
            f"for relationship/hierarchy questions on collections [{cols}]. "
            f"Call Skill('query_graph') for detailed usage."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Natural-language question to explore the knowledge graph for.",
                },
                "collection": {
                    "type": "string",
                    "description": (
                        f"Graph collection to query. "
                        f"Defaults to '{self._collections[0]}' if omitted. "
                        f"Available: {', '.join(self._collections)}"
                    ),
                },
            },
            "required": ["question"],
        }

    @property
    def category(self) -> str:
        return "retrieval"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return False  # multi_turn ReAct — 같은 질문도 LLM 분기 다를 수 있음

    @property
    def open_world_hint(self) -> bool:
        return True  # xgen-documents Fuseki / Qdrant 외부 호출

    async def execute(self, input_data: dict) -> ToolResult:
        from ..errors import ToolError

        question = (input_data.get("question") or "").strip()
        if not question:
            return ToolResult.error("'question' is required.")

        collection = (input_data.get("collection") or "").strip()
        if not collection:
            collection = self._collections[0] if self._collections else ""
        if not collection:
            return ToolResult.error("No graph collections available.")
        if collection not in self._collections:
            return ToolResult.error(
                f"Collection '{collection}' not in available graph collections: "
                f"{', '.join(self._collections)}"
            )

        if self._doc_service is None or not hasattr(self._doc_service, "ontology_query"):
            raise ToolError(
                "DocumentService.ontology_query is not available. "
                "The host must inject a documents service into the ResourceRegistry.",
                tool_name="query_graph",
            )

        try:
            r = await self._doc_service.ontology_query(question, collection)
        except Exception as e:
            logger.warning("[query_graph] ontology_query failed (%s): %s", collection, e)
            return ToolResult.error(f"Graph query failed: {str(e)}")

        if not r:
            return ToolResult.success(
                f"No graph results for '{question}' in collection '{collection}'. "
                f"Collection may not have a built ontology (build status check needed)."
            )

        body = r if isinstance(r, str) else str(r)
        if self._progressive and self._state is not None and hasattr(self._state, "pd_store"):
            return self._format_progressive(body, collection, question)
        return ToolResult.success(body)

    def _format_progressive(self, body: str, collection: str, question: str) -> ToolResult:
        """본문을 pd_stores 에 보관, 인덱스+snippet 만 반환."""
        snippet = body[: self._snippet_size]
        if len(body) > self._snippet_size:
            snippet = snippet.rsplit(" ", 1)[0] + "…"
        snippet = snippet.replace("\n", " ")
        rid = f"{collection}::{abs(hash(question)) % 10**8:08d}"
        try:
            self._state.pd_store(
                kind="graph",
                resource_id=rid,
                preview=snippet,
                full=body,
                meta={
                    "collection": collection,
                    "question": question[:200],
                    "chars": len(body),
                },
            )
        except Exception as e:
            logger.debug("[query_graph] pd_store failed: %s", e)
            return ToolResult.success(body)  # PD 실패 시 eager fallback

        text = (
            f"[graph:{collection}] id={rid} · {len(body)} chars\n"
            f"{snippet}\n\n"
            f"(Full body via fetch_pd(kind='graph', id='{rid}'))"
        )
        return ToolResult.success(text, collection=collection, chars=len(body), id=rid)
