"""
Embedder adapters — 텍스트 → 벡터 변환 콜백 (`QdrantDocService.embedder`).

v1.13 — 외부 자족용. python wheel 산출물의 `_build_doc_service` 가 cluster_defaults
에 박힌 `_rag_embedder` 메타 (provider/model/dimension/endpoint/api_key_env) 를 보고
이 모듈의 빌더로 콜백 만들어 `QdrantDocService(embedder=...)` 에 주입.

설계:
  - **generic helper** — provider 식별자는 문자열, 신규 provider 는 한 곳 등록.
    `register_embedder(name, builder)` + entry_points 그룹 `xgen_harness.embedders`.
  - **node-engine 의 dispatch.ts 와 패리티** — call_kind="rag" 직접 분기 시 TS 가
    하는 임베더 호출과 같은 provider 셋 (openai / custom_http / voyage). 응답 파싱도
    동일 규약 (OpenAI 호환 우선, 그 외 단일 list / `embedding` 키 폴백).
  - **secret 값은 spec / cluster_defaults 에 박지 않음** — env 이름만 박혀있고
    `os.environ[...]` 으로 읽음. 외부 환경에서 사용자가 박는다.

사용:
    from xgen_harness.adapters.embedders import build_embedder

    embedder_fn = build_embedder({
        "provider": "openai",
        "model": "text-embedding-3-small",
        "dimension": 1536,
        "api_key_env": "OPENAI_API_KEY",
    })
    doc_service = QdrantDocService(url=..., embedder=embedder_fn)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("harness.adapters.embedders")

EmbedderFn = Callable[[str], Awaitable[list[float]]]

# (meta, fn) → embedder. 신규 provider 는 register_embedder 로 한 줄 추가.
# entry_points 그룹 `xgen_harness.embedders` 도 자동 인입 (선택 — discover 함수 호출 시).
_REGISTRY: dict[str, Callable[[dict[str, Any]], EmbedderFn]] = {}


def register_embedder(name: str, builder: Callable[[dict[str, Any]], EmbedderFn]) -> None:
    """provider 식별자에 builder 등록. builder(meta_dict) → embedder 콜백."""
    _REGISTRY[name.lower().strip()] = builder


def build_embedder(meta: dict[str, Any]) -> EmbedderFn:
    """meta dict → embedder 콜백.

    meta 필수: ``provider``. 그 외 (model/dimension/endpoint/api_key_env) 는 provider 별
    builder 가 자체 default 처리. 미등록 provider 면 ValueError.
    """
    if not isinstance(meta, dict):
        raise ValueError(f"embedder meta must be dict, got {type(meta).__name__}")
    provider = str(meta.get("provider") or "").lower().strip()
    if not provider:
        raise ValueError("embedder meta.provider 미박힘")
    builder = _REGISTRY.get(provider)
    if builder is None:
        raise ValueError(
            f"embedder provider '{provider}' 미지원 "
            f"(registered: {sorted(_REGISTRY.keys())}). "
            "register_embedder() 로 외부 등록 가능."
        )
    return builder(meta)


# ─── 기본 provider builders ───────────────────────────────────────


def _build_openai_embedder(meta: dict[str, Any]) -> EmbedderFn:
    model = str(meta.get("model") or "text-embedding-3-small")
    key_env = str(meta.get("api_key_env") or "OPENAI_API_KEY")
    endpoint = str(meta.get("endpoint") or "https://api.openai.com/v1/embeddings")
    expected_dim = meta.get("dimension")

    async def _embed(text: str) -> list[float]:
        api_key = (os.environ.get(key_env) or "").strip()
        if not api_key:
            raise RuntimeError(f"OpenAI embedder: {key_env} 미설정")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {api_key}",
                },
                json={"model": model, "input": text},
            )
        if resp.status_code >= 300:
            raise RuntimeError(f"openai embed {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        vec = (data.get("data") or [{}])[0].get("embedding")
        if not isinstance(vec, list):
            raise RuntimeError("openai embed: 응답에 embedding 없음")
        if expected_dim and len(vec) != int(expected_dim):
            raise RuntimeError(f"embedder dimension mismatch — expected {expected_dim}, got {len(vec)}")
        return vec

    return _embed


def _build_custom_http_embedder(meta: dict[str, Any]) -> EmbedderFn:
    endpoint = str(meta.get("endpoint") or "")
    if not endpoint:
        raise ValueError("custom_http embedder: endpoint 미박힘")
    model = str(meta.get("model") or "")
    key_env = str(meta.get("api_key_env") or "CUSTOM_EMBEDDING_API_KEY")
    expected_dim = meta.get("dimension")

    async def _embed(text: str) -> list[float]:
        api_key = (os.environ.get(key_env) or "").strip()
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                endpoint,
                headers=headers,
                json={"model": model, "input": text},
            )
        if resp.status_code >= 300:
            raise RuntimeError(f"custom_http embed {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        # OpenAI 호환 우선 → 단일 list → bare {embedding}
        vec: Optional[list[float]] = None
        if isinstance(data, dict):
            d = data.get("data")
            if isinstance(d, list) and d and isinstance(d[0], dict):
                v = d[0].get("embedding")
                if isinstance(v, list):
                    vec = v
            if vec is None:
                bare = data.get("embedding")
                if isinstance(bare, list):
                    vec = bare
        elif isinstance(data, list):
            vec = data
        if vec is None:
            raise RuntimeError("custom_http embed: 알 수 없는 응답 형태")
        if expected_dim and len(vec) != int(expected_dim):
            raise RuntimeError(f"embedder dimension mismatch — expected {expected_dim}, got {len(vec)}")
        return vec

    return _embed


def _build_voyage_embedder(meta: dict[str, Any]) -> EmbedderFn:
    model = str(meta.get("model") or "voyage-3.5")
    key_env = str(meta.get("api_key_env") or "VOYAGE_API_KEY")
    endpoint = str(meta.get("endpoint") or "https://api.voyageai.com/v1/embeddings")
    expected_dim = meta.get("dimension")

    async def _embed(text: str) -> list[float]:
        api_key = (os.environ.get(key_env) or "").strip()
        if not api_key:
            raise RuntimeError(f"Voyage embedder: {key_env} 미설정")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {api_key}",
                },
                json={"model": model, "input": [text], "input_type": "query"},
            )
        if resp.status_code >= 300:
            raise RuntimeError(f"voyage embed {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        vec = (data.get("data") or [{}])[0].get("embedding")
        if not isinstance(vec, list):
            raise RuntimeError("voyage embed: 응답에 embedding 없음")
        if expected_dim and len(vec) != int(expected_dim):
            raise RuntimeError(f"embedder dimension mismatch — expected {expected_dim}, got {len(vec)}")
        return vec

    return _embed


# 기본 3종 등록
register_embedder("openai", _build_openai_embedder)
register_embedder("custom_http", _build_custom_http_embedder)
register_embedder("voyage", _build_voyage_embedder)


# entry_points 자동 발견 — 외부 패키지의 신규 embedder provider 자동 인입
def discover_external_embedders() -> None:
    """`xgen_harness.embedders` entry_points 그룹에서 외부 등록자 자동 발견.

    각 entry_point 는 `register_embedder` 처럼 동작하는 callable 또는 builder 직접.
    한 번 호출하면 이후엔 _REGISTRY 에 박혀있음 — 본 함수는 멱등.
    """
    try:
        from importlib import metadata as _md
    except ImportError:
        return
    try:
        eps = _md.entry_points(group="xgen_harness.embedders")
    except TypeError:
        all_eps = _md.entry_points()
        eps = getattr(all_eps, "select", lambda **_: [])(group="xgen_harness.embedders") or \
              all_eps.get("xgen_harness.embedders", [])
    for ep in eps:
        try:
            obj = ep.load()
        except Exception as e:
            logger.warning("[embedders] entry_point load 실패 (%s): %s", ep.name, e)
            continue
        if callable(obj):
            register_embedder(ep.name, obj)


__all__ = [
    "EmbedderFn",
    "build_embedder",
    "register_embedder",
    "discover_external_embedders",
]
