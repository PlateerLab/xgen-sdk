"""Jermes drafter — LLM-backed candidate drafting from a run trace.

The LLM only *proposes*; every proposal still passes the deterministic
curator filters and the forge gate. Provider-agnostic: `complete` is any
(prompt -> text) callable. A stdlib OpenAI-compatible completer is included
so the package stays dependency-free.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from typing import Callable

logger = logging.getLogger("xgen_skill_forge.drafter")

from .model import Provenance, RunTrace, SkillCandidate
from .signals import SignalHit

Completer = Callable[[str], str]

_PROMPT = """You are Jermes, the skill smith of the XGEN harness. You review a \
finished agent run and propose at most {max_candidates} reusable skills.

Rules (violations are discarded by a deterministic filter, so obey them):
- Only durable, portable procedures. NO environment-specific paths, NO \
transient failures (timeouts, rate limits), NO broad bans ("never use X"), \
NO secrets or credentials.
- Prefer nothing over something marginal. Return [] when the run taught \
nothing reusable.
- Procedures need >= 2 concrete steps and a verification recipe.

Run trace:
{trace}

Detected signals:
{signals}

Lessons recorded by the agent:
{lessons}

Respond with ONLY a JSON array (no prose). Each item:
{{"name": "kebab-case-name", "when_to_use": "...", "rationale": "...",
  "procedure": ["step", ...], "pitfalls": ["...", ...],
  "verification": ["...", ...]}}

Example of a good response (format reference only — do not copy content):
[{{"name": "paginate-with-cursor", "when_to_use": "when listing more than one \
page from the orders API", "rationale": "offset pagination silently drops rows \
under concurrent writes", "procedure": ["Request the first page with limit and \
no cursor", "Loop passing next_cursor until it returns empty"], "pitfalls": \
["Reusing an expired cursor returns 410"], "verification": ["Total fetched \
count equals the summary endpoint count"]}}]"""

_REPAIR_PROMPT = """The following text was supposed to be a JSON array of skill \
objects but is malformed. Output ONLY the corrected JSON array, nothing else. \
If no skill objects can be recovered, output [].

{raw}"""

_RETRY_SUFFIX = """

Your previous attempt was invalid: {feedback}
Respond again with ONLY a valid JSON array."""


def _render_trace(trace: RunTrace, cap: int = 40) -> str:
    lines = []
    for event in trace.events[:cap]:
        status = "" if event.ok else " FAILED"
        detail = f" — {event.detail}" if event.detail else ""
        lines.append(f"- {event.type}: {event.name}{status}{detail}")
    lines.append(f"- outcome: {'success' if trace.success else 'failure'}")
    return "\n".join(lines)


def build_prompt(trace: RunTrace, hits: list[SignalHit],
                 max_candidates: int = 2) -> str:
    signals = "\n".join(f"- {h.signal} ({h.strength:.2f}): {h.evidence}"
                        for h in hits) or "- none"
    lessons = "\n".join(f"- {l}" for l in trace.lessons) or "- none"
    return _PROMPT.format(max_candidates=max_candidates,
                          trace=_render_trace(trace),
                          signals=signals, lessons=lessons)


_EXAMPLE_NAME = "paginate-with-cursor"
_EXAMPLE_MARKERS = ("orders api", "next_cursor", "offset pagination")


def _is_example_leak(item: dict) -> bool:
    """Weak models sometimes copy the few-shot example despite instructions.
    Drop any candidate that reproduces the example's name or content."""
    name = str(item.get("name", "")).strip().lower()
    if name == _EXAMPLE_NAME:
        return True
    text = " ".join(str(item.get(k, "")) for k in
                    ("when_to_use", "rationale")).lower()
    return sum(marker in text for marker in _EXAMPLE_MARKERS) >= 2


def _sanitize_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9-]", "-", raw.strip().lower().replace("_", "-").replace(" ", "-"))
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:64] or "unnamed-skill"


def _extract_json_array(text: str) -> list:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start = text.find("[")
    if start == -1:
        return []
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:i + 1])
                    return parsed if isinstance(parsed, list) else []
                except json.JSONDecodeError:
                    return []
    return []


class LLMDrafter:
    """Weak-model-hardened drafter. Three recovery layers before giving up:
    (1) tolerant extraction (fences, surrounding prose), (2) an LLM repair pass
    on malformed output, (3) a bounded retry with explicit failure feedback.
    A weak model that CAN emit JSON some of the time therefore still drafts;
    a weak model that drafts nonsense is stopped later by curator + gate."""

    def __init__(self, complete: Completer, max_candidates: int = 2,
                 drafter_id: str = "jermes", repair: bool = True,
                 max_retries: int = 1) -> None:
        self.complete = complete
        self.max_candidates = max_candidates
        self.drafter_id = drafter_id
        self.repair = repair
        self.max_retries = max_retries

    def _complete_array(self, prompt: str) -> list:
        raw = self.complete(prompt)
        items = _extract_json_array(raw)
        if not items and self.repair and raw.strip() and raw.strip() != "[]":
            logger.warning("[drafter] unparsable output, repairing. head=%r",
                           raw[:200])
            repaired = self.complete(_REPAIR_PROMPT.format(raw=raw[:4000]))
            items = _extract_json_array(repaired)
            if not items:
                logger.warning("[drafter] repair failed. head=%r", repaired[:200])
        return items

    def draft(self, trace: RunTrace, hits: list[SignalHit],
              variant: str = "") -> list[SkillCandidate]:
        prompt = build_prompt(trace, hits, self.max_candidates)
        if variant:
            prompt += f"\n\nAttempt focus: {variant}"
        items: list = []
        feedback = ""
        for attempt in range(self.max_retries + 1):
            attempt_prompt = prompt if not feedback else (
                prompt + _RETRY_SUFFIX.format(feedback=feedback))
            try:
                items = self._complete_array(attempt_prompt)
            except Exception as exc:
                detail = ""
                read = getattr(exc, "read", None)
                if callable(read):
                    try:
                        detail = read().decode("utf-8", "replace")[:300]
                    except Exception:
                        detail = ""
                logger.warning("[drafter] completion failed: %s: %s %s",
                               type(exc).__name__, str(exc)[:200], detail)
                return []
            if items:
                break
            feedback = "no parsable JSON array of skill objects was found"
        candidates: list[SkillCandidate] = []
        strongest = max((h.signal for h in sorted(hits, key=lambda h: -h.strength)),
                        default="")
        dropped_leak = dropped_invalid = 0
        for item in items[: self.max_candidates]:
            if not isinstance(item, dict) or _is_example_leak(item):
                dropped_leak += 1
                continue
            try:
                candidates.append(
                    SkillCandidate(
                        name=_sanitize_name(str(item.get("name", ""))),
                        kind="guide",
                        scope=trace.scope,
                        action="create",
                        rationale=str(item.get("rationale", ""))[:500],
                        when_to_use=str(item.get("when_to_use", ""))[:300],
                        procedure=[str(s)[:300] for s in item.get("procedure", [])][:12],
                        pitfalls=[str(s)[:300] for s in item.get("pitfalls", [])][:6],
                        verification=[str(s)[:300] for s in item.get("verification", [])][:6],
                        provenance=Provenance(
                            origin="llm_drafter",
                            source_run_ids=[trace.run_id],
                            curator_id=self.drafter_id,
                            signal=strongest,
                        ),
                    )
                )
            except ValueError:
                dropped_invalid += 1
                continue
        if hits:
            # "0건"이 세 가지 다른 사건일 수 있다 — 모델이 빈 배열을 줬거나,
            # 예시 유출 가드가 전부 걷어냈거나, 필드가 깨져 버려졌거나.
            # 숫자로 나뉘지 않으면 약한 모델을 튜닝할 근거가 없다.
            logger.info("[drafter] hits=%d · model=%d · leak=%d · invalid=%d · kept=%d",
                        len(hits), len(items), dropped_leak, dropped_invalid,
                        len(candidates))
        return candidates


class EnsembleDrafter:
    """Self-consistency for weak models: sample the drafter k times, pool the
    candidates, and collapse near-duplicates. Selection among survivors is NOT
    done here — the curator (patch-over-create) and the gate (replay bench) do
    it deterministically, which is exactly what makes a weak drafter viable:
    breadth from sampling, quality from selection pressure."""

    VARIANTS = (
        "",
        "propose a different angle than the most obvious one",
        "focus on prevention and verification steps rather than the happy path",
        "focus on what the user corrected or what almost went wrong",
        "focus on ordering constraints between the tools used",
    )

    def __init__(self, drafter: LLMDrafter, samples: int = 3,
                 max_candidates: int = 4) -> None:
        self.drafter = drafter
        self.samples = samples
        self.max_candidates = max_candidates

    def draft(self, trace: RunTrace, hits: list[SignalHit]) -> list[SkillCandidate]:
        pool: list[SkillCandidate] = []
        for index in range(self.samples):
            variant = self.VARIANTS[index % len(self.VARIANTS)]
            pool.extend(self.drafter.draft(trace, hits, variant=variant))
        deduped: list[SkillCandidate] = []
        seen_names: set[str] = set()
        for candidate in pool:
            if candidate.name in seen_names:
                continue
            if any(_near_duplicate(candidate, kept) for kept in deduped):
                continue
            seen_names.add(candidate.name)
            deduped.append(candidate)
        return deduped[: self.max_candidates]


def _near_duplicate(a: "SkillCandidate", b: "SkillCandidate") -> bool:
    ta = set(re.findall(r"[a-z0-9가-힣]{2,}", (a.when_to_use + " " + a.rationale).lower()))
    tb = set(re.findall(r"[a-z0-9가-힣]{2,}", (b.when_to_use + " " + b.rationale).lower()))
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.7


def failover_completer(completers: list[Completer]) -> Completer:
    """Try each completer in order; remember the one that works.

    Observed failure this exists for: the box serving the primary model went
    down and curation stopped silently — the loop kept "running" while every
    draft returned nothing. With a second endpoint listed, learning continues.

    A completer that raises is treated as unhealthy and the next one is tried.
    The last known-good index is tried first next time, so the healthy path
    costs no extra calls.
    """
    live = [c for c in completers if c is not None]
    if not live:
        raise ValueError("failover_completer needs at least one completer")
    state = {"index": 0}

    def complete(prompt: str) -> str:
        order = list(range(state["index"], len(live))) + \
            list(range(0, state["index"]))
        last: Exception | None = None
        for i in order:
            try:
                result = live[i](prompt)
            except Exception as exc:
                last = exc
                logger.warning("[drafter] endpoint %d unhealthy: %s: %s",
                               i, type(exc).__name__, str(exc)[:120])
                continue
            if i != state["index"]:
                logger.warning("[drafter] failed over to endpoint %d", i)
                state["index"] = i
            return result
        raise last if last else RuntimeError("no endpoint answered")

    return complete


def anthropic_completer(model: str, api_key: str,
                        max_tokens: int = 2048,
                        timeout: float = 120.0) -> Completer:
    """Stdlib completer for the Anthropic Messages API. Raw HTTP is deliberate:
    this package ships with dependencies=[] (engine ethos), so the official SDK
    is not available here — hosts with the SDK installed should pass their own
    Completer instead."""
    url = "https://api.anthropic.com/v1/messages"

    def complete(prompt: str) -> str:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("stop_reason") == "refusal":
            return "[]"
        return "".join(block.get("text", "") for block in body.get("content", [])
                       if block.get("type") == "text")

    return complete


def openai_chat_completer(base_url: str, model: str,
                          temperature: float = 0.2,
                          timeout: float = 120.0,
                          api_key: str = "",
                          extra: dict | None = None) -> Completer:
    """Stdlib completer for any OpenAI-compatible /chat/completions endpoint.

    `extra` merges provider-specific body params (e.g. DashScope Qwen3 needs
    {"enable_thinking": False} on non-streaming calls)."""
    url = base_url.rstrip("/") + "/chat/completions"

    def complete(prompt: str) -> str:
        payload = {
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
            **(extra or {}),
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]

    return complete
