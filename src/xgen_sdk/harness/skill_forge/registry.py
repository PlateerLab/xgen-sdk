"""Tiny plugin registry with optional entry_points loading.

Groups follow the engine convention (`xgen_harness.skill_*`) so absorption
into xgen_sdk.harness keeps external contracts unchanged.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any, Callable

GROUP_SIGNAL_EXTRACTORS = "xgen_harness.skill_signal_extractors"
GROUP_SYNTHESIZERS = "xgen_harness.skill_synthesizers"
GROUP_GATES = "xgen_harness.skill_gates"
GROUP_LEDGERS = "xgen_harness.skill_ledgers"


class Registry:
    def __init__(self, group: str) -> None:
        self.group = group
        self._items: dict[str, Any] = {}

    def register(self, name: str, item: Any) -> None:
        self._items[name] = item

    def get(self, name: str) -> Any:
        if name not in self._items:
            raise KeyError(f"{self.group}: unknown entry {name!r}")
        return self._items[name]

    def names(self) -> list[str]:
        return sorted(self._items)

    def items(self) -> list[tuple[str, Any]]:
        return sorted(self._items.items())

    def load_entry_points(self) -> int:
        loaded = 0
        try:
            eps = metadata.entry_points(group=self.group)
        except Exception:
            return 0
        for ep in eps:
            try:
                self.register(ep.name, ep.load())
                loaded += 1
            except Exception:
                continue
        return loaded


def registry_decorator(reg: Registry) -> Callable[[str], Callable[[Any], Any]]:
    def outer(name: str) -> Callable[[Any], Any]:
        def inner(obj: Any) -> Any:
            reg.register(name, obj)
            return obj

        return inner

    return outer
