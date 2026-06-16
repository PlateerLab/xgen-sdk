"""RetryStrategy 구현체들"""

from ..interfaces import RetryStrategy
from ...errors import RateLimitError, OverloadError, ProviderError


class ExponentialBackoffRetry(RetryStrategy):
    """지수 백오프 재시도 — 기본 전략.

    429(rate limit): 10/20/40초
    529(overload): 1/2/4초
    기타 recoverable: 2/4/8초
    """

    DELAYS = {
        "rate_limit": [10, 20, 40],
        "overload": [1, 2, 4],
        "server": [2, 4, 8],
    }

    @property
    def name(self) -> str:
        return "exponential_backoff"

    @property
    def description(self) -> str:
        return "지수 백오프 재시도 (429→10/20/40s, 529→1/2/4s)"

    @property
    def max_retries(self) -> int:
        return 3

    def should_retry(self, error: Exception, attempt: int) -> bool:
        if attempt >= self.max_retries:
            return False
        if isinstance(error, (RateLimitError, OverloadError)):
            return True
        if isinstance(error, ProviderError) and error.recoverable:
            return True
        return False

    def get_delay(self, attempt: int) -> float:
        idx = min(attempt, 2)
        return self.DELAYS["server"][idx]

    def get_delay_for_error(self, error: Exception, attempt: int) -> float:
        idx = min(attempt, 2)
        if isinstance(error, RateLimitError):
            return self.DELAYS["rate_limit"][idx]
        if isinstance(error, OverloadError):
            return self.DELAYS["overload"][idx]
        return self.DELAYS["server"][idx]


class NoRetry(RetryStrategy):
    """재시도 없음 — 테스트/디버깅용"""

    @property
    def name(self) -> str:
        return "no_retry"

    @property
    def description(self) -> str:
        return "재시도 없음 (즉시 에러 전파)"

    @property
    def max_retries(self) -> int:
        return 0

    def should_retry(self, error: Exception, attempt: int) -> bool:
        return False

    def get_delay(self, attempt: int) -> float:
        return 0
