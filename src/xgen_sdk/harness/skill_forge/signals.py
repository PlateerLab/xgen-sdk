"""SF1 — candidate signal extraction from finished run traces.

Extractors are pure functions RunTrace -> list[SignalHit]; hosts add their own
via the registry / entry_points. Defaults mirror the Hermes trigger set but are
tunable and engine-neutral.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .model import RunTrace
from .registry import GROUP_SIGNAL_EXTRACTORS, Registry, registry_decorator


@dataclass
class SignalHit:
    signal: str
    strength: float  # 0..1, extractor-relative
    evidence: str
    meta: dict = field(default_factory=dict)


SIGNAL_EXTRACTORS = Registry(GROUP_SIGNAL_EXTRACTORS)
signal_extractor = registry_decorator(SIGNAL_EXTRACTORS)

Extractor = Callable[[RunTrace], list[SignalHit]]


@signal_extractor("complex_success")
def complex_success(trace: RunTrace, min_tool_calls: int = 5) -> list[SignalHit]:
    calls = trace.tool_calls()
    if trace.success and len(calls) >= min_tool_calls:
        return [
            SignalHit(
                signal="complex_success",
                strength=min(1.0, len(calls) / (min_tool_calls * 2)),
                evidence=f"{len(calls)} tool calls succeeded end-to-end",
                meta={"tool_sequence": [c.name for c in calls]},
            )
        ]
    return []


@signal_extractor("recovery")
def recovery(trace: RunTrace) -> list[SignalHit]:
    """Error followed later by a successful outcome — a workaround was found."""
    saw_error = False
    error_detail = ""
    for event in trace.events:
        if event.type == "error" or (event.type == "tool_call" and not event.ok):
            saw_error = True
            error_detail = event.detail or event.name
        if event.type == "recovery" and saw_error:
            return [
                SignalHit(
                    signal="recovery",
                    strength=0.9,
                    evidence=f"recovered from: {error_detail}",
                    meta={"error": error_detail, "recovery": event.detail},
                )
            ]
    if saw_error and trace.success:
        return [
            SignalHit(
                signal="recovery",
                strength=0.6,
                evidence=f"run succeeded despite error: {error_detail}",
                meta={"error": error_detail},
            )
        ]
    return []


@signal_extractor("user_correction")
def user_correction(trace: RunTrace) -> list[SignalHit]:
    hits = []
    for event in trace.events:
        if event.type == "user_correction":
            hits.append(
                SignalHit(
                    signal="user_correction",
                    strength=1.0,
                    evidence=event.detail or "user corrected the approach",
                    meta={"detail": event.detail},
                )
            )
    return hits


@signal_extractor("repetition")
def repetition(trace: RunTrace, prior_signatures: dict[str, int] | None = None,
               min_repeats: int = 3) -> list[SignalHit]:
    """Same tool-sequence signature seen across runs. The host passes the
    historical signature counts (e.g. aggregated from the spine)."""
    prior = trace.events and (prior_signatures or trace.__dict__.get("_prior_signatures"))
    if not prior:
        return []
    sig = trace.signature()
    seen = prior.get(sig, 0)
    if trace.success and seen + 1 >= min_repeats:
        return [
            SignalHit(
                signal="repetition",
                strength=min(1.0, (seen + 1) / (min_repeats * 2)),
                evidence=f"tool sequence repeated {seen + 1} times",
                meta={"signature": sig, "count": seen + 1},
            )
        ]
    return []


def extract_signals(trace: RunTrace,
                    prior_signatures: dict[str, int] | None = None) -> list[SignalHit]:
    hits: list[SignalHit] = []
    for name, extractor in SIGNAL_EXTRACTORS.items():
        if name == "repetition":
            hits.extend(extractor(trace, prior_signatures=prior_signatures))
        else:
            hits.extend(extractor(trace))
    return hits
