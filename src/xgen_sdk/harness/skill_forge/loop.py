"""The Skill-Forge orchestrator: observe -> curate -> synthesize -> verify -> commit.

Pure and synchronous; hosts decide scheduling (post-run async job, batch cron).
Every stage's output is returned in the report so the host can journal the
entire episode to the spine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .curator import Curator, Rejection
from .gate import BenchCase, ForgeGate
from .ledger import SkillLedger
from .model import GateResult, RunTrace, SkillCandidate, SkillDef
from .signals import SignalHit, extract_signals
from .synthesis import Reflector, synthesize


@dataclass
class ApprovalPolicy:
    """Scope-aware activation policy. Verified user-scope skills may go live
    automatically; broader scopes stage for human approval by default."""

    auto_activate_scopes: tuple[str, ...] = ("session", "user")

    def on_promoted(self, skill: SkillDef) -> str:
        return "active" if skill.scope in self.auto_activate_scopes else "staged"


@dataclass
class ForgeEpisode:
    run_id: str
    signals: list[SignalHit] = field(default_factory=list)
    drafted: list[SkillCandidate] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    results: list[tuple[SkillDef, GateResult]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"run={self.run_id} signals={len(self.signals)} "
                 f"drafted={len(self.drafted)} curator_rejects={len(self.rejected)}"]
        for skill, gate in self.results:
            lines.append(f"  {skill.name} [{skill.kind}/{skill.scope}] -> "
                         f"{gate.verdict} ({'; '.join(gate.reasons)})")
        return "\n".join(lines)


class SkillForge:
    def __init__(self, ledger: SkillLedger, gate: ForgeGate,
                 curator: Curator | None = None,
                 reflector: Reflector | None = None,
                 approval: ApprovalPolicy | None = None) -> None:
        self.ledger = ledger
        self.gate = gate
        self.curator = curator or Curator(ledger)
        self.reflector = reflector
        self.approval = approval or ApprovalPolicy()

    def process_trace(self, trace: RunTrace,
                      bench_cases: Sequence[BenchCase] = (),
                      prior_signatures: dict[str, int] | None = None,
                      drafted: list[SkillCandidate] | None = None) -> ForgeEpisode:
        episode = ForgeEpisode(run_id=trace.run_id)
        episode.signals = extract_signals(trace, prior_signatures=prior_signatures)
        if not episode.signals and drafted is None:
            return episode

        candidates = drafted if drafted is not None else self.curator.draft_from_signals(
            trace, episode.signals)
        episode.drafted = candidates

        curation = self.curator.curate(candidates)
        episode.rejected = curation.rejected

        for candidate in curation.accepted:
            skill = self._synthesize_resolved(candidate)
            gate_result = self.gate.verify(candidate, skill, bench_cases)
            if gate_result.verdict == "promoted":
                skill.verified = True
                skill.status = self.approval.on_promoted(skill)
                self.ledger.commit(skill, note=f"promoted: {gate_result.reasons[0]}")
            elif gate_result.verdict == "staged":
                existing = self.ledger.get(skill.name)
                if (existing is not None and existing.skill.verified
                        and existing.status == "active"):
                    # never let an unverified redraft downgrade a verified
                    # active skill — keep the proven version untouched
                    gate_result.reasons.append(
                        "kept existing verified active version; unverified draft dropped")
                else:
                    skill.verified = False
                    skill.status = "staged"
                    self.ledger.commit(
                        skill, note=f"staged: {'; '.join(gate_result.reasons)}")
            # rejected candidates are journaled in the episode, not the ledger
            episode.results.append((skill, gate_result))
        return episode

    def _synthesize_resolved(self, candidate: SkillCandidate) -> SkillDef:
        if candidate.action == "patch":
            target = self.ledger.get(candidate.target_skill)
            if target is not None:
                merged = candidate
                merged.name = target.name
                skill = synthesize(merged, reflector=self.reflector)
                skill.meta["patched_from"] = f"{target.name}@{target.skill.version}"
                return skill
        return synthesize(candidate, reflector=self.reflector)
