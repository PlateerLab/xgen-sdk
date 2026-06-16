"""
xgen-harness 에러 계층

모든 하네스 에러의 기반. 스테이지별 에러, 프로바이더 에러, 도구 에러 등을 포함.
ErrorCategory로 재시도 가능 여부를 판단.

## 안정 에러코드 계약 (v1.18.0+)
각 에러는 `code` (`exec.<component>.<reason>` 형식) 를 들고 다닌다. **메시지(한국어,
번역 가능)와 독립된 머신 식별자** 로, 이식측·외부 소비자가 릴리즈 간 안정적으로
분기할 수 있다. 코드 문자열은 **계약** 이다 — 한 번 배포된 코드는 바꾸지 않는다
(`test_error_codes_stability.py` 가 전체 매핑을 고정). 새 에러 추가 시 새 코드를
부여하고 stability 테스트에 등록한다.
"""

from enum import Enum
from typing import Optional


class ErrorCategory(Enum):
    RATE_LIMIT = "rate_limit"
    OVERLOAD = "overload"
    TIMEOUT = "timeout"
    NETWORK = "network"
    TOKEN_LIMIT = "token_limit"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    SERVER = "server"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"

    @property
    def recoverable(self) -> bool:
        return self in {
            ErrorCategory.RATE_LIMIT,
            ErrorCategory.OVERLOAD,
            ErrorCategory.TIMEOUT,
            ErrorCategory.NETWORK,
            ErrorCategory.SERVER,
        }


# 프로바이더 에러는 category 가 런타임에 결정(from_status)되므로, 서브클래스마다
# 코드를 박는 대신 category → code 매핑을 단일 진실 소스로 둔다.
_PROVIDER_CODE_BY_CATEGORY: dict["ErrorCategory", str] = {
    ErrorCategory.RATE_LIMIT: "exec.provider.rate_limit",
    ErrorCategory.OVERLOAD: "exec.provider.overload",
    ErrorCategory.TIMEOUT: "exec.provider.timeout",
    ErrorCategory.NETWORK: "exec.provider.network",
    ErrorCategory.TOKEN_LIMIT: "exec.provider.context_overflow",
    ErrorCategory.AUTH: "exec.provider.auth",
    ErrorCategory.BAD_REQUEST: "exec.provider.bad_request",
    ErrorCategory.SERVER: "exec.provider.server",
    ErrorCategory.TERMINAL: "exec.provider.terminal",
    ErrorCategory.UNKNOWN: "exec.provider.unknown",
}


def provider_code_for_category(category: "ErrorCategory") -> str:
    """ProviderError 의 category → 안정 코드. 미등록 카테고리는 unknown 폴백."""
    return _PROVIDER_CODE_BY_CATEGORY.get(category, "exec.provider.unknown")


class HarnessError(Exception):
    """모든 하네스 에러의 기반 클래스.

    `code` — `exec.<component>.<reason>` 안정 식별자 (메시지와 독립).
    서브클래스는 클래스 속성으로 override; ProviderError 만 category 기반 property.
    """

    code: str = "exec.harness.unknown"

    def __init__(
        self,
        message: str,
        stage_id: str = "",
        category: ErrorCategory = ErrorCategory.UNKNOWN,
    ):
        self.stage_id = stage_id
        self.category = category
        super().__init__(message)

    @property
    def recoverable(self) -> bool:
        return self.category.recoverable

    @property
    def error_code(self) -> str:
        """공개 접근자 — `code` 의 별칭 (property override 와 무관하게 항상 동작)."""
        return self.code


class ConfigError(HarnessError):
    """API 키 누락, 잘못된 모델, 잘못된 설정"""

    code = "exec.config.invalid"

    def __init__(self, message: str, stage_id: str = ""):
        super().__init__(message, stage_id, ErrorCategory.AUTH)


class ProviderError(HarnessError):
    """LLM API 에러 기반"""

    def __init__(
        self,
        message: str,
        stage_id: str = "s00_harness",
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        http_status: Optional[int] = None,
    ):
        self.http_status = http_status
        super().__init__(message, stage_id, category)

    @property
    def code(self) -> str:  # type: ignore[override]
        return provider_code_for_category(self.category)

    @classmethod
    def from_status(cls, status: int, body: str = "") -> "ProviderError":
        if status == 429:
            return RateLimitError(f"Rate limit: {body}")
        elif status == 529:
            return OverloadError(f"Overloaded: {body}")
        elif status == 401:
            return cls(f"Auth failed: {body}", category=ErrorCategory.AUTH, http_status=status)
        elif status >= 500:
            return cls(f"Server error {status}: {body}", category=ErrorCategory.SERVER, http_status=status)
        else:
            return cls(f"HTTP {status}: {body}", category=ErrorCategory.BAD_REQUEST, http_status=status)


class RateLimitError(ProviderError):
    def __init__(self, message: str = "Rate limited"):
        super().__init__(message, category=ErrorCategory.RATE_LIMIT, http_status=429)


class OverloadError(ProviderError):
    def __init__(self, message: str = "API overloaded"):
        super().__init__(message, category=ErrorCategory.OVERLOAD, http_status=529)


class ContextOverflowError(ProviderError):
    def __init__(self, message: str = "Context window exceeded"):
        super().__init__(message, category=ErrorCategory.TOKEN_LIMIT)


class ToolError(HarnessError):
    """도구 실행 실패"""

    code = "exec.tool.failed"

    def __init__(self, message: str, tool_name: str = "", stage_id: str = "s07_act"):
        self.tool_name = tool_name
        super().__init__(message, stage_id, ErrorCategory.UNKNOWN)


class ToolTimeoutError(ToolError):
    code = "exec.tool.timeout"

    def __init__(self, tool_name: str, timeout_seconds: float):
        super().__init__(f"Tool '{tool_name}' timed out after {timeout_seconds}s", tool_name)


class MCPConnectionError(ToolError):
    code = "exec.tool.mcp_connection"

    def __init__(self, message: str, session_id: str = ""):
        self.session_id = session_id
        super().__init__(message, tool_name=f"mcp:{session_id}")


class ValidationError(HarnessError):
    """Validate 스테이지에서 품질 미달 감지"""

    code = "exec.decide.validation_failed"

    def __init__(self, message: str, score: float = 0.0):
        self.score = score
        super().__init__(message, "s08_decide", ErrorCategory.UNKNOWN)


class PipelineAbortError(HarnessError):
    """복구 불가 실패, 파이프라인 중단"""

    code = "exec.pipeline.abort"

    def __init__(self, message: str, stage_id: str = ""):
        super().__init__(message, stage_id, ErrorCategory.TERMINAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  안정 코드 레지스트리 — 외부 소비자/이식측 + stability 테스트용
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 발행된 모든 안정 코드의 전체 집합. 새 에러를 추가하면 여기에 등록 +
# test_error_codes_stability.py 가 이 집합이 줄어들지 않음을 강제한다.
ALL_ERROR_CODES: frozenset = frozenset(
    {
        "exec.harness.unknown",
        "exec.config.invalid",
        "exec.tool.failed",
        "exec.tool.timeout",
        "exec.tool.mcp_connection",
        "exec.decide.validation_failed",
        "exec.pipeline.abort",
    }
    | set(_PROVIDER_CODE_BY_CATEGORY.values())
)


def error_code(exc: BaseException) -> str:
    """임의 예외의 안정 코드 추출. HarnessError 면 `.code`, 아니면 generic.

    이식측/외부 핸들러가 `error_code(e)` 한 줄로 안정 분기 키를 얻는다.
    """
    if isinstance(exc, HarnessError):
        return exc.code
    return "exec.harness.unknown"
