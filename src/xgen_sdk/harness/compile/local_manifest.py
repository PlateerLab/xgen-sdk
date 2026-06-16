"""
Local Manifest — 갤러리/플러그인 공용 로컬 매니페스트 (v0.16.1).

이 모듈은 **단일 진실 소스**. 과거:
  - `core/node_plugin.py` 가 `NodePluginManifest` 별도 스키마로 분기
같은 도메인(NOMNode 묶음을 JSON 으로 교환) 이 여러 스키마로 갈라져 drift 위험 발생.
**feedback_no_hardcoding_extensibility 에 따라 즉시 통합**.

모든 로컬 매니페스트는 이 모듈의 `LocalManifest` 로만 생성·저장·로드·upsert.
node_plugin / gallery 모듈이 이걸 공통 호출.
(v1.0.5: tools/synthesis.py 제거됨 — 더 이상 그쪽 caller 는 없음.)

스키마:
  {
    "schema": "xgen_harness.local_manifest",
    "schema_version": 1,
    "name": "<plugin or gallery name>",
    "version": "<semver>",
    "description": "...",
    "nodes": [NOMNode.to_dict(), ...]
  }

외부 확장 포인트:
  - 다른 포맷으로 쓰고 싶으면 `LocalManifest.from_dict` / `to_dict` override
    대신 `register_manifest_codec(name, codec)` — entry_points 그룹 `xgen_harness.manifest_codecs` 자동 발견
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core.nom import NOMNode

logger = logging.getLogger("harness.compile.local_manifest")

# 단일 스키마 상수 — 이 모듈에서만 정의. 다른 파일은 참조만.
SCHEMA_NAME = "xgen_harness.local_manifest"
SCHEMA_VERSION = 1


@dataclass
class LocalManifest:
    """로컬 파일에 저장·교환하는 NOMNode 묶음 단일 포맷.

    용도:
      - Tool Synthesis: 검증 통과한 도구를 로컬 갤러리에 축적
      - NodePlugin: 레거시 노드를 플러그인 패키지로 떼어낸 매니페스트
      - Gallery: 공용 재사용 자산 번들
    셋 모두 동일 JSON 스키마 — drift 불가.
    """
    name: str = "unnamed"
    version: str = "0.0.0"
    description: str = ""
    nodes: list[NOMNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LocalManifest":
        return cls(
            name=d.get("name", "unnamed"),
            version=d.get("version", "0.0.0"),
            description=d.get("description", ""),
            nodes=[NOMNode.from_dict(n) for n in d.get("nodes", []) if isinstance(n, dict)],
        )

    def upsert(self, node: NOMNode) -> bool:
        """같은 id 노드가 있으면 교체, 없으면 추가. 반환: True=update / False=insert."""
        for i, existing in enumerate(self.nodes):
            if existing.id == node.id:
                self.nodes[i] = node
                return True
        self.nodes.append(node)
        return False

    def find(self, node_id: str) -> Optional[NOMNode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None


def load_manifest(path: str) -> LocalManifest:
    """JSON 파일에서 LocalManifest 로드. 없으면 빈 매니페스트 반환."""
    p = Path(path)
    if not p.exists():
        return LocalManifest(name=p.stem)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[local_manifest] parse failed (%s): %s", path, e)
        return LocalManifest(name=p.stem)
    if not isinstance(data, dict):
        return LocalManifest(name=p.stem)
    return LocalManifest.from_dict(data)


def save_manifest(manifest: LocalManifest, path: str) -> str:
    """JSON 으로 저장. 부모 디렉토리 없으면 생성. 반환: 절대 경로."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(p)


def upsert_node_in_file(node: NOMNode, path: str, *, manifest_name: str = "") -> str:
    """편의 함수 — 파일에서 load → upsert → save 한 번에.

    manifest_name 은 파일이 비어있을 때만 사용 (기존 값 보존).
    """
    manifest = load_manifest(path)
    if not manifest.nodes and manifest_name:
        manifest.name = manifest_name
    manifest.upsert(node)
    return save_manifest(manifest, path)
