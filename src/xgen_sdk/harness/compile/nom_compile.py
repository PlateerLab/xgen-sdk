"""NOM 그래프 전용 compile one-shot 진입점 (v0.21.0 Phase C, v0.29.0 npm 전환).

기존 ``compile_workflow_to_npm(harness_config, workflow_data, ...)`` 가 "워크플로우"
중심이라면, ``compile_nom_graph(graph, ...)`` 는 "NOM 그래프" 중심.

두 경로는 마지막 ``build_npm_package()`` 에서 수렴. ``NOMGraph.to_wheel_snapshot()``
이 이미 ``WorkflowSnapshot`` 을 만들어주므로 이 모듈은 얇은 wrapper.

Use cases
---------
1. Tool Synthesis 결과 (NOMNode 여러 개) 를 npm tarball 로 쌈 → npx 가능한 도구 모듈.
2. 외부 플러그인 노드 세트 (Stage / Strategy 묶음) 를 npm 으로 배포.
3. 임의 NOM 그래프를 MCP 서버로 발행.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from ..core.nom import NOMGraph
from .npm_pack import NpmPackResult, build_npm_package


def compile_nom_graph(
    graph: NOMGraph,
    *,
    gallery_name: str,
    gallery_version: str = "0.1.0",
    harness_config: Optional[Any] = None,
    description: str = "",
    out_dir: str | os.PathLike[str] = "./dist",
    extra_metadata: Optional[dict] = None,
) -> NpmPackResult:
    """NOM 그래프 → npm tarball (한 줄 API).

    Example
    -------
    >>> from xgen_harness import NOMGraph, NOMNode, NOMKind, NOMParam, compile_nom_graph
    >>> g = NOMGraph(nodes=[
    ...     NOMNode(id="x.tools.ping", kind=NOMKind.TOOL, entry="my_pkg:ping",
    ...             inputs=[NOMParam(name="host", required=True)]),
    ... ])
    >>> r = compile_nom_graph(g, gallery_name="nom_ping", gallery_version="0.1.0")
    >>> r.tarball_path.name
    'xgen-harness-nom_ping-0.1.0.tgz'
    """
    meta = {"description": description} if description else {}
    if extra_metadata:
        meta.update(extra_metadata)
    snapshot = graph.to_wheel_snapshot(
        gallery_name=gallery_name,
        gallery_version=gallery_version,
        harness_config=harness_config,
        extra_metadata=meta,
    )
    return build_npm_package(
        snapshot,
        out_dir=Path(out_dir),
    )
