"""xgen-skill-forge — verified memory-to-skill promotion loop.

SF1 signals -> SF2 curator -> SF3 synthesis -> SF4 forge gate -> SF5 ledger/recall.
Design: D:\\harness-engineering\\SKILL-FORGE-PLAN.md
"""

from .agent import ContextPack, CycleReport, JermesAgent, RecalledSkill
from .bench import Expectation, ReplayCase, ReproReplayRunner, cases_from_repro_rows
from .constitution import Constitution
from .curator import Curator, CurationResult, Rejection
from .drafter import (
    EnsembleDrafter,
    LLMDrafter,
    anthropic_completer,
    failover_completer,
    openai_chat_completer,
)
from .gate import BenchCase, BenchRunner, ForgeGate, GateConfig
from .host import (
    InMemorySpineStore,
    SpineSkillLedger,
    SpineStore,
    signature_counts,
    trace_from_spine,
)
from .ledger import InMemorySkillLedger, JsonlSkillLedger, SkillLedger, SkillRecord
from .loop import ApprovalPolicy, ForgeEpisode, SkillForge
from .memory import (
    Contradiction,
    MemoryItem,
    MemoryPolicy,
    detect_contradictions,
    measure,
    resolve,
)
from .model import (
    GateResult,
    Provenance,
    RunTrace,
    SkillCandidate,
    SkillDef,
    TraceEvent,
    UsageStats,
)
from .portable import (
    candidate_from_skill_md,
    skill_package,
    to_skill_md,
    validate_skill_md,
)
from .recall import LedgerSkillSource, SkillListing
from .signals import SIGNAL_EXTRACTORS, SignalHit, extract_signals
from .synthesis import SYNTHESIZERS, synthesize

__version__ = "0.1.0"

__all__ = [
    "ApprovalPolicy",
    "BenchCase",
    "BenchRunner",
    "Constitution",
    "ContextPack",
    "Contradiction",
    "CurationResult",
    "Curator",
    "CycleReport",
    "EnsembleDrafter",
    "Expectation",
    "ForgeEpisode",
    "ForgeGate",
    "GateConfig",
    "GateResult",
    "InMemorySkillLedger",
    "InMemorySpineStore",
    "JermesAgent",
    "JsonlSkillLedger",
    "LLMDrafter",
    "LedgerSkillSource",
    "MemoryItem",
    "MemoryPolicy",
    "Provenance",
    "RecalledSkill",
    "Rejection",
    "ReplayCase",
    "ReproReplayRunner",
    "RunTrace",
    "SIGNAL_EXTRACTORS",
    "SYNTHESIZERS",
    "SignalHit",
    "SkillCandidate",
    "SkillDef",
    "SkillForge",
    "SkillLedger",
    "SkillListing",
    "SkillRecord",
    "SpineSkillLedger",
    "SpineStore",
    "TraceEvent",
    "UsageStats",
    "anthropic_completer",
    "candidate_from_skill_md",
    "cases_from_repro_rows",
    "detect_contradictions",
    "extract_signals",
    "measure",
    "openai_chat_completer",
    "resolve",
    "signature_counts",
    "skill_package",
    "synthesize",
    "to_skill_md",
    "trace_from_spine",
    "validate_skill_md",
]
