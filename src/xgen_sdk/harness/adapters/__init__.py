"""
Adapters — 외부 시스템과 하네스를 연결하는 어댑터 계층

각 어댑터는 외부 시스템의 언어를 하네스의 언어로 번역한다.
외부 시스템은 하네스 내부를 몰라도 되고, 하네스도 외부 시스템을 몰라도 된다.

v0.22.0 — XgenAdapter 는 이식측(xgen-workflow/harness_bridge)으로 이관됨.
엔진 코어는 ResourceRegistry 프로토콜·Protocol 기반 duck-typing 만 노출.

v1.10.0 — 외부 사용자가 자기 인프라에 wire 하는 어댑터:
    - QdrantDocService — DocService Protocol 의 Qdrant 직결 구현
    - LLM provider 는 providers/ 에 그대로 (OpenAI / Anthropic), create_provider 팩토리 re-export.
"""

from .resource_registry import ResourceRegistry, ResourceInfo
from .qdrant import QdrantDocService
from .embedders import (
    EmbedderFn,
    build_embedder,
    register_embedder,
    discover_external_embedders,
)

# Provider 는 providers/ 에 이미 정의됨 — adapters 표면에서도 편의 re-export
from ..providers import create_provider, register_provider

__all__ = [
    "ResourceRegistry",
    "ResourceInfo",
    "QdrantDocService",
    "EmbedderFn",
    "build_embedder",
    "register_embedder",
    "discover_external_embedders",
    "create_provider",
    "register_provider",
]
