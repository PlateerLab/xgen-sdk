"""Runtime safety floors — 엔진 크래시 방지용 최소값 레지스트리.

PHILOSOPHY §5 와 화해:
  엔진은 도메인을 모르므로 "정책 default" (예: max_iterations=10 이 옳다) 를 박을 수 없다.
  하지만 정책 sentinel (None) 이 산술/비교 위치까지 흘러가면 TypeError 로 크래시한다.

이 레지스트리는 그 두 요구를 통합한다:
  1. 이식측이 명시 값을 박으면 → 그 값 사용 (정책 default 책임 = 이식측)
  2. sentinel (None) 일 때 → 엔진 "안전 바닥" (safety floor) 으로 폴백
  3. 외부 플러그인이 `register_runtime_default()` 로 floor 자체를 override 가능

"floor" 단어 선택: 사용자/이식측의 "정책 default" 와 구분되는 엔진 자기방어 라인.
floor 는 안전 보장이지 권장값이 아니다.

확장 패턴 (v1.0.4 `_DECIDE_DEFAULTS` 와 동일):
  외부 패키지가 import 시점에 자기 도메인 floor 등록 →
  `from xgen_harness import register_runtime_default`
  `register_runtime_default("max_iterations", 20)`
"""

from __future__ import annotations

from typing import Any


# ─────────────────────────────────────────────
# 안전 바닥 — 엔진이 정책 None 일 때 폴백할 값들
# ─────────────────────────────────────────────
# 새 키 추가 시 README §환경변수 치트시트 와 docs/harness/PHILOSOPHY.md 동시 갱신.
_RUNTIME_DEFAULTS: dict[str, Any] = {
    # 루프 제어
    "max_iterations": 10,                   # agent loop 무한 진입 방지
    "max_retries": 3,                       # judge retry 무한 방지
    "max_tool_rounds": 8,                   # tool_use multi-turn 상한
    "validation_threshold": 0.0,            # judge 항상 통과 (sentinel-as-safe)
    "rag_top_k": 4,                         # RAG 검색당 문서 수 — 미설정 시 floor (0/limit=0 검색 방지)

    # Pipeline safeguard 임계
    "synthesis_intro_threshold_chars": 200, # tool_use 후 짧은 인트로 → synthesis 재호출 컷

    # State 누적 방지
    "max_pending_tool_results": 256,        # flush 전 결과 누적 상한 (비상 진단용)

    # Provider / token budget — sentinel None 일 때 안전 바닥
    "context_window": 200_000,              # provider 컨텍스트 윈도우 (Claude 기본)
    "max_tokens": 8192,                     # 응답 max 토큰
    "thinking_budget_tokens": 0,            # extended thinking 비활성
    "temperature": 0.7,                     # LLM creativity (도메인이 strict 면 0.0)

    # s06_context Cascade compaction — 컨텍스트 윈도우 사용률(%) 임계.
    # 사용자가 stage_param 으로 안 박으면 이 floor 사용. 0 이면 그 strategy 비활성.
    "cascade_l3_threshold_pct": 70,         # gentle: 메시지 단순 trim
    "cascade_l4_threshold_pct": 85,         # medium: 중간 overlay 교체 (비파괴)
    "cascade_l5_threshold_pct": 95,         # heavy: child LLM 9-section 요약
    "compaction_threshold_pct": 80,         # 단일 compactor strategy
    "microcompact_threshold_pct": 75,       # microcompact strategy
    "context_collapse_threshold_pct": 85,   # collapse_overlay strategy
    "autocompact_threshold_pct": 90,        # autocompact_llm strategy

    # v1.9.0 — fetch_synthesize sub-agent (Claude Code 패턴) 임계
    "synth_raw_threshold": 2000,            # 본문이 이하면 sub-agent 호출 skip (raw passthrough)
    "synth_sub_max_turns": 8,               # sub-agent ReAct 최대 turn (안전망)

    # v1.9.0 — 도구 연속 실패 graceful fallback (P0#1)
    "tool_consecutive_failure_limit": 3,    # 같은 도구 N 회 연속 실패 시 warning + LLM fallback emit
}


def register_runtime_default(key: str, value: Any) -> None:
    """엔진 안전 바닥 override.

    외부 패키지가 자기 도메인의 floor 박는 용도. HarnessConfig 의 정책 값과
    다른 차원 — 사용자가 명시 안 했을 때만 발동한다.

    Args:
        key: floor 식별자 (예: "max_iterations").
        value: 폴백 값. None 은 의도적으로 허용 — "정책 sentinel 그대로" 의미.

    Raises:
        ValueError: key 가 빈 문자열이거나 str 아님.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be non-empty string")
    _RUNTIME_DEFAULTS[key.strip()] = value


def get_runtime_default(key: str, fallback: Any = None) -> Any:
    """안전 바닥 조회.

    Args:
        key: 등록된 floor 식별자.
        fallback: 미등록 키일 때 반환. 기본 None — sentinel 그대로 흘림.

    Returns:
        등록된 값 또는 fallback.
    """
    return _RUNTIME_DEFAULTS.get(key, fallback)


def resolve_with_default(value: Any, key: str, fallback: Any = None) -> Any:
    """sentinel-aware 조회 헬퍼.

    `value` 가 None 이 아니면 그대로, None 이면 runtime default, 그것도 없으면 fallback.
    호출자에서 `if x is None else get_runtime_default(...)` 패턴을 한 줄로.

    Args:
        value: 사용자 정책 값 (None 허용).
        key: 폴백 시 조회할 floor 키.
        fallback: floor 도 미등록 시 최종 fallback.

    Returns:
        value 가 None 이 아니면 value, 아니면 floor, 아니면 fallback.
    """
    if value is not None:
        return value
    return _RUNTIME_DEFAULTS.get(key, fallback)


def list_runtime_defaults() -> dict[str, Any]:
    """등록된 안전 바닥 전수 읽기 복사 — 디버깅·UI 진단용."""
    return dict(_RUNTIME_DEFAULTS)


def unregister_runtime_default(key: str) -> None:
    """floor 제거 — 테스트/리셋 용도."""
    _RUNTIME_DEFAULTS.pop(key, None)
