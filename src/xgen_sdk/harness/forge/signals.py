"""Trace-signal extraction — final PipelineState + config -> symptom magnitudes.

No hardcoding: built-in extractors derive symptoms from the engine's own state
contract (validation_score vs threshold, loop_iteration vs cap, policy block)
with data-derived magnitudes — no magic thresholds. Extensible: external packages
add extractors via register_signal_extractor() or the entry_points group
`xgen_harness.forge_signal_extractors` (lazy-discovered), forge source 0.
Integration: reads only the engine state object, so it is provider-agnostic.
"""
from __future__ import annotations

from typing import Any, Callable

SignalExtractor = Callable[[Any, dict], dict]   # (state, config) -> {symptom: magnitude}

_EXTRA: list[SignalExtractor] = []
_discovered = False


def register_signal_extractor(fn: SignalExtractor) -> None:
    """Register a custom (state, config) -> {symptom: magnitude} extractor."""
    if fn not in _EXTRA:
        _EXTRA.append(fn)


def _resolve_threshold(config: dict) -> float:
    thr = config.get("validation_threshold")
    if thr is not None:
        return float(thr)
    try:                                            # fall back to the engine's own judge default
        from ..stages.s08_decide.strategies.judge_then_loop import JUDGE_DEFAULTS
        return float(JUDGE_DEFAULTS.get("threshold", 0.7))
    except Exception:
        return 0.7


def _judge_extractor(state, config: dict) -> dict:
    score = getattr(state, "validation_score", None)
    if score is None:
        return {"ungated_low_quality": 1.0}         # judge off or silently failed
    thr = _resolve_threshold(config)
    if float(score) < thr:
        return {"low_judge_score": round(thr - float(score), 4)}   # magnitude = the gap (data-derived)
    return {}


def _iteration_extractor(state, config: dict) -> dict:
    it = getattr(state, "loop_iteration", 0) or 0
    cap = config.get("max_iterations")
    if cap and it >= cap and getattr(state, "loop_decision", "") != "complete":
        return {"iteration_pressure": 1.0}          # hit the cap without converging
    return {}


def _policy_extractor(state, config: dict) -> dict:
    return {"policy_block": 1.0} if getattr(state, "policy_block_reason", None) else {}


_BUILTIN: tuple[SignalExtractor, ...] = (_judge_extractor, _iteration_extractor, _policy_extractor)


def _discover_once() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="xgen_harness.forge_signal_extractors"):
            try:
                register_signal_extractor(ep.load())
            except Exception:
                pass
    except Exception:
        pass


def extract_signals(state, config: dict) -> dict:
    """Run all (built-in + registered) extractors and merge symptom magnitudes."""
    _discover_once()
    out: dict[str, float] = {}
    for fn in (*_BUILTIN, *_EXTRA):
        try:
            found = fn(state, config) or {}
        except Exception:
            found = {}
        for k, v in found.items():
            out[k] = out.get(k, 0.0) + float(v)
    return out
