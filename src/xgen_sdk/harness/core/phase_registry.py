"""
Phase Registry — Stage 그룹(phase) 경계 자동 연동.

철학 (v0.15.1 자동 연동 자동 확장성):
  - `if order <= 4: return "ingress"` 같은 매직 넘버를 박지 않는다.
  - phase 이름·경계를 `PHASE_ORDER_BOUNDARIES` 레지스트리로 추출.
  - 외부 패키지가 `register_phase("post_egress", upper_order=99)` 한 줄로 새 phase
    합류 + `entry_points` 그룹 `xgen_harness.phases` 자동 발견.

Stage 서브클래스가 `@property phase` 를 override 하면 그 값 그대로 사용되므로
이 레지스트리는 **order 기반 default** 용도. Stage 저자가 직접 선언하면 우선.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhaseSpec:
    """Phase 경계 선언."""
    name: str
    # 이 phase 에 속하는 order 의 상한 (inclusive).
    upper_order: int
    description: str = ""


_REGISTRY: dict[str, PhaseSpec] = {}
_DEFAULTS_REGISTERED = False


def register_phase(name: str, *, upper_order: int, description: str = "") -> None:
    """Phase 경계 등록. 기존 이름이면 덮어씀."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("phase name must be non-empty string")
    if not isinstance(upper_order, int):
        raise ValueError("upper_order must be int")
    _REGISTRY[name] = PhaseSpec(name=name, upper_order=upper_order, description=description)


def unregister_phase(name: str) -> None:
    _REGISTRY.pop(name, None)


def list_phases() -> list[str]:
    """등록된 phase 이름을 upper_order 기준 오름차순으로."""
    _ensure_defaults_registered()
    return [p.name for p in sorted(_REGISTRY.values(), key=lambda p: p.upper_order)]


def get_phase_specs() -> list[dict]:
    _ensure_defaults_registered()
    return [
        {"name": p.name, "upper_order": p.upper_order, "description": p.description}
        for p in sorted(_REGISTRY.values(), key=lambda p: p.upper_order)
    ]


def resolve_phase(order: int) -> str:
    """Stage.order 로 phase 이름 해석. Stage.phase property 의 기본 구현."""
    _ensure_defaults_registered()
    for spec in sorted(_REGISTRY.values(), key=lambda p: p.upper_order):
        if order <= spec.upper_order:
            return spec.name
    # 모든 경계를 넘으면 마지막 phase (가장 큰 upper_order) 이름으로.
    if _REGISTRY:
        last = max(_REGISTRY.values(), key=lambda p: p.upper_order)
        return last.name
    return ""


def _ensure_defaults_registered() -> None:
    """엔진 기본 3개 phase. v1.0 10-Stage 구조 기준.

    경계 (v1.0 통합 후):
      ingress: order ≤ 4 → s01_input / s02_history / s03_prompt / s04_tool
      loop:    5 ≤ order ≤ 8 → s05_policy / s06_context / s07_act / s08_decide
      egress:  order ≥ 9 → s09_finalize

    s00_harness 는 order=0 이지만 phase property 를 ingress 로 override 함.
    외부에서 `unregister_phase("loop")` 로 덜어내거나 새 경계로 재등록 가능.
    """
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return
    _DEFAULTS_REGISTERED = True

    if "ingress" not in _REGISTRY:
        register_phase("ingress", upper_order=4,
                       description="입력 정규화·Planner·이력·프롬프트 준비")
    if "loop" not in _REGISTRY:
        register_phase("loop", upper_order=8,
                       description="Policy·Context·Act·Decide 반복 루프")
    if "egress" not in _REGISTRY:
        # upper_order 를 크게 잡아 모든 후속 order 를 egress 로 유도.
        register_phase("egress", upper_order=9999,
                       description="최종 출력·메트릭스·저장")

    _discover_from_entry_points()


def _discover_from_entry_points() -> None:
    """외부 패키지가 `xgen_harness.phases` 그룹에 PhaseSpec 노출 시 자동 등록."""
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.phases"
        if hasattr(eps, "select"):
            items = eps.select(group=group)
        else:
            items = eps.get(group, [])
        for ep in items:
            try:
                factory = ep.load()
                spec = factory() if callable(factory) else factory
                if isinstance(spec, PhaseSpec):
                    _REGISTRY[spec.name] = spec
                elif isinstance(spec, dict) and spec.get("name"):
                    register_phase(
                        spec["name"],
                        upper_order=int(spec.get("upper_order", 0)),
                        description=spec.get("description", ""),
                    )
            except Exception:
                continue
    except Exception:
        return
