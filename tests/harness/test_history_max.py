"""s02_history max_history 회귀 테스트 (v1.18.3 fix).

이전 버그: `int(get_param(...) or 0)` → 미설정·명시0 둘 다 0 → `[-0:]` = 전체 주입.
fix: None(미설정)=전체, 명시 0=없음, N>0=마지막 N.
"""

from types import SimpleNamespace

import pytest

from xgen_sdk.harness.stages.s02_history.stage import MemoryStage


def _state(history, param_val):
    st = SimpleNamespace(
        conversation_history=history,
        messages=[{"role": "user", "content": "current"}],
        previous_results=[],
        config=SimpleNamespace(stage_params={"s02_history": {"max_history": param_val} if param_val is not None else {}}),
        metadata={},
    )
    return st


HIST = [{"role": "user", "content": f"m{i}"} for i in range(5)]


@pytest.mark.asyncio
async def test_explicit_zero_injects_none():
    stage = MemoryStage()
    st = _state(HIST, 0)
    out = await stage._execute_default(st)
    assert out["injected"] == 0   # 명시 0 = 없음 (이전엔 전체였음)


@pytest.mark.asyncio
async def test_unset_injects_all():
    stage = MemoryStage()
    st = _state(HIST, None)
    out = await stage._execute_default(st)
    assert out["injected"] == 5   # 미설정 = 전체 (기본 동작 보존)


@pytest.mark.asyncio
async def test_positive_n_injects_last_n():
    stage = MemoryStage()
    st = _state(HIST, 2)
    out = await stage._execute_default(st)
    assert out["injected"] == 2
