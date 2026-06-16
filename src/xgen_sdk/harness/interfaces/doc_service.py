"""
DocService / OntologyService Protocol

cluster `doc_service` 의 메서드 셋을 외부 wire 가능하게 추상화.

cluster 측 (xgen-workflow 의 harness_bridge) 은 plateerag 의 DocumentService 를
이 Protocol 의 구현체로 wrap 해서 inject.

외부 사용자는 adapters/qdrant.py 의 QdrantDocService 같은 자기 인프라용 구현체를 inject.

옛 구현이 신규 kwargs 미지원 시 TypeError fallback 체인이 tools/rag_tool.py 에 박혀있음
— Protocol 시그니처는 가장 일반적인 형태 유지 (**kwargs).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DocService(Protocol):
    """RAG 문서 검색 Protocol.

    `search()` 가 cluster doc_service 의 핵심 메서드 (tools/rag_tool.py 가 호출).

    `**kwargs` 로 다양한 인자 (limit / top_k / score_threshold / filter / reranker /
    rerank_top_k / file_names 등) 를 받음. 구현체가 모르는 kwarg 는 TypeError 던지면
    rag_tool.py 가 단계 폴백 (옛 구현 호환).

    반환: 청크 리스트. 각 청크는 최소 `{content: str, ...metadata}` 형태.
    """

    async def search(
        self,
        query: str,
        collection_name: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """문서 검색. 매치된 청크 리스트 반환 (없으면 [])."""
        ...


@runtime_checkable
class OntologyService(Protocol):
    """온톨로지/그래프 RAG Protocol.

    `ontology_query()` 가 tools/ontology_tool.py 에서 호출.

    cluster 측은 같은 DocumentService 가 search + ontology_query 둘 다 제공 가능.
    외부는 분리된 어댑터 가능 (Neo4j / RDF endpoint 등).
    """

    async def ontology_query(
        self,
        question: str,
        collection_name: str,
        **kwargs: Any,
    ) -> Any:
        """온톨로지 질의. 그래프 형태 결과 반환."""
        ...
