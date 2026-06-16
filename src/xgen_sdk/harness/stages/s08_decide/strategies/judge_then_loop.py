"""
JudgeThenLoopStrategy — s08_decide 의 'judge_then_loop' strategy (v1.0).

구 s08_judge stage 격하 흡수:
  1. 독립 LLM 호출(또는 EvaluationStrategy)로 응답 평가
  2. 평가 점수 + Guard 체인 + 도구 호출 상태로 루프 결정 (threshold 와 동일)

박제 풀기:
  - ALL_CRITERIA 모듈 노출 + register_evaluation_criterion() 공개 API
  - EVALUATION_PROMPT_TEMPLATE 외부 등록 가능 (register_evaluation_prompt_template)
  - entry_points("xgen_harness.evaluation_criteria") 자동 발견

Strategy 결과:
  - 점수 ≥ threshold 이고 도구 호출 없으면 LOOP_COMPLETE
  - 점수 < threshold → LOOP_RETRY
  - Policy Gate 가 block 했으면 그 결정 존중
  - max_retries 초과 → LOOP_COMPLETE (강제 종료)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("harness.stage.decide.judge_then_loop")


# ─── 평가 기준 레지스트리 (박제 0) ───────────────────────────────
ALL_CRITERIA: dict[str, dict] = {
    "relevance":    {"description": "Does the response address the user's question?",     "weight": 0.3},
    "completeness": {"description": "Is the response thorough and complete?",              "weight": 0.3},
    "accuracy":     {"description": "Is the information accurate and well-supported?",     "weight": 0.2},
    "clarity":      {"description": "Is the response clear and well-organized?",           "weight": 0.2},
}


def register_evaluation_criterion(name: str, description: str, weight: float = 0.1) -> None:
    """외부 작업자가 평가 기준 추가하는 공개 API. UI 멀티셀렉터에 자동 노출."""
    ALL_CRITERIA[name] = {"description": description, "weight": float(weight)}


def _resolve_selected_criteria(criteria: Any, criteria_defs: Any = None) -> dict[str, dict]:
    """평가축 이름/정의를 {name: {"description", "weight"}} 로 해소.

    Args:
        criteria: 선택된 평가축 — 이름 리스트 / 콤마 문자열 / inline dict 리스트.
        criteria_defs: **컴파일 산출물 self-contained 용** 정의 리스트
            ([{name, description, weight, hard}]). ALL_CRITERIA 레지스트리에 없는 축
            (예: criterion_1)의 정의를 여기서 읽는다.

    배경(v1.17.0): 평가축 정의는 본래 프로세스 전역 ALL_CRITERIA 에만 있고
    register_evaluation_criterion 으로 채워졌다. 클러스터는 같은 프로세스에서 등록하므로
    동작하지만, **컴파일된 npm/pypi 산출물은 별도 프로세스라 레지스트리가 비어** 사용자
    평가축(criterion_N)이 매칭 0 → generic 4축으로 폴백되며 사용자 QA 기준이 유실됐다.
    정의를 config(stage_params.s08_decide.criteria_defs)에 self-contained 로 실으면
    직렬화로 산출물에 박혀 외부 런타임에서도 레지스트리 없이 평가축이 유지된다.

    우선순위: criteria_defs(인라인) > ALL_CRITERIA(레지스트리). 둘 다 해소 못하면 폴백.
    """
    inline: dict[str, dict] = {}
    if isinstance(criteria_defs, (list, tuple)):
        for d in criteria_defs:
            if not isinstance(d, dict):
                continue
            nm = str(d.get("name") or "").strip()
            if not nm:
                continue
            try:
                w = float(d.get("weight", 0.1))
            except (TypeError, ValueError):
                w = 0.1
            inline[nm] = {
                "description": str(d.get("description") or ""),
                "weight": w,
                "hard": bool(d.get("hard", False)),
            }

    if isinstance(criteria, str):
        criteria = [c.strip() for c in criteria.split(",") if c.strip()]
    if not isinstance(criteria, (list, tuple)):
        criteria = []

    selected: dict[str, dict] = {}
    for item in criteria:
        if isinstance(item, dict):
            nm = str(item.get("name") or "").strip()
            if not nm:
                continue
            try:
                w = float(item.get("weight", 0.1))
            except (TypeError, ValueError):
                w = 0.1
            selected[nm] = {
                "description": str(item.get("description") or ""),
                "weight": w,
                "hard": bool(item.get("hard", False)),
            }
        else:
            nm = str(item).strip()
            if not nm:
                continue
            if nm in inline:
                selected[nm] = inline[nm]
            elif nm in ALL_CRITERIA:
                selected[nm] = ALL_CRITERIA[nm]
            # 미등록 + inline 정의 없음 → skip (아래 폴백이 처리)

    if not selected:
        selected = dict(inline) if inline else dict(ALL_CRITERIA)
    return selected


# ─── 평가 프롬프트 템플릿 (박제 0) ───────────────────────────────
EVALUATION_PROMPT_TEMPLATES: dict[str, str] = {
    "default": (
        "You are an AI response evaluator. Evaluate the assistant's response based on these criteria:\n\n"
        "{criteria_block}\n\n"
        "User's question: {user_input}\n\n"
        "Assistant's response: {assistant_response}\n\n"
        "Respond with ONLY a JSON object (no markdown, no explanation):\n"
        "{{{criteria_json_fields}, \"overall\": 0.0, \"feedback\": \"brief feedback\"}}"
    ),
}


def register_evaluation_prompt_template(name: str, template: str) -> None:
    """평가 프롬프트 템플릿 등록. stage_params.evaluation_prompt_template = name 으로 선택."""
    EVALUATION_PROMPT_TEMPLATES[name] = template


# 임계값 / 길이 cap 도 외부 조정 가능
JUDGE_DEFAULTS: dict[str, Any] = {
    "threshold": 0.7,
    "user_input_cap": 500,
    "response_cap": 2000,
}


def register_judge_defaults(**kwargs: Any) -> None:
    """judge 동작 기본값 override."""
    for k, v in kwargs.items():
        if k in JUDGE_DEFAULTS:
            JUDGE_DEFAULTS[k] = v


# ─── entry_points 자동 발견 ───────────────────────────────────────
_EVAL_CRITERIA_DISCOVERED = False


def _discover_evaluation_criteria_from_entry_points() -> None:
    """entry_points 그룹 ``xgen_harness.evaluation_criteria`` 자동 발견. idempotent."""
    global _EVAL_CRITERIA_DISCOVERED
    if _EVAL_CRITERIA_DISCOVERED:
        return
    _EVAL_CRITERIA_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.evaluation_criteria"
        items = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])  # type: ignore[arg-type]
        for ep in items:
            try:
                produced = ep.load()
                if callable(produced):
                    produced = produced()
                if isinstance(produced, dict):
                    name = produced.get("name", ep.name)
                    register_evaluation_criterion(
                        name,
                        produced.get("description", ""),
                        weight=float(produced.get("weight", 0.1)),
                    )
                elif isinstance(produced, list):
                    for item in produced:
                        if isinstance(item, dict) and item.get("name"):
                            register_evaluation_criterion(
                                item["name"],
                                item.get("description", ""),
                                weight=float(item.get("weight", 0.1)),
                            )
            except Exception as e:
                logger.warning("[evaluation_criteria] entry_point %s 로드 실패: %s", ep.name, e)
    except Exception as e:
        logger.debug("[evaluation_criteria] entry_points discovery 실패: %s", e)


_discover_evaluation_criteria_from_entry_points()


# ─── 평가 실행 ───────────────────────────────────────────────────
async def evaluate_response(state, get_param) -> dict:
    """LLM 또는 EvaluationStrategy 로 응답 평가 → 점수/feedback/verdict 반환.

    state.last_assistant_text 필수. 없으면 bypass dict 반환.
    """
    if not state.provider or not state.last_assistant_text:
        return {"bypassed": True, "reason": "no response to evaluate"}

    # EvaluationStrategy 가 등록돼 있으면 먼저 시도 (외부 평가 시스템 hook)
    eval_strategy_name = (get_param("evaluation_strategy", state, "llm_judge") or "").strip().lower()
    if eval_strategy_name == "none":
        return {"bypassed": True, "reason": "evaluation_strategy=none"}

    # 1) Strategy resolver 로 EvaluationStrategy 찾기 (있으면 사용)
    try:
        from ....stages.interfaces import EvaluationStrategy
        from ....core.strategy_resolver import resolve_strategy
        ev_strat = resolve_strategy("s08_decide", "evaluation", eval_strategy_name)
        if ev_strat is not None and isinstance(ev_strat, EvaluationStrategy):
            if hasattr(ev_strat, "set_provider"):
                try:
                    ev_strat.set_provider(state.provider)
                except Exception as e:
                    logger.debug("[judge] set_provider failed: %s", e)
            user_cap = int(get_param("user_input_cap", state, JUDGE_DEFAULTS["user_input_cap"]))
            resp_cap = int(get_param("response_cap", state, JUDGE_DEFAULTS["response_cap"]))
            result = await ev_strat.evaluate(
                user_input=(state.user_input or "")[:user_cap],
                assistant_response=state.last_assistant_text[:resp_cap],
                context={"provider": state.provider, "state": state},
            )
            state.validation_score = result.score
            state.validation_feedback = result.feedback
            return {"score": result.score, "feedback": result.feedback, "verdict": result.verdict}
    except ImportError:
        pass

    # 2) LLM judge 폴백 (raw aux_call)
    return await _llm_judge_fallback(state, get_param)


async def _llm_judge_fallback(state, get_param) -> dict:
    """raw LLM 호출로 평가. EvaluationStrategy 없을 때 폴백.

    v1.9.0 — config.judge_provider 가 본문과 다른 provider 면 **별도 인스턴스** 띄워
    호출. "Judge 가 자기 답을 자기 평가" self-promotion bias 회피.
    같은 provider 면 model 만 다르게 (v1.1.0 동작 보존). judge_use_main=True 면
    본문 그대로 (v1.7.1 정신 보존).
    """
    eval_prompt, selected_criteria = _build_evaluation_prompt(state, get_param)

    # v1.0.7 — judge LLM 의 system prompt 사용자 override 지원. 미설정(None) 이면
    # provider default 그대로. 박제 0 — 엔진은 시스템 메시지 안 주입, 외부에서만.
    eval_system = get_param("evaluation_system_prompt", state, None)
    eval_system_str: str | None = (
        str(eval_system).strip() if isinstance(eval_system, str) and eval_system.strip() else None
    )

    # v1.1.0 — judge_model lookup. 빈 값이면 본문 LLM 재사용 (backward compat).
    # v1.7.1 — judge_use_main=True 면 judge_model 박혀있어도 강제 본문 재사용
    # (사용자 UI chip "본문 재사용" 명시 의도 우선).
    config = getattr(state, "config", None)
    if bool(getattr(config, "judge_use_main", False)):
        judge_model_name = ""
    else:
        judge_model_name = (str(getattr(config, "judge_model", "") or "")).strip()

    # v1.9.0 P0#3 — judge_provider 별도 인스턴스 해석.
    # judge_use_main 이거나 judge_provider 비어있거나 본문과 같은 provider 면 본문 그대로
    # (resolve_judge_provider 내부에서 분기).
    try:
        from ....core.provider_bootstrap import resolve_judge_provider
        judge_provider_inst = await resolve_judge_provider(state, stage_id="s08_decide")
    except Exception as e:
        logger.warning("[judge] resolve_judge_provider 실패 — 본문 폴백: %s", e)
        judge_provider_inst = None

    try:
        from ....core.llm_call import aux_call
        eval_text = await aux_call(
            state, stage_id="s08_decide", prompt=eval_prompt,
            system=eval_system_str,
            model=judge_model_name or None,
            provider=judge_provider_inst,
        )
    except Exception as e:
        logger.warning("[judge] aux_call 실패: %s", e)
        return {"bypassed": True, "reason": f"evaluation failed: {e}"}

    score, feedback, verdict = _parse_evaluation(eval_text, selected_criteria, get_param, state)
    if score is None:
        # judge 파싱 실패 → gate bypass. validation_score 를 세팅하지 않아 ThresholdDecide 가
        # judge 점수 없이 정상 종료(fake-pass·무한retry 둘 다 회피).
        return {"bypassed": True, "reason": feedback, "verdict": verdict}
    state.validation_score = score
    state.validation_feedback = feedback
    return {"score": score, "feedback": feedback, "verdict": verdict}


def _build_evaluation_prompt(state, get_param) -> tuple[str, list[str]]:
    user_cap = int(get_param("user_input_cap", state, JUDGE_DEFAULTS["user_input_cap"]))
    resp_cap = int(get_param("response_cap", state, JUDGE_DEFAULTS["response_cap"]))

    # v1.17.0 — criteria_defs(config self-contained 정의)로 외부 산출물에서도 평가축 유지.
    criteria = get_param("criteria", state, list(ALL_CRITERIA.keys()))
    criteria_defs = get_param("criteria_defs", state, None)
    selected = _resolve_selected_criteria(criteria, criteria_defs)

    total_weight = sum(c["weight"] for c in selected.values()) or 1.0
    criteria_block = "\n".join(
        f"{i+1}. **{name.capitalize()}** (0-1, weight {info['weight']/total_weight:.2f}"
        f"{', MUST-PASS' if info.get('hard') else ''}): {info['description']}"
        for i, (name, info) in enumerate(selected.items())
    )
    criteria_json_fields = ", ".join(f'"{name}": 0.0' for name in selected)

    template_name = get_param("evaluation_prompt_template", state, "default")
    template = EVALUATION_PROMPT_TEMPLATES.get(template_name, EVALUATION_PROMPT_TEMPLATES["default"])

    prompt = template.format(
        criteria_block=criteria_block,
        user_input=(state.user_input or "")[:user_cap],
        assistant_response=state.last_assistant_text[:resp_cap],
        criteria_json_fields=criteria_json_fields,
    )
    return prompt, list(selected.keys())


def _parse_evaluation(eval_text: str, selected_criteria: list[str],
                      get_param, state) -> tuple[Optional[float], str, str]:
    """judge 응답 파싱 → (overall, feedback, verdict).

    overall is None → **bypass**(파싱 실패): fake-pass 도 무한retry 도 하지 않고 gate 를
    건너뛴다. 호출자가 validation_score 를 세팅하지 않아 ThresholdDecide 가 정상 종료.

    hard 제약(v1.18.3 실구현): hard=true 축의 점수가 threshold 미만이면 가중평균과 무관하게
    overall 즉시 0 → 무조건 retry. 절대규칙용. (v1.17.0 가 약속만 하고 미구현이던 것)
    """
    threshold = float(get_param(
        "judge_threshold", state,
        get_param("threshold", state,
                  state.config.validation_threshold if state.config else JUDGE_DEFAULTS["threshold"])
    ))
    # 평가축(hard 플래그 포함)을 한 번만 해소 — 가중평균·hard 검사 양쪽에서 쓴다.
    selected = _resolve_selected_criteria(
        get_param("criteria", state, selected_criteria),
        get_param("criteria_defs", state, None),
    )
    try:
        text = eval_text.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            text = text[start:end]
        data = json.loads(text)
        feedback = data.get("feedback", "")

        # overall: 명시값이 있으면 존중(0.0 도 정당한 값) — 키 부재일 때만 가중평균 재계산.
        # (이전 버그: `overall == 0.0` 을 '누락'으로 보고 0 점을 ~0.5 로 부풀렸다.)
        if "overall" in data and data.get("overall") is not None:
            overall = float(data["overall"])
        else:
            total_weight = sum(c["weight"] for c in selected.values()) or 1.0
            overall = sum(
                float(data.get(name, 0.5)) * (info["weight"] / total_weight)
                for name, info in selected.items()
            )

        # hard 제약 — hard 축이 threshold 미만이면 overall 강제 0 (절대규칙).
        hard_failed = [
            name for name, info in selected.items()
            if info.get("hard") and float(data.get(name, 0.0)) < threshold
        ]
        if hard_failed:
            overall = 0.0
            feedback = (str(feedback) + f" [hard 제약 실패: {', '.join(hard_failed)}]").strip()

        verdict = "pass" if overall >= threshold else "retry"
        return overall, feedback, verdict
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        # fail-open 금지 — 가짜 pass 로 gate 를 무력화하지 않는다. bypass(루프도 회피).
        logger.warning("[judge] parse 실패 — gate bypass(fake-pass/loop 회피): %s", e)
        return None, f"judge parse 실패: {e}", "bypass"
