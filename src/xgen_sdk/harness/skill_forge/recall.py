"""SF5 recall side — progressive disclosure over the ledger.

Mirrors the engine skill_registry contract (name -> short description, body on
demand) so absorption is a thin `LedgerSkillSource` added to
tools/skill_registry.py. Two-step PD: list (metadata only) -> view (body).

Discipline: viewing/loading is NEVER recorded as a positive signal. Hosts call
`record_run_outcome` once per finished run with the set of skills that were
loaded and whether the run succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import SkillLedger


@dataclass
class SkillListing:
    name: str
    kind: str
    description: str
    version: str
    verified: bool
    rank: float


class LedgerSkillSource:
    def __init__(self, ledger: SkillLedger, scope: str | None = None,
                 include_staged: bool = False) -> None:
        self.ledger = ledger
        self.scope = scope
        self.include_staged = include_staged

    def list(self) -> list[SkillListing]:
        listings: list[SkillListing] = []
        statuses = ("active", "staged") if self.include_staged else ("active",)
        for status in statuses:
            for record in self.ledger.list(scope=self.scope, status=status):
                listings.append(
                    SkillListing(
                        name=record.name,
                        kind=record.skill.kind,
                        description=record.description,
                        version=record.skill.version,
                        verified=record.skill.verified,
                        rank=record.rank_score(),
                    )
                )
        return sorted(listings, key=lambda item: (-item.rank, item.name))

    def view(self, name: str) -> str:
        """Raw body only — this feeds the prompt-injection path (skill_registry
        get_skill_body), so no annotation header may precede the frontmatter."""
        record = self.ledger.get(name)
        if record is None:
            raise KeyError(f"unknown skill {name!r}")
        return record.skill.body

    def view_annotated(self, name: str) -> str:
        """Human-facing variant with the verification label (console use)."""
        record = self.ledger.get(name)
        if record is None:
            raise KeyError(f"unknown skill {name!r}")
        label = "verified" if record.skill.verified else "UNVERIFIED"
        header = f"[{label}] {record.name} v{record.skill.version} ({record.skill.kind})\n"
        return header + record.skill.body

    def render_index(self, limit: int = 20) -> str:
        """Compact index for prompt injection (s03) — metadata only."""
        lines = []
        for item in self.list()[:limit]:
            flag = "✔" if item.verified else "•"
            lines.append(f"{flag} {item.name} v{item.version} — {item.description}")
        return "\n".join(lines)

    def record_run_outcome(self, loaded_skills: list[str], success: bool) -> None:
        self.ledger.record_outcome(loaded_skills, success)
