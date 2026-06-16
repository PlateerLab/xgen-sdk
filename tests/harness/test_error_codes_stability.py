"""안정 에러코드 계약 테스트 (geny-executor 의 error-code stability contract 대응).

`code` 문자열은 **외부 계약** 이다 — 이식측/외부 소비자가 릴리즈 간 안정적으로
분기하는 키. 한 번 발행된 코드는 절대 바뀌거나 사라지면 안 된다.

이 테스트는 클래스→코드, 카테고리→코드, HTTP status→코드 매핑 전체를 **하드 고정**
한다. 코드를 의도적으로 바꾸려면 이 EXPECTED 표를 함께 고쳐야 하므로, 무심결의
breaking change 가 코드리뷰에 드러난다.
"""

import pytest

from xgen_sdk.harness.errors import (
    ErrorCategory,
    HarnessError,
    ConfigError,
    ProviderError,
    RateLimitError,
    OverloadError,
    ContextOverflowError,
    ToolError,
    ToolTimeoutError,
    MCPConnectionError,
    ValidationError,
    PipelineAbortError,
    ALL_ERROR_CODES,
    error_code,
    provider_code_for_category,
)


# ── 인스턴스 → 코드 (고정 표) ──

EXPECTED_INSTANCE_CODES = [
    (HarnessError("x"), "exec.harness.unknown"),
    (ConfigError("x"), "exec.config.invalid"),
    (ToolError("x"), "exec.tool.failed"),
    (ToolTimeoutError("t", 1.0), "exec.tool.timeout"),
    (MCPConnectionError("x", "s1"), "exec.tool.mcp_connection"),
    (ValidationError("x"), "exec.decide.validation_failed"),
    (PipelineAbortError("x"), "exec.pipeline.abort"),
    (RateLimitError(), "exec.provider.rate_limit"),
    (OverloadError(), "exec.provider.overload"),
    (ContextOverflowError(), "exec.provider.context_overflow"),
]


@pytest.mark.parametrize("exc,expected", EXPECTED_INSTANCE_CODES)
def test_instance_code_stable(exc, expected):
    assert exc.code == expected
    assert exc.error_code == expected
    assert error_code(exc) == expected


# ── 카테고리 → provider 코드 (고정 표) ──

EXPECTED_CATEGORY_CODES = {
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


def test_every_category_has_provider_code():
    # 모든 ErrorCategory 가 provider 코드를 가져야 한다 (새 카테고리 추가 시 강제).
    for cat in ErrorCategory:
        assert provider_code_for_category(cat) == EXPECTED_CATEGORY_CODES[cat]


# ── HTTP status → 코드 (from_status 경유) ──

@pytest.mark.parametrize("status,expected", [
    (429, "exec.provider.rate_limit"),
    (529, "exec.provider.overload"),
    (401, "exec.provider.auth"),
    (500, "exec.provider.server"),
    (503, "exec.provider.server"),
    (400, "exec.provider.bad_request"),
    (404, "exec.provider.bad_request"),
])
def test_from_status_code_stable(status, expected):
    assert ProviderError.from_status(status).code == expected


# ── 전체 코드 집합 불변식 ──

def test_all_codes_format():
    # 모든 코드는 exec.<component>.<reason> 3-세그먼트.
    for c in ALL_ERROR_CODES:
        parts = c.split(".")
        assert len(parts) == 3, c
        assert parts[0] == "exec", c


def test_all_codes_superset_of_known():
    known = {e for _, e in EXPECTED_INSTANCE_CODES} | set(EXPECTED_CATEGORY_CODES.values())
    missing = known - set(ALL_ERROR_CODES)
    assert not missing, f"ALL_ERROR_CODES 에서 누락된 발행 코드: {missing}"


def test_no_duplicate_codes():
    assert len(ALL_ERROR_CODES) == len(set(ALL_ERROR_CODES))


def test_non_harness_exception_gets_generic_code():
    assert error_code(ValueError("boom")) == "exec.harness.unknown"
