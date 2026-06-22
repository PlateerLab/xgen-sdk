"""Goodhart-defended objective for the self-forging loop.

The loop WILL overfit the judge: as the proxy (dev J) is optimized, true quality
rises then falls. Defenses (all data-derived, no magic thresholds; all pluggable):
  - optimize on `dev`, GATE promotions on a frozen `heldout` the proposer's
    selection never reads,
  - track a second, judge-INDEPENDENT metric and refuse promotions that regress it,
  - flag proxy/true DIVERGENCE (dev up while heldout down) so the loop early-stops
    instead of climbing the proxy off a cliff.

Secondary metrics are registered (engine ships none — they are domain signals):
external packages add via register_secondary_metric() or the entry_points group
`xgen_harness.forge_secondary_metrics`. Plain-bench use (no held-out) degrades to
the old behavior via `Objective.from_bench`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .runner import RunRecord, Runner

# name -> callable(list[RunRecord]) -> float in [0,1]. Judge-INDEPENDENT signals only.
SecondaryMetric = Callable[[list], float]
_SECONDARY: dict[str, SecondaryMetric] = {}
_discovered = False


def register_secondary_metric(name: str, fn: SecondaryMetric) -> None:
    _SECONDARY[name] = fn


def _discover_once() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="xgen_harness.forge_secondary_metrics"):
            try:
                register_secondary_metric(ep.name, ep.load())
            except Exception:
                pass
    except Exception:
        pass


@dataclass
class Score:
    dev: float                       # proxy the loop optimizes
    heldout: float                   # frozen gate the proposer never selects on
    secondary: Optional[float]       # judge-independent corroborating metric
    records: list                    # held-out RunRecords (for reflection/synthesis)


@dataclass
class Objective:
    runner: Runner
    dev: list[dict[str, Any]]
    heldout: list[dict[str, Any]]
    secondary: Optional[str] = None  # registered metric name

    @classmethod
    def from_bench(cls, runner: Runner, bench: list[dict[str, Any]]) -> "Objective":
        """No held-out split → dev == heldout (degrades to pre-defense behavior)."""
        return cls(runner=runner, dev=list(bench), heldout=list(bench))

    def _mean(self, config: dict[str, Any], tasks: list[dict[str, Any]]) -> tuple[float, list[RunRecord]]:
        recs = [self.runner.run(config, t) for t in tasks]
        return (round(sum(r.score for r in recs) / len(recs), 4) if recs else 0.0), recs

    def evaluate(self, config: dict[str, Any]) -> Score:
        _discover_once()
        dev, _ = self._mean(config, self.dev)
        heldout, ho_recs = self._mean(config, self.heldout)
        sec = None
        if self.secondary and self.secondary in _SECONDARY:
            try:
                sec = round(float(_SECONDARY[self.secondary](ho_recs)), 4)
            except Exception:
                sec = None
        return Score(dev=dev, heldout=heldout, secondary=sec, records=ho_recs)
