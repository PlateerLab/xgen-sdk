"""
하네스 이벤트 타입 정의

xgen-workflow의 SSE 포맷과 호환되는 이벤트 구조.
harness_router.py의 _convert_harness_event()가 이 이벤트를 받아 변환.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HarnessEvent:
    """모든 하네스 이벤트의 기반"""
    timestamp: str = field(default_factory=_now_iso)


@dataclass
class StageEnterEvent(HarnessEvent):
    """스테이지 시작"""
    stage_id: str = ""
    stage_name: str = ""        # display name (한국어 or 영어)
    phase: str = ""             # "ingress" | "loop" | "egress"
    step: int = 0               # 현재 스텝 (1-indexed)
    total: int = 0              # 전체 스테이지 수
    description: str = ""


@dataclass
class StageExitEvent(HarnessEvent):
    """스테이지 완료"""
    stage_id: str = ""
    stage_name: str = ""
    output: dict = field(default_factory=dict)
    score: Optional[float] = None
    step: int = 0
    total: int = 0


@dataclass
class MessageEvent(HarnessEvent):
    """LLM 스트리밍 텍스트 청크"""
    text: str = ""
    role: str = "assistant"
    is_final: bool = False


@dataclass
class ThinkingEvent(HarnessEvent):
    """Extended thinking 블록"""
    text: str = ""


@dataclass
class ToolCallEvent(HarnessEvent):
    """LLM이 도구 호출 요청"""
    tool_use_id: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    # v1.0 — 도구 출처 정보 (UI 가시성). state.metadata['tool_source_of'] 에 저장된 값.
    # 예: "mcp" | "builtin" | "xgen_node" | "rag" | <외부 source_id>
    tool_source: str = ""


@dataclass
class ToolResultEvent(HarnessEvent):
    """도구 실행 결과"""
    tool_use_id: str = ""
    tool_name: str = ""
    result: str = ""
    is_error: bool = False
    # v1.0 — ToolCallEvent 와 같은 source 식별자 (UI 가 어떤 채널에서 온 결과인지 표시)
    tool_source: str = ""


@dataclass
class EvaluationEvent(HarnessEvent):
    """검증 스테이지 평가 결과"""
    score: float = 0.0
    feedback: str = ""
    verdict: str = ""           # "pass" | "retry" | "fail"


@dataclass
class MetricsEvent(HarnessEvent):
    """최종 메트릭스"""
    duration_ms: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    tools_executed: int = 0
    iterations: int = 0
    model: str = ""


@dataclass
class ErrorEvent(HarnessEvent):
    """에러 발생.

    `message` 는 원본 trace 가 아닌 안전한 요약만 담는다. 호출부가 원본 예외를 그대로
    `str(e)` 로 밀어넣는 것을 금지 — `error_type` + `category` 만으로 충분한 분류가
    가능하도록 설계 (v0.11.24).
    """
    message: str = ""
    stage_id: str = ""
    recoverable: bool = False
    error_type: str = ""      # 예외 클래스명 (e.g. "RateLimitError")
    category: str = ""        # ErrorCategory.value (e.g. "rate_limit")


@dataclass
class DoneEvent(HarnessEvent):
    """파이프라인 완료"""
    final_output: str = ""
    success: bool = True


@dataclass
class MissingParamEvent(HarnessEvent):
    """필수 파라미터 누락 — 사용자/상위 시스템에 되물음 신호"""
    capability: str = ""         # capability name
    tool_name: str = ""
    param_name: str = ""
    param_type: str = ""
    description: str = ""
    source_hint: str = ""        # 어디서 찾으려 했는지 ("user_input" 등)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Verbose 이벤트 — HarnessConfig.verbose_events=True 시 발행
#  운영/디버깅 시 Redis vs env, capability 바인딩 경로, 스테이지 substep 가시화
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class ServiceLookupEvent(HarnessEvent):
    """설정/키 조회 경로 추적 — Redis 우선 정책이 실제 작동하는지 가시화"""
    key: str = ""                          # "ANTHROPIC_API_KEY" 등
    source: str = ""                       # "context" | "redis" | "env" | "fallback" | "missing"
    hit: bool = False
    provider: str = ""                     # 프로바이더 스코프 (있을 때)


@dataclass
class CapabilityBindEvent(HarnessEvent):
    """capability 바인딩 — 선언/발견/자동발행 3경로 중 어느 것"""
    name: str = ""
    source: str = ""                       # "declaration" | "discovery" | "auto_publish"
    score: Optional[float] = None
    stage_id: str = ""


@dataclass
class StageSubstepEvent(HarnessEvent):
    """스테이지 내부 단계 — 스테이지 블랙박스 해소"""
    stage_id: str = ""
    substep: str = ""                      # "rag_fetch_start" / "llm_request" / "tool_exec" 등
    duration_ms: Optional[int] = None
    meta: dict = field(default_factory=dict)


@dataclass
class RetryEvent(HarnessEvent):
    """재시도/폴백 — 어떤 스테이지/이유/몇 번째 시도"""
    stage_id: str = ""
    reason: str = ""
    attempt: int = 1
    max_attempts: int = 1


@dataclass
class ToolDeferredEvent(HarnessEvent):
    """v1.2.0 — s04_tool 가 도구를 deferred 카탈로그로 분류한 사실 보고.

    Claude Code 스타일 progressive disclosure:
      eager  : Anthropic API tools= 에 full schema 박힘. 즉시 호출 가능.
      deferred: 이름 + 1줄 desc 만 system_prompt 에 노출. schema 는 cache 에만.
                LLM 이 ToolSearch 빌트인으로 names 지정 → 그제야 schema 합류.

    UI 는 이 이벤트로 "도구 X개는 즉시 가용, Y개는 검색해야 호출 가능" 배지를 띄운다.
    """
    eager_count: int = 0
    deferred_count: int = 0
    eager_names: list = field(default_factory=list)        # 디버깅용 — eager 박힌 도구 이름
    deferred_names: list = field(default_factory=list)     # v1.8.0 — deferred 박힌 도구 이름 (UI 노출 정합)
    stage_id: str = "s04_tool"


@dataclass
class ToolLoadedEvent(HarnessEvent):
    """v1.2.0 — ToolSearch 빌트인이 deferred 도구 schema 를 eager 로 승격한 사실.

    승격 후 다음 llm_call 의 tools= 인자에 자동 합류 (state.tool_definitions
    가 매 호출마다 그대로 전달됨). UI 는 "도구 N개 활성화" 토스트 등으로 표시.
    """
    names: list = field(default_factory=list)
    total_loaded: int = 0
    stage_id: str = "s07_act"


@dataclass
class PolicyBlockedEvent(HarnessEvent):
    """Policy Gate 가 Guard 체크에서 block 한 사실을 외부에 알림.

    UI 는 이 이벤트로 "정책 차단" 배너를 띄우고, 사용자에게 어떤 Guard 가
    어떤 훅에서 왜 막았는지 가시화. 본 이벤트가 없으면 s05_policy 는
    `bypassed: true` 로만 보여 "정책이 돌긴 했나?" 의심을 산다.
    """
    guard_name: str = ""                     # "cost_budget" / "iteration" / ...
    hook: str = ""                            # "loop_boundary" / "pre_main" / "post_response" / "pre_tool"
    reason: str = ""                          # Guard 가 돌려준 사유 (예: "비용 예산 초과 ($0.0170 >= $0.00)")
    severity: str = "block"                   # "block" 만 emit (warn/info 는 안 흘림)
    tool_name: str = ""                       # PRE_TOOL 차단 시 어떤 도구가 막혔는지


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Harness Planner — v0.12.0 "Real Harness" 축 A
#  LLM 이 카탈로그를 보고 Stage/파라미터/Strategy 를 런타임 조립한 결과.
#  프론트는 이 이벤트를 카드로 렌더해 "왜 이 조합인가" 를 사용자에게 보여준다.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PlanningEvent(HarnessEvent):
    """Harness Planner 가 수립한 실행 계획"""
    chosen: list = field(default_factory=list)          # 실행할 stage_id 순서
    skipped: dict = field(default_factory=dict)         # stage_id → 스킵 이유
    params: dict = field(default_factory=dict)          # stage_id → {key: value} override
    strategies: dict = field(default_factory=dict)      # stage_id → active strategy 이름
    reasoning: str = ""                                 # 선택 근거 (explainability)
    planner_model: str = ""                             # Plan 을 만든 모델 식별자
    source: str = "llm"                                 # "llm" | "fallback_all" | "error"
    iteration: int = 0                                  # 몇 번째 replan 인지 (0=Phase A 첫 Plan, 1~=iterative)
    done: bool = False                                  # Planner 가 "이 iter 로 종료" 선언
    max_iterations: int = 0                             # v0.15.0 — LLM 이 제시한 이번 요청 적정 반복 수 (0=기본값 유지)
    orchestrator_hint: str = ""                         # v0.15.0 — "linear"|"iterative"|"react"|"plan_execute"|"dag"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HITL (Human-In-The-Loop) — v0.24.0
#  destructive/open_world 도구 호출 직전에 사용자 승인 요구. 프론트가 이 이벤트를
#  받으면 모달을 띄우고, 사용자 결정을 이식측 `/approvals/{id}` 로 POST → 엔진
#  `state.resolve_approval()` 이 대기 중인 future 를 풀어 실행 재개.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class ApprovalRequiredEvent(HarnessEvent):
    """파괴적 도구 실행 전 사용자 승인 요청."""
    approval_id: str = ""                               # 이식측이 resolve 시 참조할 고유 id
    tool_name: str = ""
    tool_use_id: str = ""
    tool_input: dict = field(default_factory=dict)
    guard_name: str = ""                                # 승인을 트리거한 Guard (예: "hitl")
    annotations: dict = field(default_factory=dict)    # readOnlyHint/destructiveHint/... 전달
    reason: str = ""                                    # "destructiveHint=true" 등
    timeout_sec: int = 0                                # 0 이면 무한 대기 (이식측 기본 5분 권장)


@dataclass
class ApprovalDecidedEvent(HarnessEvent):
    """승인 결정 결과 (감사·replay 용 이벤트 스트림 기록)."""
    approval_id: str = ""
    decision: str = ""                                  # "approve" | "deny" | "timeout"
    reason: str = ""                                    # 사용자가 제공한 사유 (deny 시 LLM 에 전달)
    edited_input: dict = field(default_factory=dict)   # 승인자가 args 를 수정했으면 그 값


def event_to_dict(event: HarnessEvent) -> dict[str, Any]:
    """이벤트를 harness_router.py가 이해하는 (event_type, data) dict로 변환"""
    type_map = {
        StageEnterEvent: "stage_enter",
        StageExitEvent: "stage_exit",
        MessageEvent: "message",
        ThinkingEvent: "thinking",
        ToolCallEvent: "tool_call",
        ToolResultEvent: "tool_result",
        EvaluationEvent: "evaluation",
        MetricsEvent: "metrics",
        ErrorEvent: "error",
        DoneEvent: "done",
        MissingParamEvent: "missing_param",
        ServiceLookupEvent: "service_lookup",
        CapabilityBindEvent: "capability_bind",
        StageSubstepEvent: "stage_substep",
        RetryEvent: "retry",
        PolicyBlockedEvent: "policy_blocked",
        PlanningEvent: "planning",
        ApprovalRequiredEvent: "approval_required",
        ApprovalDecidedEvent: "approval_decided",
    }
    event_type = type_map.get(type(event), "unknown")

    data = {}
    # v0.11.27 — 이전 필터 `v != 0 and v != 0.0 and v != ""` 은 합법적인 0 값
    # (total_tokens=0, duration_ms=0, cost_usd=0.0, iterations=0 등) 을 전부 drop 시켜
    # 프론트 집계/그래프가 undefined 로 깨졌다. 이제 `None` 만 필터하여 0 값을 그대로 전달.
    # 단, 빈 dict 는 의미 없는 noise 라 그대로 제외.
    for k, v in event.__dict__.items():
        if k == "timestamp":
            continue
        if v is None:
            continue
        if v == {}:
            continue
        data[k] = v

    data["timestamp"] = event.timestamp
    return {"event_type": event_type, "data": data}
