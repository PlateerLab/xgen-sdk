"""Self-Forging loop over a typed HarnessConfig (Goodhart-defended).

Per step: evaluate the objective → reflect(traces) for typed candidates (+ optional
synthesis / registered-reflector candidates) → cross-check validator → promote ONLY
if the proxy (dev J) improves AND the frozen held-out doesn't regress AND a second
judge-independent metric doesn't regress (overoptimization gate) → else rollback →
early-stop at the held-out peak → audit. Writes only config knobs; engine code, the
benchmark, and criteria semantics stay a locked surface (FORGE §2).

Back-compat: run(config, bench_list) wraps the list in Objective.from_bench
(dev == held-out), reproducing the pre-defense hill-climb exactly.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from .algebra import EngineAlgebra, Move
from .objective import Objective
from .reflect import Reflection, extra_candidates, reflect
from .runner import Runner
from .synthesis import synthesize

# Independent cross-check (second opinion): which (op, target) a symptom maps to.
_SYMPTOM_EXPECTED: dict[str, tuple[str, str]] = {
    "ungated_low_quality": ("set_stage_param", "s08_decide:judge_enabled"),
    "accepted_borderline": ("tune_scalar", "validation_threshold"),
    "over_strict_stall": ("tune_scalar", "validation_threshold"),
    "regulation_violation": ("toggle_guard", "content"),
    "missing_criterion": ("edit_criterion", "regulation"),
    "no_recovery": ("tune_scalar", "max_retries"),
    "retry_waste": ("tune_scalar", "max_retries"),
    "low_judge_score": ("tune_scalar", "max_retries"),
    "iteration_pressure": ("tune_scalar", "max_iterations"),
}


@dataclass
class Commit:
    step: int
    reflection_id: str
    lesson: str
    move: str
    inverse: str
    bench_before: float           # held-out (true) J before
    bench_after: float            # held-out (true) J after
    delta: float
    validator_agreement: bool
    verdict: str                  # promoted | rolled_back
    overopt: bool = False         # proxy(dev) rose while held-out fell — Goodhart alarm


@dataclass
class ForgeResult:
    config: dict[str, Any]
    history: list[float]          # held-out (true) J trajectory
    commits: list[Commit] = field(default_factory=list)

    @property
    def initial_j(self) -> float:
        return self.history[0]

    @property
    def final_j(self) -> float:
        return self.history[-1]


def _validator_agrees(refl: Reflection, move: Move, algebra: EngineAlgebra) -> bool:
    """Cross-check (independent of the reflector): legal + addresses the dominant
    symptom. Built-in symptoms match an expected (op, target); externally registered
    symptoms fall back to a structural check (legal single-knob move)."""
    if not algebra.is_legal(move):
        return False
    expected = _SYMPTOM_EXPECTED.get(refl.dominant_symptom)
    if expected is None:
        return True
    return (move.op, move.target) == expected


class SelfForge:
    """Self-improvement loop: evolve a HarnessConfig from run traces against a
    Goodhart-defended objective, promoting improvements and rolling back regressions."""

    def __init__(self, runner: Runner, algebra: Optional[EngineAlgebra] = None,
                 max_steps: int = 12, audit_log: Optional[Union[str, Path]] = None,
                 epsilon: float = 0.0, patience: int = 2, enable_synthesis: bool = False) -> None:
        self.runner = runner
        self.algebra = algebra or EngineAlgebra()
        self.max_steps = max_steps
        self.audit_log = Path(audit_log) if audit_log else None
        self.epsilon = epsilon
        self.patience = patience
        self.enable_synthesis = enable_synthesis

    def run(self, base_config: dict[str, Any],
            bench_or_objective: Union[list[dict[str, Any]], Objective]) -> ForgeResult:
        obj = (bench_or_objective if isinstance(bench_or_objective, Objective)
               else Objective.from_bench(self.runner, bench_or_objective))
        config = dict(base_config)
        score = obj.evaluate(config)
        result = ForgeResult(config=config, history=[score.heldout])
        best_heldout, best_sec, no_improve = score.heldout, score.secondary, 0

        for step in range(self.max_steps):
            refl = reflect(score.records)
            candidates: list[tuple[Move, Optional[str], str]] = []
            if refl:
                for mv in refl.candidates:
                    if _validator_agrees(refl, mv, self.algebra):
                        candidates.append((mv, refl.dominant_symptom, refl.lesson))
            for mv in self._extra(score.records, config):     # opt-in synthesis / reflectors
                if self.algebra.is_legal(mv) and not any(mv == c[0] for c in candidates):
                    candidates.append((mv, "synthesis", "synthesized/registered primitive proposal"))
            if not candidates:
                break

            progressed = False
            for move, sym, lesson in candidates:
                inv = self.algebra.inverse(config, move)
                nsc = obj.evaluate(self.algebra.apply(config, move))
                dev_up = nsc.dev > score.dev + 1e-9
                held_ok = nsc.heldout >= score.heldout - self.epsilon
                sec_ok = (nsc.secondary is None or best_sec is None or nsc.secondary >= best_sec - 1e-9)
                overopt = nsc.dev > score.dev and nsc.heldout < score.heldout
                promote = dev_up and held_ok and sec_ok and not overopt
                result.commits.append(Commit(
                    step=step, reflection_id=f"r{step:02d}:{sym}", lesson=lesson,
                    move=str(move), inverse=str(inv),
                    bench_before=score.heldout, bench_after=nsc.heldout,
                    delta=round(nsc.heldout - score.heldout, 4),
                    validator_agreement=True, verdict="promoted" if promote else "rolled_back",
                    overopt=overopt,
                ))
                if promote:
                    config = self.algebra.apply(config, move)
                    score = nsc
                    best_heldout = max(best_heldout, nsc.heldout)
                    best_sec = nsc.secondary if nsc.secondary is not None else best_sec
                    result.config = config
                    result.history.append(nsc.heldout)
                    progressed = True
                    break

            no_improve = 0 if progressed else no_improve + 1
            if no_improve >= self.patience:           # early-stop at the held-out peak
                break

        if self.audit_log:
            self._write_log(result.commits)
        return result

    def _extra(self, traces: list, config: dict) -> list[Move]:
        moves = list(extra_candidates(traces))        # registered reflectors (none by default)
        if self.enable_synthesis:
            moves += synthesize(traces, config, self.algebra)
        return moves

    def _write_log(self, commits: list[Commit]) -> None:
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_log.open("w", encoding="utf-8") as f:
            for c in commits:
                f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")


def forge_config(runner: Runner, base_config: dict[str, Any],
                 bench_or_objective: Union[list[dict[str, Any]], Objective],
                 *, max_steps: int = 12, audit_log: Optional[Union[str, Path]] = None,
                 enable_synthesis: bool = False) -> ForgeResult:
    """Convenience: run one self-forging pass and return the evolved config."""
    return SelfForge(runner, max_steps=max_steps, audit_log=audit_log,
                     enable_synthesis=enable_synthesis).run(base_config, bench_or_objective)
