"""HarnessConfig top-level 필드의 self-describing 스키마 (v1.7.1).

stage_config.STAGE_CONFIGS 가 stage_params 의 schema 이듯, 이 모듈은
HarnessConfig top-level 필드 (provider/model/judge_*/cost_budget_usd 등) 의
UI 메타. 외부 패키지가 register_top_level_field() 로 추가 가능 — 빌트인을
손대지 않고 새 필드 합류.

진실 소스 분리:
- stage_params.{stage_id}.* 의 schema → core/stage_config.py 의 STAGE_CONFIGS
- harness_config.* 의 schema (= 이 모듈) → _TOP_LEVEL_FIELDS

각 필드 dict 는 stage_config 의 field 와 같은 구조 + scope: "top_level" 로 박힘.
프론트는 scope 보고 hc 에 직접 박을지 stage_params 안에 박을지 결정.
"""

from typing import Any


# 빌트인 — HarnessConfig Pydantic 필드 중 사용자 가시성이 의미 있는 항목.
# 외부 패키지가 register_top_level_field("name", schema) 로 추가 가능.
_TOP_LEVEL_FIELDS: dict[str, dict] = {
    # ─── Judge LLM (s08_decide.judge_then_loop 가 사용할 평가 모델) ───
    # judge_use_main 은 UI 상태 (chip "본문 재사용" 활성 여부) — backend 측에선
    # 그냥 judge_model="" 와 동일 시맨틱 (빈값이면 본문 LLM 재사용, judge_then_loop.py:185).
    # 프론트는 사용자 명시 의도 표현용 별도 boolean 으로 두지만 backend 는 judge_model 만 본다.
    "judge_use_main": {
        "id": "judge_use_main",
        "label": "Judge: 본문 LLM 재사용",
        "type": "toggle",
        "scope": "top_level",
        "default": False,
        "description": (
            "true → s08_decide 평가에 본문 LLM 재사용 (judge_model 무시). "
            "false 일 때 judge_model 비어 있으면 'judge 미설정' 상태."
        ),
    },
    "judge_provider": {
        "id": "judge_provider",
        "label": "Judge Provider",
        "type": "select",
        "options_source": "providers",
        "scope": "top_level",
        "default": "",
        "depends_on": "judge_use_main",
        "description": (
            "Judge LLM provider — 본문과 다른 provider 사용 시. "
            "judge_use_main=true 면 무시 (본문 provider 재사용)."
        ),
    },
    "judge_model": {
        "id": "judge_model",
        "label": "Judge Model",
        "type": "select",
        "options_source": "provider-models",
        "scope": "top_level",
        "default": "",
        "depends_on": "judge_use_main",
        "description": (
            "Judge LLM model. 빈 값이면 본문 LLM 재사용 (judge_then_loop.py:185 "
            "backward compat). judge_use_main=true 면 무시."
        ),
    },
    # ─── Provider/Model — 본문 LLM (s00_harness 가 디스패처) ───
    "provider": {
        "id": "provider",
        "label": "Provider",
        "type": "select",
        "options_source": "providers",
        "scope": "top_level",
        "default": "",
        "description": "본문 LLM provider — providers 레지스트리 등록 값 중 선택.",
    },
    "model": {
        "id": "model",
        "label": "Model",
        "type": "select",
        "options_source": "provider-models",
        "scope": "top_level",
        "default": "",
        "description": "본문 LLM model — provider 선택 후 가용 모델 자동 노출.",
    },
    "temperature": {
        "id": "temperature",
        "label": "Temperature",
        "type": "slider",
        "min": 0,
        "max": 2,
        "step": 0.1,
        "scope": "top_level",
        "default": None,
        "description": "본문 LLM 샘플링 온도. provider/model 별 가용 범위 차이.",
    },
    "max_tokens": {
        "id": "max_tokens",
        "label": "Max Output Tokens",
        "type": "number",
        "min": 256,
        "max": 200000,
        "step": 256,
        "scope": "top_level",
        "default": None,
        "description": "본문 LLM 응답 토큰 상한.",
    },
    # ─── 정책 cap ───
    "max_iterations": {
        "id": "max_iterations",
        "label": "최대 도구 호출 라운드",
        "type": "number",
        "min": 1,
        "max": 50,
        "scope": "top_level",
        "default": None,
        "description": "에이전트 루프 최대 반복. tool_call 라운드 단위.",
    },
    "cost_budget_usd": {
        "id": "cost_budget_usd",
        "label": "비용 예산 (USD)",
        "type": "number",
        "min": 0,
        "max": 100,
        "step": 0.5,
        "scope": "top_level",
        "default": None,
        "description": "실행당 USD 예산 cap. 초과 시 s05_policy 가 차단.",
    },
    "context_window": {
        "id": "context_window",
        "label": "Context Window",
        "type": "number",
        "min": 10000,
        "max": 1000000,
        "step": 10000,
        "scope": "top_level",
        "default": None,
        "description": "provider context limit. s06_context 압축 임계 산정 기준.",
    },
    "thinking_enabled": {
        "id": "thinking_enabled",
        "label": "Extended Thinking 활성",
        "type": "toggle",
        "scope": "top_level",
        "default": False,
        "description": "Anthropic extended thinking 사용 여부 (지원 모델만).",
    },
    "thinking_budget_tokens": {
        "id": "thinking_budget_tokens",
        "label": "Thinking 토큰 예산",
        "type": "number",
        "min": 1024,
        "max": 200000,
        "step": 1024,
        "scope": "top_level",
        "default": None,
        "depends_on": "thinking_enabled",
        "description": "extended thinking 토큰 예산 (thinking_enabled=true 일 때만).",
    },
    "verbose_events": {
        "id": "verbose_events",
        "label": "Verbose Events 발행",
        "type": "toggle",
        "scope": "top_level",
        "default": True,
        "description": (
            "True 면 ServiceLookup / CapabilityBind / StageSubstep / Retry 이벤트가 "
            "추가로 SSE EventLog 에 발행됨 — 디버깅/감사 친화."
        ),
    },
}


def register_top_level_field(field_id: str, schema: dict[str, Any]) -> None:
    """외부 패키지가 HarnessConfig top-level 필드 메타를 등록.

    예) v1.8 에서 register_top_level_field("rate_limit_qps", {...}) 로 새 필드 추가.
    같은 id 를 두 번 등록하면 마지막이 승리 (덮어쓰기). field_id 는 schema["id"] 와
    일치해야 함 — 호출자 책임.
    """
    if not isinstance(field_id, str) or not field_id:
        raise ValueError("field_id must be a non-empty string")
    if not isinstance(schema, dict):
        raise TypeError("schema must be a dict")
    _TOP_LEVEL_FIELDS[field_id] = dict(schema)


def get_top_level_field_schema() -> dict[str, dict]:
    """현재 등록된 top-level 필드 메타 dict 반환 (deep copy 로 외부 변경 차단)."""
    return {k: dict(v) for k, v in _TOP_LEVEL_FIELDS.items()}


__all__ = [
    "register_top_level_field",
    "get_top_level_field_schema",
]
