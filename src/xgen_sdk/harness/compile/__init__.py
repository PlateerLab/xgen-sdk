"""
xgen_harness.compile — 하네스 워크플로우 컴파일러 (v0.29 npm 전용).

v0.29 부터 **npm package 가 단일 컴파일 산출물**. MCP 표준 생태계 (Claude
Desktop / Cursor / mcp-station server_type=node) 가 npm/npx 기반이므로 동일
채널 사용. v0.28 까지의 Python wheel 채널은 제거됨 (마이그레이션 완료).

기본 사용:
    from xgen_harness.compile import compile_workflow_to_npm

    artifact = compile_workflow_to_npm(
        harness_config=config,
        workflow_data=wf_data,
        gallery_name="my_agent",
        gallery_version="0.1.0",
        out_dir="./dist",
    )
    # → dist/xgen-harness-my_agent-0.1.0.tgz (npm tarball)
    #   안에는 spec.json + bin/cli.js + package.json
    #   외부에서 `npx -y xgen-harness-my_agent serve-mcp` 즉시 실행 가능

설계 문서:
  docs/harness/2026-04-30-npm-compile.md
"""

from .external_inputs import (
    ExternalInputSpec,
    InputType,
    scan_placeholders,
    merge_scanned,
    validate_external_inputs,
    collect_runtime_values,
    MissingExternalInputError,
)
from .snapshot import (
    WorkflowSnapshot,
    SNAPSHOT_VERSION,
    load_snapshot,
)
from .npm_spec import (
    build_spec,
    HarnessSpec,
    SPEC_VERSION,
    FrozenToolDefinition,
    freeze_http_tool,
    freeze_xgen_node_tool,
    freeze_mcp_session_tool,
    freeze_rag_tool,
    freeze_subpipeline_tool,
    freeze_canvas_tool,
)
from .npm_pack import (
    NpmPackResult,
    build_npm_package,
    compile_workflow_to_npm,
    NPM_PACKAGE_PREFIX,
    BIN_NAME_PREFIX,
    DEFAULT_ENGINE_DEP,
    ENGINE_PACKAGE,
)
from .nom_compile import compile_nom_graph
from .gallery import (
    InstalledGallery,
    discover_galleries,
    get_gallery,
    ENTRY_POINT_GROUP,
)
# v1.10.0 — Python 채널 (PyPI 패키지 빌더). npm 채널과 병행. 사용자가
# `/harness` Compile 모달에서 Python 토글 ON 시 cluster compile endpoint 가
# 아래 함수 호출 → wheel/sdist/tarball/source 산출 → PyPI publish 또는 다운로드.
from .python_compile import (
    transpile_to_python,
    write_package,
)
from .python_pack import (
    build_wheel,
    build_sdist,
    pack_tarball,
    compile_and_pack,
    BuildError,
)

__all__ = [
    # external_inputs
    "ExternalInputSpec",
    "InputType",
    "scan_placeholders",
    "merge_scanned",
    "validate_external_inputs",
    "collect_runtime_values",
    "MissingExternalInputError",
    # snapshot
    "WorkflowSnapshot",
    "SNAPSHOT_VERSION",
    "load_snapshot",
    # npm 채널 (v0.29+ 단일)
    "build_spec",
    "HarnessSpec",
    "SPEC_VERSION",
    "FrozenToolDefinition",
    "freeze_http_tool",
    "freeze_xgen_node_tool",
    "freeze_mcp_session_tool",
    "freeze_rag_tool",
    "freeze_subpipeline_tool",
    "freeze_canvas_tool",
    "NpmPackResult",
    "build_npm_package",
    "compile_workflow_to_npm",
    "NPM_PACKAGE_PREFIX",
    "BIN_NAME_PREFIX",
    "DEFAULT_ENGINE_DEP",
    "ENGINE_PACKAGE",
    # NOM (v0.21.0 Phase C → v0.29 npm 으로 전환)
    "compile_nom_graph",
    # gallery (entry_points 발견)
    "InstalledGallery",
    "discover_galleries",
    "get_gallery",
    "ENTRY_POINT_GROUP",
    # Python 채널 (v1.10.0+)
    "transpile_to_python",
    "write_package",
    "build_wheel",
    "build_sdist",
    "pack_tarball",
    "compile_and_pack",
    "BuildError",
]
