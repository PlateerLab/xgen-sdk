"""공개 API 표면 회귀 테스트.

두 가지를 고정한다:
  1. v1.0.9 "30+ register_*/등록함수 top-level export" 약속 — 외부 plugin 이
     깊은 모듈 경로를 몰라도 `from xgen_sdk.harness import register_*` 한 줄로 합류.
  2. 이식측(xgen-workflow)이 실제로 쓰는 deep import 경로 — 이게 사라지면
     이식측이 ImportError 로 죽는다 (xgen-harness>=1.16,<2.0 계약).

엔진 하드닝(테스트/에러코드/메모리/provider) 중 공개 표면을 실수로 깨면 여기서 잡힌다.
"""

import importlib

import pytest


# ── 1. top-level export (16 entry_points 등록함수 + 핵심 타입) ──

TOP_LEVEL = [
    # 핵심 타입
    "HarnessConfig", "Pipeline", "PipelineState",
    "REQUIRED_STAGES", "ALL_STAGES",
    # 확장 등록 함수 (entry_points 16 그룹과 1:1)
    "register_stage", "register_provider", "register_orchestrator",
    "register_tool_source", "register_guard", "register_phase",
    "register_evaluation_criterion",
    # 도구/capability
    "ToolSource", "CapabilityMatcher",
    # compile / publish / 검증
    "compile_workflow_to_npm", "discover_galleries", "get_gallery",
    "NOMGraph", "MCPStdioVerifier", "SandboxLimits",
    # events
    "EventEmitter",
]


@pytest.mark.parametrize("name", TOP_LEVEL)
def test_top_level_export(name):
    x = importlib.import_module("xgen_sdk.harness")
    assert hasattr(x, name), f"xgen_sdk.harness.{name} 가 top-level 에서 사라짐 (외부 plugin/이식측 깨짐)"


def test_required_stages_nonempty():
    import xgen_sdk.harness as x
    assert x.REQUIRED_STAGES, "REQUIRED_STAGES 가 비면 안 됨"
    # 합병 후 핵심 required 스테이지 이름 유지 (s01/s08/s09).
    req = set(x.REQUIRED_STAGES)
    assert any("s08" in s for s in req)
    assert any("s09" in s for s in req)


# ── 2. 이식측이 의존하는 deep import 경로 (실제 grep 으로 확인된 것들) ──

PORT_IMPORTS = [
    ("xgen_sdk.harness.core.config", "HarnessConfig"),
    ("xgen_sdk.harness.core.pipeline", "Pipeline"),
    ("xgen_sdk.harness.core.state", "PipelineState"),
    ("xgen_sdk.harness.core.execution_context", "set_execution_context"),
    ("xgen_sdk.harness.core.execution_context", "get_api_key"),
    ("xgen_sdk.harness.core.strategy_resolver", "register_strategy"),
    ("xgen_sdk.harness.core.strategy_resolver", "StrategyResolver"),
    ("xgen_sdk.harness.core.services", "ServiceProvider"),
    ("xgen_sdk.harness.core.services", "NullServiceProvider"),
    ("xgen_sdk.harness.core.service_registry", "get_service_url"),
    ("xgen_sdk.harness.providers", "list_providers"),
    ("xgen_sdk.harness.providers", "get_default_model"),
    ("xgen_sdk.harness.providers", "get_default_provider"),
    ("xgen_sdk.harness.providers", "get_provider_models"),
    ("xgen_sdk.harness.providers", "PROVIDER_API_KEY_MAP"),
    ("xgen_sdk.harness.events.emitter", "EventEmitter"),
    ("xgen_sdk.harness.events.types", "DoneEvent"),
    ("xgen_sdk.harness.events.types", "ErrorEvent"),
    ("xgen_sdk.harness.stages.interfaces", "EvaluationStrategy"),
    ("xgen_sdk.harness.adapters.node_adapters", "NodeAdapter"),
    ("xgen_sdk.harness.adapters.node_adapters", "register_node_adapter"),
    ("xgen_sdk.harness.compile", "compile_and_pack"),
    ("xgen_sdk.harness.compile", "compile_workflow_to_npm"),
    ("xgen_sdk.harness.capabilities", "get_default_registry"),
]


@pytest.mark.parametrize("module,symbol", PORT_IMPORTS)
def test_port_import_path(module, symbol):
    mod = importlib.import_module(module)
    assert hasattr(mod, symbol), f"{module}.{symbol} 사라짐 — 이식측 import 깨짐"
