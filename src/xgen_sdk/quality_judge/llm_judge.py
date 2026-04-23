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

SCORING_METHOD_MAX: Dict[str, int] = {"100": 100, "ox": 1, "5": 5}
SCORING_METHOD_LABEL: Dict[str, str] = {
    "100": "0~100점",
    "ox": "O/X (0 또는 1)",
    "5": "1~5점",
}

SUPPORTED_PROVIDERS = ("openai", "vllm", "sgl", "gemini", "anthropic", "heuristic")


@dataclass
class JudgeResult:
    """LLM 채점 결과."""
    suggested_raw_score: float
    reasoning: str
    confidence: float                 # 0.0 ~ 1.0
    judge_provider: str               # provider key 또는 "heuristic"
    actual_answer_excerpt: Optional[str] = None
    raw_response: Optional[str] = None


# ──────────────────────────────────────────
# Public entrypoint
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
    """
    expected vs actual 비교하여 채점 추천.

    provider 가 "heuristic" 이거나 base_url/api_key 가 비어 있으면 휴리스틱 사용.
    LLM 호출 실패 시에도 휴리스틱으로 fallback.
    """
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
        question=question,
        expected_answer=expected_answer,
        actual_answer=actual_answer,
        scoring_method=scoring_method,
    )


# ──────────────────────────────────────────
# Provider 라우팅
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

    # openai / vllm / sgl / gemini → OpenAI-호환
    return _call_openai_compatible(
        provider, model, base_url, api_key, prompt, scoring_method, max_score, actual_answer,
    )


# ──────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────

def _build_prompt(
    *,
    question: str,
    expected_answer: str,
    actual_answer: str,
    scoring_method: str,
    max_score: Optional[float],
    note: Optional[str],
) -> str:
    method_label = SCORING_METHOD_LABEL[scoring_method]
    method_max = SCORING_METHOD_MAX[scoring_method]
    bound = max_score if max_score is not None else method_max
    rule = {
        "ox": "정답이면 1, 오답이면 0 만 출력.",
        "5":  "1~5 사이의 정수 점수 (5=완전, 4=대부분, 3=절반, 2=일부, 1=거의 없음).",
        "100": f"0~{int(bound)} 사이의 정수 점수 (모범답을 100% 반영하면 {int(bound)}, 전혀 반영 못하면 0).",
    }[scoring_method]
    note_block = f"\n[추가 채점 기준]\n{note}\n" if note else ""

    return (
        "당신은 AI 응답 품질 평가관입니다. 아래 평가 문항에 대해 모범답과 실제 응답을 비교하여 채점하세요.\n\n"
        f"[평가 문항]\n{question}\n\n"
        f"[모범답 / Expected]\n{expected_answer}\n\n"
        f"[Agent 실제 응답 / Actual]\n{actual_answer}\n\n"
        f"[채점 방식]\n{method_label}\n{rule}\n"
        f"{note_block}\n"
        "응답은 다음 JSON 형식만 출력하세요. 다른 텍스트는 포함하지 마세요:\n"
        '{"score": <number>, "reasoning": "<채점 근거 1~3문장>", "confidence": <0.0~1.0>}'
    )


# ──────────────────────────────────────────
# OpenAI-호환 호출 (openai / vllm / sgl / gemini)
# ──────────────────────────────────────────

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
    # openai/gemini 은 response_format 을 신뢰할 수 있게 지원
    if provider in ("openai", "gemini"):
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    parsed = _safe_parse_json(content)
    if not parsed or "score" not in parsed:
        raise ValueError(f"{provider}: unexpected LLM response: {content[:200]}")

    raw = _clamp_score(float(parsed["score"]), scoring_method, max_score)
    return JudgeResult(
        suggested_raw_score=raw,
        reasoning=str(parsed.get("reasoning") or ""),
        confidence=_clamp_confidence(parsed.get("confidence")),
        judge_provider=provider,
        actual_answer_excerpt=_excerpt(actual_answer),
        raw_response=content,
    )


# ──────────────────────────────────────────
# Anthropic 호출 (/v1/messages)
# ──────────────────────────────────────────

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
        "max_tokens": 512,
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
    text = ""
    for b in blocks:
        if b.get("type") == "text":
            text += b.get("text", "")
    text = text.strip()

    parsed = _safe_parse_json(text)
    if not parsed or "score" not in parsed:
        raise ValueError(f"anthropic: unexpected response: {text[:200]}")

    raw = _clamp_score(float(parsed["score"]), scoring_method, max_score)
    return JudgeResult(
        suggested_raw_score=raw,
        reasoning=str(parsed.get("reasoning") or ""),
        confidence=_clamp_confidence(parsed.get("confidence")),
        judge_provider="anthropic",
        actual_answer_excerpt=_excerpt(actual_answer),
        raw_response=text,
    )


# ──────────────────────────────────────────
# 휴리스틱 fallback
# ──────────────────────────────────────────

def _judge_via_heuristic(
    *,
    question: str,
    expected_answer: str,
    actual_answer: str,
    scoring_method: str,
) -> JudgeResult:
    """
    매우 단순한 텍스트 유사도 기반 채점.

    - similarity = SequenceMatcher 비율 + 키워드 hit 보정
    - ox  : sim ≥ 0.5 → 1 / else 0
    - 5   : 1~5 (sim 비례)
    - 100 : 0~100 (sim 비례)
    """
    sim = _similarity(expected_answer, actual_answer)

    if scoring_method == "ox":
        raw = 1.0 if sim >= 0.5 else 0.0
    elif scoring_method == "5":
        raw = max(1.0, min(5.0, round(1 + sim * 4)))
    else:
        raw = round(sim * 100)

    return JudgeResult(
        suggested_raw_score=float(raw),
        reasoning=(
            f"휴리스틱(텍스트 유사도) 채점. similarity={sim:.2f}. "
            "LLM provider 미설정 또는 호출 실패로 fallback."
        ),
        confidence=round(min(0.5, sim), 2),
        judge_provider="heuristic",
        actual_answer_excerpt=_excerpt(actual_answer),
    )


def _similarity(a: str, b: str) -> float:
    a_n = _normalize(a)
    b_n = _normalize(b)
    if not a_n or not b_n:
        return 0.0
    base = SequenceMatcher(None, a_n, b_n).ratio()
    a_tokens = {t for t in re.split(r"\W+", a_n) if len(t) >= 3}
    b_tokens = {t for t in re.split(r"\W+", b_n) if len(t) >= 3}
    keyword_hit = (len(a_tokens & b_tokens) / len(a_tokens)) if a_tokens else 0.0
    return round(min(1.0, base * 0.5 + keyword_hit * 0.5), 4)


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


# ──────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────

def _safe_parse_json(content: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
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
    bound = max_score if max_score is not None else 100
    return float(max(0, min(int(bound), round(v))))


def _clamp_confidence(v: Any) -> float:
    try:
        c = float(v)
    except Exception:
        return 0.5
    return round(max(0.0, min(1.0, c)), 2)


def _excerpt(text: str, limit: int = 240) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…"
