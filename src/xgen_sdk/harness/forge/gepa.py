"""GEPA-style reflective prompt-guidance reflector (opt-in).

Reflects in natural language over failing traces' feedback and proposes an
`append_guidance` Move that evolves a managed `<forge_guidance>` block in the
system prompt — the text-optimization analogue of a gradient (GEPA,
arXiv:2507.19457). The loop GATES each proposal on the Goodhart-defended
objective (held-out), so only guidance that actually helps survives.

Provider-agnostic: with a provider it aux-reflects; without one it falls back to
the recurring trace feedback verbatim. Plug in via:
    register_reflector(GepaReflector(provider))
or the entry_points group `xgen_harness.forge_reflectors`. forge source 0.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from .algebra import Move

_REFLECT_PROMPT = (
    "You are improving an AI agent's system instructions. It FAILED on tasks with "
    "these observations:\n{obs}\nWrite ONE concise imperative guidance line (a single "
    "sentence) to add to its instructions so it stops making these mistakes. Output "
    "only the line — no preamble, no quotes."
)


class GepaReflector:
    def __init__(self, provider: Optional[Any] = None, k: int = 1) -> None:
        self.provider = provider
        self.k = max(1, k)

    def __call__(self, traces: list) -> list[Move]:
        fails = [t for t in traces if getattr(t, "outcome", "") != "success"]
        obs = "; ".join(
            (t.feedback or ", ".join(t.signals)) for t in fails if (t.feedback or t.signals)
        )[:1200]
        if not obs:
            return []
        return [Move("append_guidance", "system_prompt", line)
                for line in self._reflect(obs)[: self.k] if line]

    def _reflect(self, obs: str) -> list[str]:
        if self.provider is None:
            return [f"Avoid these recurring failures: {obs[:300]}"]
        try:
            text = asyncio.run(self._chat(_REFLECT_PROMPT.format(obs=obs))).strip()
        except Exception:
            return [f"Avoid these recurring failures: {obs[:300]}"]
        lines = [ln.strip().lstrip("-• ").strip() for ln in text.splitlines() if ln.strip()]
        return lines or []

    async def _chat(self, prompt: str) -> str:
        from ..providers.base import ProviderEventType
        deltas: list[str] = []
        final = ""
        async for ev in self.provider.chat(
            [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=200, stream=False,
        ):
            if ev.type == ProviderEventType.TEXT_DELTA:
                deltas.append(ev.text)
            elif ev.type == ProviderEventType.STOP and ev.text:   # stream=False carries text here
                final = ev.text
        return final or "".join(deltas)
