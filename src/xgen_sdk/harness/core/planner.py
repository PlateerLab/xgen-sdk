"""
HarnessPlan — Pipeline 직선 흐름의 pass-through 계획 객체.

v1.1.0 부터 Planner(LLM 계획자)는 폐지되고 **직선 흐름 고정**.
`s00_harness.execute()` 가 매 실행마다 `HarnessPlan.off_mode()` 를 박아
빈 Plan(= 전체 stage 직선 실행)으로 통과시킨다. 도구 호출은 사용자가 박은
`state.config.selected_tools` 를 그대로 적용한다.

과거(v0.12.0~v1.0.x): LLM 이 카탈로그(when_to_use / when_to_skip / cost_hint)를
보고 `submit_plan` 도구로 Stage·파라미터·Strategy 를 런타임 조립하던
`HarnessPlanner` 가 있었으나, 직선 흐름 단순화(v1.1.0)로 LLM 계획자 경로 전체가
제거됐다. 호환을 위해 `HarnessPlan` dataclass 만 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ───────────────────────────────────────────────────────────────────
#  Plan dataclass
# ───────────────────────────────────────────────────────────────────

@dataclass
class HarnessPlan:
    """직선 흐름 pass-through 계획 객체.

    Attributes
    ----------
    chosen : list[str]
        실행할 stage_id. 빈 리스트 = 전체 실행(직선 흐름).
    skipped : dict[str, str]
        stage_id → 스킵 이유. 프론트 표시용.
    params : dict[str, dict[str, Any]]
        stage_id → 파라미터 override. Pipeline 실행 전 state.config.stage_params 에 병합.
    strategies : dict[str, str]
        stage_id → Strategy 이름. state.config.active_strategies 에 병합.
    reasoning : str
        선택 근거. 사람 납득용 (explainability).
    done : bool
        loop 종료 신호.
    source : str
        Plan 출처 추적. 현재 직선 흐름에선 항상 "off".
    planner_model : str
        Plan 을 만든 모델 식별자 (디버그/감사용).
    max_iterations : Optional[int]
        None / "" 이면 config 기본값 유지.
    orchestrator_hint : str
        실행 패턴 힌트. 이식측 dispatcher / 프론트가 해석.
    """
    chosen: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    strategies: dict[str, str] = field(default_factory=dict)
    reasoning: str = ""
    done: bool = False
    source: str = "llm"
    planner_model: str = ""
    max_iterations: Optional[int] = None
    orchestrator_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def off_mode(cls, reason: str = "planner_disabled — Planner OFF 고정 (v1.1.0)") -> "HarnessPlan":
        """v1.1.0 — Planner OFF 직선 흐름 고정. 빈 Plan 통과.

        s00_harness.execute() 가 항상 이 Plan 을 박는다. 빈 chosen 은 Pipeline 에서
        '전체 stage 실행' = 직선 흐름. 도구 호출은 사용자가 박은
        state.config.selected_tools 그대로 적용.
        """
        return cls(chosen=[], reasoning=reason, source="off")
