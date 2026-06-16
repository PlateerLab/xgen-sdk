"""
Guard strategies — Policy Gate (v0.17.0 재설계)

## 배경
기존 _GUARD_REGISTRY dict 하드코딩 + s08_decide 내부 호출 → lotte 류
"도구 호출 직전 BLOCK" 같은 정책 요구를 못 받음. 본 재설계는 3 축으로 정리.

## 축 1 — Guard 를 1급 플러그인 (entry_points)
외부 패키지가 pyproject.toml `[project.entry-points."xgen_harness.guards"]`
한 줄 추가로 자기 Guard 클래스를 런타임 주입 가능. 내장 4종도 같은 경로로 등록.
엔진 dict 하드코딩 제거.

## 축 2 — Guard 의 자가 기술 (self-describing)
각 Guard 가 `param_schema()` / `hook_points` 를 들고 다님. UI 는 Guard 선택 시
param_schema 로 폼 동적 렌더 — 새 Guard 마다 stage_config.py 수정 불필요.

## 축 3 — 다중 훅 포인트
Guard 가 실행되어야 할 시점을 선언 (pre_main / pre_tool / post_response /
loop_boundary). Pipeline 은 각 시점에 해당 훅의 Guard 만 체인으로 실행.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..interfaces import Strategy

logger = logging.getLogger("harness.strategy.guard")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  훅 포인트 / 컨텍스트 / 스키마
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HookPoint(str, Enum):
    """Pipeline 이 Policy Gate 를 호출하는 시점.

    PRE_MAIN       — 본문 LLM 호출 직전 (입력/Plan 검증)
    PRE_TOOL       — 도구 호출 직전 (pending_tool_calls 에 대한 정책)
    POST_RESPONSE  — LLM 응답 직후 (출력 텍스트·페이로드 검증)
    LOOP_BOUNDARY  — 루프 경계 (예산·반복 등 누적 검사)
    """
    PRE_MAIN = "pre_main"
    PRE_TOOL = "pre_tool"
    POST_RESPONSE = "post_response"
    LOOP_BOUNDARY = "loop_boundary"


@dataclass
class HookContext:
    """훅 호출 시 Guard 에게 전달되는 부가 정보.

    Guard 는 이 컨텍스트로 "무슨 훅에서 호출됐는지" + "어떤 도구 호출이 대상인지"
    를 안다. PRE_TOOL 에서는 `pending_tool_call` 이 단일 도구 호출로 채워짐.
    """
    hook: HookPoint
    pending_tool_call: Optional[dict[str, Any]] = None
    tool_call_history: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FieldSchema:
    """Guard 가 자기 UI 폼 스키마를 선언하기 위한 필드 정의 — machine-only.

    v0.17.0 — label / description / placeholder 한국어 리터럴 전부 제거.
    UI 는 `id` 를 Title Case 자동 변환 (예: "blocked_patterns" → "Blocked Patterns").
    설명문이 필요하면 Guard 클래스 docstring 에 통합 기술.

    남은 필드 = **구조적 계약** (타입·제약·옵션 소스). 자연어 아님 → 하드코딩 아님.
    """
    id: str
    type: str                                  # text|number|toggle|select|multi_select|tag_input|rule_list|textarea
    default: Any = None
    options: list[Any] = field(default_factory=list)
    options_source: str = ""
    min: Optional[int] = None
    max: Optional[int] = None
    step: Optional[int] = None
    required: bool = False
    item_schema: Optional[list["FieldSchema"]] = None  # rule_list 용 — 항목 폼 스키마

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "default": self.default,
        }
        if self.options:
            out["options"] = list(self.options)
        if self.options_source:
            out["options_source"] = self.options_source
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.step is not None:
            out["step"] = self.step
        if self.required:
            out["required"] = True
        if self.item_schema:
            out["item_schema"] = [f.to_dict() for f in self.item_schema]
        return out


@dataclass
class GuardResult:
    """가드 체크 결과"""
    passed: bool
    guard_name: str
    reason: str = ""
    severity: str = "block"         # "block" | "warn" | "info"
    # PRE_TOOL 차단 시 LLM 에게 돌려줄 메시지 (가짜 tool_result content).
    # 없으면 reason 을 그대로 사용.
    tool_error_message: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Guard ABC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Guard(Strategy, ABC):
    """단일 Guard — 자기 스키마와 훅을 선언하고 state 를 검사.

    구현 계약:
      1. `name` (Strategy 상속) — 고유 이름, UI 식별자
      2. `description` — UI 설명
      3. `param_schema()` — 사용자가 UI 에서 편집할 파라미터 스키마
      4. `hook_points` — 이 Guard 가 실행되어야 할 훅 집합
      5. `configure(params)` — 런타임 파라미터 주입
      6. `check(state, context)` — 동기 검사 로직 (대부분의 Guard)
      7. `check_async(state, context)` — 옵션. 비동기 대기가 필요한 Guard (HITL 등).
         기본 구현은 `check()` 를 그대로 호출. 오버라이드 시 async 로 선언.
    """

    @classmethod
    def param_schema(cls) -> list[FieldSchema]:
        """UI 폼 자동 생성용 파라미터 스키마. 기본 빈 리스트."""
        return []

    @property
    def hook_points(self) -> set[HookPoint]:
        """이 Guard 가 실행될 훅 포인트 집합. 기본 LOOP_BOUNDARY."""
        return {HookPoint.LOOP_BOUNDARY}

    @abstractmethod
    def check(self, state: Any, context: HookContext) -> GuardResult:
        """state + context 를 검사하여 통과/차단 결정."""
        ...

    async def check_async(self, state: Any, context: HookContext) -> GuardResult:
        """비동기 검사 — HITL 같은 대기 필요한 Guard 가 오버라이드.

        기본 구현은 `check()` 동기 호출을 그대로 반환. 대부분의 Guard 는 이대로 OK.
        GuardChain.invoke_async 가 이 메서드를 호출. 동기 `invoke` 는 `check` 직접 호출.
        """
        return self.check(state, context)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GuardChain — 훅별 필터링 + 순차 실행
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GuardChain:
    """설정된 Guard 리스트를 훅 시점별로 분류·실행.

    Pipeline 이 시점 도래 시 `invoke(hook, state, pending_tool_call=...)` 호출.
    해당 hook 을 선언한 Guard 만 실행, 첫 block 에서 short-circuit.
    """

    def __init__(self, guards: Optional[list[Guard]] = None):
        self._guards: list[Guard] = list(guards or [])

    def add(self, guard: Guard) -> "GuardChain":
        self._guards.append(guard)
        return self

    @property
    def guards(self) -> list[Guard]:
        return list(self._guards)

    def invoke(
        self,
        hook: HookPoint,
        state: Any,
        pending_tool_call: Optional[dict[str, Any]] = None,
        short_circuit: bool = True,
    ) -> list[GuardResult]:
        """해당 hook 에 해당하는 Guard 만 순차 실행, 결과 리스트 반환."""
        context = HookContext(
            hook=hook,
            pending_tool_call=pending_tool_call,
            tool_call_history=getattr(state, "tool_call_history", []) or [],
        )
        results: list[GuardResult] = []
        for g in self._guards:
            if hook not in g.hook_points:
                continue
            try:
                r = g.check(state, context)
            except Exception as e:
                logger.warning("[GuardChain] %s.check failed (hook=%s): %s", g.name, hook.value, e)
                r = GuardResult(passed=True, guard_name=g.name, reason=f"check failed: {e}", severity="warn")
            results.append(r)
            if not r.passed and short_circuit and r.severity == "block":
                logger.warning("[GuardChain] Blocked by %s @ %s: %s", r.guard_name, hook.value, r.reason)
                break
        return results

    async def invoke_async(
        self,
        hook: HookPoint,
        state: Any,
        pending_tool_call: Optional[dict[str, Any]] = None,
        short_circuit: bool = True,
    ) -> list[GuardResult]:
        """비동기 경로 — HITLGuard 같이 await 가 필요한 Guard 를 지원.

        Guard.check_async 를 호출. 기본 구현이 check() 래핑이라 기존 Guard 전부
        그대로 호환. HITLGuard 만 override 해서 state.request_approval 을 await.
        """
        context = HookContext(
            hook=hook,
            pending_tool_call=pending_tool_call,
            tool_call_history=getattr(state, "tool_call_history", []) or [],
        )
        results: list[GuardResult] = []
        for g in self._guards:
            if hook not in g.hook_points:
                continue
            try:
                r = await g.check_async(state, context)
            except Exception as e:
                logger.warning("[GuardChain] %s.check_async failed (hook=%s): %s", g.name, hook.value, e)
                r = GuardResult(passed=True, guard_name=g.name, reason=f"check failed: {e}", severity="warn")
            results.append(r)
            if not r.passed and short_circuit and r.severity == "block":
                logger.warning("[GuardChain] Blocked by %s @ %s: %s", r.guard_name, hook.value, r.reason)
                break
        return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  내장 Guard 구현체
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TokenBudgetGuard(Guard):
    """누적 입출력 토큰이 설정한 예산의 95% 를 넘으면 루프 종료.

    param_budget 파라미터로 임계값 지정. 0 이면 config.context_window 폴백.
    """

    def __init__(self, token_budget: int = 0):
        self._token_budget = token_budget

    @property
    def name(self) -> str:
        return "token_budget"

    @property
    def hook_points(self) -> set[HookPoint]:
        return {HookPoint.LOOP_BOUNDARY}

    @classmethod
    def param_schema(cls) -> list[FieldSchema]:
        return [
            FieldSchema(
                id="token_budget",
                type="number",
                default=0,
                min=0,
                max=10_000_000,
                step=1000,
            ),
        ]

    def configure(self, config: dict[str, Any]) -> None:
        self._token_budget = int(config.get("token_budget", self._token_budget) or 0)

    def check(self, state: Any, context: HookContext) -> GuardResult:
        if not hasattr(state, "token_usage"):
            return GuardResult(passed=True, guard_name=self.name)

        if self._token_budget > 0:
            max_tokens = self._token_budget
        elif hasattr(state, "config") and state.config:
            max_tokens = getattr(state.config, "context_window", 1_000_000)
        else:
            max_tokens = 1_000_000

        used = state.token_usage.total if hasattr(state.token_usage, "total") else 0

        if used > max_tokens * 0.95:
            return GuardResult(
                passed=False,
                guard_name=self.name,
                reason=f"토큰 예산 95% 초과 ({used}/{max_tokens})",
                severity="block",
            )
        if used > max_tokens * 0.8:
            return GuardResult(
                passed=True,
                guard_name=self.name,
                reason=f"토큰 예산 80% 경고 ({used}/{max_tokens})",
                severity="warn",
            )
        return GuardResult(passed=True, guard_name=self.name)


class CostBudgetGuard(Guard):
    """누적 비용이 설정 예산(USD)을 초과하면 루프 종료.

    cost_budget_usd 파라미터로 임계값 지정. 0 이면 config.cost_budget_usd 폴백.
    """

    def __init__(self, cost_budget_usd: float = 0.0):
        self._cost_budget_usd = cost_budget_usd

    @property
    def name(self) -> str:
        return "cost_budget"

    @property
    def hook_points(self) -> set[HookPoint]:
        return {HookPoint.LOOP_BOUNDARY}

    @classmethod
    def param_schema(cls) -> list[FieldSchema]:
        return [
            FieldSchema(
                id="cost_budget_usd",
                type="number",
                default=0.0,
                min=0,
                max=1000,
                step=1,
            ),
        ]

    def configure(self, config: dict[str, Any]) -> None:
        self._cost_budget_usd = float(config.get("cost_budget_usd", self._cost_budget_usd) or 0.0)

    def check(self, state: Any, context: HookContext) -> GuardResult:
        if not hasattr(state, "cost_usd"):
            return GuardResult(passed=True, guard_name=self.name)

        if self._cost_budget_usd > 0:
            budget = self._cost_budget_usd
        elif hasattr(state, "config") and state.config:
            budget = getattr(state.config, "cost_budget_usd", 10.0)
        else:
            budget = 10.0

        cost = state.cost_usd
        if cost >= budget:
            return GuardResult(
                passed=False,
                guard_name=self.name,
                reason=f"비용 예산 초과 (${cost:.4f} >= ${budget:.2f})",
                severity="block",
            )
        return GuardResult(passed=True, guard_name=self.name)


class IterationGuard(Guard):
    """config.max_iterations 도달 시 루프 종료."""

    @property
    def name(self) -> str:
        return "iteration"

    @property
    def hook_points(self) -> set[HookPoint]:
        return {HookPoint.LOOP_BOUNDARY}

    @classmethod
    def param_schema(cls) -> list[FieldSchema]:
        return []  # 파라미터 없음 — config.max_iterations 사용

    def check(self, state: Any, context: HookContext) -> GuardResult:
        if not hasattr(state, "loop_iteration") or not hasattr(state, "config"):
            return GuardResult(passed=True, guard_name=self.name)

        max_iter = getattr(state.config, "max_iterations", 10)
        current = state.loop_iteration
        if current >= max_iter:
            return GuardResult(
                passed=False,
                guard_name=self.name,
                reason=f"최대 반복 횟수 도달 ({current}/{max_iter})",
                severity="block",
            )
        return GuardResult(passed=True, guard_name=self.name)


class ContentGuard(Guard):
    """콘텐츠 필터 — 금지 패턴 + PII.

    hook_points: PRE_MAIN (입력 검사) / POST_RESPONSE (출력 검사).
    check_target 파라미터로 input / output / both 선택.
    """

    _PII_PATTERNS: dict[str, re.Pattern] = {
        "email":       re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "phone_kr":    re.compile(r"\b01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}\b"),
        "resident_id": re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b"),
        "credit_card": re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    }

    def __init__(
        self,
        blocked_patterns: Optional[list[str]] = None,
        detect_pii: bool = False,
        check_target: str = "both",
    ):
        self._patterns: list[re.Pattern] = [
            re.compile(p, re.IGNORECASE) for p in (blocked_patterns or [])
        ]
        self._detect_pii = detect_pii
        self._check_target = check_target if check_target in ("input", "output", "both") else "both"

    @property
    def name(self) -> str:
        return "content"

    @property
    def hook_points(self) -> set[HookPoint]:
        # input/output 설정에 따라 훅 포인트 달라짐
        pts: set[HookPoint] = set()
        if self._check_target in ("input", "both"):
            pts.add(HookPoint.PRE_MAIN)
        if self._check_target in ("output", "both"):
            pts.add(HookPoint.POST_RESPONSE)
        return pts or {HookPoint.POST_RESPONSE}

    @classmethod
    def param_schema(cls) -> list[FieldSchema]:
        return [
            FieldSchema(
                id="blocked_patterns",
                type="tag_input",
                default=[],
            ),
            FieldSchema(
                id="detect_pii",
                type="toggle",
                default=False,
            ),
            FieldSchema(
                id="check_target",
                type="select",
                options=["input", "output", "both"],
                default="both",
            ),
        ]

    def configure(self, config: dict[str, Any]) -> None:
        if "blocked_patterns" in config:
            raw = config.get("blocked_patterns") or []
            self._patterns = [re.compile(str(p), re.IGNORECASE) for p in raw]
        if "detect_pii" in config:
            self._detect_pii = bool(config["detect_pii"])
        if "check_target" in config:
            target = str(config["check_target"])
            if target in ("input", "output", "both"):
                self._check_target = target

    def check(self, state: Any, context: HookContext) -> GuardResult:
        if not self._patterns and not self._detect_pii:
            return GuardResult(passed=True, guard_name=self.name)

        # 훅 시점에 맞는 대상만 검사
        targets: list[tuple[str, str]] = []
        if context.hook == HookPoint.POST_RESPONSE and self._check_target in ("output", "both"):
            text = getattr(state, "last_assistant_text", "") or ""
            if text:
                targets.append(("output", text))
        if context.hook == HookPoint.PRE_MAIN and self._check_target in ("input", "both"):
            msgs = getattr(state, "messages", None) or []
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    content = m.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    if isinstance(content, str) and content:
                        targets.append(("input", content))
                    break

        if not targets:
            return GuardResult(passed=True, guard_name=self.name)

        for target, text in targets:
            for p in self._patterns:
                m = p.search(text)
                if m:
                    snippet = m.group(0)[:20]
                    return GuardResult(
                        passed=False,
                        guard_name=self.name,
                        reason=f"금지 패턴 감지 ({target}): {snippet!r}",
                        severity="block",
                    )
            if self._detect_pii:
                for pii_type, p in self._PII_PATTERNS.items():
                    if p.search(text):
                        return GuardResult(
                            passed=False,
                            guard_name=self.name,
                            reason=f"PII 감지 ({target}/{pii_type})",
                            severity="block",
                        )
        return GuardResult(passed=True, guard_name=self.name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HITL Guard — Human-In-The-Loop (v0.24.0)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HITLGuard(Guard):
    """파괴적 도구 호출 전 사용자 승인 대기.

    MCP 표준 annotations 를 읽어 `destructiveHint` / `openWorldHint` 등이
    True 인 도구만 승인 모달을 띄운다. 승인되면 실행 재개, 거부되면
    가짜 tool_result(is_error=True) 로 LLM 에게 이유 전달 → 재계획 루프.

    - hook_points: PRE_TOOL (도구별 개별 호출 경로)
    - 승인 대기: state.request_approval() 을 await → 이식측이
      /approvals/{id} 로 resolve 할 때까지 블록.
    - timeout_sec=0 은 무한 대기 — 이식측 기본 300초 권장.

    trigger 파라미터:
      - `destructive` (기본 True) — destructiveHint=True 인 도구 승인 요구
      - `open_world`  (기본 False) — openWorldHint=True 만 승인 요구
      - `non_readonly`(기본 False) — readOnlyHint=False 이면 승인 (최보수)
    """

    _uuid_counter: int = 0

    def __init__(
        self,
        *,
        trigger_destructive: bool = True,
        trigger_open_world: bool = False,
        trigger_non_readonly: bool = False,
        timeout_sec: int = 300,
        auto_approve_for_dev: bool = False,
    ):
        self._trigger_destructive = bool(trigger_destructive)
        self._trigger_open_world = bool(trigger_open_world)
        self._trigger_non_readonly = bool(trigger_non_readonly)
        self._timeout_sec = int(timeout_sec) if timeout_sec else 0
        self._auto_approve = bool(auto_approve_for_dev)

    @property
    def name(self) -> str:
        return "hitl"

    @classmethod
    def param_schema(cls) -> list[FieldSchema]:
        return [
            FieldSchema(id="trigger_destructive", type="toggle", default=True),
            FieldSchema(id="trigger_open_world", type="toggle", default=False),
            FieldSchema(id="trigger_non_readonly", type="toggle", default=False),
            FieldSchema(id="timeout_sec", type="number", default=300, min=0, max=3600),
            FieldSchema(id="auto_approve_for_dev", type="toggle", default=False),
        ]

    @property
    def hook_points(self) -> set[HookPoint]:
        return {HookPoint.PRE_TOOL}

    def configure(self, config: dict[str, Any]) -> None:
        if "trigger_destructive" in config:
            self._trigger_destructive = bool(config["trigger_destructive"])
        if "trigger_open_world" in config:
            self._trigger_open_world = bool(config["trigger_open_world"])
        if "trigger_non_readonly" in config:
            self._trigger_non_readonly = bool(config["trigger_non_readonly"])
        if "timeout_sec" in config:
            try:
                self._timeout_sec = max(0, int(config["timeout_sec"]))
            except Exception:
                pass
        if "auto_approve_for_dev" in config:
            self._auto_approve = bool(config["auto_approve_for_dev"])

    def check(self, state: Any, context: HookContext) -> GuardResult:
        """sync 경로 — HITL 은 async 가 본체라 여기선 항상 pass 반환.

        PolicyGate 가 sync `invoke` 를 썼다면 HITL 없이 지나감. 이식측은
        `invoke_async` 경로로 전환해야 실제 승인 대기가 작동한다.
        """
        return GuardResult(passed=True, guard_name=self.name, severity="info",
                           reason="hitl: sync-path no-op (use invoke_async)")

    async def check_async(self, state: Any, context: HookContext) -> GuardResult:
        tc = context.pending_tool_call or {}
        tool_name = tc.get("tool_name") or tc.get("name") or "?"
        tool_use_id = tc.get("tool_use_id") or tc.get("id") or ""
        tool_input = tc.get("tool_input") or tc.get("input") or {}

        annotations = self._resolve_annotations(state, tool_name)
        if not self._should_trigger(annotations):
            return GuardResult(passed=True, guard_name=self.name)

        if self._auto_approve:
            return GuardResult(
                passed=True, guard_name=self.name, severity="info",
                reason=f"auto-approve(dev): {tool_name}",
            )

        reason = self._format_reason(annotations)
        import uuid as _uuid
        approval_id = f"apv_{_uuid.uuid4().hex[:12]}"

        decision = await state.request_approval(
            approval_id=approval_id,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            tool_input=tool_input,
            guard_name=self.name,
            annotations=annotations,
            reason=reason,
            timeout_sec=self._timeout_sec,
        )

        verdict = (decision.get("decision") or "").lower()
        if verdict == "approve":
            edited = decision.get("edited_input") or {}
            if edited and edited != tool_input:
                # 사용자가 args 편집 → pending_tool_call 에 반영.
                tc["tool_input"] = edited
            return GuardResult(passed=True, guard_name=self.name,
                               reason="approved", severity="info")

        user_reason = decision.get("reason") or ""
        block_reason = f"hitl {verdict}: {user_reason}" if user_reason else f"hitl {verdict}"
        tool_msg = (
            f"Execution of '{tool_name}' was {verdict} by human reviewer. "
            f"Reason: {user_reason or 'no reason given'}. "
            f"Do not retry unless the user explicitly asks again."
        )
        return GuardResult(
            passed=False, guard_name=self.name,
            reason=block_reason, severity="block",
            tool_error_message=tool_msg,
        )

    def _should_trigger(self, annotations: dict[str, Any]) -> bool:
        if self._trigger_destructive and bool(annotations.get("destructiveHint")):
            return True
        if self._trigger_open_world and bool(annotations.get("openWorldHint")):
            return True
        if self._trigger_non_readonly and not bool(annotations.get("readOnlyHint", True)):
            return True
        return False

    def _resolve_annotations(self, state: Any, tool_name: str) -> dict[str, Any]:
        """annotations 조회 — v0.24.4 우선순위:
        1. state.tool.annotations 분리 맵 (payload 오염 방지 설계)
        2. legacy tool_definitions[*].annotations (구 버전 외부 MCP 호환)
        3. Tool 인스턴스의 annotations() 메서드
        """
        tool_group = getattr(state, "tool", None)
        if tool_group is not None:
            ann_map = getattr(tool_group, "annotations", None) or {}
            ann = ann_map.get(tool_name)
            if ann:
                return dict(ann)
        for td in getattr(state, "tool_definitions", []) or []:
            td_name = td.get("name") or ((td.get("function") or {}).get("name"))
            if td_name != tool_name:
                continue
            legacy_ann = td.get("annotations") or (td.get("function") or {}).get("annotations")
            if legacy_ann:
                return dict(legacy_ann)
            break
        tool_registry = (getattr(state, "metadata", {}) or {}).get("tool_registry") or {}
        inst = tool_registry.get(tool_name)
        if inst is not None and hasattr(inst, "annotations"):
            try:
                return dict(inst.annotations())
            except Exception:
                pass
        return {}

    def _format_reason(self, annotations: dict[str, Any]) -> str:
        parts = []
        if annotations.get("destructiveHint"):
            parts.append("destructiveHint=true")
        if annotations.get("openWorldHint"):
            parts.append("openWorldHint=true")
        if annotations.get("readOnlyHint") is False:
            parts.append("readOnlyHint=false")
        return ", ".join(parts) or "hitl trigger"


class ToolDiversityGuard(Guard):
    """동일 도구를 같은 인자로 반복 호출(무의미한 루프)하면 PRE_TOOL 에서 차단·교정 유도.

    에이전트가 같은 검색/도구를 같은 인자로 계속 반복하면 새 정보 없이 루프에 빠지고
    예산만 소모한다. 같은 (tool_name, args) 호출이 max_repeats 회 이상 누적되면 차단하고,
    "접근을 바꾸거나 멈추라" 는 가짜 tool_result 로 교정한다. PRE_TOOL 훅이라 history 엔
    직전 호출만 있다.

    params:
      max_repeats — 동일 호출 허용 횟수(이미 이만큼 했으면 다음 호출 차단). 기본 3.
      window      — 최근 N개 이력만 고려 (0 = 전체). 기본 0.
    """

    _DEFAULT_MAX_REPEATS = 3

    def __init__(self, max_repeats: int = 0, window: int = 0):
        self._max_repeats = max_repeats or self._DEFAULT_MAX_REPEATS
        self._window = window or 0

    @property
    def name(self) -> str:
        return "tool_diversity"

    @property
    def description(self) -> str:
        return "Block repeated identical tool calls (anti search-collapse)."

    @property
    def hook_points(self) -> set[HookPoint]:
        return {HookPoint.PRE_TOOL}

    @classmethod
    def param_schema(cls) -> list[FieldSchema]:
        return [
            FieldSchema(id="max_repeats", type="number",
                        default=cls._DEFAULT_MAX_REPEATS, min=1, max=100, step=1),
            FieldSchema(id="window", type="number", default=0, min=0, max=1000, step=1),
        ]

    def configure(self, config: dict[str, Any]) -> None:
        self._max_repeats = int(config.get("max_repeats", self._max_repeats) or self._DEFAULT_MAX_REPEATS)
        self._window = int(config.get("window", self._window) or 0)

    @staticmethod
    def _signature(tool_name: str, tool_input: Any) -> str:
        import json as _json
        try:
            payload = _json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            payload = str(tool_input)
        return f"{tool_name}\x1f{payload}"

    def check(self, state: Any, context: HookContext) -> GuardResult:
        tc = context.pending_tool_call
        if not isinstance(tc, dict):
            return GuardResult(passed=True, guard_name=self.name)
        name = tc.get("tool_name") or tc.get("name") or ""
        if not name:
            return GuardResult(passed=True, guard_name=self.name)
        sig = self._signature(name, tc.get("tool_input", tc.get("input")))

        history = context.tool_call_history or []
        if self._window > 0:
            history = history[-self._window:]
        prior = 0
        for h in history:
            if not isinstance(h, dict):
                continue
            hname = h.get("tool_name") or h.get("name") or ""
            if hname != name:
                continue
            if self._signature(hname, h.get("tool_input", h.get("input"))) == sig:
                prior += 1

        if prior >= self._max_repeats:
            return GuardResult(
                passed=False,
                guard_name=self.name,
                reason=f"동일 호출 {prior}회 반복 (max_repeats={self._max_repeats})",
                severity="block",
                tool_error_message=(
                    f"You've already called '{name}' with identical arguments {prior} times. "
                    f"Repeating it won't yield new information. Change your approach "
                    f"(different query / tool / arguments) or stop and answer with what you have."
                ),
            )
        return GuardResult(passed=True, guard_name=self.name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  entry_points 기반 Guard discovery
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DISCOVERED: dict[str, type[Guard]] = {}
_DISCOVERY_DONE = False


def _discover_guards_once() -> None:
    """xgen_harness.guards entry_points 에서 Guard 클래스 자동 발견.

    내장 4종도 pyproject.toml 의 이 그룹에 등록되어 있어 같은 경로로 주입됨.
    외부 패키지가 자기 Guard 를 entry_points 에 선언하면 pip install 만으로 합류.
    """
    global _DISCOVERY_DONE
    if _DISCOVERY_DONE:
        return

    try:
        from importlib.metadata import entry_points
        # Python 3.10+ API
        try:
            eps = entry_points(group="xgen_harness.guards")
        except TypeError:
            # Python 3.9 fallback
            eps = entry_points().get("xgen_harness.guards", [])
    except Exception as e:
        logger.debug("[guards] entry_points backend 없음 — 내장 Guard 만 사용: %s", e)
        _DISCOVERY_DONE = True
        _DISCOVERED.update({
            "token_budget": TokenBudgetGuard,
            "cost_budget": CostBudgetGuard,
            "iteration": IterationGuard,
            "content": ContentGuard,
            "tool_diversity": ToolDiversityGuard,
        })
        return

    for ep in eps:
        try:
            cls = ep.load()
            if not isinstance(cls, type) or not issubclass(cls, Guard):
                logger.warning("[guards] %s 는 Guard 서브클래스가 아님 — skip", ep.value)
                continue
            key = ep.name
            if key in _DISCOVERED and _DISCOVERED[key] is not cls:
                logger.warning("[guards] 중복 이름 %s — 기존 유지, 새 등록 무시", key)
                continue
            _DISCOVERED[key] = cls
            logger.debug("[guards] 발견: %s = %s", key, cls.__name__)
        except Exception as e:
            logger.warning("[guards] entry_point %s 로드 실패: %s", ep, e)

    # 내장이 entry_points 에 등록되어 있지 않은 경우 대비 fallback.
    # editable install / 미빌드 환경에서도 엔진 번들 Guard 가 UI 에 나오도록 보장.
    # entry_points 가 정상 작동하면 이 경로는 setdefault 로 덮어쓰지 않음.
    for key, cls in (
        ("token_budget", TokenBudgetGuard),
        ("cost_budget", CostBudgetGuard),
        ("iteration", IterationGuard),
        ("content", ContentGuard),
        ("tool_diversity", ToolDiversityGuard),
    ):
        _DISCOVERED.setdefault(key, cls)

    # 엔진 번들 "sample" Guard — 순환 import 회피 위해 late import.
    # 외부 작업자가 자기 Guard 를 entry_points 로 얹으면 동일 경로로 합류.
    try:
        from .guard_precondition import ToolPreconditionGuard as _TPG
        _DISCOVERED.setdefault("tool_precondition", _TPG)
    except Exception as e:
        logger.debug("[guards] tool_precondition 번들 로드 skip: %s", e)

    _DISCOVERY_DONE = True


def available_guards() -> dict[str, type[Guard]]:
    """발견된 모든 Guard 클래스 (name → class)."""
    _discover_guards_once()
    return dict(_DISCOVERED)


def register_guard(name: str, cls: type[Guard]) -> None:
    """공개 API — 런타임에 Guard 를 직접 등록 (entry_points 없이).

    테스트 / 일회성 주입용. 정식 확장은 pyproject.toml entry_points 권장.
    """
    if not isinstance(cls, type) or not issubclass(cls, Guard):
        raise TypeError(f"register_guard: {cls} is not a Guard subclass")
    _discover_guards_once()
    _DISCOVERED[name] = cls


def build_guard_chain(
    guard_configs: list[dict[str, Any]],
) -> GuardChain:
    """선언형 Guard 설정 리스트로 GuardChain 생성.

    guard_configs 예시:
        [
            {"name": "iteration"},
            {"name": "cost_budget", "params": {"cost_budget_usd": 5.0}},
            {"name": "content", "params": {"detect_pii": True, "check_target": "output"}},
        ]

    각 항목:
      - name (필수): available_guards() 에 등록된 이름
      - params (선택): Guard.configure() 로 전달될 딕셔너리
    """
    _discover_guards_once()
    chain = GuardChain()
    for cfg in guard_configs or []:
        if not isinstance(cfg, dict):
            continue
        name = cfg.get("name")
        if not name or name not in _DISCOVERED:
            logger.warning("[build_guard_chain] 알 수 없는 Guard 이름 '%s' — skip", name)
            continue
        try:
            inst = _DISCOVERED[name]()
        except Exception as e:
            logger.warning("[build_guard_chain] %s 인스턴스화 실패: %s", name, e)
            continue
        params = cfg.get("params") or {}
        if params and hasattr(inst, "configure"):
            try:
                inst.configure(params)
            except Exception as e:
                logger.warning("[build_guard_chain] %s.configure 실패: %s", name, e)
        chain.add(inst)
    return chain


def describe_guards() -> list[dict[str, Any]]:
    """UI 용 — 모든 Guard 의 메타데이터 + 파라미터 스키마.

    v0.17.0 — description 은 **클래스 docstring 첫 문단** 에서 자동 추출.
    한국어 리터럴을 클래스 property 로 박지 않는다 (확장성·연동성 원칙).

    stage_config.py 의 `guards_available` 동적 옵션 소스가 이 함수를 호출.
    반환 포맷:
        [
            {
                "name": "...",
                "description": "...",          # cls docstring 첫 문단
                "hook_points": ["..."],
                "param_schema": [FieldSchema.to_dict(), ...],
            },
            ...
        ]
    """
    import inspect as _inspect

    _discover_guards_once()
    out: list[dict[str, Any]] = []
    for name, cls in _DISCOVERED.items():
        try:
            # hook_points — 인스턴스 속성 (default 생성자 동작 시만 조회).
            try:
                inst = cls()
                hooks = [hp.value for hp in inst.hook_points]
            except Exception:
                hooks = [HookPoint.LOOP_BOUNDARY.value]

            # description 은 docstring 파싱 — Guard 클래스가 한국어 리터럴을
            # property 로 들고 있지 않아도 UI 가 설명을 받을 수 있게.
            doc = _inspect.getdoc(cls) or ""
            desc = doc.split("\n\n", 1)[0].strip() if doc else ""

            schema = [f.to_dict() for f in cls.param_schema()]
            out.append({
                "name": name,
                "description": desc,
                "hook_points": hooks,
                "param_schema": schema,
            })
        except Exception as e:
            logger.warning("[describe_guards] %s 기술 실패: %s", name, e)
    return out
