"""
Node Plugin Loader (v0.16.0).

용도:
  xgen-workflow 의 기존 노드(Input String, LLM, Retriever 등)를 **플러그인 매니페스트** 로
  규격화해 엔진에 자동 인식 + NOMNode 변환까지 한 번에. 레거시 노드 박제를 해소.

매니페스트 규약 (plugin.yaml 또는 dict):
  name: string
  version: semver
  nodes:
    - id: string            # 전역 식별자 (예: xgen.nodes.input_string)
      name: string
      description: string
      entry: "module:callable"
      inputs:
        - name, type, required, default, description
      outputs:
        - name, type, description
      tags: [list]

외부 기여자:
  - `xgen_harness.node_plugins` entry_points 그룹에 매니페스트 dict 를 반환하는 callable 등록
  - 또는 파일 경로를 `NodePluginLoader.load_file(path)` 로 직접 로드
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .nom import NOMGraph, NOMNode, NOMKind, NOMParam, NOMOutput

logger = logging.getLogger("harness.node_plugin")


_PLUGIN_REGISTRY: dict[str, "NodePluginManifest"] = {}
_ENTRY_POINTS_DISCOVERED = False


@dataclass
class NodePluginManifest:
    """노드 플러그인 매니페스트 — 여러 NOMNode 를 묶어 배포 단위로 관리."""
    name: str
    version: str = "0.0.0"
    description: str = ""
    nodes: list[NOMNode] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.nodes is None:
            self.nodes = []

    @classmethod
    def from_dict(cls, d: dict) -> "NodePluginManifest":
        raw_nodes = d.get("nodes") or []
        nodes: list[NOMNode] = []
        for rn in raw_nodes:
            if not isinstance(rn, dict):
                continue
            inputs = [
                NOMParam(
                    name=p.get("name", ""),
                    type=p.get("type", "string"),
                    description=p.get("description", ""),
                    required=bool(p.get("required", False)),
                    default=p.get("default"),
                    enum=p.get("enum"),
                ) for p in (rn.get("inputs") or []) if isinstance(p, dict)
            ]
            outputs = [
                NOMOutput(
                    name=o.get("name", ""),
                    type=o.get("type", "string"),
                    description=o.get("description", ""),
                ) for o in (rn.get("outputs") or []) if isinstance(o, dict)
            ]
            nodes.append(NOMNode(
                id=rn.get("id", ""),
                kind=NOMKind.NODE,
                name=rn.get("name", rn.get("id", "")),
                description=rn.get("description", ""),
                source_file=rn.get("source_file", ""),
                entry=rn.get("entry", ""),
                kind_meta=rn.get("kind_meta", {}) or {},
                inputs=inputs,
                outputs=outputs,
                tags=list(rn.get("tags") or []),
                version=rn.get("version", d.get("version", "0.0.0")),
                plugin_package=d.get("name", ""),
            ))
        return cls(
            name=d.get("name", "unnamed"),
            version=d.get("version", "0.0.0"),
            description=d.get("description", ""),
            nodes=nodes,
        )


def register_node_plugin(manifest: NodePluginManifest) -> None:
    """매니페스트 등록. 같은 이름이면 덮어씀."""
    if not manifest.name:
        raise ValueError("manifest.name required")
    _PLUGIN_REGISTRY[manifest.name] = manifest
    logger.info(
        "[node_plugin] registered %s (%d nodes, version=%s)",
        manifest.name, len(manifest.nodes), manifest.version,
    )


def list_node_plugins() -> list[NodePluginManifest]:
    _ensure_defaults_registered()
    return list(_PLUGIN_REGISTRY.values())


def list_all_nodes() -> list[NOMNode]:
    """등록된 모든 노드 매니페스트의 전체 NOMNode 평탄 목록."""
    out: list[NOMNode] = []
    for m in list_node_plugins():
        out.extend(m.nodes)
    return out


def load_manifest_file(path: str) -> Optional[NodePluginManifest]:
    """YAML 또는 JSON 매니페스트 파일을 읽어 등록.

    v0.16.1 — JSON 파일은 `compile.local_manifest.LocalManifest` 통일 스키마
    우선 시도 후 실패 시 NodePluginManifest 로 fallback. YAML 은 node_plugin 고유.
    두 경로 모두 최종 NodePluginManifest 로 수렴.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("[node_plugin] manifest not found: %s", path)
        return None
    text = p.read_text(encoding="utf-8")

    # 1) 통합 LocalManifest 우선 시도 (JSON). Tool Synthesis 와 스키마 공유.
    if path.lower().endswith(".json"):
        try:
            from ..compile.local_manifest import load_manifest as _load_local, SCHEMA_NAME
            local = _load_local(path)
            # schema 키 확인: LocalManifest 면 바로 변환
            import json as _json
            raw = _json.loads(text)
            if isinstance(raw, dict) and raw.get("schema") == SCHEMA_NAME:
                manifest = NodePluginManifest(
                    name=local.name, version=local.version,
                    description=local.description,
                    nodes=list(local.nodes),
                )
                register_node_plugin(manifest)
                return manifest
        except Exception as e:
            logger.debug("[node_plugin] local_manifest path failed: %s", e)

    # 2) YAML/JSON legacy — NodePluginManifest.from_dict
    data: Optional[dict] = None
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
    except Exception:
        try:
            import json
            data = json.loads(text)
        except Exception as e:
            logger.warning("[node_plugin] parse failed (%s): %s", path, e)
            return None
    if not isinstance(data, dict):
        return None
    manifest = NodePluginManifest.from_dict(data)
    register_node_plugin(manifest)
    return manifest


def _ensure_defaults_registered() -> None:
    """entry_points `xgen_harness.node_plugins` 자동 발견. idempotent."""
    global _ENTRY_POINTS_DISCOVERED
    if _ENTRY_POINTS_DISCOVERED:
        return
    _ENTRY_POINTS_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.node_plugins"
        if hasattr(eps, "select"):
            items = eps.select(group=group)
        else:
            items = eps.get(group, [])
        for ep in items:
            try:
                loaded = ep.load()
                result = loaded() if callable(loaded) else loaded
                if isinstance(result, NodePluginManifest):
                    register_node_plugin(result)
                elif isinstance(result, dict):
                    register_node_plugin(NodePluginManifest.from_dict(result))
            except Exception as e:
                logger.debug("[node_plugin] ep %s failed: %s", ep.name, e)
    except Exception:
        return
