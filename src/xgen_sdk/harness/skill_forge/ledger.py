"""SF5 — the skill ledger: versioned, provenance-carrying skill records.

Protocol + two reference backends (in-memory, JSONL append-only). The XGEN
host maps this onto the State Spine (`spine_type=skill_def`) — same shape:
every mutation is an append with version + provenance, state is a fold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .model import (
    SkillDef,
    UsageStats,
    bump_patch,
)


@dataclass
class SkillRecord:
    skill: SkillDef
    usage: UsageStats = field(default_factory=UsageStats)
    history: list[str] = field(default_factory=list)  # "<version>: <note>"

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def status(self) -> str:
        return self.skill.status

    @property
    def description(self) -> str:
        return self.skill.description

    def rank_score(self) -> float:
        """Recall ordering: verified beats unverified, then Wilson lower bound.
        Loading is never counted — only run outcomes feed `usage`."""
        base = 0.5 if self.skill.verified else 0.0
        return base + self.usage.wilson_lower()


class SkillLedger(Protocol):
    def get(self, name: str) -> SkillRecord | None: ...
    def list(self, scope: str | None = None,
             status: str | None = None) -> list[SkillRecord]: ...
    def commit(self, skill: SkillDef, note: str = "") -> SkillRecord: ...
    def set_status(self, name: str, status: str, note: str = "") -> SkillRecord: ...
    def record_outcome(self, names: Iterable[str], success: bool) -> None: ...


class InMemorySkillLedger:
    def __init__(self) -> None:
        self._records: dict[str, SkillRecord] = {}

    def get(self, name: str) -> SkillRecord | None:
        return self._records.get(name)

    def list(self, scope: str | None = None,
             status: str | None = None) -> list[SkillRecord]:
        records = self._records.values()
        if scope is not None:
            records = [r for r in records if r.skill.scope == scope]
        if status is not None:
            records = [r for r in records if r.status == status]
        return sorted(records, key=lambda r: (-r.rank_score(), r.name))

    def commit(self, skill: SkillDef, note: str = "") -> SkillRecord:
        existing = self._records.get(skill.name)
        if existing is not None:
            prior = existing.skill
            skill.version = bump_patch(prior.version)
            skill.supersedes = f"{prior.name}@{prior.version}"
            existing.skill = skill
            existing.history.append(f"{skill.version}: {note or 'update'}")
            return existing
        record = SkillRecord(skill=skill, history=[f"{skill.version}: {note or 'create'}"])
        self._records[skill.name] = record
        return record

    def set_status(self, name: str, status: str, note: str = "") -> SkillRecord:
        record = self._records[name]
        record.skill.status = status
        record.history.append(f"{record.skill.version}: status={status} {note}".rstrip())
        return record

    def record_outcome(self, names: Iterable[str], success: bool) -> None:
        for name in names:
            record = self._records.get(name)
            if record is None:
                continue
            if success:
                record.usage.successes += 1
            else:
                record.usage.failures += 1

    def sweep_deprecate(self, min_uses: int = 5, max_wilson: float = 0.2) -> list[str]:
        """Low-signal prune (4-scope routine #5): enough real outcomes, and the
        Wilson lower bound still under the floor -> deprecate, never delete."""
        deprecated = []
        for record in self._records.values():
            if record.status != "active":
                continue
            if record.usage.total >= min_uses and record.usage.wilson_lower() <= max_wilson:
                self.set_status(record.name, "deprecated", "low-signal sweep")
                deprecated.append(record.name)
        return deprecated


class JsonlSkillLedger(InMemorySkillLedger):
    """Append-only JSONL journal + in-memory fold. Crash-safe enough for the
    research phase; the production backend is the State Spine."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            self._replay()

    def _append(self, kind: str, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, **payload}, ensure_ascii=False) + "\n")

    def _replay(self) -> None:
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                kind = event.pop("kind")
                if kind == "commit":
                    data = event["skill"]
                    provenance = data.pop("provenance", None)
                    skill = SkillDef(**{**data, "provenance": None})
                    if provenance:
                        from .model import Provenance
                        skill.provenance = Provenance(**provenance)
                    super().commit(skill, note=event.get("note", ""))
                    # replay keeps journal authority over derived version fields
                    record = super().get(skill.name)
                    if record is not None:
                        record.skill.version = data.get("version", record.skill.version)
                elif kind == "status":
                    super().set_status(event["name"], event["status"], event.get("note", ""))
                elif kind == "outcome":
                    super().record_outcome([event["name"]], event["success"])

    def commit(self, skill: SkillDef, note: str = "") -> SkillRecord:
        record = super().commit(skill, note)
        self._append("commit", {"skill": record.skill.to_dict(), "note": note})
        return record

    def set_status(self, name: str, status: str, note: str = "") -> SkillRecord:
        record = super().set_status(name, status, note)
        self._append("status", {"name": name, "status": status, "note": note})
        return record

    def record_outcome(self, names: Iterable[str], success: bool) -> None:
        names = list(names)
        super().record_outcome(names, success)
        for name in names:
            if super().get(name) is not None:
                self._append("outcome", {"name": name, "success": success})
