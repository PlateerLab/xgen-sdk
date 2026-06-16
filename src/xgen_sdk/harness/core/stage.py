"""
Stage ABC — 모든 스테이지의 기반 인터페이스

Stage×Strategy 이중 추상화 기반 인터페이스.
Dual Abstraction:
  Level 1: Stage 전체 교체 (artifact)
  Level 2: Stage 내부 Strategy 교체
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .state import PipelineState


# --- 표시 이름 매핑 (사용자 편의 용어 강제) ---

# v1.0 — 11→10 통합. s05_strategy 분해 / s08_judge·s10_save 격하 / s12_publish 삭제.
STAGE_DISPLAY_NAMES: dict[str, str] = {
    "s00_harness":    "Settings",
    "s01_input":      "Input",
    "s02_history":    "History",
    "s03_prompt":     "Prompt",
    "s04_tool":       "Tool",
    "s05_policy":     "Policy",
    "s06_context":    "Context",
    "s07_act":        "Act",
    "s08_decide":     "Decide",
    "s09_finalize":   "Finalize",
}

# v1.7.1 — s00_harness "Auto" 폐기. v1.1.0 BREAKING 으로 mode 시스템 (Auto/Selected/Off)
# 자체 사라졌는데 stage 표시명 잔재로 "Auto" 가 남아 EventLog 에 raw 노출됨.
# frontend stage-list.tsx / stage-detail-panel.tsx 이 이미 "Settings/설정" 으로
# override 박았으므로 엔진도 동일 표현으로 통일 — override 자연스럽게 noop.
STAGE_DISPLAY_NAMES_KO: dict[str, str] = {
    "s00_harness":    "설정",
    "s01_input":      "입력",
    "s02_history":    "이력",
    "s03_prompt":     "프롬프트",
    "s04_tool":       "도구",
    "s05_policy":     "정책",
    "s06_context":    "컨텍스트",
    "s07_act":        "실행",
    "s08_decide":     "결정",
    "s09_finalize":   "마무리",
}


@dataclass
class StrategyInfo:
    """스테이지 내부 전략 정보"""
    name: str
    description: str
    is_default: bool = False


@dataclass
class StageDescription:
    """스테이지 설명 (UI/API용)"""
    stage_id: str
    display_name: str
    display_name_ko: str
    phase: str                          # "ingress" | "loop" | "egress"
    order: int
    description: str = ""
    required: bool = False
    active: bool = True
    # Pipeline role — "policy_gate" / "orchestrator_planner" / "main_actor" / "scorer" / "".
    # 프론트가 hook-only Stage (policy_gate) 를 일반 loop 순번에서 분리 렌더할 때 기준.
    role: str = ""
    strategies: list[StrategyInfo] = field(default_factory=list)
    # I/O 계약 (Stage 인터페이스 정형화)
    input_requires: list[str] = field(default_factory=list)
    input_optional: list[str] = field(default_factory=list)
    output_produces: list[str] = field(default_factory=list)
    output_modifies: list[str] = field(default_factory=list)


class Stage(ABC):
    """하네스 스테이지 기반 클래스

    Stage×Strategy 이중 추상화:
      Level 1: Stage 전체 교체 (Artifact) — 같은 stage_id, 다른 구현
      Level 2: Stage 내부 Strategy 교체 — 같은 Stage, 다른 전략

    I/O 계약:
      각 Stage는 input_spec/output_spec으로 뭘 받고 뭘 내보내는지 선언.
      Pipeline이 실행 전에 input 검증, 실행 후에 output 추적.
    """

    @property
    @abstractmethod
    def stage_id(self) -> str:
        """내부 ID (예: 's07_act')"""
        ...

    @property
    @abstractmethod
    def order(self) -> int:
        """실행 순서. v1.0 통합 기준 0~9 (s00_harness=0 본문 LLM 호출 stage + s01_input~s09_finalize)."""
        ...

    @property
    def input_spec(self):
        """이 스테이지가 요구하는 입력. override 가능."""
        from .stage_io import get_stage_io
        spec = get_stage_io(self.stage_id)
        return spec.get("input")

    @property
    def output_spec(self):
        """이 스테이지가 생산하는 출력. override 가능."""
        from .stage_io import get_stage_io
        spec = get_stage_io(self.stage_id)
        return spec.get("output")

    # v0.16.6 — Pipeline Role 체계. Pipeline Orchestrator 가 특정 Stage 를
    # 이름(stage_id)으로 알고 있던 12 지점을 **Stage 가 자기 역할을 선언** 하는
    # 방식으로 전환. 외부 기여자가 자기 Stage 를 같은 역할로 바꿔 끼우면 Pipeline
    # 코드 변경 0. 하드코딩 리터럴 제거의 핵심.
    #
    # 엔진이 알고 있는 역할 (확장 자유):
    #   "orchestrator_planner" — Plan 수립 + ingress 최상단 prepend + bypass 금지.
    #                            Pipeline Phase B iter 시작에 재실행(replan).
    #   "main_actor"           — 본문 LLM 호출을 이 Stage **직전** 에 주입.
    #                            (과거 s07_llm 자리를 planner.main_call 이 메움)
    #   "scorer"               — StageExit 이벤트의 score 필드에
    #                            state.validation_score 를 노출.
    #
    # 외부 플러그인이 새 role 을 도입해도 Pipeline 은 그 role 을 모르고 단순히
    # `_find_by_role(name)` 이 None 반환 → 그 분기만 조용히 비활성.
    @property
    def role(self) -> str:
        return ""

    # v0.17.0 — Machine-only Stage meta.
    # LLM(planner) 이 실제로 읽는 선택 근거 필드.
    # 인간 UI 설명문(description_ko/behavior/icon 등)은 엔진에 두지 않는다 —
    # 그건 LLM 이 안 읽고 UI 편의일 뿐. 확장성·연동성 기준에서는 노이즈.
    # Stage 클래스가 class attribute 로 override 선언:
    #   class MyStage(Stage):
    #       when_to_use = "..."
    #       when_to_skip = "..."
    #       cost_hint = "low"  # low|medium|high
    when_to_use: str = ""
    when_to_skip: str = ""
    cost_hint: str = "medium"

    @property
    def display_name(self) -> str:
        return STAGE_DISPLAY_NAMES.get(self.stage_id, self.stage_id)

    @property
    def display_name_ko(self) -> str:
        return STAGE_DISPLAY_NAMES_KO.get(self.stage_id, self.display_name)

    @property
    def phase(self) -> str:
        """Stage 가 속한 phase 이름.

        v0.15.1 — phase 경계를 `PHASE_ORDER_BOUNDARIES` 레지스트리에서 읽어 하드코딩
        제거. 외부 패키지가 `register_phase("post_egress", upper_order=99)` 한 줄로
        새 phase 합류 가능. Stage 서브클래스가 이 property 를 override 하면 값 그대로 사용.
        """
        from .phase_registry import resolve_phase
        return resolve_phase(self.order)

    @abstractmethod
    async def execute(self, state: "PipelineState") -> dict:
        """스테이지 실행. state를 직접 변경하고 결과 dict를 반환."""
        ...

    def should_bypass(self, state: "PipelineState") -> bool:
        """이 스테이지를 건너뛸지 결정"""
        return False

    async def on_enter(self, state: "PipelineState") -> None:
        """스테이지 시작 전 훅"""
        pass

    async def on_exit(self, result: dict, state: "PipelineState") -> None:
        """스테이지 완료 후 훅"""
        pass

    async def on_error(self, error: Exception, state: "PipelineState") -> Optional[dict]:
        """에러 발생 시 훅. dict 반환 시 복구, None 반환 시 전파."""
        return None

    def describe(self) -> StageDescription:
        desc = StageDescription(
            stage_id=self.stage_id,
            display_name=self.display_name,
            display_name_ko=self.display_name_ko,
            phase=self.phase,
            order=self.order,
            role=self.role,
            strategies=self.list_strategies(),
        )
        # I/O 스펙 포함
        if self.input_spec:
            desc.input_requires = self.input_spec.requires
            desc.input_optional = self.input_spec.optional
        if self.output_spec:
            desc.output_produces = self.output_spec.produces
            desc.output_modifies = self.output_spec.modifies
        return desc

    def get_param(self, key: str, state: "PipelineState", default=None):
        """스테이지 파라미터 조회.

        조회 순서:
        1. state.config.stage_params[stage_id][key]  (사용자가 UI에서 설정한 값)
        2. default 인자                                (코드에서 넘긴 값, 보통 config.xxx)
        3. stage_config.py 기본값                      (개발자가 정의한 스키마 기본값)

        주의: stage_config 기본값이 코드 폴백보다 우선하면
        config에 설정된 값을 덮어씌우는 버그가 발생.
        (예: config.provider="openai"인데 stage_config.default="anthropic"이 반환)
        """
        # 1. 사용자 설정 (런타임 — UI에서 명시 설정)
        if hasattr(state, "config") and state.config:
            params = state.config.stage_params.get(self.stage_id, {})
            if key in params:
                return params[key]
            # v1.0 — Strategy 카드 픽 (active_strategies) 도 "strategy" 키로 lookup 가능.
            # UI 드롭다운(active_strategies)과 stage_params.strategy 가 서로 못 보던 mismatch 해소.
            if key == "strategy":
                active = getattr(state.config, "active_strategies", None) or {}
                if self.stage_id in active:
                    return active[self.stage_id]

        # 2. 코드 폴백 (config.provider 등 — 호출자가 넘긴 값)
        if default is not None:
            return default

        # 3. stage_config.py 스키마 기본값 (최후 폴백)
        from .stage_config import get_stage_config
        cfg = get_stage_config(self.stage_id)
        for f in cfg.get("fields", []):
            if f.get("id") == key:
                return f.get("default")

        return None

    def resolve_strategy(self, slot_name: str, state: "PipelineState", default_impl: str = ""):
        """Strategy 이름을 인스턴스로 해석.

        조회 순서:
        1. state.config.active_strategies[stage_id]  (UI에서 선택한 전략)
        2. state.config.stage_params[stage_id]["strategy"]
        3. default_impl 인자

        **Strategy Variants (v0.10.4+)**:
        active_strategies 값이 config.strategy_variants[stage_id] 에 선언된
        variant 이름이면, variant.base 로 resolver 를 태우고 variant.params 를
        configure 에 병합. "디폴트 건드리지 않고 복사해서 v2 쓰기" 지원.
        """
        from .strategy_resolver import StrategyResolver

        # 사용자가 선택한 전략 이름 조회
        impl_name = default_impl
        if hasattr(state, "config") and state.config:
            # active_strategies에서 먼저 (UI 드롭다운)
            active = getattr(state.config, "active_strategies", {})
            if self.stage_id in active:
                impl_name = active[self.stage_id]
            # stage_params에서 폴백
            params = state.config.stage_params.get(self.stage_id, {})
            if "strategy" in params:
                impl_name = params["strategy"]
            # slot별 파라미터
            if slot_name in params:
                impl_name = params[slot_name]

        if not impl_name:
            return None

        resolver = StrategyResolver.default()
        strategy_config: dict = {}
        if hasattr(state, "config") and state.config:
            strategy_config = dict(state.config.stage_params.get(self.stage_id, {}))

        # variants 해결: impl_name 이 variant 이면 base 로 바꾸고 params 병합
        resolve_impl = impl_name
        if hasattr(state, "config") and state.config:
            variants = getattr(state.config, "strategy_variants", {}) or {}
            for v in variants.get(self.stage_id, []) or []:
                if v.get("name") == impl_name and v.get("base"):
                    resolve_impl = v["base"]
                    strategy_config.update(v.get("params") or {})
                    break

        return resolver.resolve(self.stage_id, slot_name, resolve_impl, strategy_config)

    def list_strategies(self) -> list[StrategyInfo]:
        return []

    @classmethod
    def describe_config(cls) -> Optional[dict]:
        """Stage 가 자기 UI 설정 스키마를 self-describe.

        v0.17.0 — `stage_config.STAGE_CONFIGS` dict 중앙화에서 벗어나는 경로.
        Stage 가 이 메서드를 override 해서 `{description_ko, description_en,
        when_to_use, when_to_skip, cost_hint, icon, fields, behavior}` dict 를
        반환하면 `get_stage_config()` 가 중앙 dict 대신 이 값을 사용.

        기본 구현: None 반환 — 기존 STAGE_CONFIGS dict 경로 유지 (하위 호환).
        새 Stage 는 override 해서 dict 박제 없이 자체 선언 권장.
        """
        return None
