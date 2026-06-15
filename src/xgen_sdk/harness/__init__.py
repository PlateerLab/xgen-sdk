"""xgen-sdk.harness

XGEN 하네스 엔진(`xgen-harness`) facade. 플랫폼(xgen-workflow 등)이 SDK 한 곳에서
하네스를 일관되게 쓰도록 감싼다. 엔진은 도메인 agnostic 별도 패키지이고, 이 모듈은
**래핑만** 한다(의존 방향 SDK → engine, 엔진 무수정). 엔진은 정규 의존성이라
`pip install xgen-sdk` 에 함께 설치된다(배터리 포함 — extra 없음).

Public API:
    Harness                                      # 실행 facade (add_step / delete_step / run)
    HarnessConfig, PipelineBuilder, Pipeline     # 엔진 핵심 타입 재노출
    Stage, ALL_STAGES, REQUIRED_STAGES           # 커스텀 스텝 작성용

Quick Start:
    from xgen_sdk.harness import Harness
    h = Harness(provider="anthropic", model="claude-sonnet-4-6", api_key=key)
    h.delete_step("s06_context").add_step("s_audit", MyAuditStage)
    state = await h.run("질문")
"""

from xgen_harness import (
    ALL_STAGES,
    REQUIRED_STAGES,
    HarnessConfig,
    Pipeline,
    PipelineBuilder,
    Stage,
)

from xgen_sdk.harness.facade import Harness

__all__ = [
    "Harness",
    "HarnessConfig",
    "PipelineBuilder",
    "Pipeline",
    "Stage",
    "ALL_STAGES",
    "REQUIRED_STAGES",
]
