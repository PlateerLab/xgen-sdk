"""xgen-sdk.quality_judge

LLM-as-judge 공용 라이브러리. xgen-core / xgen-workflow 에서 동일하게 import.

재설계 (1 preset = 1 criterion):
    judge_with_preset(...) -> PresetJudgeResult

Public API:
    judge_question(...)              # 호환 — 단발 채점
    judge_with_preset(...)           # 신규 — 프리셋 단일 척도 채점
    JudgeResult, PresetJudgeResult
    DEFAULT_CRITERION
    SCORING_METHOD_MAX, SCORING_METHOD_LABEL, SUPPORTED_PROVIDERS
"""
from xgen_sdk.quality_judge.llm_judge import (
    DEFAULT_CRITERION,
    JudgeResult,
    PresetJudgeResult,
    SCORING_METHOD_LABEL,
    SCORING_METHOD_MAX,
    SUPPORTED_PROVIDERS,
    judge_question,
    judge_with_preset,
)

__all__ = [
    "DEFAULT_CRITERION",
    "JudgeResult",
    "PresetJudgeResult",
    "SCORING_METHOD_LABEL",
    "SCORING_METHOD_MAX",
    "SUPPORTED_PROVIDERS",
    "judge_question",
    "judge_with_preset",
]
