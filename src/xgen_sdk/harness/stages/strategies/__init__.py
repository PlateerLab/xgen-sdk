"""
Strategy 기본 구현체들.

각 인터페이스의 기본(default) 구현을 제공.
새 구현체 추가: 이 패키지에 파일 추가 → ArtifactRegistry에 등록.
"""

from .retry import ExponentialBackoffRetry, NoRetry
from .tool_router import CompositeToolRouter, MCPToolRouter, BuiltinToolRouter
from .tool_executor import SequentialToolExecutor, ParallelToolExecutor
from .evaluation import LLMJudgeEvaluation, RuleBasedEvaluation, NoValidation
from .scorer import WeightedScorer
from .discovery import ProgressiveDiscovery, EagerLoadDiscovery
from .compactor import (
    TokenBudgetCompactor, SlidingWindowCompactor,
    AdvancedContextCompactor,
    MicrocompactCompactor, ContextCollapseOverlayCompactor,
    AutocompactLLMCompactor, CascadeCompactor,
)
from ._decide import ThresholdDecide, AlwaysPassDecide

__all__ = [
    "ExponentialBackoffRetry", "NoRetry",
    "CompositeToolRouter", "MCPToolRouter", "BuiltinToolRouter",
    "SequentialToolExecutor", "ParallelToolExecutor",
    "LLMJudgeEvaluation", "RuleBasedEvaluation", "NoValidation",
    "WeightedScorer",
    "ProgressiveDiscovery", "EagerLoadDiscovery",
    "TokenBudgetCompactor", "SlidingWindowCompactor",
    "AdvancedContextCompactor",
    "MicrocompactCompactor", "ContextCollapseOverlayCompactor",
    "AutocompactLLMCompactor", "CascadeCompactor",
    "ThresholdDecide", "AlwaysPassDecide",
]
