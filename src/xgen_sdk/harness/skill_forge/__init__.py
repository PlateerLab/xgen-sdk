"""xgen-skill-forge — verified memory-to-skill promotion loop.

SF1 signals -> SF2 curator -> SF3 synthesis -> SF4 forge gate -> SF5 ledger/recall.
Design: D:\\harness-engineering\\SKILL-FORGE-PLAN.md
"""

from .bench import Expectation, ReplayCase, ReproReplayRunner, cases_from_repro_rows
from .curator import Curator, CurationResult, Rejection
from .drafter import (
    EnsembleDrafter,
    LLMDrafter,
    anthropic_completer,
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
from .model import (
    GateResult,
    Provenance,
    RunTrace,
    SkillCandidate,
    SkillDef,
    TraceEvent,
    UsageStats,
)
from .recall import LedgerSkillSource, SkillListing
from .signals import SIGNAL_EXTRACTORS, SignalHit, extract_signals
from .synthesis import SYNTHESIZERS, synthesize

__version__ = "0.1.0"

__all__ = [
    "ApprovalPolicy",
    "BenchCase",
    "BenchRunner",
    "Curator",
    "CurationResult",
    "EnsembleDrafter",
    "Expectation",
    "ForgeEpisode",
    "ForgeGate",
    "GateConfig",
    "GateResult",
    "InMemorySkillLedger",
    "InMemorySpineStore",
    "JsonlSkillLedger",
    "LLMDrafter",
    "LedgerSkillSource",
    "Provenance",
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
    "cases_from_repro_rows",
    "extract_signals",
    "openai_chat_completer",
    "signature_counts",
    "synthesize",
    "trace_from_spine",
]
