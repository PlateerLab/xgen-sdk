"""
QdrantDocService — DocService Protocol 의 Qdrant 직결 구현

cluster 의 doc_service (plateerag DocumentService) 가 cluster Qdrant 를 wrap 하던 것과
달리, 외부 사용자가 자기 Qdrant 인스턴스에 직접 wire.

httpx 기반 — qdrant-client 의존성 없음 (xgen-harness 의 base dep `httpx>=0.27` 만 사용).
Qdrant REST API (https://qdrant.tech/documentation/concepts/search/) 직접 호출.

cluster 측 doc_service 가 search() 호출 시 텍스트 검색 → vector 자동 임베딩까지
한 번에 처리하던 것과 달리, 외부에서는 임베딩 책임이 분리됨:

(a) 임베딩 함수를 inject 하는 패턴 — `embedder` 콜백 받음
(b) 외부 Qdrant 가 자체 임베딩 (예: text -> vector 자동) 지원하면 그쪽 위임

사용:
    from xgen_harness.adapters import QdrantDocService

    # (a) 임베딩 함수 주입
    async def embed(text: str) -> list[float]:
        ...  # 사용자 OpenAI embedding 호출 등
    doc_service = QdrantDocService(
        url="http://localhost:6333",
        embedder=embed,
    )

    # (b) Qdrant 가 자체 임베딩 endpoint 제공 시
    doc_service = QdrantDocService(
        url="http://localhost:6333",
        text_search_endpoint="/collections/{collection}/points/search/text",
    )
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("harness.adapters.qdrant")

Embedder = Callable[[str], Awaitable[list[float]]]


class QdrantDocService:
    """DocService Protocol 구현 — Qdrant REST API 직결."""

    def __init__(
        self,
        url: str,
        *,
        api_key: Optional[str] = None,
        embedder: Optional[Embedder] = None,
        timeout: float = 30.0,
    ):
        """
        Args:
            url: Qdrant 서버 URL (예: "http://localhost:6333")
            api_key: Qdrant API key (cloud 환경)
            embedder: 텍스트 → 벡터 변환 콜백. 미지정 시 Qdrant
                자체 임베딩 endpoint 시도 (지원 시).
            timeout: HTTP 요청 타임아웃 (초)
        """
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._embedder = embedder
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        return headers

    async def search(
        self,
        query: str,
        collection_name: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """문서 검색.

        kwargs (cluster doc_service 와 호환):
            limit / top_k: 결과 개수 (top_k 우선)
            score_threshold: 최소 score
            filter: Qdrant payload filter (dict)
            file_names: payload.file_name in [...] 자동 union
            reranker / rerank_top_k: 외부에서는 reranker 미구현 (옵션 무시) —
                필요 시 사용자가 별도 reranker 구현 wire
        """
        top_k = kwargs.get("top_k") or kwargs.get("limit") or 10
        score_threshold = kwargs.get("score_threshold", 0.0)
        filter_ = self._build_filter(
            kwargs.get("filter"),
            kwargs.get("file_names"),
        )

        if self._embedder is None:
            raise ValueError(
                "QdrantDocService: embedder 미지정. "
                "텍스트 → 벡터 변환 콜백을 inject 하거나 (`embedder=embed_fn`), "
                "Qdrant 가 자체 text search 지원 시 별도 어댑터 구현 필요."
            )
        vector = await self._embedder(query)

        body: dict[str, Any] = {
            "vector": vector,
            "limit": top_k,
            "with_payload": True,
            "score_threshold": score_threshold,
        }
        if filter_:
            body["filter"] = filter_

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._url}/collections/{collection_name}/points/search",
                json=body,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        # Qdrant 응답: {"result": [{"id": ..., "score": ..., "payload": {...}}, ...]}
        # 하네스 청크 포맷 (cluster doc_service 와 정합): {"content": str, ...payload, "score": float}
        chunks: list[dict[str, Any]] = []
        for hit in data.get("result", []):
            payload = hit.get("payload", {}) or {}
            chunk = dict(payload)  # payload 통째로 spread
            # cluster 측이 기대하는 'content' 키 보장 — payload 가 'text' 나 'page_content' 일 때 매핑
            if "content" not in chunk:
                if "text" in chunk:
                    chunk["content"] = chunk["text"]
                elif "page_content" in chunk:
                    chunk["content"] = chunk["page_content"]
            chunk["score"] = hit.get("score", 0.0)
            chunk["id"] = hit.get("id")
            chunks.append(chunk)
        return chunks

    @staticmethod
    def _build_filter(
        user_filter: Optional[dict],
        file_names: Optional[list[str]],
    ) -> Optional[dict]:
        """user filter + file_names 를 Qdrant filter 로 union.

        Qdrant filter: {"must": [...], "should": [...], "must_not": [...]}
        """
        conditions: list[dict] = []
        if user_filter:
            # user_filter 가 이미 Qdrant 포맷이면 그대로
            if any(k in user_filter for k in ("must", "should", "must_not")):
                return user_filter
            # 평탄한 dict 면 must 조건으로 변환: {"file_name": "x"} → {"key": "file_name", "match": {"value": "x"}}
            for key, value in user_filter.items():
                conditions.append({
                    "key": key,
                    "match": {"value": value},
                })
        if file_names:
            # payload.file_name in file_names
            conditions.append({
                "key": "file_name",
                "match": {"any": list(file_names)},
            })
        if not conditions:
            return None
        return {"must": conditions}
