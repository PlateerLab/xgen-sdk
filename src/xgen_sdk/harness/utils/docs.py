"""문서 dict 헬퍼 — RAG 결과 정규화.

xgen-documents / qdrant / 외부 RAG 모두 응답 형식이 살짝씩 다름.
이 모듈은 단일 진실 소스 추출 함수를 제공하여 동일 로직 4중 복제를 제거.
"""
from __future__ import annotations

from typing import Any


def extract_source(doc: dict[str, Any]) -> str:
    """문서 dict 에서 source(파일명/url/chunk_id) 추출.

    조회 순서: doc['source'] → doc['metadata']['source'] → doc['file_name'] → "".
    """
    if not isinstance(doc, dict):
        return ""
    src = doc.get("source")
    if src:
        return str(src)
    meta = doc.get("metadata") or {}
    if isinstance(meta, dict):
        if meta.get("source"):
            return str(meta["source"])
        if meta.get("file_name"):
            return str(meta["file_name"])
    return str(doc.get("file_name", ""))


def extract_text(doc: dict[str, Any]) -> str:
    """문서 dict 에서 본문 텍스트 추출. content/chunk_text/text 순."""
    if not isinstance(doc, dict):
        return ""
    for key in ("content", "chunk_text", "text", "page_content"):
        v = doc.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def extract_score(doc: dict[str, Any]) -> float:
    """문서 dict 에서 score 추출. 없으면 0.0."""
    if not isinstance(doc, dict):
        return 0.0
    for key in ("score", "similarity", "distance"):
        v = doc.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0
