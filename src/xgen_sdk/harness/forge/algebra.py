"""Substitution Algebra — legal config moves discovered from engine registries.

Every move is a single-knob, type-checked mutation of a HarnessConfig *dict*.
The legal vocabulary is introspected live from the engine's own registries
(strategies / guards / evaluation criteria / orchestrators / runtime-default
floors) — nothing about the move set is hardcoded. Moves compose and each has
an inverse, so Inertia-Brake rollback is deterministic.

Real config paths (verified against core/config.py + the s05/s08 stages):
  - strategy   -> config["active_strategies"][stage] = impl
  - guard      -> config["stage_params"]["s05_policy"]["guards"] += {"name","params"}
  - scalar     -> config[key]                         (top-level HarnessConfig field)
  - stage_param-> config["stage_params"][stage][key]  (e.g. s08_decide.judge_enabled)
  - criterion  -> config["stage_params"]["s08_decide"]["criteria_defs"]
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

# Managed prompt block the GEPA reflector evolves (kept separate from the user's prompt).
_GUIDANCE_RE = re.compile(r"\n*<forge_guidance>.*?</forge_guidance>", re.S)

# Scalars the forge may tune, with a discretized candidate ladder. Keys must be
# real HarnessConfig fields; ranges are clamped to runtime-default floors at use.
_SCALAR_CHOICES: dict[str, list[Any]] = {
    "validation_threshold": [0.5, 0.6, 0.7, 0.8, 0.9],
    "max_retries": [0, 1, 2, 3],
    "max_iterations": [4, 6, 8, 10],
    "temperature": [0.0, 0.2, 0.5, 0.7],
    # stateful loop — forge 가 state 설정까지 자가튜닝(HarnessConfig 정식 필드).
    "state_max_lessons": [2, 3, 5],
    "state_max_refined": [3, 5, 8],
    "state_max_recall": [5, 8, 12],
    "state_char_budget": [1000, 2000, 3000],
}


def register_tunable_scalar(name: str, choices: list) -> None:
    """forge 변이공간에 tunable scalar 추가 — 엔진 수정 없이 확장(무하드코딩).

    name 은 forge 가 mutate 하는 top-level HarnessConfig 필드. 외부(이식/도메인)가
    자기 knob 을 forge 의 자가튜닝 대상으로 등록. 같은 이름이면 후보 갱신.
    (stage_params 경로 knob 은 set_stage_param move 로 별도 튜닝.)
    """
    if name and choices:
        _SCALAR_CHOICES[str(name)] = list(choices)


def tunable_scalars() -> dict[str, list]:
    """현재 forge 가 튜닝 가능한 scalar 카탈로그(내성/디버그용)."""
    return dict(_SCALAR_CHOICES)


@dataclass(frozen=True)
class Move:
    op: str          # set_strategy | toggle_guard | tune_scalar | set_stage_param | edit_criterion
    target: str      # stage | guard name | scalar key | "stage:key" | criterion name
    value: Any

    def __str__(self) -> str:
        return f"{self.op}({self.target}={self.value!r})"


def _discover_registry() -> dict[str, Any]:
    """Snapshot the engine's registered primitive vocabulary (best-effort)."""
    reg: dict[str, Any] = {"strategies": {}, "guards": [], "criteria": [], "orchestrators": [], "scalars": list(_SCALAR_CHOICES)}
    try:
        from ..core import strategy_resolver as sr
        sr._register_defaults()  # idempotent; populates the module registry
        for (stage, slot, impl) in sr._REGISTRY:
            reg["strategies"].setdefault(stage, set()).add(impl)
        reg["strategies"] = {s: sorted(v) for s, v in reg["strategies"].items()}
    except Exception:
        pass
    try:
        from ..stages.strategies.guard import available_guards
        reg["guards"] = sorted(available_guards())
    except Exception:
        pass
    try:
        from ..stages.s08_decide.strategies.judge_then_loop import ALL_CRITERIA
        reg["criteria"] = sorted(ALL_CRITERIA)
    except Exception:
        pass
    try:
        from ..core.orchestrator_registry import list_orchestrators
        reg["orchestrators"] = list(list_orchestrators())
    except Exception:
        pass
    return reg


def _guards(config: dict) -> list[dict]:
    return config.get("stage_params", {}).get("s05_policy", {}).get("guards", []) or []


def _stage_param(config: dict, stage: str, key: str, default: Any = None) -> Any:
    return config.get("stage_params", {}).get(stage, {}).get(key, default)


class EngineAlgebra:
    def __init__(self, registry: dict[str, Any] | None = None) -> None:
        self.reg = registry or _discover_registry()

    # ---- generation -------------------------------------------------------
    def legal_moves(self, config: dict[str, Any]) -> list[Move]:
        moves: list[Move] = []
        for stage, impls in self.reg["strategies"].items():
            cur = config.get("active_strategies", {}).get(stage)
            moves += [Move("set_strategy", stage, i) for i in impls if i != cur]
        present = {g.get("name") for g in _guards(config)}
        for name in self.reg["guards"]:
            moves.append(Move("toggle_guard", name, name not in present))
        for key, choices in _SCALAR_CHOICES.items():
            cur = config.get(key)
            moves += [Move("tune_scalar", key, v) for v in choices if v != cur]
        moves.append(Move("set_stage_param", "s08_decide:judge_enabled",
                          not bool(_stage_param(config, "s08_decide", "judge_enabled", False))))
        active_crit = {c.get("name") for c in _stage_param(config, "s08_decide", "criteria_defs", []) or []}
        for name in self.reg["criteria"]:
            if name not in active_crit:
                moves.append(Move("edit_criterion", name, {"weight": 0.2, "hard": False}))
        return moves

    def is_legal(self, move: Move) -> bool:
        if move.op == "set_strategy":
            return move.value in self.reg["strategies"].get(move.target, []) or move.value == "judge_then_loop"
        if move.op == "toggle_guard":
            return move.target in self.reg["guards"] and isinstance(move.value, bool)
        if move.op == "tune_scalar":
            return move.target in _SCALAR_CHOICES
        if move.op == "set_stage_param":
            return ":" in move.target
        if move.op == "edit_criterion":
            return isinstance(move.value, dict)
        if move.op in ("append_guidance", "set_system_prompt"):   # GEPA-evolved prompt surface
            return isinstance(move.value, str)
        return False

    # ---- application / inversion -----------------------------------------
    def apply(self, config: dict[str, Any], move: Move) -> dict[str, Any]:
        if not self.is_legal(move):
            raise ValueError(f"illegal move: {move}")
        c = copy.deepcopy(config)
        c.setdefault("active_strategies", {})
        c.setdefault("stage_params", {})
        if move.op == "set_strategy":
            c["active_strategies"][move.target] = move.value
        elif move.op == "toggle_guard":
            sp = c["stage_params"].setdefault("s05_policy", {})
            guards = [g for g in (sp.get("guards") or []) if g.get("name") != move.target]
            if move.value:
                guards.append({"name": move.target, "params": {}})
            sp["guards"] = guards
        elif move.op == "tune_scalar":
            c[move.target] = move.value
        elif move.op == "set_stage_param":
            stage, key = move.target.split(":", 1)
            c["stage_params"].setdefault(stage, {})[key] = move.value
        elif move.op == "edit_criterion":
            sp = c["stage_params"].setdefault("s08_decide", {})
            defs = [dict(x) for x in (sp.get("criteria_defs") or [])]
            existing = next((x for x in defs if x.get("name") == move.target), None)
            if existing:
                existing.update(move.value)
            else:
                defs.append({"name": move.target, **move.value})
            sp["criteria_defs"] = defs
        elif move.op == "append_guidance":
            base = _GUIDANCE_RE.sub("", c.get("system_prompt") or "").rstrip()
            block = f"<forge_guidance>\n{move.value}\n</forge_guidance>"
            c["system_prompt"] = (base + "\n\n" + block) if base else block
        elif move.op == "set_system_prompt":
            c["system_prompt"] = move.value
        return c

    def inverse(self, config_before: dict[str, Any], move: Move) -> Move:
        if move.op == "set_strategy":
            return Move("set_strategy", move.target,
                        config_before.get("active_strategies", {}).get(move.target, "none"))
        if move.op == "toggle_guard":
            return Move("toggle_guard", move.target,
                        any(g.get("name") == move.target for g in _guards(config_before)))
        if move.op == "tune_scalar":
            return Move("tune_scalar", move.target, config_before.get(move.target))
        if move.op == "set_stage_param":
            stage, key = move.target.split(":", 1)
            return Move("set_stage_param", move.target, _stage_param(config_before, stage, key))
        if move.op == "edit_criterion":
            existing = next((x for x in _stage_param(config_before, "s08_decide", "criteria_defs", []) or []
                             if x.get("name") == move.target), None)
            return Move("edit_criterion", move.target, dict(existing) if existing else {"weight": 0.0, "hard": False})
        if move.op in ("append_guidance", "set_system_prompt"):
            return Move("set_system_prompt", "system_prompt", config_before.get("system_prompt", ""))
        raise ValueError(move.op)
