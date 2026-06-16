"""ResourceProvider — v1.7. 자원 메타 자동 인식 패턴.

사용자 정신: 새 자원 종 (mcp / db / files / ontology / 신규 영역) 추가 시 매번
state.metadata['*_meta'] 박는 wiring 코드 추가 X. **자원 종이 자기 메타 fetch 책임**
지고, 등록만 하면 엔진이 알아서 호출 → 자동 cache → s03_prompt 풍부 노출.

= Anthropic Skills 의 filesystem-based 자동 발견 패턴 isomorphic. SKILL.md 가
SKILL directory 에 있으면 frontmatter 자동 노출되는 것처럼, ResourceProvider 등록만
하면 자기 자원 종의 메타 자동 발견 + s03_prompt 합류.

사용:

    @register_resource_provider
    class MyMcpProvider(ResourceProvider):
        kind = "mcp_sessions"   # state.metadata 의 키 = "{kind}_meta"

        async def list_meta(self, state):
            sessions = state.config.mcp_sessions or []
            # 사용자가 박은 sessions 의 메타 fetch
            result = {}
            for sname in sessions:
                meta = await my_service.get_session_meta(sname)
                result[sname] = {
                    "tool_count": meta.tools_count,
                    "when_to_use": meta.description,
                }
            return result

entry_points:

    [project.entry-points."xgen_harness.resource_providers"]
    my_mcp = "my_pkg:MyMcpProvider"
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PipelineState

logger = logging.getLogger("harness.resource_providers")


@runtime_checkable
class ResourceProvider(Protocol):
    """자원 종별 메타 자동 fetcher.

    - `kind`: 자원 종 식별자 (예: "rag_collections", "ontology_collections", "db_connections",
      "files", "mcp_sessions"). state.metadata 의 cache 키 = `f"{kind}_meta"`.
    - `list_meta(state)`: 사용자가 박은 자원의 메타 dict 반환. {name: meta_dict, ...}.
      meta_dict 의 키는 자원 종별 자유 (description / total_documents / type / schema / size 등).
      s03_prompt 가 메타 dict 보고 풍부 노출.
    """
    kind: str

    async def list_meta(self, state) -> dict[str, dict]:
        ...


_PROVIDERS: list[ResourceProvider] = []
_LOADED_FROM_ENTRY_POINTS = False


def register_resource_provider(provider) -> None:
    """ResourceProvider 등록.

    클래스 또는 인스턴스 둘 다 가능. 클래스면 인스턴스화. 같은 kind 의 provider 가
    여러 개면 결과 merge (key 충돌 시 나중 등록자가 override).
    """
    inst = provider() if isinstance(provider, type) else provider
    if not hasattr(inst, "kind") or not callable(getattr(inst, "list_meta", None)):
        raise TypeError(
            f"register_resource_provider: {provider!r} is not a ResourceProvider "
            f"(must have 'kind' attr and async 'list_meta(state)' method)"
        )
    _PROVIDERS.append(inst)
    logger.info("[resource_providers] registered: kind=%s class=%s",
                inst.kind, type(inst).__name__)


async def fetch_all_resource_meta(state) -> dict[str, dict]:
    """등록된 모든 ResourceProvider 호출 → state.metadata 의 *_meta 키들 자동 cache.

    return 값: {kind: meta_dict_merged}. s03_prompt 가 state.metadata['{kind}_meta'] 직접
    읽지만, fetch_all 도 결과 반환 (테스트/디버그 용).
    """
    _ensure_loaded()
    result: dict[str, dict] = {}
    for provider in _PROVIDERS:
        kind = provider.kind
        try:
            meta = await provider.list_meta(state)
            if not isinstance(meta, dict):
                logger.warning("[resource_providers] %s.list_meta returned non-dict: %r",
                               kind, type(meta))
                continue
            # state.metadata cache (s03_prompt 가 직접 read)
            cache_key = f"{kind}_meta"
            existing = state.metadata.get(cache_key) if hasattr(state, "metadata") else None
            if isinstance(existing, dict):
                existing.update(meta)
                merged = existing
            else:
                merged = dict(meta)
            if hasattr(state, "metadata"):
                state.metadata[cache_key] = merged
            result[kind] = merged
            logger.info("[resource_providers] %s: %d items cached at state.metadata['%s']",
                        kind, len(merged), cache_key)
        except Exception as e:
            logger.warning("[resource_providers] %s.list_meta failed: %s", kind, e)
    return result


def get_registered_kinds() -> list[str]:
    _ensure_loaded()
    return [p.kind for p in _PROVIDERS]


def _ensure_loaded() -> None:
    global _LOADED_FROM_ENTRY_POINTS
    if _LOADED_FROM_ENTRY_POINTS:
        return
    _LOADED_FROM_ENTRY_POINTS = True
    # 빌트인 등록
    _register_builtins()
    # entry_points 자동 발견
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="xgen_harness.resource_providers")
        for ep in eps:
            try:
                provider_cls = ep.load()
                register_resource_provider(provider_cls)
                logger.info("[resource_providers] loaded entry_point: %s", ep.name)
            except Exception as e:
                logger.warning("[resource_providers] entry_point %s load 실패: %s",
                               ep.name, e)
    except Exception as e:
        logger.debug("[resource_providers] entry_points 미사용: %s", e)


def _register_builtins() -> None:
    """빌트인 ResourceProvider — DocumentService 의존 (RAG / Ontology)."""
    from .builtin_resource_providers import (
        RagCollectionMetaProvider,
        OntologyCollectionMetaProvider,
    )
    register_resource_provider(RagCollectionMetaProvider())
    register_resource_provider(OntologyCollectionMetaProvider())
