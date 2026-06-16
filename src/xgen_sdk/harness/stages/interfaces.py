"""
스테이지 Strategy 인터페이스 계약

Stage×Strategy 이중 추상화를 Python ABC로 구현.
각 스테이지는 1개 이상의 Strategy 슬롯을 가지고,
슬롯마다 갈아끼울 수 있는 구현체를 인스턴스로 보유.

xgen-workflow 이식 시 고려사항:
- ToolRouter: MCP는 xgen-mcp-station HTTP, Node Bridge는 xgen-workflow 내부
- EvaluationStrategy: 독립 LLM 호출 → 프로바이더 인터페이스 재사용
- RetryStrategy: 429/529 재시도 → 프로바이더별 다를 수 있음
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  공통 기반
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Strategy(ABC):
    """모든 Strategy의 기반 인터페이스."""

    @property
    @abstractmethod
    def name(self) -> str:
        """구현체 이름 (예: 'anthropic', 'sequential', 'llm_judge')"""
        ...

    @property
    def description(self) -> str:
        """UI에 표시할 설명"""
        return ""

    def configure(self, config: dict[str, Any]) -> None:
        """런타임 설정 주입 (stage_params에서 호출)"""
        pass


@dataclass
class StrategySlot:
    """Strategy 슬롯 메타데이터 — UI의 드롭다운 하나에 대응"""
    slot_name: str              # "provider", "retry", "executor"
    current_impl: str           # 현재 선택된 구현체 이름
    available_impls: list[str]  # 선택 가능한 구현체 목록
    description: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  S00 Harness — 본문 LLM 호출 Transport 전략 (v0.14.0)
#  "streaming" / "batch" 등은 이 인터페이스의 구현체.
#  외부 패키지가 새 Transport 를 entry_points 로 얹을 수 있음.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TransportStrategy(Strategy):
    """s00_harness 의 본문 LLM 호출 전송 전략 인터페이스.

    하드코딩된 "streaming vs batch" 분기를 대체. Pipeline 은 Strategy 인스턴스만
    받고, 세부는 전적으로 impl 에 위임. 플러그인이 WebSocket / 재시도 정책이
    다른 전송 / 로깅 wrap 등 임의의 Transport 를 등록 가능.
    """

    @abstractmethod
    async def call(self, state: Any) -> dict:
        """본문 LLM 호출 실행. state.provider 사용, 이벤트 방출, token_usage 갱신.

        Returns: {call_count, has_tool_calls, text_length, input_tokens, output_tokens}
        """
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM 재시도 전략 (Transport 내부에서 사용)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RetryStrategy(Strategy):
    """LLM 호출 재시도 전략 인터페이스"""

    @abstractmethod
    def should_retry(self, error: Exception, attempt: int) -> bool:
        """이 에러에 대해 재시도할지 결정"""
        ...

    @abstractmethod
    def get_delay(self, attempt: int) -> float:
        """n번째 재시도 전 대기 시간 (초)"""
        ...

    @property
    @abstractmethod
    def max_retries(self) -> int:
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  S08 Execute — 도구 라우팅/실행
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class ToolResult:
    """도구 실행 결과 — 모든 도구 라우터가 이 타입을 반환"""
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolRouter(Strategy):
    """도구 라우팅 인터페이스.

    xgen-workflow 이식 시:
    - MCP 도구: xgen-mcp-station HTTP → MCPToolRouter
    - Node Bridge 도구: xgen-workflow 내부 → NodeBridgeRouter
    - 빌트인 도구: discover_tools 등 → BuiltinRouter
    """

    @abstractmethod
    async def route(self, tool_name: str, tool_input: dict) -> ToolResult:
        """도구 이름으로 라우팅하여 실행"""
        ...

    @abstractmethod
    async def list_available(self) -> list[dict[str, str]]:
        """사용 가능한 도구 목록 반환 [{name, description}]"""
        ...


class ToolExecutor(Strategy):
    """도구 실행 전략 인터페이스 — 순차/병렬 선택"""

    @abstractmethod
    async def execute_all(
        self,
        tool_calls: list[dict],
        router: ToolRouter,
    ) -> list[tuple[str, ToolResult]]:
        """tool_calls를 실행하고 [(tool_use_id, ToolResult)] 반환"""
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  S09 Validate — 평가/검증
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class EvaluationResult:
    """평가 결과 — 모든 EvaluationStrategy가 이 타입을 반환"""
    passed: bool
    score: float                     # 0.0 ~ 1.0
    feedback: str = ""
    verdict: str = "pass"            # "pass" | "retry" | "fail"
    criteria: dict[str, float] = field(default_factory=dict)  # {"relevance": 0.8, ...}


class EvaluationStrategy(Strategy):
    """응답 품질 평가 전략 인터페이스.

    구현체:
    - LLMJudge: 독립 LLM 호출로 4가지 기준 평가 (기본)
    - RuleBased: 길이/키워드/정규식 규칙 (빠름, LLM 비용 없음)
    - NoValidation: 항상 통과 (검증 불필요 시)
    """

    @abstractmethod
    async def evaluate(
        self,
        user_input: str,
        assistant_response: str,
        context: Optional[dict] = None,
    ) -> EvaluationResult:
        ...


class QualityScorer(Strategy):
    """가중평균 점수 계산기 인터페이스"""

    @abstractmethod
    def score(self, criteria: dict[str, float]) -> float:
        """개별 기준 점수 → 종합 점수"""
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  S04 Tool Index — 도구 디스커버리
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ToolDiscoveryStrategy(Strategy):
    """도구 디스커버리 전략 인터페이스.

    구현체:
    - ProgressiveDisclosure: 메타데이터만 → discover_tools → 실행 (기본)
    - EagerLoad: 모든 도구 스키마 즉시 로드
    - NoDiscovery: 도구 인덱싱 비활성화
    """

    @abstractmethod
    async def discover(
        self,
        tool_definitions: list[dict],
        state: Any,
    ) -> tuple[list[dict], list[dict]]:
        """(tool_index, augmented_tool_definitions) 반환.

        tool_index: UI/프롬프트용 메타데이터 [{name, description, category}]
        augmented_tool_definitions: discover_tools 등 추가된 도구 정의
        """
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  S06 Context — 컨텍스트 압축
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ContextCompactor(Strategy):
    """컨텍스트 압축 전략 인터페이스.

    구현체:
    - TokenBudget: 토큰 예산 기반 3단계 압축 (기본)
    - SlidingWindow: 최근 N개 메시지만 유지
    - Summarize: LLM으로 이전 대화 요약
    """

    @abstractmethod
    async def compact(
        self,
        messages: list[dict],
        system_prompt: str,
        budget_tokens: int,
        max_tokens: int,
    ) -> tuple[list[dict], str, bool]:
        """(compacted_messages, compacted_system_prompt, was_compacted) 반환"""
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  S10 Decide — 루프 판단
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DecideStrategy(Strategy):
    """루프 계속/완료 판단 전략 인터페이스.

    Stage.execute() 는 이 메서드로 전적으로 위임한다 — Stage 내부 분기 로직 금지.
    각 구현체가 자기만의 판단 규칙을 전부 들고 있어야 한다.

    구현체:
    - Threshold: Guard 체인 + 도구 호출/점수/텍스트 기반 판단 (기본)
    - AlwaysPass: 1회 실행 후 즉시 complete (루프 없음)
    """

    @abstractmethod
    async def decide(self, state: Any, params: dict[str, Any]) -> dict[str, Any]:
        """판단 결과 dict 반환.

        필드:
          decision: "continue" | "complete" | "retry" | "error" | "escalate"
          reason: 사람이 읽는 이유
          guard: (optional) 차단된 guard 이름

        params 는 Stage 가 수집한 stage_params — guards / cost_budget_usd /
        token_budget / max_retries 등 Strategy 가 직접 가져올 필요 없게 미리 전달.
        """
        ...
