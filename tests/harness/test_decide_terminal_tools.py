"""ThresholdDecide terminal_tools 회귀 테스트 (v1.18.2 fix).

버그: submit_result 같은 종착 도구 호출 후 짧은 확인문구(<200자)를 '인트로(합성 미완)'
로 오인 → 무한 continue. fix: stage_params.s08_decide.terminal_tools 의 마지막 호출
도구면 LOOP_COMPLETE (인트로 휴리스틱보다 우선). 미지정 시 동작 불변(backward-safe).

이 테스트가 fix 를 미래 회귀로부터 고정한다 — terminal 우선순위, backward-safe,
config 폴백 경로, 비종착 도구 fall-through 4 케이스.
"""

from types import SimpleNamespace

import pytest

from xgen_sdk.harness.stages.strategies._decide import (
    ThresholdDecide,
    LOOP_COMPLETE,
    LOOP_CONTINUE,
    LOOP_RETRY,
)


def _bug_state(history, config=None):
    """버그를 유발하던 정확한 상태: 도구 실행됨 + final 비어있음 + 짧은 인트로.

    terminal_tools 가 없으면 intro 휴리스틱이 CONTINUE 를 돌려 무한 continue 가 났다.
    """
    return SimpleNamespace(
        policy_block_reason=None,
        pending_tool_calls=[],
        validation_score=None,
        tools_executed_count=1,
        final_output="",
        last_assistant_text="제출했습니다.",   # 짧은 확인문구(<200자) — 버그 트리거
        tool_call_history=history,
        config=config,
    )


@pytest.mark.asyncio
async def test_terminal_tool_completes_overriding_intro_heuristic():
    """마지막 호출이 terminal 도구면 인트로 휴리스틱을 누르고 COMPLETE."""
    strat = ThresholdDecide()
    state = _bug_state([{"tool_name": "rag_search"}, {"tool_name": "submit_result"}])
    out = await strat.decide(state, {"terminal_tools": ["submit_result"]})
    assert out["decision"] == LOOP_COMPLETE
    assert "terminal" in out["reason"]


@pytest.mark.asyncio
async def test_unspecified_terminal_tools_is_backward_safe():
    """terminal_tools 미지정 → 옛 동작(intro 휴리스틱 CONTINUE) 그대로."""
    strat = ThresholdDecide()
    state = _bug_state([{"tool_name": "submit_result"}], config=SimpleNamespace(stage_params={}))
    out = await strat.decide(state, {})
    assert out["decision"] == LOOP_CONTINUE
    assert "합성" in out["reason"]


@pytest.mark.asyncio
async def test_non_terminal_last_tool_falls_through():
    """terminal_tools 지정됐어도 마지막 도구가 종착이 아니면 통과(CONTINUE)."""
    strat = ThresholdDecide()
    # 마지막 호출 = rag_search (비종착) → terminal 분기 미발동 → intro 휴리스틱.
    state = _bug_state([{"tool_name": "submit_result"}, {"tool_name": "rag_search"}])
    out = await strat.decide(state, {"terminal_tools": ["submit_result"]})
    assert out["decision"] == LOOP_CONTINUE


@pytest.mark.asyncio
async def test_terminal_tools_via_config_fallback():
    """params 에 없어도 state.config.stage_params.s08_decide 폴백으로 인식."""
    strat = ThresholdDecide()
    cfg = SimpleNamespace(stage_params={"s08_decide": {"terminal_tools": ["submit_result"]}})
    state = _bug_state([{"tool_name": "submit_result"}], config=cfg)
    out = await strat.decide(state, {})   # params 비어도 config 폴백
    assert out["decision"] == LOOP_COMPLETE
    assert "submit_result" in out["reason"]


@pytest.mark.asyncio
async def test_pending_tool_calls_still_take_precedence():
    """대기 도구가 있으면 terminal 검사보다 먼저 CONTINUE (앞단 분기 보존)."""
    strat = ThresholdDecide()
    state = _bug_state([{"tool_name": "submit_result"}])
    state.pending_tool_calls = [{"tool_name": "x"}]
    out = await strat.decide(state, {"terminal_tools": ["submit_result"]})
    assert out["decision"] == LOOP_CONTINUE
    assert "대기" in out["reason"]


# ── v1.18.3: 현재-iteration 가드 (과거 iteration 의 잔존 terminal 호출 방지) ──

@pytest.mark.asyncio
async def test_terminal_from_past_iteration_does_not_complete():
    """terminal 도구가 과거 iteration 호출이고 이번엔 호출 안 됐으면 조기종료 X."""
    strat = ThresholdDecide()
    state = _bug_state([{"tool_name": "submit_result", "iteration": 1}])
    state.loop_iteration = 3   # 현재 iter=3, terminal 은 iter=1 잔존 → fall-through
    out = await strat.decide(state, {"terminal_tools": ["submit_result"]})
    assert out["decision"] == LOOP_CONTINUE


@pytest.mark.asyncio
async def test_terminal_current_iteration_completes():
    """이번 iteration 에 호출된 terminal → 완료."""
    strat = ThresholdDecide()
    state = _bug_state([{"tool_name": "submit_result", "iteration": 3}])
    state.loop_iteration = 3
    out = await strat.decide(state, {"terminal_tools": ["submit_result"]})
    assert out["decision"] == LOOP_COMPLETE


# ── v1.18.3: max_retries 명시 0 = retry 끔 (이전엔 0 이 default 3 으로 삼켜짐) ──

def _retry_state(score=0.5, threshold=0.7, retry_count=0):
    return SimpleNamespace(
        policy_block_reason=None,
        pending_tool_calls=[],
        validation_score=score,
        retry_count=retry_count,
        config=SimpleNamespace(validation_threshold=threshold),
    )


@pytest.mark.asyncio
async def test_max_retries_explicit_zero_disables_retry():
    strat = ThresholdDecide()
    # 점수 미달이지만 max_retries=0 → retry 안 함 → COMPLETE.
    out = await strat.decide(_retry_state(), {"max_retries": 0})
    assert out["decision"] == LOOP_COMPLETE
    assert "한도" in out["reason"]


@pytest.mark.asyncio
async def test_max_retries_unset_uses_default():
    strat = ThresholdDecide()
    # 미설정(None) → default(>0) → 점수 미달이면 RETRY.
    out = await strat.decide(_retry_state(), {})
    assert out["decision"] == LOOP_RETRY
