"""빌트인 ResourceProvider 들 — RAG / Ontology.

DocumentService (state.metadata['services'].documents) 인터페이스 의존.
이식측이 ServiceProvider 박을 때 documents 가 list_collections() / ontology_query()
같은 메서드 가지면 자동 작동. 없으면 graceful skip.

다른 자원 종 (DB / Files / MCP) 은 이식측 또는 외부 wheel 이 자기 ResourceProvider
등록 — 빌트인 X (각자 service 인터페이스 다름).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("harness.resource_providers.builtin")


class RagCollectionMetaProvider:
    """RAG 컬렉션 메타 fetcher.

    state.config.rag_collections (또는 stage_params.s04_tool.rag_collections) 가 박혀있으면
    DocumentService.list_collections() 호출해 description / total_documents 자동 fetch.
    description 빈 칸이면 register_collection_enricher() 등록된 enricher 들 호출 (default OFF).
    """

    kind = "rag_collections"

    async def list_meta(self, state) -> dict[str, dict]:
        # 사용자가 박은 컬렉션 list — config 또는 stage_params 양쪽 확인
        cols: list[str] = []
        config = getattr(state, "config", None)
        if config:
            cols = list(getattr(config, "rag_collections", None) or [])
            if not cols and hasattr(config, "stage_params"):
                sp = config.stage_params or {}
                cols = list((sp.get("s04_tool") or {}).get("rag_collections") or [])
                if not cols:
                    cols = list((sp.get("s06_context") or {}).get("rag_collections") or [])
        if not cols:
            return {}

        services = state.metadata.get("services") if hasattr(state, "metadata") else None
        doc_service = getattr(services, "documents", None) if services else None
        if not doc_service or not hasattr(doc_service, "list_collections"):
            logger.debug("[rag_meta] DocumentService 또는 list_collections 미주입 — skip")
            return {}

        try:
            all_cols = await doc_service.list_collections() or []
        except Exception as e:
            logger.warning("[rag_meta] list_collections failed: %s", e)
            return {}

        meta_map: dict[str, dict] = {}
        for c in all_cols:
            if not isinstance(c, dict):
                continue
            cn = c.get("collection_name")
            if cn and cn in cols:
                desc = (c.get("description") or "").strip()
                total = c.get("total_documents") or c.get("document_count") or 0
                # v1.7.2 — collection_make_name (UUID 없는 사용자 친화 이름) 도 박음.
                # 사용자가 description 비워둔 경우 (5/9 worklog 시나리오) 에도 LLM 이 친화 이름
                # 보고 적합도 추측 가능. system_prompt 의 reference_resources 가 사용.
                make_name = (c.get("collection_make_name") or "").strip()
                meta_map[cn] = {
                    "description": desc,
                    "total_documents": int(total) if total else 0,
                    "make_name": make_name,
                }

        # description 빈 칸 컬렉션 자동 enrich (default OFF, register 한 enricher 만 발동)
        try:
            from ..tools.builtin import enrich_collection_description, _COLLECTION_ENRICHERS
            if _COLLECTION_ENRICHERS:
                for cn, m in meta_map.items():
                    if not m.get("description"):
                        try:
                            samples = await doc_service.search(
                                "", cn, limit=3, score_threshold=0.0,
                            ) or []
                            sample_texts = [
                                (s.get("chunk_text") or s.get("text") or "")[:500]
                                for s in samples if isinstance(s, dict)
                            ]
                            new_desc = await enrich_collection_description(cn, sample_texts)
                            if new_desc:
                                m["description"] = new_desc
                                m["_enriched"] = True
                        except Exception:
                            continue
        except Exception:
            pass

        return meta_map


class OntologyCollectionMetaProvider:
    """Ontology / GraphRAG 컬렉션 메타 fetcher.

    state.config.ontology_collections 박혀있으면 DocumentService 의 ontology 메서드
    (list_ontology_collections / ontology_query) 시도. 메타 (description / node_count
    / edge_count 등) 자동 fetch.
    """

    kind = "ontology_collections"

    async def list_meta(self, state) -> dict[str, dict]:
        cols: list[str] = []
        config = getattr(state, "config", None)
        if config:
            cols = list(getattr(config, "ontology_collections", None) or [])
            if not cols and hasattr(config, "stage_params"):
                sp = config.stage_params or {}
                cols = list((sp.get("s04_tool") or {}).get("ontology_collections") or [])
        if not cols:
            return {}

        services = state.metadata.get("services") if hasattr(state, "metadata") else None
        doc_service = getattr(services, "documents", None) if services else None
        if not doc_service:
            return {}

        # DocumentService 의 ontology 메타 메서드 — 환경마다 이름 다를 수 있어 여러 후보 시도
        meta_map: dict[str, dict] = {}
        for fn_name in ("list_ontology_collections", "ontology_meta", "list_collections"):
            if not hasattr(doc_service, fn_name):
                continue
            try:
                fn = getattr(doc_service, fn_name)
                rows = await fn() or []
                for c in rows:
                    if not isinstance(c, dict):
                        continue
                    cn = c.get("collection_name") or c.get("name")
                    if cn and cn in cols:
                        meta_map[cn] = {
                            "description": (c.get("description") or "").strip(),
                            "node_count": c.get("node_count") or c.get("nodes") or 0,
                            "edge_count": c.get("edge_count") or c.get("edges") or 0,
                        }
                if meta_map:
                    break
            except Exception as e:
                logger.debug("[ontology_meta] %s failed: %s", fn_name, e)
                continue

        # 메서드 없으면 이름만 박힘
        if not meta_map:
            for cn in cols:
                meta_map[cn] = {"description": ""}

        return meta_map
