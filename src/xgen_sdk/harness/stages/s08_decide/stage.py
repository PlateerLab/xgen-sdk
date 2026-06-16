"""
S08 Decide — 루프 계속/완료 판단 + 응답 품질 평가 (v1.0)

Strategy 에 전적 위임 — Stage 내부에 분기 로직 없음.
각 DecideStrategy 구현체가 자기 판단 규칙을 전부 들고 있다.

기본 Strategy:
  threshold        — Guard 체인 + 도구 호출 + 점수 + 응답 기반 단순 판정
  judge_then_loop  — v1.0: LLM 평가(구 s08_judge) → 점수 → threshold 결정 (격하 흡수)
  always_pass      — 항상 complete

v1.0 통합:
  - 구 s09_decide → s08_decide (번호 −1 시프트)
  - 구 s08_judge stage 삭제 — judge_then_loop strategy 로 격하
  - 응답 평가가 필요하면 active_strategies['s08_decide'] = 'judge_then_loop' 픽
"""

import logging

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState
from ..strategies._decide import (
    LOOP_CONTINUE,
    LOOP_COMPLETE,
    LOOP_RETRY,
    LOOP_ERROR,
    LOOP_ESCALATE,
)

logger = logging.getLogger("harness.stage.decide")

# geny 패턴: loop decision 상수 재노출 (하위 호환 — 외부 import 하는 코드 보호)
__all__ = [
    "DecideStage",
    "LOOP_CONTINUE",
    "LOOP_COMPLETE",
    "LOOP_RETRY",
    "LOOP_ERROR",
    "LOOP_ESCALATE",
]


class DecideStage(Stage):
    """루프 계속/완료 판단 — 전적으로 DecideStrategy 에 위임."""

    @property
    def stage_id(self) -> str:
        return "s08_decide"

    @property
    def order(self) -> int:
        return 8

    async def execute(self, state: PipelineState) -> dict:
        # v1.0 — judge_then_loop strategy (구 s08_judge 격하 흡수).
        # active_strategies['s08_decide'] == 'judge_then_loop' 이거나
        # stage_params.judge_enabled == True 면 평가 실행.
        active = ""
        if state.config:
            picked = (state.config.active_strategies or {}).get(self.stage_id)
            if isinstance(picked, str):
                active = picked.strip()
        do_judge = (active == "judge_then_loop") or bool(self.get_param("judge_enabled", state, False))
        if do_judge:
            from .strategies.judge_then_loop import evaluate_response
            judge_result = await evaluate_response(state, self.get_param)
            if state.event_emitter and not judge_result.get("bypassed"):
                from ...events.types import EvaluationEvent
                await state.event_emitter.emit(EvaluationEvent(
                    score=judge_result.get("score", 0.0),
                    feedback=judge_result.get("feedback", ""),
                    verdict=judge_result.get("verdict", "pass"),
                ))

        # s07_act 의 strict_no_error 변형이 도구 실패를 감지하면 즉시 stop.
        # 박은 측: stages/s07_act/stage.py · 키 prefix 's07_strict_*'.
        # 폴리시: 부분 성공 위에서 LLM 이 추측 답변하느니 명시 에러로 종료.
        if state.metadata.get("s07_strict_failed"):
            failures = state.metadata.get("s07_strict_failures") or []
            reason = (
                f"strict_no_error: {len(failures)}개 도구 실패 — "
                f"후속 LLM 합성 차단"
            )
            logger.warning("[Decide] %s · failures=%s", reason, failures[:3])
            state.loop_decision = LOOP_COMPLETE
            return {
                "decision": LOOP_COMPLETE,
                "reason": reason,
                "strict_failures": failures,
            }

        strategy = self.resolve_strategy("decide", state, "threshold")
        if strategy is None:
            state.loop_decision = LOOP_ERROR
            reason = "Decide strategy 미등록 — threshold 를 찾지 못함"
            logger.error("[Decide] %s", reason)
            return {"decision": LOOP_ERROR, "reason": reason}

        # v0.29.2 — guards / cost_budget_usd / token_budget / content_* dead read 제거.
        # ThresholdDecide (v0.17.0+) 가 이 params 를 더 이상 소비하지 않음 — Policy Gate
        # (s05_policy) 가 LOOP_BOUNDARY 훅에서 Guard 체인을 직접 실행한 결과를
        # state.policy_block_reason / loop_decision 으로 전달받아 그 결정을 존중.
        # max_retries 만 retry 카운트 비교용으로 의미.
        # None(미설정) 을 그대로 전달 — _decide 가 미설정→default, 명시 0→retry 끔 구분.
        # (이전: `or 0` 이 명시 0 을 삼켜 _decide 의 `0 or default` 로 default(3) 가 강제됐다.)
        params = {
            "max_retries": self.get_param("max_retries", state, None),
        }

        try:
            result = await strategy.decide(state, params)
        except Exception as e:
            logger.exception("[Decide] Strategy '%s' 실행 실패", strategy.name)
            state.loop_decision = LOOP_ERROR
            return {"decision": LOOP_ERROR, "reason": f"{strategy.name} 실패: {e}"}

        decision = result.get("decision", LOOP_CONTINUE)
        state.loop_decision = decision
        return result

    def list_strategies(self) -> list[StrategyInfo]:
        # v1.0 — judge_then_loop 추가 (구 s08_judge stage 격하).
        return [
            StrategyInfo("threshold", "Guard 체인 + 점수 기반 단순 판단", is_default=True),
            StrategyInfo("judge_then_loop", "LLM 평가 → 점수 → threshold (구 s08_judge 흡수)"),
            StrategyInfo("always_pass", "항상 완료 (루프 없음)"),
        ]
