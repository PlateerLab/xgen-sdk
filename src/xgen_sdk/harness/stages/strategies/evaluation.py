"""EvaluationStrategy 구현체들"""

import json
import logging
from typing import Any, Optional

from ..interfaces import EvaluationStrategy, EvaluationResult

logger = logging.getLogger("harness.strategy.evaluation")

EVALUATION_PROMPT = """You are an AI response evaluator. Evaluate the assistant's response based on these criteria:

1. **Relevance** (0-1, weight 0.3): Does the response address the user's question?
2. **Completeness** (0-1, weight 0.3): Is the response thorough and complete?
3. **Accuracy** (0-1, weight 0.2): Is the information accurate and well-supported?
4. **Clarity** (0-1, weight 0.2): Is the response clear and well-organized?

User's question: {user_input}

Assistant's response: {assistant_response}

Respond with ONLY a JSON object (no markdown, no explanation):
{{"relevance": 0.0, "completeness": 0.0, "accuracy": 0.0, "clarity": 0.0, "overall": 0.0, "feedback": "brief feedback"}}"""


class LLMJudgeEvaluation(EvaluationStrategy):
    """독립 LLM 호출로 4가지 기준 평가 — 기본 전략."""

    def __init__(self, provider=None, threshold: float = 0.7):
        self._provider = provider
        self._threshold = threshold

    @property
    def name(self) -> str:
        return "llm_judge"

    @property
    def description(self) -> str:
        return "독립 LLM으로 4가지 기준 평가 (관련성/완전성/정확성/명확성)"

    def configure(self, config: dict) -> None:
        self._threshold = config.get("threshold", self._threshold)

    def set_provider(self, provider) -> None:
        self._provider = provider

    async def evaluate(
        self,
        user_input: str,
        assistant_response: str,
        context: Optional[dict] = None,
    ) -> EvaluationResult:
        if not self._provider:
            return EvaluationResult(passed=True, score=0.7, feedback="No provider for evaluation", verdict="pass")

        eval_prompt = EVALUATION_PROMPT.format(
            user_input=user_input[:500],
            assistant_response=assistant_response[:2000],
        )

        # v0.26.11 — context.state 로 _aux_call 통합 (max_tokens 단일 진실 소스).
        # state 가 있으면 token/cost 누적 + verbose substep emit. 없으면 폴백으로 직접 호출.
        state = (context or {}).get("state")
        if state is not None:
            try:
                from ...core.llm_call import aux_call
                eval_text = await aux_call(state, stage_id="evaluation.llm_judge", prompt=eval_prompt)
                return self._parse(eval_text)
            except Exception as e:
                logger.warning("[LLMJudge] Evaluation failed: %s", e)
                return EvaluationResult(passed=True, score=0.7, feedback=f"Evaluation failed: {e}", verdict="pass")

        # state 없는 standalone 사용 — provider.chat 폴백 (token tracking 없음 — best-effort).
        from ...providers.base import ProviderEventType
        eval_text = ""
        try:
            async for event in self._provider.chat(
                messages=[{"role": "user", "content": eval_prompt}],
                temperature=0.0,
                stream=False,
            ):
                if event.type == ProviderEventType.STOP:
                    eval_text = event.text
                elif event.type == ProviderEventType.TEXT_DELTA:
                    eval_text += event.text
        except Exception as e:
            logger.warning("[LLMJudge] Evaluation failed: %s", e)
            return EvaluationResult(passed=True, score=0.7, feedback=f"Evaluation failed: {e}", verdict="pass")

        return self._parse(eval_text)

    def _parse(self, eval_text: str) -> EvaluationResult:
        try:
            text = eval_text.strip()
            if "```" in text:
                start = text.find("{")
                end = text.rfind("}") + 1
                text = text[start:end]

            data = json.loads(text)
            overall = float(data.get("overall", 0.0))
            feedback = data.get("feedback", "")

            if overall == 0.0:
                r = float(data.get("relevance", 0.5))
                c = float(data.get("completeness", 0.5))
                a = float(data.get("accuracy", 0.5))
                cl = float(data.get("clarity", 0.5))
                overall = r * 0.3 + c * 0.3 + a * 0.2 + cl * 0.2

            criteria = {
                "relevance": float(data.get("relevance", 0)),
                "completeness": float(data.get("completeness", 0)),
                "accuracy": float(data.get("accuracy", 0)),
                "clarity": float(data.get("clarity", 0)),
            }
            verdict = "pass" if overall >= self._threshold else "retry"
            return EvaluationResult(
                passed=overall >= self._threshold,
                score=overall,
                feedback=feedback,
                verdict=verdict,
                criteria=criteria,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("[LLMJudge] Parse failed: %s", e)
            return EvaluationResult(passed=True, score=0.7, feedback="Evaluation parsing failed", verdict="pass")


class RuleBasedEvaluation(EvaluationStrategy):
    """규칙 기반 평가 — LLM 비용 없이 빠르게 품질 체크.

    규칙:
    - 최소 응답 길이 (기본 50자)
    - 사용자 키워드 포함 여부
    - 에러 메시지 감지
    """

    def __init__(self, min_length: int = 50, threshold: float = 0.7):
        self._min_length = min_length
        self._threshold = threshold

    @property
    def name(self) -> str:
        return "rule_based"

    @property
    def description(self) -> str:
        return "규칙 기반 평가 (길이, 키워드, 에러 감지)"

    async def evaluate(
        self,
        user_input: str,
        assistant_response: str,
        context: Optional[dict] = None,
    ) -> EvaluationResult:
        score = 1.0
        feedback_parts = []

        # 길이 체크
        if len(assistant_response) < self._min_length:
            score -= 0.3
            feedback_parts.append(f"응답이 짧음 ({len(assistant_response)}자 < {self._min_length}자)")

        # 에러 패턴 감지
        error_patterns = ["I cannot", "I'm sorry", "Error:", "I don't have access"]
        if any(p.lower() in assistant_response.lower() for p in error_patterns):
            score -= 0.2
            feedback_parts.append("에러/거부 패턴 감지됨")

        # 입력 키워드 반영 체크 (간단 heuristic)
        input_words = set(user_input.lower().split())
        response_words = set(assistant_response.lower().split())
        overlap = len(input_words & response_words) / max(len(input_words), 1)
        if overlap < 0.1:
            score -= 0.2
            feedback_parts.append("입력 키워드 반영 부족")

        score = max(0.0, min(1.0, score))
        verdict = "pass" if score >= self._threshold else "retry"
        return EvaluationResult(
            passed=score >= self._threshold,
            score=score,
            feedback="; ".join(feedback_parts) if feedback_parts else "규칙 기반 평가 통과",
            verdict=verdict,
        )


class NoValidation(EvaluationStrategy):
    """검증 비활성화 — 항상 통과."""

    @property
    def name(self) -> str:
        return "none"

    @property
    def description(self) -> str:
        return "검증 비활성화 (항상 통과)"

    async def evaluate(
        self,
        user_input: str,
        assistant_response: str,
        context: Optional[dict] = None,
    ) -> EvaluationResult:
        return EvaluationResult(passed=True, score=1.0, feedback="Validation disabled", verdict="pass")
