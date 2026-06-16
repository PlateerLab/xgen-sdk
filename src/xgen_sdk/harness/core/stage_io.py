"""
Stage I/O 계약 — 각 스테이지의 Input/Output 타입 정의

geny-harness의 Stage trait 패턴:
  - 각 Stage는 뭘 받고(Input) 뭘 내보내는지(Output) 명확히 선언
  - 실행기(Pipeline)가 타입 검증
  - Artifact를 갈아끼워도 I/O 계약은 동일

사용:
    class ActStage(Stage):
        input_spec = StageInput(
            requires=["pending_tool_calls"],
            optional=["provider"],
        )
        output_spec = StageOutput(
            produces=["tool_results"],
            modifies=["messages"],
        )
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageInput:
    """스테이지가 요구하는 입력 필드 선언.

    requires: 반드시 있어야 하는 PipelineState 필드
    optional: 있으면 사용하지만 없어도 실행 가능
    """
    requires: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)

    def validate(self, state: Any) -> list[str]:
        """state에서 필수 필드가 있는지 검증. 누락된 필드 목록 반환."""
        missing = []
        for field_name in self.requires:
            val = getattr(state, field_name, None)
            if val is None:
                # dict 형태 필드도 확인
                if hasattr(state, 'metadata') and field_name in state.metadata:
                    continue
                missing.append(field_name)
            elif isinstance(val, (str, list, dict)) and not val:
                # 빈 문자열/리스트/딕트도 누락으로 간주하지 않음 (명시적 빈 값은 OK)
                pass
        return missing


@dataclass
class StageOutput:
    """스테이지가 생산하는 출력 필드 선언.

    produces: 이 스테이지가 새로 생성하는 PipelineState 필드
    modifies: 이 스테이지가 수정하는 기존 필드
    events: 이 스테이지가 발행하는 이벤트 타입
    """
    produces: list[str] = field(default_factory=list)
    modifies: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10개 스테이지 I/O 계약 레지스트리 (v1.0)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STAGE_IO_SPECS: dict[str, dict[str, StageInput | StageOutput]] = {
    "s01_input": {
        "input": StageInput(
            requires=["user_input"],
            optional=["attached_files", "workflow_data"],
        ),
        "output": StageOutput(
            produces=["provider"],
            modifies=["messages", "tool_definitions"],
            events=["StageEnterEvent", "StageExitEvent"],
        ),
    },
    "s02_history": {
        "input": StageInput(
            requires=[],
            optional=["previous_results", "conversation_history"],
        ),
        "output": StageOutput(
            modifies=["messages"],
            events=["StageEnterEvent", "StageExitEvent"],
        ),
    },
    "s03_prompt": {
        "input": StageInput(
            requires=[],
            optional=["rag_context", "previous_results"],
        ),
        "output": StageOutput(
            produces=["system_prompt"],
            events=["StageEnterEvent", "StageExitEvent"],
        ),
    },
    "s04_tool": {
        "input": StageInput(
            requires=[],
            optional=["tool_definitions"],
        ),
        "output": StageOutput(
            produces=["tool_index", "tool_schemas"],
            modifies=["tool_definitions"],
            events=["StageEnterEvent", "StageExitEvent"],
        ),
    },
    "s05_policy": {
        "input": StageInput(
            requires=[],
            optional=["pending_tool_calls", "messages", "last_assistant_text"],
        ),
        "output": StageOutput(
            produces=["loop_decision"],
            events=["StageEnterEvent", "StageExitEvent", "PolicyBlockEvent"],
        ),
    },
    "s06_context": {
        "input": StageInput(
            requires=["user_input"],
            optional=["messages", "system_prompt"],
        ),
        "output": StageOutput(
            modifies=["system_prompt", "messages"],
            events=["StageEnterEvent", "StageExitEvent"],
        ),
    },
    # 본문 LLM 호출은 s00_harness.main_call 이 담당. 도구 호출만 s07.
    "s07_act": {
        "input": StageInput(
            requires=["pending_tool_calls"],
            optional=["provider"],
        ),
        "output": StageOutput(
            produces=["tool_results"],
            modifies=["messages", "pending_tool_calls"],
            events=["StageEnterEvent", "StageExitEvent", "ToolCallEvent", "ToolResultEvent"],
        ),
    },
    # v1.0 — s08_judge → s08_decide 의 judge_then_loop strategy 로 격하 (stage 통합).
    "s08_decide": {
        "input": StageInput(
            requires=[],
            optional=["pending_tool_calls", "last_assistant_text", "provider", "user_input"],
        ),
        "output": StageOutput(
            produces=["loop_decision", "validation_score", "validation_feedback"],
            events=["StageEnterEvent", "StageExitEvent", "EvaluationEvent"],
        ),
    },
    # v1.0 — s10_save → s09_finalize 의 persist strategy 로 격하 (stage 통합).
    "s09_finalize": {
        "input": StageInput(
            requires=[],
            optional=["final_output", "token_usage", "cost_usd"],
        ),
        "output": StageOutput(
            produces=["final_output"],
            events=["StageEnterEvent", "StageExitEvent", "MetricsEvent", "DoneEvent"],
        ),
    },
    # s00_harness — 본문 LLM 호출 stage. Phase A 에서 Plan 생성, Phase B 루프 안에서 main_call 로 본문 LLM 호출.
    "s00_harness": {
        "input": StageInput(
            requires=["user_input"],
            optional=["messages", "system_prompt", "tool_definitions"],
        ),
        "output": StageOutput(
            produces=["provider", "last_assistant_text"],
            modifies=["messages", "pending_tool_calls", "token_usage", "cost_usd"],
            events=[
                "StageEnterEvent", "StageExitEvent", "PlanningEvent",
                "MessageEvent", "ThinkingEvent", "ToolCallEvent",
            ],
        ),
    },
}


def get_stage_io(stage_id: str) -> dict[str, StageInput | StageOutput]:
    """스테이지 I/O 스펙 조회"""
    return STAGE_IO_SPECS.get(stage_id, {})


def validate_stage_input(stage_id: str, state: Any) -> list[str]:
    """스테이지 실행 전 입력 검증"""
    spec = STAGE_IO_SPECS.get(stage_id, {})
    input_spec = spec.get("input")
    if not input_spec:
        return []
    return input_spec.validate(state)
