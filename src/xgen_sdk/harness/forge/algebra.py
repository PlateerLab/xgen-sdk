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

# forge 가 tune 하는 scalar 의 **범위 스펙**(고정 ladder 아님). 후보는 현재값 기준
# 적응형 이웃(±step·×2·÷2)으로 생성 → forge 가 국소탐색하며 범위 전체를 단계적으로
# 도달(작은 ladder 한계 제거). lo/hi 는 임의 튜닝값이 아니라 **타입-안전 경계**.
# spec = {lo,hi,step,kind} (범위) 또는 {choices:[...]} (명시 후보). Keys = HarnessConfig 필드.
_SCALAR_SPECS: dict[str, dict[str, Any]] = {
    "validation_threshold": {"lo": 0.0, "hi": 1.0, "step": 0.1, "kind": "float"},
    "max_retries":          {"lo": 0,   "hi": 8,   "step": 1,   "kind": "int"},
    "max_iterations":       {"lo": 1,   "hi": 20,  "step": 2,   "kind": "int"},
    "temperature":          {"lo": 0.0, "hi": 1.5, "step": 0.2, "kind": "float"},
    # stateful loop — forge 가 state 설정까지 자가튜닝(HarnessConfig 정식 필드).
    "state_max_lessons":    {"lo": 1,   "hi": 20,    "step": 1,    "kind": "int"},
    "state_max_refined":    {"lo": 1,   "hi": 20,    "step": 2,    "kind": "int"},
    "state_max_recall":     {"lo": 1,   "hi": 40,    "step": 3,    "kind": "int"},
    "state_char_budget":    {"lo": 500, "hi": 20000, "step": 1000, "kind": "int"},
}


def _gen_candidates(key: str, cur: Any) -> list[Any]:
    """현재값 기준 적응형 후보 — ±step·×2·÷2 를 경계 클램프. 고정 ladder 없음.

    forge step 마다 현재값 주변을 탐색 → 여러 step 에 걸쳐 [lo,hi] 전 구간 도달.
    cur 미설정이면 lo/중앙/hi 로 시드. choices spec 이면 그대로(명시 override).
    """
    spec = _SCALAR_SPECS.get(key)
    if not spec:
        return []
    if "choices" in spec:
        return [v for v in spec["choices"] if v != cur]
    lo, hi, step, kind = spec["lo"], spec["hi"], spec["step"], spec.get("kind", "float")

    def _clamp(x: float) -> Any:
        x = max(lo, min(hi, x))
        return int(round(x)) if kind == "int" else round(x, 4)

    if cur is None:
        raw = [lo, (lo + hi) / 2.0, hi]
    else:
        raw = [cur - step, cur + step, cur * 2, cur * 0.5]
    out: list[Any] = []
    for x in raw:
        try:
            v = _clamp(float(x))
        except (TypeError, ValueError):
            continue
        if v != cur and v not in out:
            out.append(v)
    return out


def register_tunable_scalar(name: str, spec: Any) -> None:
    """forge 변이공간에 tunable scalar 추가/갱신 — 엔진 수정 없이 확장(무하드코딩).

    spec 형식(셋 다 허용):
      · list  → 명시 후보 [v1, v2, …]
      · tuple → 범위 (lo, hi, step[, kind])  kind="int"|"float"
      · dict  → {"lo","hi","step","kind"} 또는 {"choices":[…]}
    name 은 forge 가 mutate 하는 HarnessConfig 필드. 외부(이식/도메인)가 자기 knob·범위를 등록.
    """
    if not name or spec is None:
        return
    if isinstance(spec, dict):
        _SCALAR_SPECS[str(name)] = dict(spec)
    elif isinstance(spec, tuple):
        lo, hi, st = spec[0], spec[1], spec[2]
        kind = spec[3] if len(spec) > 3 else ("int" if all(isinstance(v, int) for v in (lo, hi, st)) else "float")
        _SCALAR_SPECS[str(name)] = {"lo": lo, "hi": hi, "step": st, "kind": kind}
    elif isinstance(spec, list) and spec:
        _SCALAR_SPECS[str(name)] = {"choices": list(spec)}


def tunable_scalars() -> dict[str, dict]:
    """현재 forge 가 튜닝 가능한 scalar 범위 스펙(내성/디버그용)."""
    return {k: dict(v) for k, v in _SCALAR_SPECS.items()}


@dataclass(frozen=True)
class Move:
    op: str          # set_strategy | toggle_guard | tune_scalar | set_stage_param | edit_criterion
    target: str      # stage | guard name | scalar key | "stage:key" | criterion name
    value: Any

    def __str__(self) -> str:
        return f"{self.op}({self.target}={self.value!r})"


def _discover_registry() -> dict[str, Any]:
    """Snapshot the engine's registered primitive vocabulary (best-effort)."""
    reg: dict[str, Any] = {"strategies": {}, "guards": [], "criteria": [], "orchestrators": [], "scalars": list(_SCALAR_SPECS)}
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
        for key in _SCALAR_SPECS:
            cur = config.get(key)
            moves += [Move("tune_scalar", key, v) for v in _gen_candidates(key, cur)]
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
            return move.target in _SCALAR_SPECS
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
