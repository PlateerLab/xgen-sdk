"""Deterministic replay bench — the weak-model equalizer.

The gate's verdict must not depend on model intelligence, so the bench judges
with code, not an LLM: each ReplayCase carries machine-checkable expectations
(substrings, regexes, forbidden markers). A weak drafter's skill survives only
if replaying real historic cases with the skill injected measurably helps.
Quality comes from selection pressure, not from the drafting model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .gate import BenchCase
from .model import SkillDef


@dataclass
class Expectation:
    require: list[str] = field(default_factory=list)   # substrings that must appear
    require_regex: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)    # markers that must not appear

    def score(self, output: str) -> float:
        checks: list[bool] = []
        low = output.lower()
        checks += [needle.lower() in low for needle in self.require]
        checks += [re.search(pattern, output, re.IGNORECASE) is not None
                   for pattern in self.require_regex]
        checks += [marker.lower() not in low for marker in self.forbid]
        if not checks:
            return 0.0
        return sum(checks) / len(checks)


@dataclass
class ReplayCase:
    """One historic interaction worth re-running (built from a ReproBundle)."""

    case_id: str
    payload: dict
    expect: Expectation

    def as_bench_case(self) -> BenchCase:
        return BenchCase(case_id=self.case_id, payload=self.payload)


RunFn = Callable[[dict, SkillDef | None], str]
"""(case payload, candidate skill or None) -> the run's final output text.
Hosts back this with a real pipeline run (skill injected via loaded_skills);
tests back it with a scripted function."""


class ReproReplayRunner:
    """BenchRunner over replay cases. Deterministic given a deterministic RunFn:
    zero LLM-judge dependence, so verification quality is model-independent."""

    def __init__(self, run_fn: RunFn, cases: list[ReplayCase]) -> None:
        self.run_fn = run_fn
        self._by_id = {case.case_id: case for case in cases}

    def bench_cases(self) -> list[BenchCase]:
        return [case.as_bench_case() for case in self._by_id.values()]

    def score(self, case: BenchCase, skill: SkillDef | None) -> float:
        replay = self._by_id.get(case.case_id)
        if replay is None:
            return 0.0
        try:
            output = self.run_fn(replay.payload, skill)
        except Exception:
            return 0.0
        return replay.expect.score(output)


_ERROR_MARKER = re.compile(r"\b([45]\d\d)\b|\b(timeout|refused|denied|not found|failed)\b",
                           re.IGNORECASE)


def capture_repro_rows(trace) -> list[dict]:
    """Auto-capture replay rows from a finished trace's error->recovery pairs.

    Conservative heuristic: forbid the error's distinctive marker (status code
    or failure word), require the recovery detail's distinctive tokens. Rows
    are tagged auto_captured so hosts can review before trusting them as gate
    evidence. Returns [] when the trace has no usable error/recovery signal."""
    rows: list[dict] = []
    recovery_details = [e.detail for e in trace.events
                        if e.type == "recovery" and e.detail]
    for index, event in enumerate(trace.events):
        if event.type != "error" or not event.detail:
            continue
        marker = _ERROR_MARKER.search(event.detail)
        if not marker:
            continue
        require = []
        if recovery_details:
            tokens = re.findall(r"[a-zA-Z0-9?=_-]{4,}", recovery_details[0])
            require = tokens[:2]
        rows.append({
            "case_id": f"{trace.run_id}-repro-{index}",
            "payload": {"error_detail": event.detail, "tool": event.name,
                        "run_id": trace.run_id},
            "forbid": [marker.group(0)],
            "require": require,
            "auto_captured": True,
        })
    return rows


def cases_from_repro_rows(rows: list[dict]) -> list[ReplayCase]:
    """Build replay cases from persisted repro rows. Expected row shape:
    {case_id, payload, require?, require_regex?, forbid?} — spine `repro`
    entries and ReproBundle exports both map onto this."""
    cases = []
    for row in rows:
        cases.append(
            ReplayCase(
                case_id=str(row["case_id"]),
                payload=dict(row.get("payload", {})),
                expect=Expectation(
                    require=[str(s) for s in row.get("require", [])],
                    require_regex=[str(s) for s in row.get("require_regex", [])],
                    forbid=[str(s) for s in row.get("forbid", [])],
                ),
            )
        )
    return cases
