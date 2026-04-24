"""xgen-sdk.quality_judge

LLM-as-judge 공용 라이브러리. xgen-core / xgen-workflow 에서 동일하게 import.

시그니처는 단순화되어 있으며, 각 호출 컨테이너가 자기 설정 로더에서 provider /
model / base_url / api_key 를 읽어 명시적으로 주입한다. 본 패키지는 config_composer
를 알지 못한다.

Public API:
    judge_question(...)
    JudgeResult
    SCORING_METHOD_MAX, SCORING_METHOD_LABEL, SUPPORTED_PROVIDERS
"""
from xgen_sdk.quality_judge.llm_judge import (
    CriterionScore,
    DEFAULT_CRITERION,
    JudgeResult,
    MultiJudgeResult,
    SCORING_METHOD_LABEL,
    SCORING_METHOD_MAX,
    SUPPORTED_PROVIDERS,
    judge_question,
    judge_with_criteria,
)

__all__ = [
    "CriterionScore",
    "DEFAULT_CRITERION",
    "JudgeResult",
    "MultiJudgeResult",
    "SCORING_METHOD_LABEL",
    "SCORING_METHOD_MAX",
    "SUPPORTED_PROVIDERS",
    "judge_question",
    "judge_with_criteria",
]
