"""ToolDiversityGuard — 동일 도구 반복 호출(검색 붕괴) 차단."""

from xgen_sdk.harness.core.state import PipelineState
from xgen_sdk.harness.stages.strategies.guard import (
    HookContext,
    HookPoint,
    ToolDiversityGuard,
    available_guards,
)


def _ctx(pending, history):
    return HookContext(hook=HookPoint.PRE_TOOL, pending_tool_call=pending, tool_call_history=history)


def test_passes_under_threshold():
    g = ToolDiversityGuard(max_repeats=3)
    pending = {"tool_name": "search", "tool_input": {"q": "x"}}
    hist = [{"tool_name": "search", "tool_input": {"q": "x"}}] * 2
    assert g.check(PipelineState(), _ctx(pending, hist)).passed


def test_blocks_at_threshold():
    g = ToolDiversityGuard(max_repeats=3)
    pending = {"tool_name": "search", "tool_input": {"q": "x"}}
    hist = [{"tool_name": "search", "tool_input": {"q": "x"}}] * 3
    r = g.check(PipelineState(), _ctx(pending, hist))
    assert not r.passed and r.severity == "block"
    assert "search" in r.tool_error_message


def test_different_args_not_counted():
    g = ToolDiversityGuard(max_repeats=2)
    pending = {"tool_name": "search", "tool_input": {"q": "x"}}
    hist = [
        {"tool_name": "search", "tool_input": {"q": "y"}},
        {"tool_name": "search", "tool_input": {"q": "z"}},
    ]
    assert g.check(PipelineState(), _ctx(pending, hist)).passed


def test_input_key_order_irrelevant():
    g = ToolDiversityGuard(max_repeats=1)
    pending = {"tool_name": "t", "tool_input": {"a": 1, "b": 2}}
    hist = [{"tool_name": "t", "tool_input": {"b": 2, "a": 1}}]
    assert not g.check(PipelineState(), _ctx(pending, hist)).passed  # sort_keys → 같은 지문


def test_window_limits_history():
    pending = {"tool_name": "s", "tool_input": {"q": "x"}}
    hist = [{"tool_name": "s", "tool_input": {"q": "x"}}] * 3
    assert not ToolDiversityGuard(max_repeats=2, window=2).check(PipelineState(), _ctx(pending, hist)).passed
    assert ToolDiversityGuard(max_repeats=3, window=2).check(PipelineState(), _ctx(pending, hist)).passed


def test_no_pending_passes():
    assert ToolDiversityGuard().check(PipelineState(), _ctx(None, [])).passed


def test_configure():
    g = ToolDiversityGuard()
    g.configure({"max_repeats": 5, "window": 10})
    assert g._max_repeats == 5 and g._window == 10


def test_hook_point_is_pre_tool():
    assert ToolDiversityGuard().hook_points == {HookPoint.PRE_TOOL}


def test_registered_in_available_guards():
    assert "tool_diversity" in available_guards()
