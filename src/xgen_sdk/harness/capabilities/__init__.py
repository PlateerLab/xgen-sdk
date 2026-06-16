"""
Capability System — 선언형 도구 자동 조립

사용자는 capability(기능)만 선언 → Registry가 노드/도구 매칭 → ParameterResolver가 파라미터 채움.

라이브러리는 빈 레지스트리로 출발. 외부(Adapter)가 워크플로우/노드 자산을 주입.
"""

from .schema import (
    CapabilitySpec,
    CapabilityMatch,
    ParamSpec,
    ProviderKind,
)
from .registry import (
    CapabilityRegistry,
    get_default_registry,
    set_default_registry,
)
from .matcher import CapabilityMatcher, MatchStrategy
from .materializer import (
    materialize_capabilities,
    merge_into_state,
    MaterializationReport,
)
from .parameter_resolver import ParameterResolver, ResolveResult

__all__ = [
    "CapabilitySpec",
    "CapabilityMatch",
    "ParamSpec",
    "ProviderKind",
    "CapabilityRegistry",
    "get_default_registry",
    "set_default_registry",
    "CapabilityMatcher",
    "MatchStrategy",
    "materialize_capabilities",
    "merge_into_state",
    "MaterializationReport",
    "ParameterResolver",
    "ResolveResult",
]
