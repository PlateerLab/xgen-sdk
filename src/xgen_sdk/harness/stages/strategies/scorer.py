"""QualityScorer 구현체들"""

from ..interfaces import QualityScorer


class WeightedScorer(QualityScorer):
    """가중평균 점수 계산 — 기본 가중치: 관련성 0.3 + 완전성 0.3 + 정확성 0.2 + 명확성 0.2"""

    DEFAULT_WEIGHTS = {
        "relevance": 0.3,
        "completeness": 0.3,
        "accuracy": 0.2,
        "clarity": 0.2,
    }

    def __init__(self, weights: dict[str, float] = None):
        self._weights = weights or self.DEFAULT_WEIGHTS

    @property
    def name(self) -> str:
        return "weighted"

    @property
    def description(self) -> str:
        return "가중평균 점수 (관련성 0.3 + 완전성 0.3 + 정확성 0.2 + 명확성 0.2)"

    def configure(self, config: dict) -> None:
        if "weights" in config:
            self._weights = config["weights"]

    def score(self, criteria: dict[str, float]) -> float:
        total = 0.0
        weight_sum = 0.0
        for key, weight in self._weights.items():
            if key in criteria:
                total += criteria[key] * weight
                weight_sum += weight
        return total / weight_sum if weight_sum > 0 else 0.5
