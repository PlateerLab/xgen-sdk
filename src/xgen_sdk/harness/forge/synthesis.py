"""Primitive synthesis seam — propose NEW primitives, not just select registered ones.

The safe subset (Voyager/LATM discipline): a synthesizer proposes a candidate
primitive expressed as a DECLARATIVE Move (e.g. a new judge criterion drawn into
criteria_defs), the loop GATES it on the objective, and only promoted ones survive
— no arbitrary code execution in the engine. Heavy synthesizers (LLM-authored
criteria/prompts, GEPA, codegen-with-unit-tests) plug in via register_synthesizer()
or the entry_points group `xgen_harness.forge_synthesizers`, forge source 0.

Built-in: `promote_registered_criteria` — pulls a registered-but-unused evaluation
criterion (from the engine's ALL_CRITERIA) into criteria_defs. It synthesizes from
the engine's own registry, so it stays domain-agnostic and hardcode-free.
"""
from __future__ import annotations

from typing import Any, Callable

from .algebra import EngineAlgebra, Move

# (traces, config, algebra) -> list[Move]. Candidates; the loop's objective gates them.
Synthesizer = Callable[[list, dict, EngineAlgebra], list]
_SYNTH: list[Synthesizer] = []
_discovered = False


def register_synthesizer(fn: Synthesizer) -> None:
    if fn not in _SYNTH:
        _SYNTH.append(fn)


def promote_registered_criteria(traces: list, config: dict, algebra: EngineAlgebra) -> list[Move]:
    """Draw a registered-but-unused judge criterion into criteria_defs (safe, no codegen)."""
    try:
        from ..stages.s08_decide.strategies.judge_then_loop import ALL_CRITERIA
        registered = list(ALL_CRITERIA)
    except Exception:
        registered = []
    used = {c.get("name") for c in
            config.get("stage_params", {}).get("s08_decide", {}).get("criteria_defs", []) or []}
    return [Move("edit_criterion", name, {"weight": 0.2, "hard": False})
            for name in registered if name and name not in used]


_BUILTIN: tuple[Synthesizer, ...] = (promote_registered_criteria,)


def _discover_once() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="xgen_harness.forge_synthesizers"):
            try:
                register_synthesizer(ep.load())
            except Exception:
                pass
    except Exception:
        pass


def synthesize(traces: list, config: dict, algebra: EngineAlgebra) -> list[Move]:
    """Collect candidate primitives from all (built-in + registered) synthesizers.
    Returns only legal, not-yet-applied Moves — the loop gates them on the objective."""
    _discover_once()
    out: list[Move] = []
    seen: set = set()
    for fn in (*_BUILTIN, *_SYNTH):
        try:
            cands = fn(traces, config, algebra) or []
        except Exception:
            cands = []
        for mv in cands:
            key = (mv.op, mv.target, str(mv.value))
            if key in seen or not algebra.is_legal(mv):
                continue
            seen.add(key)
            out.append(mv)
    return out
