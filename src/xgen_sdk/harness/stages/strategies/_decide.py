"""
Decide strategies — s08_decide 용 Strategy 구현체 (v1.0)

v0.17.0 책임 분리:
  - Guard 호출은 전부 s05_policy Stage (Policy Gate) 로 이관.
  - 본 모듈은 "현 state 로 루프를 계속/완료/재시도" 판단만 수행.
  - s05_policy 가 block 을 걸면 state.loop_decision 을 미리 설정하므로
    ThresholdDecide 는 그 신호를 존중하여 즉시 complete.

구현체:
  ThresholdDecide — 도구 호출/검증 점수/텍스트 응답 기반 판단 (기본)
  AlwaysPassDecide — 항상 complete (루프 없음, 1회 실행)
"""

import logging
from typing import Any

from ..interfaces import DecideStrategy

logger = logging.getLogger("harness.strategy.decide")

LOOP_CONTINUE = "continue"
LOOP_COMPLETE = "complete"
LOOP_RETRY = "retry"
LOOP_ERROR = "error"
LOOP_ESCALATE = "escalate"


# default 값은 외부 override 가능한 단일 레지스트리. v1.0.2 "정책 default 박제 정리"
# 룰을 strategy 영역에도 적용 — magic number 가 함수 본문에 박히지 않도록 한다.
_DECIDE_DEFAULTS: dict[str, Any] = {
    "max_retries": 3,
    "validation_threshold": 0.7,
}


def register_decide_defaults(**kwargs: Any) -> None:
    """외부 갤러리/이식측에서 decide 기본값을 조정.

    예: `register_decide_defaults(max_retries=5)` → ThresholdDecide.decide 가
    params 에 max_retries 가 없을 때 5 사용.
    """
    for k, v in kwargs.items():
        if k in _DECIDE_DEFAULTS:
            _DECIDE_DEFAULTS[k] = v


def get_decide_default(key: str) -> Any:
    return _DECIDE_DEFAULTS.get(key)


class ThresholdDecide(DecideStrategy):
    """도구 호출 대기 / 검증 점수 / 텍스트 응답 기반 루프 판단.

    v0.17.0 — Guard 체인 호출 제거. Policy Gate (s05_policy) 가 이미 loop_boundary
    훅에서 Guard 를 실행해 state.policy_block_reason / loop_decision 을 설정.
    본 Strategy 는 순수 루프 판단만 담당.
    """

    @property
    def name(self) -> str:
        return "threshold"

    @property
    def description(self) -> str:
        return "도구 호출 / 검증 점수 / 응답 기반 루프 판단 (Policy Gate 결과 존중)"

    async def decide(self, state: Any, params: dict[str, Any]) -> dict[str, Any]:
        # Policy Gate (s05_policy) 가 이미 block 을 걸어놨으면 그 결정을 존중.
        policy_reason = getattr(state, "policy_block_reason", None)
        if policy_reason:
            return {
                "decision": LOOP_COMPLETE,
                "reason": f"Policy Gate 차단: {policy_reason}",
                "guard": getattr(state, "policy_block_guard", "") or "",
            }

        # 도구 호출 대기 → continue
        pending = getattr(state, "pending_tool_calls", None) or []
        if pending:
            return {
                "decision": LOOP_CONTINUE,
                "reason": f"도구 호출 {len(pending)}건 대기",
            }

        # 검증 점수 기반 retry
        validation_score = getattr(state, "validation_score", None)
        if validation_score is not None:
            config = getattr(state, "config", None)
            default_threshold = _DECIDE_DEFAULTS["validation_threshold"]
            threshold = getattr(config, "validation_threshold", default_threshold) if config else default_threshold
            if validation_score < threshold:
                retry_count = int(getattr(state, "retry_count", 0) or 0)
                default_max = _DECIDE_DEFAULTS["max_retries"]
                # None(미설정)→default, 명시 0→retry 끔. `or default` 는 명시 0 을 삼켜서 X.
                _mr = params.get("max_retries", None)
                max_retries = default_max if _mr is None else int(_mr)
                if retry_count < max_retries:
                    return {
                        "decision": LOOP_RETRY,
                        "reason": f"검증 점수 미달 ({validation_score:.2f} < {threshold})",
                    }
                return {
                    "decision": LOOP_COMPLETE,
                    "reason": f"재시도 한도 도달 ({retry_count}/{max_retries}, 점수 {validation_score:.2f})",
                }

        # ── terminal tool 호출 완료 — submit_result 같은 '종착' 도구가 마지막으로 호출됐으면
        #   합성을 더 요구하지 말고 즉시 complete. submit 후 짧은 확인문구를 아래 '인트로(<200자)'
        #   로 오인해 무한 continue(→ 90루프 + assistant-prefill ValidationException)하던 것을
        #   근본 차단. 하드코딩 X — 이식측이 stage_params.s08_decide.terminal_tools 로 지정.
        terminal_tools = params.get("terminal_tools")
        if not terminal_tools:
            try:
                _cfg = getattr(state, "config", None)
                terminal_tools = (
                    (_cfg.stage_params.get("s08_decide") or {}).get("terminal_tools")
                ) if _cfg else None
            except Exception:
                terminal_tools = None
        terminal_tools = set(terminal_tools or [])
        if terminal_tools:
            _cur_iter = getattr(state, "loop_iteration", None)
            for _h in reversed(getattr(state, "tool_call_history", None) or []):
                if not isinstance(_h, dict):
                    continue
                _last_tool = str(_h.get("tool_name") or "")
                _it = _h.get("iteration")
                # 최신 도구 호출 1건만 본다. terminal 이고 '이번 iteration' 에 호출됐을 때만 완료.
                # 과거 iteration 의 잔존 terminal 호출이 텍스트-only 턴을 조기종료하던 것 방지.
                # backward-safe: iteration 정보가 없으면(_it None / 외부 history) 옛 동작 유지.
                if _last_tool in terminal_tools and (
                    _cur_iter is None or _it is None or _it == _cur_iter
                ):
                    return {
                        "decision": LOOP_COMPLETE,
                        "reason": f"terminal tool 호출 완료 ({_last_tool})",
                    }
                break  # 최신 1건만 판정

        # v1.0.6 — 도구 호출 직후 합성 답변 미완 케이스 우선 식별.
        # 도구가 실행됐고 (tools_executed_count > 0) + 최종 답변이 비어있고 +
        # last_assistant_text 가 짧은 인트로(< 200자)이면 그 텍스트는 도구 호출
        # 직전의 인트로일 가능성이 높음 — 합성 답변은 아직 안 만들어진 상태.
        # 이 케이스를 CONTINUE 로 흘려야 다음 iter 에서 LLM 이 도구 결과를 보고
        # 답변을 합성. 200자 임계는 pipeline._needs_synthesis_kick 의 safeguard
        # 와 동일 (`_SHORT_INTRO_THRESHOLD`). 두 곳이 같은 정책을 공유한다는 의미.
        tools_run = int(getattr(state, "tools_executed_count", 0) or 0)
        last_text_len = len(getattr(state, "last_assistant_text", "") or "")
        final_out = str(getattr(state, "final_output", "") or "")
        _SHORT_INTRO_THRESHOLD = 200
        if tools_run > 0 and not final_out and last_text_len < _SHORT_INTRO_THRESHOLD:
            return {
                "decision": LOOP_CONTINUE,
                "reason": f"도구 결과 합성 필요 (실행={tools_run}, 인트로={last_text_len}자)",
            }

        # 텍스트 응답 있음 → complete
        if getattr(state, "last_assistant_text", ""):
            return {"decision": LOOP_COMPLETE, "reason": "응답 생성 완료"}

        # 기본 → continue
        return {"decision": LOOP_CONTINUE, "reason": "추가 처리 필요"}


class AlwaysPassDecide(DecideStrategy):
    """1회 실행 후 즉시 complete — 단발성 워크플로우용."""

    @property
    def name(self) -> str:
        return "always_pass"

    @property
    def description(self) -> str:
        return "항상 완료 (루프 없음, 1회 실행)"

    async def decide(self, state: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {"decision": LOOP_COMPLETE, "reason": "always_pass strategy"}
