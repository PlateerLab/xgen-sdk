"""SF3 — synthesizers: SkillCandidate -> SkillDef artifact.

Three kinds (the ladder Hermes/Geny don't have):
- guide  : SKILL.md-style markdown (agentskills.io-compatible sections)
- config : a HarnessConfig fragment (stage_params / criteria preset) as JSON
- tool   : a compile manifest pointing at the workflow->npm->MCP path

An optional `reflector` callable (LLM seam, same shape as forge's GEPA seam)
may polish the drafted body; synthesis must still work with reflector=None.
"""

from __future__ import annotations

import json
from typing import Callable

from .model import SkillCandidate, SkillDef
from .registry import GROUP_SYNTHESIZERS, Registry, registry_decorator

SYNTHESIZERS = Registry(GROUP_SYNTHESIZERS)
synthesizer = registry_decorator(SYNTHESIZERS)

Reflector = Callable[[str, str], str]  # (purpose, draft) -> improved draft


def _frontmatter(candidate: SkillCandidate, description: str) -> str:
    return (
        "---\n"
        f"name: {candidate.name}\n"
        f"description: {description[:60]}\n"
        "version: 0.1.0\n"
        f"kind: {candidate.kind}\n"
        f"scope: {candidate.scope}\n"
        f"origin: {candidate.provenance.origin if candidate.provenance else 'manual'}\n"
        "---\n"
    )


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return f"\n## {title}\n{body}\n"


@synthesizer("guide")
def synthesize_guide(candidate: SkillCandidate,
                     reflector: Reflector | None = None) -> SkillDef:
    description = candidate.when_to_use or candidate.rationale
    body = _frontmatter(candidate, description)
    body += f"\n# {candidate.name}\n\n{candidate.rationale}\n"
    body += _section("When to Use", [candidate.when_to_use] if candidate.when_to_use else [])
    body += _section("Procedure", candidate.procedure)
    body += _section("Pitfalls", candidate.pitfalls)
    body += _section("Verification", candidate.verification)
    if reflector is not None:
        body = reflector("guide-skill", body)
    return SkillDef(
        name=candidate.name,
        kind="guide",
        scope=candidate.scope,
        description=description[:200],
        body=body,
        provenance=candidate.provenance,
    )


@synthesizer("config")
def synthesize_config(candidate: SkillCandidate,
                      reflector: Reflector | None = None) -> SkillDef:
    fragment = candidate.payload.get("config_fragment")
    if not isinstance(fragment, dict) or not fragment:
        raise ValueError("config candidate requires payload['config_fragment'] dict")
    body = json.dumps(
        {
            "name": candidate.name,
            "when_to_use": candidate.when_to_use,
            "fragment": fragment,
        },
        ensure_ascii=False,
        indent=2,
    )
    return SkillDef(
        name=candidate.name,
        kind="config",
        scope=candidate.scope,
        description=(candidate.when_to_use or candidate.rationale)[:200],
        body=body,
        provenance=candidate.provenance,
        meta={"fragment_keys": sorted(fragment)},
    )


@synthesizer("tool")
def synthesize_tool(candidate: SkillCandidate,
                    reflector: Reflector | None = None) -> SkillDef:
    workflow = candidate.payload.get("workflow_ref")
    if not workflow:
        raise ValueError("tool candidate requires payload['workflow_ref']")
    manifest = {
        "name": candidate.name,
        "when_to_use": candidate.when_to_use,
        "workflow_ref": workflow,
        "compile": {"target": "npm-mcp", "entry": "compile_workflow_to_npm"},
    }
    return SkillDef(
        name=candidate.name,
        kind="tool",
        scope=candidate.scope,
        description=(candidate.when_to_use or candidate.rationale)[:200],
        body=json.dumps(manifest, ensure_ascii=False, indent=2),
        provenance=candidate.provenance,
        meta={"workflow_ref": workflow},
    )


def synthesize(candidate: SkillCandidate,
               reflector: Reflector | None = None) -> SkillDef:
    fn = SYNTHESIZERS.get(candidate.kind)
    return fn(candidate, reflector=reflector)
