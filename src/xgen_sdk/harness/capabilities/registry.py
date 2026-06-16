"""
CapabilityRegistry — capability 카탈로그

라이브러리는 빈 레지스트리로 시작.
XgenAdapter가 workflow 노드 → CapabilitySpec 변환 후 주입.
Gallery/MCP 어댑터도 같은 방식으로 등록 가능.

Thread-safe: 등록/조회 동시성 안전.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Iterable, Optional

from .schema import CapabilitySpec, ProviderKind


class CapabilityRegistry:
    """capability 중앙 레지스트리 — 태그/카테고리/제공자별 인덱스 유지"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_name: dict[str, CapabilitySpec] = {}
        self._by_category: dict[str, set[str]] = defaultdict(set)
        self._by_tag: dict[str, set[str]] = defaultdict(set)
        self._by_provider: dict[ProviderKind, set[str]] = defaultdict(set)
        self._by_alias: dict[str, str] = {}         # alias → canonical name

    # ---------- 등록/제거 ----------

    def register(self, spec: CapabilitySpec, *, overwrite: bool = False) -> None:
        """capability 등록. overwrite=False면 중복 이름은 무시."""
        with self._lock:
            if spec.name in self._by_name and not overwrite:
                return
            # 이전 등록이 있으면 인덱스 정리
            if spec.name in self._by_name:
                self._remove_from_indexes(self._by_name[spec.name])

            self._by_name[spec.name] = spec
            self._by_category[spec.category].add(spec.name)
            for tag in spec.tags:
                self._by_tag[tag.lower()].add(spec.name)
            self._by_provider[spec.provider_kind].add(spec.name)
            for alias in spec.aliases:
                self._by_alias[alias.lower()] = spec.name

    def register_many(self, specs: Iterable[CapabilitySpec], *, overwrite: bool = False) -> int:
        """여러 개 한번에 등록. 등록된 개수 반환."""
        count = 0
        for spec in specs:
            before = spec.name in self._by_name
            self.register(spec, overwrite=overwrite)
            if overwrite or not before:
                count += 1
        return count

    def unregister(self, name: str) -> bool:
        with self._lock:
            spec = self._by_name.pop(name, None)
            if spec is None:
                return False
            self._remove_from_indexes(spec)
            return True

    def clear(self) -> None:
        with self._lock:
            self._by_name.clear()
            self._by_category.clear()
            self._by_tag.clear()
            self._by_provider.clear()
            self._by_alias.clear()

    def _remove_from_indexes(self, spec: CapabilitySpec) -> None:
        self._by_category[spec.category].discard(spec.name)
        for tag in spec.tags:
            self._by_tag[tag.lower()].discard(spec.name)
        self._by_provider[spec.provider_kind].discard(spec.name)
        for alias in spec.aliases:
            if self._by_alias.get(alias.lower()) == spec.name:
                self._by_alias.pop(alias.lower(), None)

    # ---------- 조회 ----------

    def get(self, name: str) -> Optional[CapabilitySpec]:
        with self._lock:
            if name in self._by_name:
                return self._by_name[name]
            # alias 조회
            canonical = self._by_alias.get(name.lower())
            return self._by_name.get(canonical) if canonical else None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def list_all(self) -> list[CapabilitySpec]:
        with self._lock:
            return list(self._by_name.values())

    def list_names(self) -> list[str]:
        with self._lock:
            return list(self._by_name.keys())

    def list_categories(self) -> list[str]:
        with self._lock:
            return [c for c in self._by_category.keys() if self._by_category[c]]

    def list_tags(self) -> list[str]:
        with self._lock:
            return [t for t in self._by_tag.keys() if self._by_tag[t]]

    def find_by_category(self, category: str) -> list[CapabilitySpec]:
        with self._lock:
            names = self._by_category.get(category, set())
            return [self._by_name[n] for n in names if n in self._by_name]

    def find_by_tag(self, tag: str) -> list[CapabilitySpec]:
        with self._lock:
            names = self._by_tag.get(tag.lower(), set())
            return [self._by_name[n] for n in names if n in self._by_name]

    def find_by_tags(self, tags: Iterable[str], *, mode: str = "any") -> list[CapabilitySpec]:
        """
        mode="any"  — 하나라도 매칭
        mode="all"  — 전부 매칭
        """
        tags_lower = [t.lower() for t in tags]
        with self._lock:
            sets = [self._by_tag.get(t, set()) for t in tags_lower]
            if not sets:
                return []
            if mode == "all":
                result = set.intersection(*sets) if sets else set()
            else:
                result = set().union(*sets)
            return [self._by_name[n] for n in result if n in self._by_name]

    def find_by_provider(self, kind: ProviderKind) -> list[CapabilitySpec]:
        with self._lock:
            names = self._by_provider.get(kind, set())
            return [self._by_name[n] for n in names if n in self._by_name]

    # ---------- 통계 ----------

    def stats(self) -> dict:
        with self._lock:
            return {
                "total": len(self._by_name),
                "by_category": {c: len(s) for c, s in self._by_category.items() if s},
                "by_provider": {k.value: len(s) for k, s in self._by_provider.items() if s},
                "tag_count": sum(1 for s in self._by_tag.values() if s),
            }


# ---------- 전역 기본 레지스트리 ----------

_default_registry: CapabilityRegistry = CapabilityRegistry()
_default_lock = threading.Lock()
_ENTRY_POINTS_DISCOVERED = False


def get_default_registry() -> CapabilityRegistry:
    """전역 기본 레지스트리. Adapter/테스트가 공유."""
    _discover_from_entry_points_once()
    return _default_registry


def set_default_registry(registry: CapabilityRegistry) -> None:
    """전역 레지스트리 교체 (테스트 격리용)"""
    global _default_registry
    with _default_lock:
        _default_registry = registry


def _discover_from_entry_points_once() -> None:
    """외부 패키지가 `xgen_harness.capabilities` 그룹에 CapabilitySpec 을 노출하면 자동 등록.

    v0.15.1 자동 연동 자동 확장성 — pip install xgen-capability-xxx 한 것이
    get_default_registry() 첫 조회 시점에 합류. 반복 호출 idempotent.
    """
    global _ENTRY_POINTS_DISCOVERED
    if _ENTRY_POINTS_DISCOVERED:
        return
    _ENTRY_POINTS_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.capabilities"
        if hasattr(eps, "select"):
            items = eps.select(group=group)
        else:
            items = eps.get(group, [])
        for ep in items:
            try:
                factory = ep.load()
                result = factory() if callable(factory) else factory
                # 단일 CapabilitySpec 또는 Iterable 허용.
                if isinstance(result, CapabilitySpec):
                    _default_registry.register(result)
                elif hasattr(result, "__iter__"):
                    _default_registry.register_many(
                        [s for s in result if isinstance(s, CapabilitySpec)]
                    )
            except Exception:
                # 개별 entry_point 실패는 전체 발견을 막지 않는다.
                continue
    except Exception:
        return
