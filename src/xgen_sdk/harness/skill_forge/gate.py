"""SF4 — the forge verification gate.

Same promote algebra as SelfForge: promote = dev_up AND held_ok AND sec_ok
AND NOT overopt. Bench cases are split deterministically by id hash into
dev/holdout (the Synapse/forge discipline: holdout never trains, only judges).

With no bench cases available the gate is honest: verdict "staged"
(unverified 2nd-track), never a fake pass.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

from .curator import safety_check
from .model import GateResult, SkillCandidate, SkillDef


@dataclass
class BenchCase:
    case_id: str
    payload: dict = field(default_factory=dict)

    def is_holdout(self, ratio: float = 0.25) -> bool:
        digest = hashlib.sha256(self.case_id.encode("utf-8")).digest()
        return (digest[0] / 255.0) < ratio


class BenchRunner(Protocol):
    """Scores one bench case, optionally with the candidate skill installed.
    Hosts back this with PipelineRunner + ReproBundle replays."""

    def score(self, case: BenchCase, skill: SkillDef | None) -> float: ...


ScoreFn = Callable[[BenchCase, SkillDef | None], float]


@dataclass
class GateConfig:
    holdout_ratio: float = 0.25
    min_gain: float = 0.0        # dev must strictly beat baseline by more than this
    max_holdout_drop: float = 0.02
    overopt_gap: float = 0.25    # dev-holdout gain divergence alarm
    min_cases: int = 4


class ForgeGate:
    def __init__(self, runner: BenchRunner | ScoreFn,
                 config: GateConfig | None = None) -> None:
        self._score: ScoreFn = runner.score if hasattr(runner, "score") else runner  # type: ignore[union-attr]
        self.config = config or GateConfig()

    def verify(self, candidate: SkillCandidate, skill: SkillDef,
               cases: Sequence[BenchCase]) -> GateResult:
        reason = safety_check(candidate)
        if reason:
            return GateResult(verdict="rejected", reasons=[f"sec: {reason}"])

        if len(cases) < self.config.min_cases:
            return GateResult(
                verdict="staged",
                reasons=[
                    f"unverified: {len(cases)} bench case(s) < min {self.config.min_cases}"
                ],
            )

        dev = [c for c in cases if not c.is_holdout(self.config.holdout_ratio)]
        holdout = [c for c in cases if c.is_holdout(self.config.holdout_ratio)]
        if not dev or not holdout:
            return GateResult(verdict="staged",
                              reasons=["unverified: degenerate dev/holdout split"])

        def mean(cs: Sequence[BenchCase], skill_def: SkillDef | None) -> float:
            return sum(self._score(c, skill_def) for c in cs) / len(cs)

        baseline_dev = mean(dev, None)
        baseline_holdout = mean(holdout, None)
        dev_score = mean(dev, skill)
        holdout_score = mean(holdout, skill)

        dev_gain = dev_score - baseline_dev
        holdout_gain = holdout_score - baseline_holdout

        dev_up = dev_gain > self.config.min_gain
        held_ok = holdout_gain >= -self.config.max_holdout_drop
        overopt = (dev_gain - holdout_gain) > self.config.overopt_gap

        reasons = [
            f"dev {baseline_dev:.3f}->{dev_score:.3f} ({dev_gain:+.3f})",
            f"holdout {baseline_holdout:.3f}->{holdout_score:.3f} ({holdout_gain:+.3f})",
        ]
        if dev_up and held_ok and not overopt:
            verdict = "promoted"
        elif not dev_up:
            verdict = "rejected"
            reasons.append("no dev gain — the skill does not help")
        elif not held_ok:
            verdict = "rejected"
            reasons.append("holdout regressed — memorization, not knowledge")
        else:
            verdict = "rejected"
            reasons.append("over-optimization gap — dev gain does not generalize")
        return GateResult(
            verdict=verdict,
            reasons=reasons,
            dev_score=dev_score,
            holdout_score=holdout_score,
            baseline_dev=baseline_dev,
            baseline_holdout=baseline_holdout,
        )
