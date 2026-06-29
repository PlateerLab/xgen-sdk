"""MemoryStore — 스코프(user/room/org) 기반 장기기억 read/search/write (provider-agnostic).

엔진은 Protocol 만 소유하고 DB·임베딩·권한은 entry_points `xgen_harness.memory_stores` 로
이식이 끼운다. 빌트인 InMemory 는 의존성 0. 미주입 시 어떤 스테이지도 호출 안 함(하위호환).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("harness.memory.memory_store")


@dataclass
class MemoryEntry:
    """기억 1건. description=인덱스(관련성 판단), content=본문. metadata 에 도메인 부가정보."""

    scope: str
    memory_key: str
    content: str
    description: str = ""
    type: str = "fact"
    metadata: dict[str, Any] = field(default_factory=dict)
    score: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEntry":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@runtime_checkable
class MemoryStore(Protocol):
    """구현체는 이 3 메서드만 만족하면 된다. 권한 트리밍·임베딩은 구현체 책임."""

    def search(self, query: str, *, scopes: list[str], top_k: int = 5,
               types: Optional[list[str]] = None) -> list[MemoryEntry]: ...

    def write(self, entry: MemoryEntry) -> str: ...

    def delete(self, scope: str, memory_key: str) -> bool: ...


class InMemoryMemoryStore:
    """프로세스 메모리 + 키워드 점수 검색. standalone 기본값 — 임베딩 없이 동작."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], MemoryEntry] = {}

    def search(self, query: str, *, scopes: list[str], top_k: int = 5,
               types: Optional[list[str]] = None) -> list[MemoryEntry]:
        scope_set = set(scopes or [])
        type_set = set(types) if types else None
        terms = [t for t in (query or "").lower().split() if t]
        scored: list[MemoryEntry] = []
        for (scope, _key), entry in self._data.items():
            if scope_set and scope not in scope_set:
                continue
            if type_set and entry.type not in type_set:
                continue
            hay = f"{entry.description}\n{entry.content}\n{entry.type}".lower()
            s = (sum(1 for t in terms if t in hay) / len(terms)) if terms else 0.0
            if terms and s == 0.0:
                continue
            scored.append(MemoryEntry(
                scope=entry.scope, memory_key=entry.memory_key, content=entry.content,
                description=entry.description, type=entry.type,
                metadata=dict(entry.metadata), score=s,
            ))
        scored.sort(key=lambda e: (e.score or 0.0), reverse=True)
        return scored[: max(0, top_k)]

    def write(self, entry: MemoryEntry) -> str:
        self._data[(entry.scope, entry.memory_key)] = MemoryEntry(
            scope=entry.scope, memory_key=entry.memory_key, content=entry.content,
            description=entry.description, type=entry.type,
            metadata=dict(entry.metadata), score=None,
        )
        return f"{entry.scope}/{entry.memory_key}"

    def delete(self, scope: str, memory_key: str) -> bool:
        return self._data.pop((scope, memory_key), None) is not None


_STORE_REGISTRY: dict[str, MemoryStore] = {}
_DISCOVERY_DONE = False


def register_memory_store(name: str, store: MemoryStore) -> None:
    if not isinstance(store, MemoryStore):
        raise TypeError(f"register_memory_store: {store!r} 는 MemoryStore 프로토콜 미충족")
    _STORE_REGISTRY[name] = store


def _discover_once() -> None:
    global _DISCOVERY_DONE
    if _DISCOVERY_DONE:
        return
    _DISCOVERY_DONE = True
    _STORE_REGISTRY.setdefault("memory", InMemoryMemoryStore())
    _STORE_REGISTRY.setdefault("default", _STORE_REGISTRY["memory"])
    try:
        from importlib.metadata import entry_points
        try:
            eps = entry_points(group="xgen_harness.memory_stores")
        except TypeError:
            eps = entry_points().get("xgen_harness.memory_stores", [])
    except Exception as e:
        logger.debug("[memory_stores] entry_points backend 없음: %s", e)
        return
    for ep in eps:
        try:
            factory = ep.load()
            store = factory() if callable(factory) else factory
            if isinstance(store, MemoryStore):
                _STORE_REGISTRY[ep.name] = store
        except Exception as e:
            logger.warning("[memory_stores] %s 로드 실패: %s", ep, e)


def get_memory_store(name: str = "default") -> MemoryStore:
    _discover_once()
    store = _STORE_REGISTRY.get(name)
    if store is None:
        raise KeyError(f"MemoryStore 없음: {name!r} (available={available_memory_stores()})")
    return store


def has_memory_store(name: str = "default") -> bool:
    """이식 백엔드 주입 여부 — InMemory 빌트인뿐이면 False (회상 스킵 판단용)."""
    _discover_once()
    store = _STORE_REGISTRY.get(name)
    return store is not None and not isinstance(store, InMemoryMemoryStore)


def available_memory_stores() -> list[str]:
    _discover_once()
    return sorted(_STORE_REGISTRY.keys())


# 기억 추출 훅 (HP3) — 호출 타이밍만 엔진이 제공, 판정·저장은 콜백(이식)이 책임.
# fn(state) -> int | Awaitable[int] | None.
_EXTRACTOR: dict[str, Any] = {"fn": None}
_EXTRACTOR_DISCOVERY_DONE = False


def register_memory_extractor(fn: Any) -> None:
    if fn is not None and not callable(fn):
        raise TypeError(f"register_memory_extractor: {fn!r} 는 callable 이 아님")
    _EXTRACTOR["fn"] = fn


def _discover_extractor_once() -> None:
    global _EXTRACTOR_DISCOVERY_DONE
    if _EXTRACTOR_DISCOVERY_DONE or _EXTRACTOR["fn"] is not None:
        _EXTRACTOR_DISCOVERY_DONE = True
        return
    _EXTRACTOR_DISCOVERY_DONE = True
    try:
        from importlib.metadata import entry_points
        try:
            eps = entry_points(group="xgen_harness.memory_extractors")
        except TypeError:
            eps = entry_points().get("xgen_harness.memory_extractors", [])
    except Exception as e:
        logger.debug("[memory_extractors] entry_points backend 없음: %s", e)
        return
    for ep in eps:
        try:
            fn = ep.load()
            if callable(fn):
                _EXTRACTOR["fn"] = fn
                break
        except Exception as e:
            logger.warning("[memory_extractors] %s 로드 실패: %s", ep, e)


def get_memory_extractor() -> Any:
    _discover_extractor_once()
    return _EXTRACTOR["fn"]
