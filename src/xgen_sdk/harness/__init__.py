"""
xgen-harness — XGEN 하네스 파이프라인 라이브러리

12-stage dual-abstraction agent pipeline.
xgen 생태계 전용 Pure Python 하네스 파이프라인.

Usage:
    from xgen_harness import Pipeline, PipelineState, HarnessConfig

    # provider 생략 → providers.get_default_provider() 가 런타임 해석
    # (XGEN_HARNESS_DEFAULT_PROVIDER env → openai → anthropic → 레지스트리 첫 항목)
    config = HarnessConfig()
    pipeline = Pipeline.from_config(config)
    state = PipelineState(user_input="Hello")
    result = await pipeline.run(state)
"""

from .core.pipeline import Pipeline
from .core.state import PipelineState, TokenUsage
from .core.config import HarnessConfig, ALL_STAGES, REQUIRED_STAGES
from .core.presets import PRESETS, Preset, get_preset, apply_preset, list_presets, register_preset
from .core.stage import Stage, StageDescription, StrategyInfo
from .core.stage_io import StageInput, StageOutput, STAGE_IO_SPECS, get_stage_io
from .core.registry import ArtifactRegistry, register_stage
# v1.0.9 — register_* 인프라 단일 진입 (외부 plugin 이 모듈 깊이 알 필요 없도록).
# 모든 plugin 그룹은 entry_points 와 1:1 매핑되며 import 시 발견 + register_* 로 즉시 호출도 가능.
from .core.runtime_defaults import (
    register_runtime_default,
    get_runtime_default,
    resolve_with_default,
    list_runtime_defaults,
)
from .core.phase_registry import register_phase
from .core.orchestrator_registry import register_orchestrator
from .core.service_registry import register_service, register_env_mapping
from .core.strategy_resolver import register_strategy
from .core.node_plugin import register_node_plugin
# v1.6 — Active policies registry + collection enricher registry
from .core.active_policies import register_active_policy_renderer
from .tools.builtin import register_collection_enricher
# v1.7 — ResourceProvider 자동 인식 패턴 (자원 종별 자기 메타 fetch)
from .core.resource_providers import register_resource_provider, ResourceProvider
# v1.7.1 — HarnessConfig top-level 필드 self-describing schema
from .core.top_level_field_schema import (
    register_top_level_field,
    get_top_level_field_schema,
)
from .events.emitter import EventEmitter
from .events.stdout_logger import enable_stdout_logging
from .events.types import (
    HarnessEvent,
    StageEnterEvent,
    StageExitEvent,
    MessageEvent,
    ToolCallEvent,
    ToolResultEvent,
    MetricsEvent,
    ErrorEvent,
    DoneEvent,
    MissingParamEvent,
    ServiceLookupEvent,
    CapabilityBindEvent,
    StageSubstepEvent,
    RetryEvent,
    PlanningEvent,
    # v0.24.0 — HITL 승인 이벤트. 이식측이 SSE 로 프론트에 중계.
    ApprovalRequiredEvent,
    ApprovalDecidedEvent,
)
from .core.catalog import get_catalog
from .core.planner import HarnessPlan
from .core.provider_bootstrap import ensure_provider
from .core.llm_call import aux_call
from .errors import (
    HarnessError,
    ConfigError,
    ProviderError,
    ToolError,
    PipelineAbortError,
    # v0.11.27 — 세분화된 예외도 top-level 노출. 외부 기여자가
    # `from xgen_harness import RateLimitError` 처럼 잡을 수 있도록.
    RateLimitError,
    OverloadError,
    ContextOverflowError,
    ToolTimeoutError,
    MCPConnectionError,
    ValidationError,
    ErrorCategory,
)
from .core.builder import PipelineBuilder
from .core.strategy_resolver import StrategyResolver
from .core.session import HarnessSession, SessionManager
# 장기실행(multi-session) 메모리 — progress artifact + 영속 SessionStore (entry_point 확장).
from .memory import (
    ProgressStatus,
    ProgressItem,
    ProgressLog,
    Priority,
    RecallItem,
    RecallSet,
    SessionStore,
    InMemorySessionStore,
    FileSessionStore,
    register_session_store,
    get_session_store,
    available_session_stores,
    save_session,
    load_session,
    attach_progress,
    read_progress,
)
from .orchestrator.dag import DAGOrchestrator, AgentNode, DAGEdge, DAGResult, DAGCycleError
from .orchestrator.multi_agent import MultiAgentExecutor
from .core.services import ServiceProvider, NullServiceProvider
from .tools.gallery import ToolPackageSpec, GalleryTool, load_tool_package, discover_gallery_tools
from .tools import ToolSource, register_tool_source, get_tool_sources
from .tools.frozen_source import FrozenToolSource
# v1.0.9 — term expansion (search_tools query 확장 메커니즘)
from .tools.term_expansion import (
    TermExpander,
    register_term_expander,
    register_search_alias,
)
from .providers import register_provider
from .adapters.node_adapters import register_node_adapter
from .orchestrator.multi_agent_planner import register_fan_out_strategy
from .stages.strategies._decide import register_decide_defaults
from .stages.strategies.token_tracker import register_model_pricing
from .tools.rag_tool import register_progressive_policy, get_progressive_policy
from .stages.strategies.discovery import (
    register_default_tool_strategy,
    get_default_tool_strategy,
)
from .tools.skill_registry import register_skill_body, list_skill_names, get_skill_body
from .stages.s03_prompt.stage import (
    register_identity,
    register_rules,
    register_thinking_mode,
)
from .stages.s04_tool.stage import register_capability_discovery_defaults
from .stages.s08_decide.strategies.judge_then_loop import (
    register_evaluation_criterion,
    register_evaluation_prompt_template,
    register_judge_defaults,
)
from .stages.s09_finalize.stage import register_output_formatter
from .stages.s09_finalize.strategies.persist import register_persist_defaults
# v0.17.0 — Policy Gate: ABC/인프라만 노출. 구체 Guard 클래스(TokenBudgetGuard,
# ToolPreconditionGuard 등)는 top-level 에 두지 않는다 — 외부 코드가 이름에
# 결합되지 않도록. 구체 Guard 는 entry_points 로만 발견·사용.
from .stages.strategies.guard import (
    Guard,
    GuardResult,
    GuardChain,
    HookPoint,
    HookContext,
    FieldSchema as GuardFieldSchema,
    available_guards,
    register_guard,
    describe_guards,
    build_guard_chain,
)
from .adapters.resource_registry import (
    register_xgen_node_resolver,
    get_xgen_node_resolver,
    XgenNodeResolver,
)
from .capabilities import (
    CapabilitySpec,
    CapabilityMatch,
    ParamSpec,
    ProviderKind,
    CapabilityRegistry,
    get_default_registry,
    set_default_registry,
    CapabilityMatcher,
    MatchStrategy,
    materialize_capabilities,
    merge_into_state,
    MaterializationReport,
    ParameterResolver,
    ResolveResult,
)
from .compile import (
    # v0.29 — npm 채널 단일
    compile_workflow_to_npm,
    compile_workflow_to_npm as compile,  # legacy alias — 외부 호출자 호환
    build_npm_package,
    NpmPackResult,
    build_spec,
    HarnessSpec,
    SPEC_VERSION,
    FrozenToolDefinition,
    freeze_http_tool,
    freeze_xgen_node_tool,
    freeze_mcp_session_tool,
    freeze_rag_tool,
    NPM_PACKAGE_PREFIX,
    ENGINE_PACKAGE,
    DEFAULT_ENGINE_DEP,
    # snapshot / external_inputs (재사용)
    WorkflowSnapshot,
    SNAPSHOT_VERSION,
    load_snapshot,
    ExternalInputSpec,
    InputType,
    scan_placeholders,
    merge_scanned,
    collect_runtime_values,
    MissingExternalInputError,
    # gallery discover (entry_points)
    InstalledGallery,
    discover_galleries,
    get_gallery,
)
# v0.20.0 — Sandbox Verifier (Phase B: publish-time gate)
from .core.sandbox import Sandbox, SandboxLimits, SandboxResult, run_sandboxed
from .core.sandbox_verifiers import (
    SandboxVerifier,
    VerifyResult,
    MCPStdioVerifier,
    register_sandbox_verifier,
    get_sandbox_verifier,
    list_sandbox_verifiers,
    bootstrap_default_sandbox_verifiers,
    verify_mcp_stdio,
)
# v0.21.0 — NOM IR (Phase C: 단일 IR 허브)
from .core.nom import (
    NOMKind,
    NOMParam,
    NOMOutput,
    NOMNode,
    NOMGraph,
    snapshot_current_registry_as_nom,
)
from .compile import compile_nom_graph

# v0.24.1 — pyproject.toml 과의 버전 drift 방지를 위해 런타임 조회로 전환.
# 이전 방식(하드코딩 "0.22.1") 은 pyproject bump 시마다 수동 갱신 필요해 0.24.0 시점에
# drift 발각(pip metadata=0.24.0 vs runtime=0.22.1). importlib.metadata 는 설치된
# wheel 의 METADATA 에서 정확한 값을 읽으므로 drift 불가.
try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        __version__ = _pkg_version("xgen-sdk")
    except PackageNotFoundError:
        __version__ = "0.0.0+uninstalled"
except Exception:
    __version__ = "0.0.0+unknown"

__all__ = [
    # Core
    "Pipeline",
    "PipelineState",
    "TokenUsage",
    "HarnessConfig",
    "ALL_STAGES",
    "REQUIRED_STAGES",
    # 장기실행 메모리 (multi-session)
    "ProgressStatus",
    "ProgressItem",
    "ProgressLog",
    "Priority",
    "RecallItem",
    "RecallSet",
    "SessionStore",
    "InMemorySessionStore",
    "FileSessionStore",
    "register_session_store",
    "get_session_store",
    "available_session_stores",
    "save_session",
    "load_session",
    "attach_progress",
    "read_progress",
    "PRESETS",
    "Preset",
    "get_preset",
    "apply_preset",
    "list_presets",
    "Stage",
    "StageDescription",
    "StrategyInfo",
    "StageInput",
    "StageOutput",
    "STAGE_IO_SPECS",
    "get_stage_io",
    "ArtifactRegistry",
    "register_stage",
    # Events
    "EventEmitter",
    "enable_stdout_logging",
    "HarnessEvent",
    "StageEnterEvent",
    "StageExitEvent",
    "MessageEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "MetricsEvent",
    "ErrorEvent",
    "DoneEvent",
    "MissingParamEvent",
    "ServiceLookupEvent",
    "CapabilityBindEvent",
    "StageSubstepEvent",
    "RetryEvent",
    # v0.12.0 — Harness Planner (REAL_HARNESS §4)
    "PlanningEvent",
    # v0.24.0 — HITL 승인 이벤트
    "ApprovalRequiredEvent",
    "ApprovalDecidedEvent",
    "get_catalog",
    "HarnessPlan",
    "ensure_provider",
    "aux_call",
    # Errors
    "HarnessError",
    "ConfigError",
    "ProviderError",
    "ToolError",
    "PipelineAbortError",
    # v0.11.27 — 세분화된 예외 top-level export
    "RateLimitError",
    "OverloadError",
    "ContextOverflowError",
    "ToolTimeoutError",
    "MCPConnectionError",
    "ValidationError",
    "ErrorCategory",
    # Builder & Session
    "PipelineBuilder",
    "HarnessSession",
    "SessionManager",
    # Orchestrator
    "DAGOrchestrator",
    "AgentNode",
    "DAGEdge",
    "DAGResult",
    "DAGCycleError",
    "MultiAgentExecutor",
    # Services (pluggable)
    "ServiceProvider",
    "NullServiceProvider",
    # Gallery Tools
    "ToolPackageSpec",
    "GalleryTool",
    "load_tool_package",
    "discover_gallery_tools",
    # Plugin API
    "ToolSource",
    "register_tool_source",
    "get_tool_sources",
    "FrozenToolSource",
    "register_stage",
    # 호스트 독립성 — xgen 노드 resolver 외부 주입 (v0.11.24)
    "register_xgen_node_resolver",
    "get_xgen_node_resolver",
    "XgenNodeResolver",
    # Capability System
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
    # Compiler (v0.29+ npm 단일 채널)
    "compile",
    "compile_workflow_to_npm",
    "build_npm_package",
    "NpmPackResult",
    "build_spec",
    "HarnessSpec",
    "SPEC_VERSION",
    "FrozenToolDefinition",
    "freeze_http_tool",
    "freeze_xgen_node_tool",
    "freeze_mcp_session_tool",
    "freeze_rag_tool",
    "NPM_PACKAGE_PREFIX",
    "ENGINE_PACKAGE",
    "DEFAULT_ENGINE_DEP",
    "WorkflowSnapshot",
    "SNAPSHOT_VERSION",
    "load_snapshot",
    "ExternalInputSpec",
    "InputType",
    "scan_placeholders",
    "merge_scanned",
    "collect_runtime_values",
    "MissingExternalInputError",
    "InstalledGallery",
    "discover_galleries",
    "get_gallery",
    # v0.20.0 — Sandbox Verifier
    "Sandbox",
    "SandboxLimits",
    "SandboxResult",
    "run_sandboxed",
    "SandboxVerifier",
    "VerifyResult",
    "MCPStdioVerifier",
    "register_sandbox_verifier",
    "get_sandbox_verifier",
    "list_sandbox_verifiers",
    "bootstrap_default_sandbox_verifiers",
    "verify_mcp_stdio",
    # v0.21.0 — NOM IR (Phase C)
    "NOMKind",
    "NOMParam",
    "NOMOutput",
    "NOMNode",
    "NOMGraph",
    "snapshot_current_registry_as_nom",
    "compile_nom_graph",
    # v1.0.9 — Plugin Registration API (entry_points 16 그룹과 1:1 매핑).
    # 외부 패키지가 import 시점에 자기 도메인 자원을 등록하는 단일 진입.
    # 깊은 모듈 경로(예: from xgen_harness.core.phase_registry import ...) 대신
    # `from xgen_harness import register_phase` 한 줄로 사용.
    "register_runtime_default",
    "get_runtime_default",
    "resolve_with_default",
    "list_runtime_defaults",
    "register_phase",
    "register_orchestrator",
    "register_service",
    "register_env_mapping",
    "register_strategy",
    "register_node_plugin",
    "register_provider",
    "register_node_adapter",
    "register_fan_out_strategy",
    "register_decide_defaults",
    "register_model_pricing",
    "register_preset",
    "register_capability_discovery_defaults",
    "register_output_formatter",
    "register_persist_defaults",
    "register_identity",
    "register_rules",
    "register_thinking_mode",
    "register_evaluation_criterion",
    "register_evaluation_prompt_template",
    "register_judge_defaults",
    "register_term_expander",
    "register_search_alias",
    "TermExpander",
    "register_guard",
    "available_guards",
    "describe_guards",
    "build_guard_chain",
    # v1.7 — ResourceProvider
    "register_resource_provider",
    "ResourceProvider",
    # v1.6 — active policies + collection enricher
    "register_active_policy_renderer",
    "register_collection_enricher",
    # v1.7.1 — top-level field schema
    "register_top_level_field",
    "get_top_level_field_schema",
    # v1.7.3 — RAG progressive PD policy (default OFF, auto-on > 50KB)
    "register_progressive_policy",
    "get_progressive_policy",
    # v1.8.0 — default tool exposure mode (deferred_default vs eager_all)
    "register_default_tool_strategy",
    "get_default_tool_strategy",
    # v1.8.0 — Claude Code Skills 패턴 (body lazy + session 고정)
    "register_skill_body",
    "list_skill_names",
    "get_skill_body",
]

from xgen_sdk.harness._sdk import Harness, XgenDBSessionStore, logging_emitter

__all__ += ["Harness", "XgenDBSessionStore", "logging_emitter"]
