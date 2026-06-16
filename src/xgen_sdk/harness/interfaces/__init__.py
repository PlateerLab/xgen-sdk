"""
xgen_harness.interfaces — 외부 인프라 주입 Protocol

cluster 의 doc_service / provider 같은 인프라 의존을 Protocol 로 격상.
xgen-harness 코드에 cluster (xgen_workflow / xgen_sdk / etc.) import 0 보장.

사용자가 외부 환경에서 wire:
    from xgen_harness.interfaces import DocService, LLMProvider
    from xgen_harness.adapters import QdrantDocService, OpenAIProvider

    doc_service = QdrantDocService(url="http://localhost:6333")
    provider = OpenAIProvider(api_key="sk-...", model="gpt-4o")

    config = HarnessConfig(...)
    pipeline = Pipeline.from_config(config, doc_service=doc_service, provider=provider)
"""

from .doc_service import DocService, OntologyService

# LLMProvider 는 v0 부터 ABC 로 박혀있음 — 그대로 re-export
from ..providers.base import LLMProvider, ProviderEvent, ProviderEventType

__all__ = [
    "DocService",
    "OntologyService",
    "LLMProvider",
    "ProviderEvent",
    "ProviderEventType",
]
