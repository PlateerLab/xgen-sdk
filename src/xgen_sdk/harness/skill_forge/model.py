"""Core data model for the Skill-Forge loop.

Everything is a plain dataclass with dict round-trip so hosts can persist
records in any store (State Spine, SQL, files) without importing this package
at the storage layer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

SKILL_KINDS = ("guide", "config", "tool")
SKILL_SCOPES = ("session", "workflow", "user", "platform")
CANDIDATE_ACTIONS = ("create", "patch")
GATE_VERDICTS = ("promoted", "staged", "rejected")
SKILL_STATUSES = ("staged", "active", "deprecated", "rejected")


def _require(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"{label} must be one of {allowed}, got {value!r}")
    return value


@dataclass
class TraceEvent:
    """One observed event of a finished run. Hosts map their own trace
    (spine activity, RunEvent, tool journal) into this neutral shape."""

    type: str  # tool_call | error | recovery | user_correction | outcome | note
    name: str = ""
    ok: bool = True
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunTrace:
    run_id: str
    scope: str = "user"
    scope_key: str = ""
    events: list[TraceEvent] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    refined_memory: str = ""
    judge_score: float | None = None
    success: bool = True

    def __post_init__(self) -> None:
        _require(self.scope, SKILL_SCOPES, "scope")

    def tool_calls(self) -> list[TraceEvent]:
        return [e for e in self.events if e.type == "tool_call"]

    def signature(self) -> str:
        """Stable signature of the tool sequence, for repetition detection."""
        seq = "|".join(e.name for e in self.tool_calls())
        return hashlib.sha256(seq.encode("utf-8")).hexdigest()[:16]


@dataclass
class Provenance:
    origin: str  # e.g. "background_curator", "manual", "import"
    source_run_ids: list[str] = field(default_factory=list)
    curator_id: str = ""
    signal: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "source_run_ids": list(self.source_run_ids),
            "curator_id": self.curator_id,
            "signal": self.signal,
        }


@dataclass
class SkillCandidate:
    """What the curator emits: not yet a skill, just an argued proposal."""

    name: str
    kind: str
    scope: str
    action: str  # create | patch
    rationale: str
    procedure: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    when_to_use: str = ""
    target_skill: str = ""  # set when action == "patch"
    provenance: Provenance | None = None
    payload: dict[str, Any] = field(default_factory=dict)  # kind-specific extras

    def __post_init__(self) -> None:
        _require(self.kind, SKILL_KINDS, "kind")
        _require(self.scope, SKILL_SCOPES, "scope")
        _require(self.action, CANDIDATE_ACTIONS, "action")
        if not _NAME_RE.match(self.name):
            raise ValueError(f"skill name must be kebab-case, got {self.name!r}")
        if self.action == "patch" and not self.target_skill:
            raise ValueError("patch candidate requires target_skill")


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


@dataclass
class SkillDef:
    """A synthesized skill artifact ready for gating / the ledger."""

    name: str
    kind: str
    scope: str
    description: str
    body: str  # guide: SKILL.md text | config: json text | tool: manifest text
    version: str = "0.1.0"
    status: str = "staged"
    provenance: Provenance | None = None
    supersedes: str = ""  # "<name>@<version>" lineage pointer
    verified: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(self.kind, SKILL_KINDS, "kind")
        _require(self.scope, SKILL_SCOPES, "scope")
        _require(self.status, SKILL_STATUSES, "status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "scope": self.scope,
            "description": self.description,
            "body": self.body,
            "version": self.version,
            "status": self.status,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "supersedes": self.supersedes,
            "verified": self.verified,
            "meta": dict(self.meta),
        }


@dataclass
class GateResult:
    verdict: str  # promoted | staged | rejected
    reasons: list[str] = field(default_factory=list)
    dev_score: float | None = None
    holdout_score: float | None = None
    baseline_dev: float | None = None
    baseline_holdout: float | None = None

    def __post_init__(self) -> None:
        _require(self.verdict, GATE_VERDICTS, "verdict")


@dataclass
class UsageStats:
    """Run-outcome-only feedback. Loading a skill is NOT a signal —
    only the outcome of a run that had it loaded counts (anti self-reinforcement,
    same discipline as forge/Synapse)."""

    successes: int = 0
    failures: int = 0

    @property
    def total(self) -> int:
        return self.successes + self.failures

    def wilson_lower(self, z: float = 1.96) -> float:
        """Wilson score lower bound of the success rate; 0.0 when unused."""
        n = self.total
        if n == 0:
            return 0.0
        p = self.successes / n
        denom = 1 + z * z / n
        centre = p + z * z / (2 * n)
        margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
        return max(0.0, (centre - margin) / denom)


def bump_patch(version: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    return f"{major}.{minor}.{patch + 1}"
