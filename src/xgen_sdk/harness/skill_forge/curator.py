"""SF2 — curation: turn raw signal hits into vetted skill candidates.

Three responsibilities, in order:
1. anti-learning filter  — refuse what must never become a skill (Hermes rules)
2. safety filter         — refuse injection / secret material (poisoning defense)
3. patch-over-create     — resolve against the ledger so knowledge accretes
                           instead of fragmenting

The LLM (if any) only *drafts* candidate content upstream; every draft passes
through these deterministic filters. That ordering is the point: curation is
code, not vibes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ledger import SkillLedger
from .model import Provenance, RunTrace, SkillCandidate
from .signals import SignalHit

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*\S{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
]

_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore (all )?(previous|prior|above) (instructions|rules)"),
    re.compile(r"(?i)(always|must) (run|execute|call)\b.*\b(curl|wget|nc|powershell|bash)\b"),
    re.compile(r"(?i)do not (tell|inform|alert) the (user|admin)"),
    re.compile(r"(?i)(exfiltrate|send|post) .*(http|ftp)s?://"),
    re.compile(r"(?i)disable (the )?(guard|policy|safety|approval)"),
]

_BROAD_NEGATIVE = re.compile(
    r"(?i)^(never|don'?t|do not|avoid) (use|call|try|trust)\b"
)

_TRANSIENT_HINTS = re.compile(
    r"(?i)(timeout|timed out|rate.?limit|429|503|connection (reset|refused)|"
    r"temporarily|transient|flaky|dns)"
)

_ENV_SPECIFIC_HINTS = re.compile(
    r"(?i)(not installed|command not found|no such file|missing (binary|package)|"
    r"only on (this|my) (machine|host)|c:\\users\\|/home/\w+/)"
)


@dataclass
class Rejection:
    candidate_name: str
    rule: str
    reason: str


@dataclass
class CurationResult:
    accepted: list[SkillCandidate] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)


def _candidate_text(candidate: SkillCandidate) -> str:
    parts = [candidate.rationale, candidate.when_to_use]
    parts += candidate.procedure + candidate.pitfalls + candidate.verification
    return "\n".join(p for p in parts if p)


def anti_learning_check(candidate: SkillCandidate) -> str | None:
    """Returns a rejection reason, or None when the candidate may proceed."""
    text = _candidate_text(candidate)
    if len(candidate.procedure) < 2 and candidate.action == "create":
        return "one-off narrative: fewer than 2 reusable procedure steps"
    if _TRANSIENT_HINTS.search(text) and not candidate.verification:
        return "transient failure with no verification recipe — likely self-resolving"
    if _ENV_SPECIFIC_HINTS.search(text):
        return "environment-specific detail — not portable knowledge"
    negatives = [s for s in candidate.procedure if _BROAD_NEGATIVE.match(s.strip())]
    if candidate.procedure and len(negatives) == len(candidate.procedure):
        return "broad negative claims only — bans are not procedures"
    return None


def safety_check(candidate: SkillCandidate) -> str | None:
    text = _candidate_text(candidate) + "\n" + str(candidate.payload)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return f"secret material matched {pattern.pattern!r}"
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return f"injection pattern matched {pattern.pattern!r}"
    return None


def _token_set(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9가-힣]{2,}", text.lower())}


def resolve_patch_over_create(candidate: SkillCandidate, ledger: SkillLedger,
                              overlap_threshold: float = 0.5) -> SkillCandidate:
    """If a live skill already covers this ground, convert `create` to `patch`."""
    if candidate.action == "patch":
        return candidate
    existing = ledger.get(candidate.name)
    if existing is not None:
        candidate.action = "patch"
        candidate.target_skill = existing.name
        return candidate
    cand_tokens = _token_set(candidate.rationale + " " + candidate.when_to_use)
    if not cand_tokens:
        return candidate
    best_name, best_overlap = "", 0.0
    for record in ledger.list(scope=candidate.scope):
        if record.status not in ("active", "staged"):
            continue
        rec_tokens = _token_set(record.description)
        if not rec_tokens:
            continue
        overlap = len(cand_tokens & rec_tokens) / min(len(cand_tokens), len(rec_tokens))
        if overlap > best_overlap:
            best_name, best_overlap = record.name, overlap
    if best_overlap >= overlap_threshold:
        candidate.action = "patch"
        candidate.target_skill = best_name
    return candidate


class Curator:
    def __init__(self, ledger: SkillLedger, curator_id: str = "background_curator") -> None:
        self.ledger = ledger
        self.curator_id = curator_id

    def curate(self, candidates: list[SkillCandidate]) -> CurationResult:
        result = CurationResult()
        for candidate in candidates:
            reason = anti_learning_check(candidate)
            if reason:
                result.rejected.append(Rejection(candidate.name, "anti_learning", reason))
                continue
            reason = safety_check(candidate)
            if reason:
                result.rejected.append(Rejection(candidate.name, "safety", reason))
                continue
            candidate = resolve_patch_over_create(candidate, self.ledger)
            result.accepted.append(candidate)
        return result

    def draft_from_signals(self, trace: RunTrace,
                           hits: list[SignalHit]) -> list[SkillCandidate]:
        """Deterministic fallback drafter (no LLM): one candidate per strong hit.
        Hosts normally replace this with an LLM drafter run inside a
        tool-whitelisted harness; the filters above still apply either way."""
        candidates: list[SkillCandidate] = []
        for hit in hits:
            if hit.strength < 0.5:
                continue
            steps = [e.name for e in trace.tool_calls()]
            if not steps:
                continue
            name = f"auto-{hit.signal.replace('_', '-')}-{trace.signature()[:8]}"
            candidates.append(
                SkillCandidate(
                    name=name,
                    kind="guide",
                    scope=trace.scope,
                    action="create",
                    rationale=hit.evidence,
                    when_to_use=f"When a task resembles run {trace.run_id} ({hit.signal})",
                    procedure=[f"Use `{s}`" for s in steps],
                    pitfalls=[e.detail for e in trace.events
                              if e.type == "error" and e.detail][:3],
                    verification=trace.lessons[:3] or ["Re-run and compare the outcome."],
                    provenance=Provenance(
                        origin="background_curator",
                        source_run_ids=[trace.run_id],
                        curator_id=self.curator_id,
                        signal=hit.signal,
                    ),
                )
            )
        return candidates
