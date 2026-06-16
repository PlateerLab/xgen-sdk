"""
Preset 시스템 — 스테이지 활성/비활성 + Strategy 일괄 변경 (v1.0)

각 Preset은 10개 스테이지 중 어떤 것을 켜고 끌지, 어떤 strategy 를 쓸지 정의.

v1.0 변경:
- s05_strategy / s08_judge / s10_save / s12_publish 삭제 (분해/격하)
- s09_decide → s08_decide / s11_finalize → s09_finalize 번호 시프트
- judge / save 는 별도 stage 가 아니라 strategy 격하 → disabled_stages 가 아닌 active_strategies 로 토글

사용:
    config = HarnessConfig.from_preset("agent")
"""

from dataclasses import dataclass, field
from typing import Any

from .config import ALL_STAGES, REQUIRED_STAGES


@dataclass
class Preset:
    """프리셋 정의"""
    name: str
    description: str
    description_ko: str
    # 비활성화할 스테이지 (REQUIRED_STAGES는 비활성화 불가)
    disabled_stages: set[str] = field(default_factory=set)
    # Strategy 선택
    active_strategies: dict[str, str] = field(default_factory=dict)
    # 기본 stage_params
    default_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 기본 설정
    temperature: float = 0.7
    # default 는 외부 (이식측/프론트) 가 owns. 엔진은 sentinel 0.
    max_iterations: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  내장 프리셋 — 모든 텍스트는 외부 등록 가능 (박제 0)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PRESETS: dict[str, Preset] = {
    "minimal": Preset(
        name="minimal",
        description="Minimal chat — no tools, no RAG, no policy, no judge",
        description_ko="최소 채팅 — 도구/RAG/정책/평가 없이 바로 대화",
        disabled_stages={"s02_history", "s04_tool", "s05_policy", "s06_context", "s07_act"},
        active_strategies={
            "s08_decide": "always_pass",
            "s09_finalize": "noop",   # save 비활성
        },
        temperature=0.7,
        # max_iterations 미지정 — 이식측이 자기 default 박음
    ),

    "chat": Preset(
        name="chat",
        description="Chat with memory — conversation history maintained",
        description_ko="대화형 — 이전 대화 이력 유지, 멀티턴",
        disabled_stages={"s04_tool", "s05_policy", "s06_context", "s07_act"},
        active_strategies={
            "s08_decide": "always_pass",
            "s09_finalize": "noop",
        },
        temperature=0.7,
    ),

    "agent": Preset(
        name="agent",
        description="Full agent — tools, RAG, policy, judge, loop",
        description_ko="에이전트 — 도구·RAG·정책·평가·루프 전부 활성",
        disabled_stages=set(),
        active_strategies={
            "s04_tool": "progressive_3level",
            "s08_decide": "threshold",      # judge 없는 단순 결정
            "s09_finalize": "default",
        },
        temperature=0.3,
        # v1.8.0 — 10 → 5 하향. 5-tier PD graph 가 5 turn 안에 완결되도록 정합:
        # T1 메타 인식 → T2 도구 호출 → T3 결과 합성 또는 추가 → T4 최종 시도 → T5 합성/종료.
        # 약한 모델 (Qwen 등) 이 자율 stop 안 해도 5 iter 안에 강제 종료 → UX 손상 적음.
        max_iterations=5,
    ),

    "evaluator": Preset(
        name="evaluator",
        description="Evaluator agent — strict validation with LLM judge (in-decide)",
        description_ko="평가형 — s08_decide 의 judge_then_loop strategy 로 엄격 검증",
        disabled_stages=set(),
        active_strategies={
            "s04_tool": "progressive_3level",
            "s08_decide": "judge_then_loop",   # judge 격하: decide 안 strategy
            "s09_finalize": "persist",
        },
        default_params={
            "s08_decide": {"judge_threshold": 0.8},
        },
        temperature=0.2,
        max_iterations=15,
    ),

    "rag": Preset(
        name="rag",
        description="RAG-focused — document search, no tools",
        description_ko="RAG 전용 — 문서 검색 기반 답변, 도구 없음",
        disabled_stages={"s04_tool", "s07_act"},
        active_strategies={
            "s08_decide": "always_pass",
            "s09_finalize": "noop",
        },
        temperature=0.3,
        max_iterations=5,
    ),

    "multi_agent": Preset(
        name="multi_agent",
        description="Multi-agent — s00_harness multi_agent strategy (RAG fan-out + 종합)",
        description_ko="멀티 에이전트 — s00 의 multi_agent 카드로 컬렉션별 sub-agent 병렬",
        disabled_stages={"s04_tool", "s07_act"},
        active_strategies={
            "s00_harness": "multi_agent",
            "s08_decide": "always_pass",
            "s09_finalize": "default",
        },
        temperature=0.3,
        max_iterations=2,
    ),
}


def register_preset(preset: Preset) -> None:
    """외부 패키지가 자기 프리셋 등록. 같은 이름이면 덮어씀."""
    PRESETS[preset.name] = preset


def get_preset(name: str) -> Preset | None:
    return PRESETS.get(name)


def list_presets() -> list[dict[str, Any]]:
    """프리셋 목록 반환 (API/UI용)"""
    return [
        {
            "name": p.name,
            "description": p.description,
            "description_ko": p.description_ko,
            "disabled_stages": list(p.disabled_stages),
            "active_strategies": p.active_strategies,
            "temperature": p.temperature,
            "max_iterations": p.max_iterations,
        }
        for p in PRESETS.values()
    ]


def apply_preset(config, preset_name: str) -> None:
    """HarnessConfig에 프리셋 적용"""
    preset = get_preset(preset_name)
    if not preset:
        return

    config.disabled_stages = preset.disabled_stages - REQUIRED_STAGES
    config.active_strategies = dict(preset.active_strategies)
    config.temperature = preset.temperature
    config.max_iterations = preset.max_iterations

    # stage_params 병합 (기존 설정 유지, 프리셋 기본값 추가)
    for stage_id, params in preset.default_params.items():
        existing = config.stage_params.get(stage_id, {})
        merged = {**params, **existing}  # 기존 설정이 우선
        config.stage_params[stage_id] = merged
