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
    require_holdout_gain: bool = True
    """A dev-only gain is not evidence of a reusable skill — it is evidence of
    fitting the cases the skill was written from. Verified promotion therefore
    requires the gain to reproduce on held-out cases. When dev improves but
    holdout stays flat the verdict is `staged` (unproven, human may approve),
    not `promoted` and not `rejected`."""


class ForgeGate:
    def __init__(self, runner: BenchRunner | ScoreFn,
                 config: GateConfig | None = None,
                 constitution=None) -> None:
        self._score: ScoreFn = runner.score if hasattr(runner, "score") else runner  # type: ignore[union-attr]
        self.config = config or GateConfig()
        # 규약(constitution.py). Hermes 는 "배우지 말 것"을 프롬프트 문장으로 두지만
        # 문장은 모델이 지키면 지켜지고 안 지키면 안 지켜진다 — 여기서 집행한다.
        self.constitution = constitution

    def verify(self, candidate: SkillCandidate, skill: SkillDef,
               cases: Sequence[BenchCase]) -> GateResult:
        reason = safety_check(candidate)
        if reason:
            return GateResult(verdict="rejected", reasons=[f"sec: {reason}"])

        if self.constitution is not None:
            violation = self.constitution.check_candidate(candidate)
            if violation:
                # 벤치를 돌려보기 전에 막는다 — 배우면 안 되는 것은 성능이 좋아도 안 된다.
                return GateResult(verdict="rejected", reasons=[violation])

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
            if self.config.require_holdout_gain and holdout_gain <= 0:
                verdict = "staged"
                reasons.append("dev gain did not reproduce on held-out cases — "
                               "unproven, not auto-verified")
            else:
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
