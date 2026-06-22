"""Reflection — run traces -> root-cause lesson + ranked typed candidate moves.

Reads only trace *signals* (never the runner's hidden profile): honest L2->L4.
Each candidate is an EngineAlgebra Move on a real config path. The shortlist
hedges across the neighborhood; the Inertia-Brake decides the winner empirically
(reflection only NARROWS the move space). Heuristic default; swap an LLM
reflector (reflection.v1 JSON) later without touching the loop.
"""
from __future__ import annotations

from dataclasses import dataclass

from .algebra import Move
from .runner import RunRecord


@dataclass
class Reflection:
    lesson: str
    dominant_symptom: str
    candidates: list[Move]


# symptom -> (lesson, ordered candidate moves). Moves target real config paths.
_SYMPTOM_FIXES: dict[str, tuple[str, list[Move]]] = {
    "ungated_low_quality": (
        "answers ship without an isolated judge -> enable s08 judge",
        [Move("set_stage_param", "s08_decide:judge_enabled", True)],
    ),
    "accepted_borderline": (
        "validation threshold too low -> raise it (try 0.9, fall back to 0.8)",
        [Move("tune_scalar", "validation_threshold", 0.9),    # exploratory: brake rejects if over-strict
         Move("tune_scalar", "validation_threshold", 0.8)],
    ),
    "over_strict_stall": (
        "validation threshold too high -> relax to 0.8",
        [Move("tune_scalar", "validation_threshold", 0.8)],
    ),
    "regulation_violation": (
        "regulated tasks lack a deterministic content gate -> add the content guard",
        [Move("toggle_guard", "content", True)],
    ),
    "missing_criterion": (
        "regulated tasks have no regulation criterion -> add it as a hard axis",
        [Move("edit_criterion", "regulation", {"weight": 0.3, "hard": True})],
    ),
    "no_recovery": (
        "no retry budget -> a single bad turn is terminal",
        [Move("tune_scalar", "max_retries", 2)],
    ),
    "retry_waste": (
        "too many retries -> wasted loops",
        [Move("tune_scalar", "max_retries", 2)],
    ),
    # real-trace symptoms (PipelineRunner via signals.py)
    "low_judge_score": (
        "judge score below threshold -> grant retry budget to self-correct",
        [Move("tune_scalar", "max_retries", 2)],
    ),
    "iteration_pressure": (
        "loop hit the iteration cap without converging -> raise max_iterations",
        [Move("tune_scalar", "max_iterations", 8)],
    ),
}


def register_symptom_fix(symptom: str, lesson: str, moves: list[Move]) -> None:
    """Register/override a diagnosis: symptom -> (lesson, ranked candidate moves).

    External packages add domain diagnoses without forking (extensibility).
    """
    _SYMPTOM_FIXES[symptom] = (lesson, list(moves))


def reflect(traces: list[RunRecord]) -> Reflection | None:
    agg: dict[str, float] = {}
    for r in traces:
        if r.outcome == "success":
            continue
        for sym, mag in r.signals.items():
            if sym in _SYMPTOM_FIXES and mag > 0:
                agg[sym] = agg.get(sym, 0.0) + mag
    if not agg:
        return None
    dominant = max(agg, key=agg.get)
    lesson, candidates = _SYMPTOM_FIXES[dominant]
    return Reflection(lesson=lesson, dominant_symptom=dominant, candidates=list(candidates))


# ---- extensible reflector seam (GEPA / LLM reflectors plug in here) ----
# A reflector reads traces (incl. RunRecord.feedback text) and proposes candidate
# Moves. The loop gates them structurally (legal single-knob) — independent of the
# heuristic reflect() above. Register via register_reflector() or entry_points
# group `xgen_harness.forge_reflectors`. forge source 0.
_REFLECTORS: list = []
_discovered = False


def register_reflector(fn) -> None:
    """fn: (traces) -> list[Move]  (candidate moves from reflection over traces)."""
    if fn not in _REFLECTORS:
        _REFLECTORS.append(fn)


def _discover_once() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="xgen_harness.forge_reflectors"):
            try:
                register_reflector(ep.load())
            except Exception:
                pass
    except Exception:
        pass


def extra_candidates(traces: list[RunRecord]) -> list[Move]:
    """Candidate moves from all registered reflectors (none by default → empty)."""
    _discover_once()
    out: list[Move] = []
    for fn in _REFLECTORS:
        try:
            out += fn(traces) or []
        except Exception:
            pass
    return out
