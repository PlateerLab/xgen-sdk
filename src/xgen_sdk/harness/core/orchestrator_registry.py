"""
Orchestrator Registry — LLM 이 선택 가능한 실행 패턴 레지스트리.

철학 (v0.15.0 재귀적 자율주행 / 자동 연동 자동 확장성):
  - `orchestrator_hint` 의 enum 을 리터럴로 박지 않는다.
  - 엔진 기본 4개는 defaults 로 등록하되, 외부 플러그인이 `register_orchestrator()`
    한 줄만 호출하면 즉시 Planner 의 도구 스키마(enum) 에 합류한다.
  - LLM 은 레지스트리의 description 을 보고 어느 패턴이 이번 요청에 어울릴지 판단.

이게 "하드코딩 없이 자동 연동" 의 구현 — planner.py / _build_plan_from_tool_input /
이식측 dispatcher / 프론트 PlanningCard 모두 이 레지스트리를 조회한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OrchestratorSpec:
    """Orchestrator 설명 + 엔진이 읽는 행동 속성.

    v0.22.0 — 행동 분기를 spec 으로 옮김. pipeline 은 이름으로 if-else 하지 않고
    속성을 조회한다. 외부 orchestrator 는 새 값을 선언적으로 주기만 하면 엔진이 반영.
    """
    name: str
    description: str
    dispatch_key: str = ""
    # 엔진 행동 힌트. 외부 패턴도 동일 의미로 사용.
    replan_per_iter: bool = True           # False 면 매 iter s00 재호출 생략 (plan_execute 류)
    max_iterations_override: Optional[int] = None  # 지정하면 config.max_iterations 를 이것으로 덮음 (linear=1)


_REGISTRY: dict[str, OrchestratorSpec] = {}
_DEFAULTS_REGISTERED = False


def register_orchestrator(
    name: str,
    *,
    description: str = "",
    dispatch_key: str = "",
    replan_per_iter: bool = True,
    max_iterations_override: Optional[int] = None,
) -> None:
    """새 orchestrator 를 레지스트리에 등록.

    외부 패키지는 import 시 이 함수를 호출하거나 `entry_points` 그룹
    `xgen_harness.orchestrators` 에 Spec 을 노출하면 자동 발견된다.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("orchestrator name must be non-empty string")
    # v0.22.1 — 대소문자 정규화. pipeline.py 는 orch_hint 를 .lower() 로 조회하므로
    # 등록 이름과 조회 키가 case 불일치하면 영원히 miss → iterative fallback 되는 사일런트 버그.
    key = name.strip().lower()
    _REGISTRY[key] = OrchestratorSpec(
        name=key,
        description=description,
        dispatch_key=(dispatch_key or key).strip().lower(),
        replan_per_iter=replan_per_iter,
        max_iterations_override=max_iterations_override,
    )


def unregister_orchestrator(name: str) -> None:
    """테스트/런타임 교체용. 기본값도 제거 가능."""
    _REGISTRY.pop(name.strip().lower() if isinstance(name, str) else name, None)


def list_orchestrators() -> list[str]:
    """등록된 orchestrator 이름 리스트 (정렬). orchestrator_hint 해석에 사용."""
    _ensure_defaults_registered()
    return sorted(_REGISTRY.keys())


def get_orchestrator_specs() -> list[dict]:
    """Planner / 프론트 / 이식측에 설명까지 포함해 노출."""
    _ensure_defaults_registered()
    return [
        {"name": spec.name, "description": spec.description, "dispatch_key": spec.dispatch_key}
        for spec in sorted(_REGISTRY.values(), key=lambda s: s.name)
    ]


def get_orchestrator(name: str) -> Optional[OrchestratorSpec]:
    _ensure_defaults_registered()
    if not isinstance(name, str):
        return None
    return _REGISTRY.get(name.strip().lower())


def _ensure_defaults_registered() -> None:
    """엔진 기본 4개 패턴. 사용자가 `unregister_orchestrator` 로 덜어낼 수 있도록 idempotent."""
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return
    _DEFAULTS_REGISTERED = True

    # 기본 4개 — geny-harness / LangGraph 패턴을 참고한 최소 집합.
    # 외부에서 새 패턴 추가는 `register_orchestrator("custom_name", ...)` 한 줄.
    if "linear" not in _REGISTRY:
        register_orchestrator(
            "linear",
            description="한 번 돌고 종료. 단순 Q&A, 단발성 요청에 적합.",
            dispatch_key="linear",
            replan_per_iter=False,
            max_iterations_override=1,
        )
    if "iterative" not in _REGISTRY:
        register_orchestrator(
            "iterative",
            description="매 iter 재계획. Plan 이 이전 결과 보고 다음 행동 결정.",
            dispatch_key="iterative",
            replan_per_iter=True,
        )
    if "react" not in _REGISTRY:
        register_orchestrator(
            "react",
            description="도구 호출 결과(Observation)보고 다음 Thought→Action.",
            dispatch_key="react",
            replan_per_iter=True,
        )
    if "plan_execute" not in _REGISTRY:
        register_orchestrator(
            "plan_execute",
            description="첫 Plan 고수. 멀티 스텝 계획을 세우고 흔들리지 않고 실행.",
            dispatch_key="plan_execute",
            replan_per_iter=False,
        )
    # entry_points 기반 자동 발견 — pip install xxx 한 것이 즉시 합류.
    _discover_from_entry_points()


def _discover_from_entry_points() -> None:
    """외부 패키지가 `xgen_harness.orchestrators` 그룹에 Spec 을 노출하면 자동 등록."""
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.orchestrators"
        if hasattr(eps, "select"):
            items = eps.select(group=group)  # Python 3.10+
        else:
            items = eps.get(group, [])       # legacy
        for ep in items:
            try:
                factory = ep.load()
                spec = factory() if callable(factory) else factory
                if isinstance(spec, OrchestratorSpec):
                    _REGISTRY[spec.name] = spec
                elif isinstance(spec, dict) and spec.get("name"):
                    register_orchestrator(
                        spec["name"],
                        description=spec.get("description", ""),
                        dispatch_key=spec.get("dispatch_key", ""),
                        replan_per_iter=bool(spec.get("replan_per_iter", True)),
                        max_iterations_override=spec.get("max_iterations_override"),
                    )
            except Exception:
                # 개별 entry_point 실패는 전체 등록을 막지 않는다.
                continue
    except Exception:
        return
