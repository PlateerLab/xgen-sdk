"""ComplexityDetector — 자동 멀티에이전트 발화 트리거.

사용 의도가 단일 에이전트로 풀기엔 복잡한지 휴리스틱 + 사용자 룰로 판단.
판정 결과 = ComplexityVerdict.escalate ⇒ MultiAgentPlannerStage 가 sub-agent 분기.

문서 RAG 가 주 use case 라 RAG 컬렉션 수도 신호로 사용한다.
모든 임계값은 stage_params 로 override 가능 — 코드 하드코딩 없음.

기본 신호 (any → 가산점):
- 사용자 입력 길이가 N 이상
- 다중 인텐트 키워드 (and then / 비교 / 각각 / 또한 / step 1, step 2 ...)
- RAG 컬렉션 ≥ K 개 (멀티 도메인 검색)
- 선언된 capability 가 ≥ M 개
- 요구된 도구 다수 (e.g., 도구 카탈로그 ≥ T)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ComplexityVerdict:
    escalate: bool
    score: int
    signals: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


_MULTI_INTENT_PATTERNS = [
    r"and then", r"after that", r"also", r"compare", r"각각", r"또한",
    r"비교", r"step\s*\d+", r"first[, ]+then", r"먼저.*그.*다음",
]


class ComplexityDetector:
    """입력/설정에서 복잡도 신호를 모아 임계값 비교.

    가중치/임계값 모두 인자로 주입 — call site (보통 Stage) 에서 stage_params 로 받아 전달.
    """

    def __init__(
        self,
        long_input_chars: int = 280,
        rag_threshold: int = 2,
        capability_threshold: int = 2,
        tool_threshold: int = 8,
        score_to_escalate: int = 2,
        weights: dict[str, float] | None = None,
    ):
        self.long_input_chars = long_input_chars
        self.rag_threshold = rag_threshold
        self.capability_threshold = capability_threshold
        self.tool_threshold = tool_threshold
        self.score_to_escalate = score_to_escalate
        self.weights = weights or {
            "long_input": 1.0,
            "multi_intent": 1.5,
            "many_rag": 1.5,
            "many_capabilities": 1.0,
            "many_tools": 0.5,
        }

    def evaluate(
        self,
        user_input: str,
        rag_collections: list[str],
        capabilities: list[str],
        tool_count: int,
    ) -> ComplexityVerdict:
        score = 0.0
        signals: dict[str, float] = {}
        reasons: list[str] = []

        if user_input and len(user_input) >= self.long_input_chars:
            w = self.weights["long_input"]
            score += w
            signals["long_input"] = w
            reasons.append(f"입력 길이 {len(user_input)} ≥ {self.long_input_chars}")

        if user_input:
            hits = sum(
                1 for pat in _MULTI_INTENT_PATTERNS
                if re.search(pat, user_input, flags=re.IGNORECASE)
            )
            if hits:
                w = self.weights["multi_intent"]
                score += w
                signals["multi_intent"] = w
                reasons.append(f"다중 인텐트 키워드 {hits}건")

        if rag_collections and len(rag_collections) >= self.rag_threshold:
            w = self.weights["many_rag"]
            score += w
            signals["many_rag"] = w
            reasons.append(f"RAG 컬렉션 {len(rag_collections)} ≥ {self.rag_threshold}")

        if capabilities and len(capabilities) >= self.capability_threshold:
            w = self.weights["many_capabilities"]
            score += w
            signals["many_capabilities"] = w
            reasons.append(f"capability {len(capabilities)} ≥ {self.capability_threshold}")

        if tool_count >= self.tool_threshold:
            w = self.weights["many_tools"]
            score += w
            signals["many_tools"] = w
            reasons.append(f"도구 {tool_count} ≥ {self.tool_threshold}")

        return ComplexityVerdict(
            escalate=score >= self.score_to_escalate,
            score=int(score),
            signals=signals,
            reasons=reasons,
        )
