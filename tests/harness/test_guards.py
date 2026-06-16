"""Policy Gate / Guard 회귀 테스트.

내장 Guard(iteration/cost_budget/token_budget/content)의 차단 로직, 훅 포인트
선언, build_guard_chain 선언형 조립, GuardChain short-circuit 을 고정한다.
Guard 는 state 를 getattr/hasattr 로 읽으므로 SimpleNamespace 스텁으로 검증한다.
"""

from types import SimpleNamespace

import pytest

from xgen_sdk.harness.stages.strategies.guard import (
    HookPoint,
    HookContext,
    IterationGuard,
    CostBudgetGuard,
    TokenBudgetGuard,
    ContentGuard,
    GuardChain,
    build_guard_chain,
    available_guards,
    register_guard,
    Guard,
    GuardResult,
)


def _loop_ctx():
    return HookContext(hook=HookPoint.LOOP_BOUNDARY)


# ── IterationGuard ──

def test_iteration_guard_blocks_at_max():
    g = IterationGuard()
    state = SimpleNamespace(loop_iteration=5, config=SimpleNamespace(max_iterations=5))
    r = g.check(state, _loop_ctx())
    assert r.passed is False
    assert r.severity == "block"


def test_iteration_guard_passes_below_max():
    g = IterationGuard()
    state = SimpleNamespace(loop_iteration=2, config=SimpleNamespace(max_iterations=5))
    assert g.check(state, _loop_ctx()).passed is True


def test_iteration_guard_missing_state_is_pass():
    g = IterationGuard()
    assert g.check(SimpleNamespace(), _loop_ctx()).passed is True


# ── CostBudgetGuard ──

def test_cost_guard_blocks_over_budget():
    g = CostBudgetGuard()
    g.configure({"cost_budget_usd": 5.0})
    state = SimpleNamespace(cost_usd=5.01)
    r = g.check(state, _loop_ctx())
    assert r.passed is False and r.severity == "block"


def test_cost_guard_passes_under_budget():
    g = CostBudgetGuard()
    g.configure({"cost_budget_usd": 5.0})
    assert g.check(SimpleNamespace(cost_usd=1.0), _loop_ctx()).passed is True


def test_cost_guard_config_fallback():
    g = CostBudgetGuard()  # 미설정 → state.config.cost_budget_usd 폴백
    state = SimpleNamespace(cost_usd=3.0, config=SimpleNamespace(cost_budget_usd=2.0))
    assert g.check(state, _loop_ctx()).passed is False


# ── TokenBudgetGuard ──

def test_token_guard_blocks_over_95pct():
    g = TokenBudgetGuard(token_budget=1000)
    state = SimpleNamespace(token_usage=SimpleNamespace(total=960))
    r = g.check(state, _loop_ctx())
    assert r.passed is False and r.severity == "block"


def test_token_guard_warns_over_80pct():
    g = TokenBudgetGuard(token_budget=1000)
    state = SimpleNamespace(token_usage=SimpleNamespace(total=850))
    r = g.check(state, _loop_ctx())
    assert r.passed is True and r.severity == "warn"


def test_token_guard_passes_low():
    g = TokenBudgetGuard(token_budget=1000)
    state = SimpleNamespace(token_usage=SimpleNamespace(total=100))
    assert g.check(state, _loop_ctx()).passed is True


# ── ContentGuard — blocked patterns + PII ──

def test_content_guard_blocks_pattern_on_output():
    g = ContentGuard(blocked_patterns=["forbidden"], check_target="output")
    state = SimpleNamespace(last_assistant_text="this is forbidden content")
    ctx = HookContext(hook=HookPoint.POST_RESPONSE)
    assert g.check(state, ctx).passed is False


def test_content_guard_detects_pii_email():
    g = ContentGuard(detect_pii=True, check_target="output")
    state = SimpleNamespace(last_assistant_text="contact me at a@b.com please")
    ctx = HookContext(hook=HookPoint.POST_RESPONSE)
    r = g.check(state, ctx)
    assert r.passed is False and "PII" in r.reason


def test_content_guard_clean_passes():
    g = ContentGuard(blocked_patterns=["forbidden"], detect_pii=True, check_target="output")
    state = SimpleNamespace(last_assistant_text="perfectly fine text")
    ctx = HookContext(hook=HookPoint.POST_RESPONSE)
    assert g.check(state, ctx).passed is True


def test_content_guard_hook_points_follow_target():
    assert ContentGuard(check_target="input").hook_points == {HookPoint.PRE_MAIN}
    assert ContentGuard(check_target="output").hook_points == {HookPoint.POST_RESPONSE}
    assert ContentGuard(check_target="both").hook_points == {
        HookPoint.PRE_MAIN, HookPoint.POST_RESPONSE,
    }


# ── 레지스트리 / 체인 ──

def test_available_guards_has_builtins():
    names = set(available_guards().keys())
    assert {"iteration", "cost_budget", "token_budget", "content"} <= names


def test_build_guard_chain_skips_unknown():
    chain = build_guard_chain([
        {"name": "iteration"},
        {"name": "cost_budget", "params": {"cost_budget_usd": 5.0}},
        {"name": "does_not_exist"},
    ])
    assert len(chain.guards) == 2


def test_guard_chain_short_circuits_on_block():
    chain = build_guard_chain([
        {"name": "cost_budget", "params": {"cost_budget_usd": 1.0}},
        {"name": "iteration"},
    ])
    state = SimpleNamespace(
        cost_usd=2.0,  # cost_budget blocks first
        loop_iteration=0,
        config=SimpleNamespace(max_iterations=5, cost_budget_usd=1.0),
    )
    results = chain.invoke(HookPoint.LOOP_BOUNDARY, state)
    # 첫 block 에서 멈춤 → iteration 까지 안 감.
    assert results[-1].passed is False
    assert results[-1].guard_name == "cost_budget"


def test_register_guard_rejects_non_guard():
    with pytest.raises(TypeError):
        register_guard("bad", str)  # type: ignore[arg-type]


def test_register_custom_guard():
    class AlwaysBlock(Guard):
        @property
        def name(self):
            return "always_block_test"

        def check(self, state, context):
            return GuardResult(passed=False, guard_name=self.name, severity="block")

    register_guard("always_block_test", AlwaysBlock)
    assert "always_block_test" in available_guards()
