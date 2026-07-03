"""RAG Search Tool — 에이전트가 문서 검색을 직접 호출하는 도구.

pre-search(s06)와 달리, 에이전트가 필요할 때 직접 검색 질의를 작성하여 호출.

v0.11.25 — 엔진 독립성 원칙 준수:
  이 도구는 xgen-documents API 경로(`/api/retrieval/documents/search`) 를 알지 않는다.
  ServiceProvider.documents (DocumentService Protocol) 구현체를 생성자에서 주입받아
  그 `search()` 메서드만 호출한다. 외부 조직이 다른 RAG 스택을 쓸 때 DocumentService
  프로토콜을 만족하는 구현체만 넘기면 이 도구는 변경 없이 동작한다.
"""

import logging
from typing import Any, Optional

from .base import Tool, ToolResult

logger = logging.getLogger("harness.tools.rag")


# v1.7.4 — PD 패턴 (지도→인덱스→원문) 유지하되, "지도" 단계 정보량 풍부화.
# v1.7.3 (default OFF) 은 PD 자체를 폐기하는 잘못된 진단이라 v1.7.4 에서 즉시 환원.
# 사용자 의도: 가용한 도구/RAG/정책의 메타(지도) 노출 → LLM 이 인덱스 보고 원문 fetch →
#   원문 기반 답변 합성. 이 흐름이 **작동** 해야 한다.
# 분노 사례 (9 라운드 헛 fetch) 의 진짜 원인: 인덱스가 너무 빈약 (snippet 120자 + source
#   + score 만) → LLM 이 어떤 청크 fetch 할지 판단 못 함 → 같은 쿼리 반복 → max_iter 도달.
# v1.7.4 정책:
#   - default ON — 인덱스+풍부 메타 노출, 본문은 pd_stores 보관, fetch_pd 로 lazy 가져감.
#   - 인덱스에 chunk_index / total_chunks / length / source 박음 + snippet 250자
#     (이전 120 → 2x ↑). PD "지도" 의 본분 (=메타) 유지하면서 LLM 판단 정보량 보강.
#     500자 시도는 "지도" 라기엔 두꺼워 PD 와 비대칭. 250자는 claude skills 의
#     description (~150자) 보다 약간 두껍지만 청크 본문 미리보기 역할 정합.
#   - auto_threshold_chars 미사용 (= 0). 명시 인자만 override.
_PROGRESSIVE_POLICY: dict[str, Any] = {
    "enabled": True,                # default ON — PD 패턴 정상 작동
    "auto_threshold_chars": 0,      # 0 = 자동 임계 미사용. 명시 인자만 override
    # v1.8.0 — 250 → 600. 사용자 명시: "fetch_pd 청크 들이붓지 마라, search 결과로 합성".
    # snippet 길게 = 인덱스만으로 합성 가능 → fetch_pd 의존도 ↓.
    "snippet_size": 600,
}


def register_progressive_policy(
    *,
    enabled: Optional[bool] = None,
    auto_threshold_chars: Optional[int] = None,
    snippet_size: Optional[int] = None,
) -> None:
    """RAG 검색 결과 progressive PD 정책 외부 override.

    호스트 측에서 결과 데이터의 평균 크기 / LLM context window / 비용 목표에 맞춰 임계 조정.
    예: 호스트가 항상 작은 청크만 다루면 ``enabled=False, auto_threshold_chars=10_000_000`` —
    progressive 사실상 영구 OFF.
    """
    if enabled is not None:
        _PROGRESSIVE_POLICY["enabled"] = bool(enabled)
    if auto_threshold_chars is not None:
        _PROGRESSIVE_POLICY["auto_threshold_chars"] = int(auto_threshold_chars)
    if snippet_size is not None:
        _PROGRESSIVE_POLICY["snippet_size"] = int(snippet_size)


def get_progressive_policy() -> dict[str, Any]:
    return dict(_PROGRESSIVE_POLICY)


class RAGSearchTool(Tool):
    """에이전트가 직접 호출하는 RAG 문서 검색 도구.

    s06_context의 pre-search와 달리, 에이전트가 대화 중에 필요하다고 판단할 때
    직접 검색 질의를 작성하여 호출한다.

    Args:
        collections: 검색 가능한 컬렉션 이름 목록 — LLM 이 이 중에서 선택.
        default_top_k: collection 당 반환 결과 수 기본값.
        doc_service: `DocumentService` 프로토콜 구현체. 미주입 시 실행 단계에서
            `ToolError` 반환 — 엔진은 호스트가 안 붙여준 서비스를 상상으로 부르지 않는다.
    """

    def __init__(
        self,
        collections: list[str],
        default_top_k: int = 4,
        doc_service: Optional[Any] = None,
        state_ref: Optional[Any] = None,
        progressive: Optional[bool] = None,
        snippet_size: Optional[int] = None,
        # v1.9.0 — Option C 정합. s06 의 옛 9 stage_params 가 도구 default args 로
        # 이주. LLM 이 명시 인자 안 박으면 이 default 사용. UI 사용자 의도 (top_k=10,
        # reranker=True 등) 자동 적용. None = 미지정 → search() 호출 시 생략.
        default_score_threshold: Optional[float] = None,
        default_filter: Optional[dict] = None,
        default_reranker: Optional[bool] = None,
        default_rerank_top_k: Optional[int] = None,
        default_file_names: Optional[list[str]] = None,
    ):
        # v1.7.3 — progressive 가 미명시(None) 면 _PROGRESSIVE_POLICY 의 enabled +
        # auto_threshold_chars 가 결정. 명시 True/False 면 그것 우선.
        # snippet_size 도 동일 — 미명시 시 policy default.
        self._collections = collections
        self._default_top_k = default_top_k
        self._doc_service = doc_service
        self._state = state_ref
        self._progressive_explicit = progressive
        self._snippet_size_override = snippet_size
        # v1.9.0 — s06 옛 stage_params 이주분
        self._default_score_threshold = default_score_threshold
        # default_filter + default_file_names 합성 (files 가 file_name 키로 자동 union)
        merged_filter: dict = {}
        if isinstance(default_filter, dict):
            merged_filter.update(default_filter)
        if default_file_names:
            existing = merged_filter.get("file_name") or []
            if not isinstance(existing, list):
                existing = [existing]
            merged_filter["file_name"] = list({*existing, *default_file_names})
        self._default_filter = merged_filter or None
        self._default_reranker = default_reranker
        self._default_rerank_top_k = default_rerank_top_k
        # v1.28 — self-govern 이 실행 중 되쓰는 검색 폭 하한(floor). 0=하한 없음(기본).
        # 자가조정이 근거 부족으로 넓히면 이 값이 올라가고, execute 가 요청 top_k 를 이 하한
        # 아래로 못 내려가게 강제한다(연결 RAG 노드 "환경 지배").
        self._min_top_k = 0

    @property
    def name(self) -> str:
        return "rag_search"

    @property
    def description(self) -> str:
        collections_str = ", ".join(self._collections)
        # v1.8.0 — search 도구 1회 사용, 인덱스로 합성. fetch_pd 의존 X.
        # 답변에서 "RAG/컬렉션" 같은 기술용어 X — "문서" 라고 자연스럽게 표현.
        return (
            f"Search attached documents [{collections_str}] semantically. "
            f"Returns a rich INDEX (600-char snippet + source + chunk position). "
            f"Synthesize directly from snippets — most questions answerable without "
            f"fetching full chunks. Don't repeat same query. Search isn't time-sorted. "
            f"If 0 results: change query, try other document set, or STOP. "
            f"In your answer, refer to results as 'documents' (not 'RAG' / 'collection')."
        )

    @property
    def input_schema(self) -> dict:
        schema: dict = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to find relevant documents. Be specific and descriptive.",
                },
                "collection_name": {
                    "type": "string",
                    "description": (
                        f"Document collection to search in. "
                        f"Defaults to '{self._collections[0]}' if omitted. "
                        f"Available: {', '.join(self._collections)}"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": f"Number of results to return (default: {self._default_top_k}).",
                },
            },
            "required": ["query"],
        }
        return schema

    @property
    def category(self) -> str:
        return "retrieval"

    @property
    def read_only_hint(self) -> bool:
        return True

    @property
    def idempotent_hint(self) -> bool:
        return True  # 같은 쿼리·컬렉션이면 같은 결과 (인덱스 고정일 때)

    @property
    def open_world_hint(self) -> bool:
        return True  # xgen-documents / Qdrant 외부 호출

    async def execute(self, input_data: dict) -> ToolResult:
        query = input_data.get("query", "")
        if not query:
            return ToolResult.error("query is required.")

        collection_name = input_data.get("collection_name", self._collections[0])
        top_k = input_data.get("top_k", self._default_top_k)
        # v1.28 — self-govern 검색 폭 하한 강제. 에이전트가 더 넓게 요청하는 건 허용하되,
        # 자가조정이 근거 부족으로 올린 하한 아래로는 못 내려간다(연결 RAG 노드 지배).
        _floor = getattr(self, "_min_top_k", 0) or 0
        try:
            _tk = int(top_k)
        except (TypeError, ValueError):
            _tk = int(self._default_top_k)
        if _floor and _tk < _floor:
            logger.info("[RAG Tool] self-govern 검색 폭 하한 적용: top_k %s→%s", _tk, _floor)
            _tk = _floor
        top_k = _tk

        # 유효한 컬렉션인지 확인
        if collection_name not in self._collections:
            return ToolResult.error(
                f"Collection '{collection_name}' not available. "
                f"Available collections: {', '.join(self._collections)}"
            )

        try:
            result_text = await self._search_documents(query, collection_name, top_k)
            if not result_text:
                # v1.8.0 — RESTRICTIONS_ONLY 톤. 명령형 단축.
                others = [c for c in self._collections if c != collection_name]
                others_str = (", ".join(others)) if others else "(none)"
                return ToolResult.success(
                    f"0 results for query='{query}' on '{collection_name}'. "
                    f"Don't repeat. Change keywords, or try: {others_str}, "
                    f"or query_graph for relationships, or STOP."
                )
            return ToolResult.success(result_text, collection=collection_name, top_k=top_k)
        except Exception as e:
            logger.error("[RAG Tool] Search failed: %s", e)
            return ToolResult.error(f"Document search failed: {str(e)}")

    async def _search_documents(
        self, query: str, collection_name: str, top_k: int,
    ) -> str:
        """주입된 DocumentService.search() 로 검색.

        v1.7.3 — 기본 동작은 결과를 통째 박는 eager 포맷 ([DOC_N] tagged content).
        결과 본문 합산이 _PROGRESSIVE_POLICY['auto_threshold_chars'] 를 초과하거나
        생성자에서 progressive=True 명시한 경우에만 인덱스+snippet + pd_store 모드.

        v1.9.0 — Option C: s06 의 옛 9 stage_params 가 도구 default args 로 이주됨.
        score_threshold / filter / rerank / rerank_top_k 가 None 이 아니면 그대로
        search 에 전달 (UI 사용자 의도 보존).
        """
        from ..errors import ToolError
        if self._doc_service is None or not hasattr(self._doc_service, "search"):
            raise ToolError(
                "DocumentService is not available. RAG search is unavailable — "
                "the host must inject a documents service into the ResourceRegistry.",
                tool_name="rag_search",
            )

        # v1.9.0 — default args 합류. None 면 search 가 자체 default 사용.
        search_kwargs: dict = {
            "limit": top_k,
            "score_threshold": (
                float(self._default_score_threshold)
                if self._default_score_threshold is not None
                else 0.0
            ),
        }
        if self._default_filter:
            search_kwargs["filter"] = self._default_filter
        if self._default_reranker is not None:
            search_kwargs["rerank"] = bool(self._default_reranker)
        if self._default_rerank_top_k is not None:
            search_kwargs["rerank_top_k"] = int(self._default_rerank_top_k)
        try:
            results = await self._doc_service.search(
                query, collection_name, **search_kwargs,
            ) or []
        except TypeError:
            # 옛 DocumentService 구현이 rerank/filter 같은 신규 인자 미지원 — 단계 폴백.
            for unsupported in ("rerank_top_k", "rerank", "filter"):
                search_kwargs.pop(unsupported, None)
                try:
                    results = await self._doc_service.search(
                        query, collection_name, **search_kwargs,
                    ) or []
                    break
                except TypeError:
                    continue
            else:
                # 최소 인자로 한 번 더
                results = await self._doc_service.search(
                    query, collection_name, limit=top_k, score_threshold=0.0,
                ) or []
        if not results:
            return ""

        use_progressive = self._should_use_progressive(results)
        if use_progressive and self._state is not None and hasattr(self._state, "pd_store"):
            return self._format_progressive(results, collection_name)
        return self._format_results(results)

    def _should_use_progressive(self, results: list) -> bool:
        """progressive 사용 여부 결정 — 명시 인자 > 자동 임계 > policy default."""
        if self._progressive_explicit is not None:
            return bool(self._progressive_explicit)
        policy = _PROGRESSIVE_POLICY
        if policy.get("enabled"):
            return True
        threshold = int(policy.get("auto_threshold_chars") or 0)
        if threshold <= 0:
            return False
        try:
            from ..utils.docs import extract_text
            total_chars = 0
            for doc in results:
                if isinstance(doc, dict):
                    total_chars += len(extract_text(doc) or doc.get("chunk_text") or "")
                elif isinstance(doc, str):
                    total_chars += len(doc)
                if total_chars > threshold:
                    return True
        except Exception:
            return False
        return False

    def _effective_snippet_size(self) -> int:
        if self._snippet_size_override is not None:
            return int(self._snippet_size_override)
        return int(_PROGRESSIVE_POLICY.get("snippet_size") or 120)

    def _format_progressive(self, results: list, collection_name: str) -> str:
        """v1.4.0 R3 — 결과를 pd_stores 에 박고 인덱스+snippet 만 반환.

        v1.7.4 — 인덱스 정보량 풍부화. snippet 250자 + chunk_index / total_chunks /
        length 메타 박음. LLM 이 진짜 "지도" 로 사용할 수 있게.
        """
        from ..utils.docs import extract_source, extract_text, extract_score
        snippet_size = self._effective_snippet_size()
        lines: list[str] = []
        for i, doc in enumerate(results, 1):
            if not isinstance(doc, dict):
                continue
            text = extract_text(doc) or doc.get("chunk_text", "") or ""
            source = extract_source(doc) or doc.get("file_name", "") or ""
            score = extract_score(doc)
            # v1.7.4 — 청킹 메타 (있는 경우만, 백엔드별 키 후보 시도)
            chunk_idx = (
                doc.get("chunk_index")
                or doc.get("chunk_idx")
                or (doc.get("metadata") or {}).get("chunk_index")
            )
            total_chunks = (
                doc.get("total_chunks")
                or (doc.get("metadata") or {}).get("total_chunks")
            )
            rid = f"{collection_name}#{i}"
            snippet = (text[: snippet_size] + "…") if len(text) > snippet_size else text
            snippet = snippet.replace("\n", " ")
            score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "-"
            # 메타 prefix — LLM 이 위치 / 크기 보고 fetch 결정
            meta_bits = [f"len={len(text)}"]
            if chunk_idx is not None:
                pos = f"chunk={chunk_idx}"
                if total_chunks:
                    pos += f"/{total_chunks}"
                meta_bits.append(pos)
            meta_str = " · ".join(meta_bits)
            lines.append(f"[{i}] id={rid} · {source} ({score_str}) · {meta_str} · {snippet}")
            try:
                self._state.pd_store(
                    kind="rag",
                    resource_id=rid,
                    preview=snippet,
                    full=text,
                    meta={
                        "collection": collection_name,
                        "index": i,
                        "source": source,
                        "score": score,
                        "chars": len(text),
                    },
                )
            except Exception as e:
                logger.debug("[RAG Tool] pd_store failed for %s: %s", rid, e)
        if not lines:
            return ""
        lines.append("")
        lines.append(
            "(For a full body, call fetch_pd(kind='rag', id='<id above>'). "
            f"e.g. fetch_pd(kind='rag', id='{collection_name}#1'))"
        )
        return "\n".join(lines)

    @staticmethod
    def _format_results(results: list) -> str:
        """검색 결과를 [DOC_N] 태그 포맷으로 변환."""
        from ..utils.docs import extract_source, extract_text, extract_score
        chunks: list[str] = []
        for i, doc in enumerate(results, 1):
            tag = f"[DOC_{i}]"
            if isinstance(doc, dict):
                content = extract_text(doc)
                source = extract_source(doc)
                score = extract_score(doc) or None

                header = tag
                if source:
                    header += f" (source: {source})"
                if score is not None:
                    score_str = f"{score:.3f}" if isinstance(score, float) else str(score)
                    header += f" [score: {score_str}]"

                chunks.append(f"{header}\n{content}")
            elif isinstance(doc, str):
                chunks.append(f"{tag}\n{doc}")

        return "\n\n".join(chunks)
