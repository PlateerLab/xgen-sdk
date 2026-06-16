"""judge_then_loop 회귀 테스트 (v1.18.3 fix).

- hard 제약 실구현(hard 축 < threshold → overall 0). v1.17.0 가 약속만 하고 미구현이던 것.
- 정당한 overall:0.0 존중(이전엔 '누락'으로 보고 가중평균으로 부풀림).
- judge 파싱 실패 → bypass(None), fake-pass(0.7) 금지.
- _resolve_selected_criteria 가 hard 플래그 보존.
"""

from types import SimpleNamespace

from xgen_sdk.harness.stages.s08_decide.strategies.judge_then_loop import (
    _parse_evaluation,
    _resolve_selected_criteria,
)


def _gp(params):
    def get_param(name, state, default=None):
        return params.get(name, default)
    return get_param


def _state(threshold=0.7):
    return SimpleNamespace(config=SimpleNamespace(validation_threshold=threshold))


# ── hard 플래그 보존 ──

def test_resolve_preserves_hard_from_criteria_defs():
    sel = _resolve_selected_criteria(["규정"], [{"name": "규정", "weight": 0.5, "hard": True}])
    assert sel["규정"]["hard"] is True


def test_resolve_default_hard_false():
    sel = _resolve_selected_criteria([{"name": "x", "weight": 0.5}], None)
    assert sel["x"].get("hard") is False


# ── hard 제약 실구현 ──

def test_hard_criterion_fail_zeros_overall():
    params = {
        "criteria": ["규정"],
        "criteria_defs": [{"name": "규정", "description": "", "weight": 1.0, "hard": True}],
        "judge_threshold": 0.7,
    }
    # LLM 이 overall 0.9 라 줘도 hard 축(규정=0.3<0.7) 실패면 overall 강제 0 → retry.
    eval_text = '{"규정": 0.3, "overall": 0.9, "feedback": "ok"}'
    overall, feedback, verdict = _parse_evaluation(eval_text, ["규정"], _gp(params), _state(0.7))
    assert overall == 0.0
    assert verdict == "retry"
    assert "hard 제약 실패" in feedback


def test_hard_criterion_pass_keeps_overall():
    params = {
        "criteria": ["규정"],
        "criteria_defs": [{"name": "규정", "description": "", "weight": 1.0, "hard": True}],
        "judge_threshold": 0.7,
    }
    eval_text = '{"규정": 0.9, "overall": 0.9, "feedback": "good"}'
    overall, _, verdict = _parse_evaluation(eval_text, ["규정"], _gp(params), _state(0.7))
    assert overall == 0.9
    assert verdict == "pass"


# ── 정당한 overall:0.0 존중 ──

def test_legit_zero_overall_respected():
    params = {"criteria": ["relevance"], "judge_threshold": 0.7}
    # judge 가 진짜 0 점을 줬으면 ~0.5 로 부풀리지 말 것.
    eval_text = '{"relevance": 0.0, "overall": 0.0, "feedback": "terrible"}'
    overall, _, verdict = _parse_evaluation(eval_text, ["relevance"], _gp(params), _state(0.7))
    assert overall == 0.0
    assert verdict == "retry"


def test_missing_overall_recomputed_from_weighted():
    params = {"criteria": ["relevance"], "judge_threshold": 0.5}
    # overall 키 부재 → 가중평균(relevance=0.8) 재계산.
    eval_text = '{"relevance": 0.8, "feedback": "x"}'
    overall, _, _ = _parse_evaluation(eval_text, ["relevance"], _gp(params), _state(0.5))
    assert abs(overall - 0.8) < 1e-6


# ── fail-open 금지 → bypass ──

def test_parse_failure_bypasses_not_fakepass():
    params = {"criteria": ["relevance"], "judge_threshold": 0.7}
    overall, feedback, verdict = _parse_evaluation("not a json at all", ["relevance"], _gp(params), _state(0.7))
    assert overall is None          # bypass sentinel — fake-pass(0.7) 아님
    assert verdict == "bypass"
    assert "parse" in feedback.lower() or "실패" in feedback
