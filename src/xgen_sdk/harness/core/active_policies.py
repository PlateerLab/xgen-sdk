"""Active policies renderer registry — v1.6.

`<active_policies>` 섹션에 노출할 정책 종을 외부 등록 가능하게 하는 registry.
빌트인 (max_iterations / cost_budget_usd / context_window / s05_guards) 도 같은
register API 통해 등록. 외부 wheel 이 entry_points 로 추가 가능:

    [project.entry-points."xgen_harness.active_policy_renderers"]
    my_policy = "my_pkg:render_my_policy"

renderer 시그니처:
    def render(config: HarnessConfig) -> str | None
        - return None  →  해당 정책 노출 X (값 미박힘)
        - return str   →  "- 메시지" 형태로 노출

사용자 정신 — 정책 종 hardcoded list 대신 자기서술 (register API + entry_points).
"""
from __future__ import annotations

import logging
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import HarnessConfig

logger = logging.getLogger("harness.active_policies")

PolicyRenderer = Callable[["HarnessConfig"], str | None]

_RENDERERS: dict[str, PolicyRenderer] = {}
_LOADED_FROM_ENTRY_POINTS = False


def register_active_policy_renderer(name: str, fn: PolicyRenderer) -> None:
    """외부 wheel 또는 본체 부팅 시 정책 종 등록.

    name: 고유 식별자 (예: "max_iterations", "cost_budget_usd", "data_residency")
    fn: HarnessConfig 받아 노출 라인 (또는 None) 반환
    """
    if name in _RENDERERS:
        logger.debug("[active_policies] override existing renderer: %s", name)
    _RENDERERS[name] = fn


def render_all(config) -> list[str]:
    """등록된 모든 renderer 호출. 빈 결과 (None) 는 skip."""
    _ensure_loaded()
    lines: list[str] = []
    for name, fn in _RENDERERS.items():
        try:
            line = fn(config)
            if line:
                lines.append(line)
        except Exception as e:
            logger.warning("[active_policies] renderer %s failed: %s", name, e)
    return lines


def _ensure_loaded() -> None:
    global _LOADED_FROM_ENTRY_POINTS
    if _LOADED_FROM_ENTRY_POINTS:
        return
    _LOADED_FROM_ENTRY_POINTS = True
    # 빌트인 4 종 등록
    _register_builtins()
    # entry_points 로드
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="xgen_harness.active_policy_renderers")
        for ep in eps:
            try:
                fn = ep.load()
                register_active_policy_renderer(ep.name, fn)
                logger.info("[active_policies] loaded entry_point: %s", ep.name)
            except Exception as e:
                logger.warning("[active_policies] entry_point %s 로드 실패: %s", ep.name, e)
    except Exception as e:
        logger.debug("[active_policies] entry_points 미사용: %s", e)


def _register_builtins() -> None:
    """v1.5.5 의 빌트인 4 종을 register API 로 통합 (정신 정합)."""
    def _max_iterations(config) -> str | None:
        v = getattr(config, "max_iterations", None)
        return f"- 도구 호출 최대 {v}회" if v else None

    def _cost_budget(config) -> str | None:
        v = getattr(config, "cost_budget_usd", None)
        return f"- 비용 예산 ${v:.2f}" if v else None

    def _context_window(config) -> str | None:
        v = getattr(config, "context_window", None)
        return f"- 컨텍스트 윈도우 {v:,} tokens" if v else None

    def _s05_guards(config) -> str | None:
        sp = getattr(config, "stage_params", None) or {}
        guards = (sp.get("s05_policy") or {}).get("guards") or []
        names = [g.get("name") for g in guards if isinstance(g, dict) and g.get("name")]
        return f"- 활성 가드: {', '.join(names)}" if names else None

    register_active_policy_renderer("max_iterations", _max_iterations)
    register_active_policy_renderer("cost_budget_usd", _cost_budget)
    register_active_policy_renderer("context_window", _context_window)
    register_active_policy_renderer("s05_guards", _s05_guards)
