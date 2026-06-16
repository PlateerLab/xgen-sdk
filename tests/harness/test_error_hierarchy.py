"""에러 계층 회귀 테스트.

ErrorCategory.recoverable 분류, ProviderError.from_status 매핑, 에러 클래스
속성(stage_id/category/http_status)을 고정한다. retry 로직(s00/s08)이 이 분류에
의존하므로 분류가 바뀌면 재시도 동작이 조용히 깨진다 — 그 회귀를 잡는다.
"""

import pytest

from xgen_sdk.harness.errors.hierarchy import (
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
)


# ── recoverable 분류 — retry 가능 여부의 단일 진실 소스 ──

RECOVERABLE = {
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.OVERLOAD,
    ErrorCategory.TIMEOUT,
    ErrorCategory.NETWORK,
    ErrorCategory.SERVER,
}
TERMINAL = {
    ErrorCategory.TOKEN_LIMIT,
    ErrorCategory.AUTH,
    ErrorCategory.BAD_REQUEST,
    ErrorCategory.TERMINAL,
    ErrorCategory.UNKNOWN,
}


@pytest.mark.parametrize("cat", sorted(RECOVERABLE, key=lambda c: c.value))
def test_recoverable_categories(cat):
    assert cat.recoverable is True


@pytest.mark.parametrize("cat", sorted(TERMINAL, key=lambda c: c.value))
def test_non_recoverable_categories(cat):
    assert cat.recoverable is False


def test_every_category_classified():
    # 새 카테고리가 추가되면 이 테스트가 깨져 분류를 강제로 갱신하게 한다.
    assert RECOVERABLE | TERMINAL == set(ErrorCategory)


# ── ProviderError.from_status — HTTP status → 카테고리/타입 매핑 ──

def test_from_status_429_is_rate_limit():
    err = ProviderError.from_status(429, "slow down")
    assert isinstance(err, RateLimitError)
    assert err.category is ErrorCategory.RATE_LIMIT
    assert err.recoverable is True
    assert err.http_status == 429


def test_from_status_529_is_overload():
    err = ProviderError.from_status(529)
    assert isinstance(err, OverloadError)
    assert err.category is ErrorCategory.OVERLOAD
    assert err.recoverable is True


def test_from_status_401_is_auth_terminal():
    err = ProviderError.from_status(401, "bad key")
    assert err.category is ErrorCategory.AUTH
    assert err.recoverable is False
    assert err.http_status == 401


@pytest.mark.parametrize("status", [500, 502, 503])
def test_from_status_5xx_is_server_recoverable(status):
    err = ProviderError.from_status(status)
    assert err.category is ErrorCategory.SERVER
    assert err.recoverable is True
    assert err.http_status == status


@pytest.mark.parametrize("status", [400, 404, 422])
def test_from_status_4xx_is_bad_request_terminal(status):
    err = ProviderError.from_status(status)
    assert err.category is ErrorCategory.BAD_REQUEST
    assert err.recoverable is False


# ── 클래스 속성/기본 stage_id 계약 ──

def test_config_error_is_auth():
    err = ConfigError("missing API key")
    assert err.category is ErrorCategory.AUTH
    assert err.recoverable is False


def test_context_overflow_is_token_limit():
    assert ContextOverflowError().category is ErrorCategory.TOKEN_LIMIT


def test_tool_error_defaults_to_s07():
    err = ToolError("boom", tool_name="rag_search")
    assert err.stage_id == "s07_act"
    assert err.tool_name == "rag_search"


def test_tool_timeout_carries_name():
    err = ToolTimeoutError("query_graph", 30.0)
    assert err.tool_name == "query_graph"
    assert "30" in str(err)


def test_mcp_connection_error_session():
    err = MCPConnectionError("handshake failed", session_id="sess-1")
    assert err.session_id == "sess-1"
    assert err.tool_name == "mcp:sess-1"


def test_validation_error_is_s08_with_score():
    err = ValidationError("quality below threshold", score=0.42)
    assert err.stage_id == "s08_decide"
    assert err.score == 0.42


def test_pipeline_abort_is_terminal():
    err = PipelineAbortError("unrecoverable")
    assert err.category is ErrorCategory.TERMINAL
    assert err.recoverable is False


def test_all_errors_subclass_harness_error():
    for cls in (ConfigError, ProviderError, ToolError, ValidationError, PipelineAbortError):
        assert issubclass(cls, HarnessError)
