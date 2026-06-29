"""Self-Forge — opt-in self-improvement loop over a typed HarnessConfig.

The harness tunes its OWN configuration — which registered strategy / guard /
criterion / scalar each stage uses — from its own run traces, validated by a
Goodhart-defended objective (optimize on dev, gate on a frozen held-out, track a
second judge-independent metric, early-stop at the held-out peak), promoting
improvements and rolling back regressions.

Provider-agnostic and domain-blind: the legal-move set is discovered from the
engine's own registries (substitution algebra); signal extraction, reflection,
synthesis, and secondary metrics are all extensible registries (+ entry_points),
never hardcoded; the loop only writes config knobs (engine code, benchmark, and
criteria semantics are a locked surface). Not imported by `import xgen_harness` —
opt in with `from xgen_harness.forge import SelfForge`.

See spec: forge-engineering/spec/CONFIG-FORGE.md.
"""
from __future__ import annotations

from .algebra import EngineAlgebra, Move, register_tunable_scalar, tunable_scalars
from .gepa import GepaReflector
from .loop import Commit, ForgeResult, SelfForge, forge_config
from .objective import Objective, Score, register_secondary_metric
from .reflect import Reflection, extra_candidates, reflect, register_reflector, register_symptom_fix
from .runner import FakeProvider, PipelineRunner, RunRecord, Runner, SyntheticRunner
from .signals import extract_signals, register_signal_extractor
from .synthesis import register_synthesizer, synthesize

__all__ = [
    "EngineAlgebra",
    "Move",
    "register_tunable_scalar",
    "tunable_scalars",
    "Reflection",
    "reflect",
    "register_symptom_fix",
    "register_reflector",
    "GepaReflector",
    "extra_candidates",
    "extract_signals",
    "register_signal_extractor",
    "Objective",
    "Score",
    "register_secondary_metric",
    "synthesize",
    "register_synthesizer",
    "Runner",
    "RunRecord",
    "PipelineRunner",
    "SyntheticRunner",
    "FakeProvider",
    "SelfForge",
    "ForgeResult",
    "Commit",
    "forge_config",
]
