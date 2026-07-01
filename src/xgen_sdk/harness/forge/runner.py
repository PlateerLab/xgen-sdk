"""Runner — config + task -> RunRecord. One contract; mock or real engine.

- SyntheticRunner: offline, provider-free. Scores a HarnessConfig by distance to
  a hidden healthy profile and emits honest trace *signals* (symptoms). Used to
  exercise the loop/algebra/brake without an LLM. Reads REAL engine config paths.
- PipelineRunner: drives the real engine (Pipeline.from_config(...).run(state)),
  score = state.validation_score, signals derived from the final state. Wrapped
  so a broken config yields a failure record rather than crashing the loop.
- FakeProvider: a minimal deterministic LLMProvider so PipelineRunner can run
  end-to-end offline (no API key).
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional, Protocol

from ..providers.base import LLMProvider, ProviderEvent, ProviderEventType


@dataclass
class RunRecord:
    task_id: str
    score: float                       # [0,1]
    outcome: str                       # success | partial | failure
    signals: dict[str, float] = field(default_factory=dict)
    error: str = ""
    feedback: str = ""                 # natural-language trace feedback (GEPA-style reflectors)


class Runner(Protocol):
    def run(self, config: dict[str, Any], task: dict[str, Any]) -> RunRecord: ...


# ---- config-path helpers (verified against core/config.py + s05/s08 stages) ----

def _judge_enabled(config: dict) -> bool:
    sp = config.get("stage_params", {}).get("s08_decide", {})
    active = (config.get("active_strategies") or {}).get("s08_decide")
    return bool(sp.get("judge_enabled")) or active == "judge_then_loop"


def _guard_names(config: dict) -> set[str]:
    guards = config.get("stage_params", {}).get("s05_policy", {}).get("guards", []) or []
    return {g.get("name") for g in guards if isinstance(g, dict)}


def _criteria_names(config: dict) -> set[str]:
    defs = config.get("stage_params", {}).get("s08_decide", {}).get("criteria_defs", []) or []
    return {c.get("name") for c in defs if isinstance(c, dict)}


def _seeded_noise(task_id: str, magnitude: float = 0.02) -> float:
    h = int(hashlib.sha1(task_id.encode()).hexdigest()[:8], 16)
    return ((h % 1000) / 1000.0 - 0.5) * 2 * magnitude


class SyntheticRunner:
    """Offline scorer. The healthy profile is what the loop must discover."""

    def run(self, config: dict[str, Any], task: dict[str, Any]) -> RunRecord:
        score = 1.0
        sig: dict[str, float] = {}
        regulated = task.get("regulated", False)

        if not _judge_enabled(config):
            score -= 0.30
            sig["ungated_low_quality"] = 1.0

        thr = config.get("validation_threshold")
        thr = 0.5 if thr is None else thr
        if thr < 0.7:
            score -= 0.18 * (0.7 - thr) / 0.2
            sig["accepted_borderline"] = (0.7 - thr) / 0.2
        elif thr > 0.85:
            score -= 0.25 * (thr - 0.85) / 0.05       # over-strict is genuinely worse than mild borderline
            sig["over_strict_stall"] = (thr - 0.85) / 0.05

        if regulated and "content" not in _guard_names(config):
            score -= 0.25
            sig["regulation_violation"] = 1.0
        if regulated and "regulation" not in _criteria_names(config):
            score -= 0.07
            sig["missing_criterion"] = 1.0

        retries = config.get("max_retries")
        retries = 1 if retries is None else retries
        if retries == 0:
            score -= 0.10
            sig["no_recovery"] = 1.0
        elif retries > 2:
            score -= 0.05
            sig["retry_waste"] = retries - 2

        score = max(0.0, min(1.0, score + _seeded_noise(task["id"])))
        outcome = "success" if score >= 0.85 else "partial" if score >= 0.6 else "failure"
        return RunRecord(task["id"], round(score, 4), outcome, sig)


class FakeProvider(LLMProvider):
    """Deterministic offline provider. Optionally returns a judge JSON so the
    s08 evaluation parses a score without a live API."""

    def __init__(self, text: str = "Answer.", judge_score: Optional[float] = None) -> None:
        self._text = text
        self._judge = judge_score

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-1"

    def supports_tool_use(self) -> bool:
        return False

    def supports_thinking(self) -> bool:
        return False

    async def chat(self, messages, system=None, tools=None, temperature=0.7,
                   max_tokens=8192, stream=True, thinking=None,
                   tool_choice=None, response_format=None) -> AsyncGenerator[ProviderEvent, None]:
        blob = f"{system or ''} {messages}"
        is_eval = self._judge is not None and ("criteria" in blob.lower() or "overall" in blob.lower())
        text = (f'{{"overall": {self._judge}, "scores": {{}}, "feedback": "ok"}}'
                if is_eval else self._text)
        yield ProviderEvent(type=ProviderEventType.TEXT_DELTA, text=text)
        yield ProviderEvent(type=ProviderEventType.USAGE, input_tokens=10, output_tokens=5)
        yield ProviderEvent(type=ProviderEventType.STOP, stop_reason="end_turn")


class PipelineRunner:
    """Drives the real engine. A config that errors yields a failure record."""

    def __init__(self, provider: Optional[LLMProvider] = None) -> None:
        self.provider = provider or FakeProvider(judge_score=0.9)

    def run(self, config: dict[str, Any], task: dict[str, Any]) -> RunRecord:
        return asyncio.run(self._arun(config, task))

    async def _arun(self, config: dict[str, Any], task: dict[str, Any]) -> RunRecord:
        from ..core.config import HarnessConfig
        from ..core.pipeline import Pipeline
        from ..core.state import PipelineState
        try:
            cfg = HarnessConfig.from_dict({**config, "provider": self.provider.provider_name})
            pipeline = Pipeline.from_config(cfg, provider=self.provider)
            state = PipelineState(user_input=task.get("input", ""))
            result = await pipeline.run(state)
        except Exception as exc:                       # broken config -> failure record
            return RunRecord(task["id"], 0.0, "failure", {"crash": 1.0}, error=f"{type(exc).__name__}: {exc}")

        from .signals import extract_signals
        sig = extract_signals(result, config)        # data-derived, extensible (no hardcode)
        score = result.validation_score
        if score is None:
            score = 0.5 if (result.final_output or "").strip() else 0.0
        outcome = "success" if score >= 0.85 else "partial" if score >= 0.6 else "failure"
        feedback = getattr(result, "validation_feedback", None) or ""   # for GEPA-style reflectors
        return RunRecord(task["id"], round(float(score), 4), outcome, sig, feedback=feedback)
