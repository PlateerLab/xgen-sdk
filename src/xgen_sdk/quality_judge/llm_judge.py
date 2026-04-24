"""
LLM-as-judge: Agent 응답 자동 채점 (폐쇄망 호환).

호출 컨테이너(xgen-core, xgen-workflow 등)가 자기 설정에서 provider / model /
base_url / api_key 를 읽어 명시적으로 주입하면, 본 모듈이 provider 별 HTTP API 를
호출하여 채점한다. 호출 실패 / 키 미설정 / provider="heuristic" 인 경우에는
텍스트 유사도 기반 휴리스틱으로 graceful degradation.

지원 provider:
    - "openai"    : OpenAI-호환 /chat/completions
    - "vllm"      : OpenAI-호환 /chat/completions
    - "sgl"       : OpenAI-호환 /chat/completions
    - "gemini"    : OpenAI-호환 /chat/completions (generativelanguage.googleapis.com)
    - "anthropic" : /v1/messages (별도 포맷)
    - "heuristic" : LLM 호출 없음, 텍스트 유사도만 사용

재설계 (1 preset = 1 criterion):
    - 프리셋 자체가 곧 단일 평가 척도이며 가중치 개념 폐기.
    - judge_with_preset(...) 가 PresetJudgeResult 1건을 반환.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("xgen-sdk.quality-judge")

DEFAULT_TIMEOUT_SECONDS = 30.0

SCORING_METHOD_MAX: Dict[str, int] = {"100": 100, "ox": 1, "5": 5, "custom": 100}
SCORING_METHOD_LABEL: Dict[str, str] = {
    "100": "0~100점",
    "ox": "O/X (0 또는 1)",
    "5": "1~5점",
    "custom": "사용자 지정",
}

SUPPORTED_PROVIDERS = ("openai", "vllm", "sgl", "gemini", "anthropic", "heuristic")

# 프리셋 미선택 시 사용되는 기본 단일 척도.
DEFAULT_CRITERION: Dict[str, Any] = {
    "name": "정확도",
    "description": "모범답변과의 의미적 일치도",
    "scoring_method": "100",
    "scoring_description": None,
}


@dataclass
class JudgeResult:
    """LLM 채점 결과 (단발 척도)."""
    suggested_raw_score: float
    reasoning: str
    confidence: float                 # 0.0 ~ 1.0
    judge_provider: str               # provider key 또는 "heuristic"
    actual_answer_excerpt: Optional[str] = None
    raw_response: Optional[str] = None


# ──────────────────────────────────────────
# Public entrypoint (단순 채점 — 외부 호출자 직접 사용 X, 호환용)
# ──────────────────────────────────────────

def judge_question(
    *,
    question: str,
    expected_answer: str,
    actual_answer: str,
    scoring_method: str,
    max_score: Optional[float] = None,
    note: Optional[str] = None,
    provider: str = "heuristic",
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> JudgeResult:
    """expected vs actual 비교하여 채점 추천 (단순 인터페이스)."""
    if scoring_method not in SCORING_METHOD_MAX:
        raise ValueError(f"unknown scoring_method: {scoring_method}")

    p = (provider or "heuristic").lower()
    if p not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported LLM provider: {p}")

    if p != "heuristic" and base_url and api_key:
        try:
            return _judge_via_provider(
                provider=p,
                model=model or "",
                base_url=base_url,
                api_key=api_key,
                question=question,
                expected_answer=expected_answer,
                actual_answer=actual_answer,
                scoring_method=scoring_method,
                max_score=max_score,
                note=note,
            )
        except Exception as e:  # pragma: no cover - network failures
            logger.warning("LLM judge fallback to heuristic: %s", e)
    elif p != "heuristic":
        logger.info(
            "LLM judge: provider=%s 설정 미완(base_url/api_key 누락) → 휴리스틱 fallback",
            p,
        )

    return _judge_via_heuristic(
        expected_answer=expected_answer,
        actual_answer=actual_answer,
        scoring_method=scoring_method,
    )


# ──────────────────────────────────────────
# Provider 라우팅 (단발 채점)
# ──────────────────────────────────────────

def _judge_via_provider(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    question: str,
    expected_answer: str,
    actual_answer: str,
    scoring_method: str,
    max_score: Optional[float],
    note: Optional[str],
) -> JudgeResult:
    prompt = _build_prompt(
        question=question,
        expected_answer=expected_answer,
        actual_answer=actual_answer,
        scoring_method=scoring_method,
        max_score=max_score,
        note=note,
    )
    if provider == "anthropic":
        return _call_anthropic(model, base_url, api_key, prompt, scoring_method, max_score, actual_answer)
    return _call_openai_compatible(
        provider, model, base_url, api_key, prompt, scoring_method, max_score, actual_answer,
    )


def _build_prompt(
    *,
    question: str,
    expected_answer: str,
    actual_answer: str,
    scoring_method: str,
    max_score: Optional[float],
    note: Optional[str],
) -> str:
    method_label = SCORING_METHOD_LABEL.get(scoring_method, "0~100점")
    if scoring_method == "ox":
        rule = "raw_score = 0(오답) 또는 1(정답)"
    elif scoring_method == "5":
        rule = "raw_score = 1~5 정수 (5=완전, 4=대부분, 3=절반, 2=일부, 1=거의 없음)"
    else:
        rule = f"raw_score = 0~{int(max_score or 100)} 정수"
    note_block = f"\n[추가 채점 기준]\n{note}\n" if note else ""
    return (
        "당신은 AI 응답 품질 평가관입니다. 모범답과 실제 응답을 비교하여 점수를 매기세요.\n\n"
        f"[채점 방식]\n{method_label}\n{rule}\n\n"
        f"[평가 문항]\n{question}\n\n"
        f"[모범답 / Expected]\n{expected_answer}\n\n"
        f"[Agent 실제 응답 / Actual]\n{actual_answer}\n"
        f"{note_block}\n"
        "응답은 다음 JSON 형식만 출력하세요. 다른 텍스트는 포함하지 마세요:\n"
        '{"raw_score":<number>,"reasoning":"<1~3문장>","confidence":<0.0~1.0>}'
    )


def _call_openai_compatible(
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    prompt: str,
    scoring_method: str,
    max_score: Optional[float],
    actual_answer: str,
) -> JudgeResult:
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError(f"{provider}: base_url 미설정")
    url = f"{base}/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict but fair grader. Reply ONLY with valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
    }
    if provider in ("openai", "gemini"):
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    parsed = _safe_parse_json(content)
    if not parsed or "raw_score" not in parsed:
        raise ValueError(f"{provider}: unexpected LLM response: {content[:300]}")
    raw = _clamp_score(float(parsed.get("raw_score") or 0), scoring_method, max_score)
    return JudgeResult(
        suggested_raw_score=raw,
        reasoning=str(parsed.get("reasoning") or ""),
        confidence=_clamp_confidence(parsed.get("confidence")),
        judge_provider=provider,
        actual_answer_excerpt=_excerpt(actual_answer),
        raw_response=content,
    )


def _call_anthropic(
    model: str,
    base_url: str,
    api_key: str,
    prompt: str,
    scoring_method: str,
    max_score: Optional[float],
    actual_answer: str,
) -> JudgeResult:
    base = (base_url or "https://api.anthropic.com").rstrip("/")
    url = f"{base}/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 1024,
        "temperature": 0.0,
        "system": "You are a strict but fair grader. Reply ONLY with valid JSON.",
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key or "",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    parsed = _safe_parse_json(text)
    if not parsed or "raw_score" not in parsed:
        raise ValueError(f"anthropic: unexpected LLM response: {text[:300]}")
    raw = _clamp_score(float(parsed.get("raw_score") or 0), scoring_method, max_score)
    return JudgeResult(
        suggested_raw_score=raw,
        reasoning=str(parsed.get("reasoning") or ""),
        confidence=_clamp_confidence(parsed.get("confidence")),
        judge_provider="anthropic",
        actual_answer_excerpt=_excerpt(actual_answer),
        raw_response=text,
    )


def _judge_via_heuristic(
    *,
    expected_answer: str,
    actual_answer: str,
    scoring_method: str,
) -> JudgeResult:
    sim = _similarity(expected_answer, actual_answer)
    if scoring_method == "ox":
        raw = 1.0 if sim >= 0.5 else 0.0
    elif scoring_method == "5":
        raw = float(max(1, min(5, round(1 + sim * 4))))
    else:
        raw = float(round(sim * 100))
    return JudgeResult(
        suggested_raw_score=raw,
        reasoning=f"휴리스틱(텍스트 유사도) 채점. similarity={sim:.2f}.",
        confidence=round(min(0.5, sim), 2),
        judge_provider="heuristic",
        actual_answer_excerpt=_excerpt(actual_answer),
    )


# ──────────────────────────────────────────
# 공통 유틸
# ──────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    na = _normalize(a)
    nb = _normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _normalize(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _safe_parse_json(content: str) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _clamp_score(v: float, scoring_method: str, max_score: Optional[float]) -> float:
    if scoring_method == "ox":
        return 1.0 if v >= 0.5 else 0.0
    if scoring_method == "5":
        return float(max(1, min(5, round(v))))
    upper = int(max_score or 100)
    return float(max(0, min(upper, round(v))))


def _clamp_confidence(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def _excerpt(text: str, limit: int = 240) -> str:
    if not text:
        return ""
    t = text.strip()
    return t if len(t) <= limit else t[:limit] + "…"


# ──────────────────────────────────────────
# Preset 단일 척도 채점 (1 preset = 1 criterion)
# ──────────────────────────────────────────


@dataclass
class PresetJudgeResult:
    """프리셋(=단일 척도) 채점 결과."""
    criteria_name: str               # 프리셋 name (= 평가 기준)
    scoring_method: str              # '100' | 'ox' | '5' | 'custom'
    raw_score: float
    scored_point: float              # 0~100 환산값
    reasoning: str
    confidence: float
    judge_provider: str
    actual_answer_excerpt: Optional[str] = None
    raw_response: Optional[str] = None


_SCORING_METHOD_RULES: Dict[str, str] = {
    "100": "raw_score = 0~100 정수",
    "ox":  "raw_score = 0(오답) 또는 1(정답)",
    "5":   "raw_score = 1~5 정수 (5=완전, 4=대부분, 3=절반, 2=일부, 1=거의 없음)",
}


def _scored_point_single(raw: float, scoring_method: str) -> float:
    if scoring_method == "ox":
        return float(raw) * 100.0
    if scoring_method == "5":
        return round(float(raw) / 5.0 * 100.0, 2)
    return float(raw)  # '100' / 'custom'


def _clamp_raw_for_method(v: float, scoring_method: str) -> float:
    if scoring_method == "ox":
        return 1.0 if v >= 0.5 else 0.0
    if scoring_method == "5":
        return float(max(1, min(5, round(v))))
    # '100' / 'custom' — 0~100 정수
    return float(max(0, min(100, round(v))))


def _build_preset_prompt(
    *,
    question: str,
    expected_answer: str,
    actual_answer: str,
    preset_name: str,
    preset_description: str,
    scoring_method: str,
    scoring_description: Optional[str],
    note: Optional[str],
) -> str:
    method = (scoring_method or "100").lower()
    if method == "custom":
        rule_block = (
            "채점 방식: 사용자 지정\n"
            f"채점 규칙:\n{(scoring_description or '').strip() or '(미정의)'}\n"
            "raw_score 는 위 규칙에 따라 0~100 범위 정수로 환산하여 출력하세요."
        )
    else:
        base_rule = _SCORING_METHOD_RULES.get(method, _SCORING_METHOD_RULES["100"])
        rule_block = f"채점 방식: {base_rule}"
        if scoring_description and scoring_description.strip():
            rule_block += f"\n추가 채점 가이드:\n{scoring_description.strip()}"

    note_block = f"\n[추가 채점 기준]\n{note}\n" if note else ""

    return (
        "당신은 AI 응답 품질 평가관입니다. 아래 평가 문항에 대해 모범답과 실제 응답을 비교하여 "
        "주어진 단일 평가 척도로 점수를 매기세요.\n\n"
        f"[평가 기준 / Criteria]\n{preset_name}\n\n"
        f"[평가 방법 / Description]\n{(preset_description or '').strip() or '(설명 없음)'}\n\n"
        f"[채점 방식]\n{rule_block}\n\n"
        f"[평가 문항]\n{question}\n\n"
        f"[모범답 / Expected]\n{expected_answer}\n\n"
        f"[Agent 실제 응답 / Actual]\n{actual_answer}\n"
        f"{note_block}\n"
        "응답은 다음 JSON 형식만 출력하세요. 다른 텍스트는 포함하지 마세요:\n"
        '{"raw_score":<number>,"reasoning":"<1~3문장>","confidence":<0.0~1.0>}'
    )


def _preset_via_heuristic(
    *,
    expected_answer: str,
    actual_answer: str,
    preset_name: str,
    scoring_method: str,
) -> PresetJudgeResult:
    sim = _similarity(expected_answer, actual_answer)
    method = (scoring_method or "100").lower()
    if method == "ox":
        raw = 1.0 if sim >= 0.5 else 0.0
    elif method == "5":
        raw = float(max(1, min(5, round(1 + sim * 4))))
    else:
        raw = float(round(sim * 100))
    sp = _scored_point_single(raw, method)
    return PresetJudgeResult(
        criteria_name=preset_name,
        scoring_method=method,
        raw_score=raw,
        scored_point=sp,
        reasoning=f"휴리스틱(텍스트 유사도) 채점. similarity={sim:.2f}.",
        confidence=round(min(0.5, sim), 2),
        judge_provider="heuristic",
        actual_answer_excerpt=_excerpt(actual_answer),
    )


def _parse_preset_response(
    content: str,
    preset_name: str,
    scoring_method: str,
    provider: str,
    actual_answer: str,
) -> PresetJudgeResult:
    parsed = _safe_parse_json(content)
    if not parsed or "raw_score" not in parsed:
        raise ValueError(f"{provider}: unexpected LLM response: {content[:300]}")
    method = (scoring_method or "100").lower()
    try:
        raw = _clamp_raw_for_method(float(parsed.get("raw_score") or 0), method)
    except (TypeError, ValueError):
        raw = 0.0
    sp = _scored_point_single(raw, method)
    return PresetJudgeResult(
        criteria_name=preset_name,
        scoring_method=method,
        raw_score=raw,
        scored_point=sp,
        reasoning=str(parsed.get("reasoning") or ""),
        confidence=_clamp_confidence(parsed.get("confidence")),
        judge_provider=provider,
        actual_answer_excerpt=_excerpt(actual_answer),
        raw_response=content,
    )


def _preset_call_openai_compatible(
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    prompt: str,
    preset_name: str,
    scoring_method: str,
    actual_answer: str,
) -> PresetJudgeResult:
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError(f"{provider}: base_url 미설정")
    url = f"{base}/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict but fair grader. Reply ONLY with valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
    }
    if provider in ("openai", "gemini"):
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    return _parse_preset_response(content, preset_name, scoring_method, provider, actual_answer)


def _preset_call_anthropic(
    model: str,
    base_url: str,
    api_key: str,
    prompt: str,
    preset_name: str,
    scoring_method: str,
    actual_answer: str,
) -> PresetJudgeResult:
    base = (base_url or "https://api.anthropic.com").rstrip("/")
    url = f"{base}/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 1024,
        "temperature": 0.0,
        "system": "You are a strict but fair grader. Reply ONLY with valid JSON.",
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key or "",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    return _parse_preset_response(text, preset_name, scoring_method, "anthropic", actual_answer)


def judge_with_preset(
    *,
    question: str,
    expected_answer: str,
    actual_answer: str,
    preset: Optional[Dict[str, Any]] = None,
    provider: str = "heuristic",
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    note: Optional[str] = None,
) -> PresetJudgeResult:
    """
    1 프리셋 = 1 평가 척도 채점.

    `preset` dict 키 (모두 필수는 아님):
        - name                : 평가 기준 (LLM 프롬프트의 척도 이름)
        - description         : 평가 방법 (LLM 프롬프트에 그대로 삽입)
        - scoring_method      : '100' | 'ox' | '5' | 'custom'
        - scoring_description : 채점 방식 설명 (custom 일 때 규칙으로 사용,
                                나머지는 추가 채점 가이드로 활용)

    `preset` 미제공 시 `DEFAULT_CRITERION` 사용 (정확도 / 0~100점).
    """
    p_dict: Dict[str, Any] = dict(preset) if preset else dict(DEFAULT_CRITERION)
    preset_name = str(
        p_dict.get("name") or p_dict.get("criteria_name") or "정확도"
    ).strip() or "정확도"
    preset_description = str(p_dict.get("description") or "").strip()
    scoring_method = str(p_dict.get("scoring_method") or "100").lower()
    if scoring_method not in ("100", "ox", "5", "custom"):
        scoring_method = "100"
    scoring_description = p_dict.get("scoring_description")

    p = (provider or "heuristic").lower()
    if p not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported LLM provider: {p}")

    if p != "heuristic" and base_url and api_key:
        try:
            prompt = _build_preset_prompt(
                question=question,
                expected_answer=expected_answer,
                actual_answer=actual_answer,
                preset_name=preset_name,
                preset_description=preset_description,
                scoring_method=scoring_method,
                scoring_description=scoring_description,
                note=note,
            )
            if p == "anthropic":
                return _preset_call_anthropic(
                    model or "", base_url, api_key, prompt,
                    preset_name, scoring_method, actual_answer,
                )
            return _preset_call_openai_compatible(
                p, model or "", base_url, api_key, prompt,
                preset_name, scoring_method, actual_answer,
            )
        except Exception as e:  # pragma: no cover - network failures
            logger.warning("LLM preset-judge fallback to heuristic: %s", e)
    elif p != "heuristic":
        logger.info("LLM judge: provider=%s 설정 미완 → 휴리스틱 fallback", p)

    return _preset_via_heuristic(
        expected_answer=expected_answer,
        actual_answer=actual_answer,
        preset_name=preset_name,
        scoring_method=scoring_method,
    )
