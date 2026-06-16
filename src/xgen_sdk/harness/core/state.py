"""
PipelineState — 파이프라인 실행 상태

모든 스테이지가 공유하는 뮤터블 상태 객체.
하네스 파이프라인 실행 상태.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..events.emitter import EventEmitter
    from ..providers.base import LLMProvider
    from .config import HarnessConfig


@dataclass
class TokenUsage:
    """토큰 사용량 추적"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        return self


@dataclass
class ToolGroup:
    """도구 관련 state 서브-그룹 — v0.11.22 에서 PipelineState 에서 분리.

    모든 Stage 는 여전히 `state.tool_definitions` 같은 flat 경로로 접근 가능 (property shim 유지).
    새 코드는 `state.tool.definitions` 를 선호. 외부 기여자가 ToolGroup 을 서브클래싱해
    인덱스 구조나 캐시 정책을 커스터마이즈할 수 있도록 독립 dataclass 유지.
    """
    definitions: list[dict[str, Any]] = field(default_factory=list)   # Anthropic API 포맷 (eager — tools= 인자에 박힘)
    index: list[dict[str, str]] = field(default_factory=list)         # Level 1 메타데이터 (system_prompt 노출)
    schemas: dict[str, dict] = field(default_factory=dict)            # Level 2 (on-demand) — 모든 도구 full schema 캐시
    # v1.2.0 — Claude Code 스타일 deferred 카탈로그.
    # eager (definitions) 에는 사용자 명시 selected_tools + 시스템 빌트인만 들어가고,
    # 나머지 도구는 여기 이름+1줄 desc 만 노출된다. LLM 이 ToolSearch 빌트인으로
    # names 를 지정해 schema 를 schemas 캐시에서 꺼내 definitions 에 합류시키면
    # 다음 llm_call 의 tools= 에 자연스럽게 누적된다 (dynamic catalogue).
    deferred: list[dict[str, str]] = field(default_factory=list)
    loaded_names: set[str] = field(default_factory=set)
    # v1.8.0 — Claude Code Skills 패턴. Skill('도구이름') 호출로 lazy load 한 markdown
    # body 를 session 끝까지 유지. s03_prompt 의 <loaded_skills> 섹션이 이 dict 의
    # 모든 entry 를 매 turn 박음. 한 번 load 한 skill 은 재호출 X (Claude Code 정합).
    loaded_skills: dict[str, str] = field(default_factory=dict)
    pending_calls: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    executed_count: int = 0
    # v0.24.4 — MCP annotations 블록(readOnlyHint / destructiveHint / idempotentHint /
    # openWorldHint) 을 tool_name 별로 보관. definitions 에 섞여 있으면 LLM provider 가
    # unknown field 로 거부(Anthropic 400) 하므로 **payload 와 분리**. s07_act / HITLGuard
    # 가 이 맵을 1차로 조회하고, 없으면 Tool 인스턴스의 annotations() 메서드로 fallback.
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ValidationGroup:
    """검증 관련 state 서브-그룹 — v0.11.22 에서 PipelineState 에서 분리.

    EvaluationStrategy 결과 + retry 카운터 보관. 외부 EvaluationStrategy 가
    score/feedback 시그니처를 공유하므로 그룹으로 묶어두면 결과 전달 DX 개선.
    """
    score: Optional[float] = None
    feedback: str = ""
    retry_count: int = 0


@dataclass
class PipelineState:
    """파이프라인 실행 상태 — 모든 스테이지가 읽고 쓴다.

    v0.11.22 — code review B+ 지적 "100+ flat 필드 / tool_* 3 중 중복" 해소 착수.
    `tool` / `validation` 두 도메인 그룹으로 분리. 기존 Stage 코드는 `state.tool_definitions`
    같은 flat 경로로 그대로 동작하도록 property shim 을 추가 (migrate 기간).
    """

    # --- 실행 식별자 ---
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    workflow_name: str = ""
    interaction_id: str = ""
    user_id: str = ""

    # --- 설정 (초기화 시 주입) ---
    config: Optional["HarnessConfig"] = None

    # --- 입력 ---
    user_input: str = ""
    attached_files: list[dict[str, Any]] = field(default_factory=list)

    # --- LLM ---
    provider: Optional["LLMProvider"] = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""

    # --- 상태 범위 ToolSource (v1.16 — nested 격리) ---
    # 전역 register_tool_source() 와 별개로 "이 실행에만" 보이는 ToolSource.
    # s04(카탈로그)/s07(dispatch) 가 전역 + 이 리스트를 합쳐서 본다. nested
    # subpipeline 이 자기 frozen 도구를 전역 오염 없이 주입하는 데 사용 —
    # 부모/자식 파이프라인의 도구 카탈로그가 source_id 충돌 없이 격리된다.
    extra_tool_sources: list[Any] = field(default_factory=list)

    # --- 도메인 그룹 (v0.11.22 도입) ---
    tool: ToolGroup = field(default_factory=ToolGroup)
    validation: ValidationGroup = field(default_factory=ValidationGroup)

    # --- 메모리 ---
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    previous_results: list[str] = field(default_factory=list)

    # --- RAG ---
    rag_context: str = ""

    # --- 루프 제어 ---
    loop_iteration: int = 0
    loop_decision: str = "continue"    # "continue" | "complete" | "retry"

    # --- 토큰/비용 ---
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    turn_usages: list[TokenUsage] = field(default_factory=list)
    cost_usd: float = 0.0
    llm_call_count: int = 0

    # --- 타이밍 ---
    start_time: float = field(default_factory=time.time)
    stage_timings: dict[str, float] = field(default_factory=dict)

    # --- 출력 ---
    final_output: str = ""
    last_assistant_text: str = ""

    # --- 이벤트 ---
    event_emitter: Optional["EventEmitter"] = None

    # --- 워크플로우 원본 데이터 (연동용) ---
    workflow_data: dict[str, Any] = field(default_factory=dict)

    # --- 메타데이터 ---
    metadata: dict[str, Any] = field(default_factory=dict)

    # --- Policy Gate (v0.17.0) ---
    # 지금까지 호출된 tool_name 이력. s07_act 가 실행 직전에 append.
    # ToolPreconditionGuard 가 "submit_result 호출 전 iterative_document_search 가
    # N회 이상 호출됐는가" 같은 규칙을 평가할 때 참조.
    tool_call_history: list[dict[str, Any]] = field(default_factory=list)
    # Policy Gate 가 차단한 경우 이유/Guard 이름. ThresholdDecide 가 이 신호로 loop 종료.
    policy_block_reason: str = ""
    policy_block_guard: str = ""

    # --- HITL (Human-In-The-Loop) 승인 큐 — v0.24.0 ---
    # approval_id → Future. HITLGuard 가 `await_approval(id, timeout)` 으로 대기,
    # 이식측 엔드포인트가 `resolve_approval(id, decision, ...)` 로 풀어줌.
    # 이식측 SSE 중계가 `ApprovalRequiredEvent` 를 프론트에 전달 → 사용자 승인 →
    # POST /approvals/{id} → 엔진 resolve.
    _approval_futures: dict[str, "asyncio.Future[dict[str, Any]]"] = field(
        default_factory=dict, repr=False, compare=False,
    )

    # --- Progressive Disclosure 저장소 ---
    # 큰 리소스의 원본을 보존하면서 messages 에는 preview 만 노출하는 패턴의 백업 저장소.
    # Level 1 (preview in messages) → Level 2 (fetch_pd(kind, id) 빌트인으로 원본 조회) → Level 3 (본문 삽입).
    # kind: "tool_result" | "rag" | "history" | "db_schema" | "gallery" | ...
    # id:   kind 별 식별자 (tool_use_id / chunk_index / turn_index / table_name / ...)
    # value: {"preview": str, "full": str, "meta": dict} — preview 는 이미 messages 에 있고,
    #                                                     full 이 복원용 원본.
    pd_stores: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    # === 도메인 그룹 → flat 속성 shim (backward compat) ===
    # Stage 코드는 `state.tool_definitions`, `state.validation_score` 처럼 기존 경로를
    # 사용한다. 아래 property 들이 그 경로를 ToolGroup/ValidationGroup 로 투명하게 위임.
    # 신규 코드는 `state.tool.definitions` / `state.validation.score` 를 권장.

    @property
    def tool_definitions(self) -> list[dict[str, Any]]:
        return self.tool.definitions

    @tool_definitions.setter
    def tool_definitions(self, value: list[dict[str, Any]]) -> None:
        self.tool.definitions = value

    @property
    def tool_index(self) -> list[dict[str, str]]:
        return self.tool.index

    @tool_index.setter
    def tool_index(self, value: list[dict[str, str]]) -> None:
        self.tool.index = value

    @property
    def tool_schemas(self) -> dict[str, dict]:
        return self.tool.schemas

    @tool_schemas.setter
    def tool_schemas(self, value: dict[str, dict]) -> None:
        self.tool.schemas = value

    @property
    def deferred_tools(self) -> list[dict[str, str]]:
        return self.tool.deferred

    @deferred_tools.setter
    def deferred_tools(self, value: list[dict[str, str]]) -> None:
        self.tool.deferred = value

    @property
    def loaded_tool_names(self) -> set[str]:
        return self.tool.loaded_names

    @property
    def pending_tool_calls(self) -> list[dict[str, Any]]:
        return self.tool.pending_calls

    @pending_tool_calls.setter
    def pending_tool_calls(self, value: list[dict[str, Any]]) -> None:
        self.tool.pending_calls = value

    @property
    def tool_results(self) -> list[dict[str, Any]]:
        return self.tool.results

    @tool_results.setter
    def tool_results(self, value: list[dict[str, Any]]) -> None:
        self.tool.results = value

    @property
    def tools_executed_count(self) -> int:
        return self.tool.executed_count

    @tools_executed_count.setter
    def tools_executed_count(self, value: int) -> None:
        self.tool.executed_count = value

    @property
    def validation_score(self) -> Optional[float]:
        return self.validation.score

    @validation_score.setter
    def validation_score(self, value: Optional[float]) -> None:
        self.validation.score = value

    @property
    def validation_feedback(self) -> str:
        return self.validation.feedback

    @validation_feedback.setter
    def validation_feedback(self, value: str) -> None:
        self.validation.feedback = value

    @property
    def retry_count(self) -> int:
        return self.validation.retry_count

    @retry_count.setter
    def retry_count(self, value: int) -> None:
        self.validation.retry_count = value

    # === 헬퍼 메서드 ===

    def add_message(self, role: str, content: Any) -> None:
        """메시지 추가 (Anthropic API 포맷)"""
        self.messages.append({"role": role, "content": content})

    def add_tool_result(self, tool_use_id: str, content: str, is_error: bool = False) -> None:
        """도구 결과를 user 메시지로 추가"""
        result_block = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            result_block["is_error"] = True
        self.tool.results.append(result_block)

    def flush_tool_results(self) -> None:
        """축적된 도구 결과를 user 메시지로 밀어넣고 클리어"""
        if self.tool.results:
            self.add_message("user", list(self.tool.results))
            self.tool.results.clear()
            self.tool.pending_calls.clear()

    # --- Progressive Disclosure 헬퍼 ---
    def pd_store(
        self,
        kind: str,
        resource_id: str,
        preview: str,
        full: str,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        """PD 리소스 등록. messages 에는 preview 만 노출, 원본은 pd_stores 에 보존.

        kind: "tool_result" / "rag" / "history" / "db_schema" / "gallery" 등 자유 문자열.
        resource_id: kind 내부에서 고유한 식별자 (tool_use_id / chunk index / turn idx ...).
        preview: messages 경로로 흘릴 경량 요약.
        full: fetch_pd 빌트인이 돌려줄 원본.
        meta: 진단용 메타 (chars, source, truncated_at 등).
        """
        bucket = self.pd_stores.setdefault(kind, {})
        bucket[resource_id] = {
            "preview": preview,
            "full": full,
            "meta": dict(meta or {}),
        }

    def pd_fetch(self, kind: str, resource_id: str) -> Optional[dict[str, Any]]:
        """PD 리소스 조회. 없으면 None."""
        bucket = self.pd_stores.get(kind)
        if not bucket:
            return None
        return bucket.get(resource_id)

    def pd_list(self, kind: str) -> list[str]:
        """해당 kind 의 보존 리소스 id 목록."""
        return list(self.pd_stores.get(kind, {}).keys())

    async def emit_verbose(self, event: Any) -> None:
        """HarnessConfig.verbose_events=True 시에만 이벤트 발행.

        Stage/어댑터가 세밀한 관찰 이벤트(CapabilityBindEvent, StageSubstepEvent,
        RetryEvent 등)를 뿌릴 때 이 한 줄만 호출. 플래그가 꺼져 있으면 no-op —
        기본 경로에 성능/출력 영향 0.
        """
        if self.event_emitter is None:
            return
        if not (self.config and getattr(self.config, "verbose_events", False)):
            return
        try:
            await self.event_emitter.emit(event)
        except Exception as e:
            # v0.11.21 — 관찰 이벤트 실패는 실행 흐름에 영향 없음. 단 완전 swallow 는 디버깅
            # 난도를 올리므로 로거에 흔적. logging 모듈은 모듈 로드 시점에만 필요.
            import logging as _logging
            _logging.getLogger("harness.state").debug("emit_verbose suppressed: %s", e)

    # --- HITL (Human-In-The-Loop) 승인 API — v0.24.0 ---

    async def emit_event(self, event: Any) -> None:
        """verbose 여부와 무관하게 이벤트 발행. HITL 같은 실행 흐름 필수 이벤트 전용.

        `emit_verbose` 는 관찰용으로 verbose_events 플래그에 따라 게이트되지만,
        approval_required 는 놓치면 사용자 승인을 못 받아 대기가 영원해지므로
        별도 경로로 무조건 방출.
        """
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter.emit(event)
        except Exception as e:
            import logging as _logging
            _logging.getLogger("harness.state").warning("emit_event failed: %s", e)

    async def request_approval(
        self,
        *,
        approval_id: str,
        tool_name: str,
        tool_use_id: str,
        tool_input: dict[str, Any],
        guard_name: str,
        annotations: Optional[dict[str, Any]] = None,
        reason: str = "",
        timeout_sec: int = 0,
    ) -> dict[str, Any]:
        """ApprovalRequiredEvent 를 방출하고 resolve 까지 대기.

        이식측이 `resolve_approval(approval_id, decision, ...)` 호출 → 반환값 dict.
        `timeout_sec > 0` 이면 시간 초과 시 {"decision": "timeout"} 로 풀림.

        반환 dict 키:
          - decision: "approve" | "deny" | "timeout"
          - reason: 사용자 제공 사유 (deny/timeout)
          - edited_input: 승인자가 수정한 args (approve 에서만 의미 있음)
        """
        from ..events.types import ApprovalRequiredEvent, ApprovalDecidedEvent

        loop = asyncio.get_running_loop()
        future: "asyncio.Future[dict[str, Any]]" = loop.create_future()
        self._approval_futures[approval_id] = future

        await self.emit_event(ApprovalRequiredEvent(
            approval_id=approval_id,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            tool_input=dict(tool_input or {}),
            guard_name=guard_name,
            annotations=dict(annotations or {}),
            reason=reason,
            timeout_sec=int(timeout_sec or 0),
        ))

        try:
            if timeout_sec and timeout_sec > 0:
                decision = await asyncio.wait_for(future, timeout=timeout_sec)
            else:
                decision = await future
        except asyncio.TimeoutError:
            decision = {"decision": "timeout", "reason": "no response in time", "edited_input": {}}
        finally:
            self._approval_futures.pop(approval_id, None)

        await self.emit_event(ApprovalDecidedEvent(
            approval_id=approval_id,
            decision=decision.get("decision", ""),
            reason=decision.get("reason", ""),
            edited_input=dict(decision.get("edited_input") or {}),
        ))
        return decision

    def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        reason: str = "",
        edited_input: Optional[dict[str, Any]] = None,
    ) -> bool:
        """이식측이 사용자 결정을 엔진에 전달. 이미 풀렸거나 없으면 False 반환."""
        fut = self._approval_futures.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result({
            "decision": decision,
            "reason": reason or "",
            "edited_input": dict(edited_input or {}),
        })
        return True

    def pending_approval_ids(self) -> list[str]:
        """현재 대기 중인 approval id 목록. 이식측 헬스체크·취소 용."""
        return [k for k, f in self._approval_futures.items() if not f.done()]

    @property
    def elapsed_ms(self) -> int:
        return int((time.time() - self.start_time) * 1000)

    @property
    def is_over_budget(self) -> bool:
        if self.config and self.config.cost_budget_usd:
            return self.cost_usd > self.config.cost_budget_usd
        return False
    # v0.22.1 — is_over_iterations property 삭제. pipeline.py 가 직접
    # `loop_iteration < effective_max_iter` 조건으로 체크하며 override 가능한
    # max_iterations_override(OrchestratorSpec) 를 존중하므로 이 property 는 죽은 코드였음.
