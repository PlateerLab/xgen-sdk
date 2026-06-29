"""
S02 Memory — 대화 이력/기억 로드

- 이전 실행 결과를 state.previous_results에서 가져옴
- conversation_history가 있으면 messages에 추가
- embedding_search 전략: 임베딩 기반으로 관련 기억 검색
- 기억이 없으면 bypass
"""

import logging

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState

logger = logging.getLogger("harness.stage.memory")


class MemoryStage(Stage):

    @property
    def stage_id(self) -> str:
        return "s02_history"

    @property
    def order(self) -> int:
        return 2

    def should_bypass(self, state: PipelineState) -> bool:
        strategy_name = self.get_param("strategy", state, "default")
        # 'none' — 이력 무시 (독립 실행). 사용자가 매 turn 깨끗한 상태로 시작하고
        # 싶을 때 사용. 사유: chat 모드에서도 이전 대화 기억이 필요 없는 단발 질의가
        # 더 흔하다는 사용자 피드백.
        if strategy_name == "none":
            return True
        # embedding_search 전략은 conversation_history 없어도 실행 가능
        if strategy_name == "embedding_search":
            return not state.user_input
        return not state.previous_results and not state.conversation_history

    async def execute(self, state: PipelineState) -> dict:
        strategy_name = self.get_param("strategy", state, "default")

        if strategy_name == "none":
            # 이력/이전결과 미주입. messages 는 s01_input 이 만든 그대로 유지.
            # 보통 should_bypass=True 가 이 분기 도달 전에 차단하지만, 외부에서
            # 직접 execute() 를 호출하는 테스트/서브클래스 시나리오를 위한 안전 가드.
            return {"injected": 0, "previous_results": 0, "strategy": "none"}

        if strategy_name == "embedding_search":
            return await self._execute_embedding_search(state)

        return await self._execute_default(state)

    async def _execute_default(self, state: PipelineState) -> dict:
        """기본 전략: 대화 이력 + previous_results"""
        injected = 0
        # None(미설정)=제한 없음(전체 주입), 명시 0=주입 안 함, N>0=마지막 N.
        # (이전 버그: `or 0` → 미설정·명시0 둘 다 0 → `[-0:]` = 전체. 명시 0(없음 의도)이
        #  전체를 주입하던 것을 바로잡고, 미설정 기본=전체 동작은 그대로 유지.)
        _mh = self.get_param("max_history", state, None)
        max_history = None if _mh is None else int(_mh)

        # 1. 대화 이력이 있으면 messages에 추가 (max_history로 제한)
        if state.conversation_history:
            if max_history is None:
                history = list(state.conversation_history)
            elif max_history > 0:
                history = state.conversation_history[-max_history:]
            else:
                history = []
            for msg in history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if content:
                    state.messages.insert(len(state.messages) - 1, {"role": role, "content": content})
                    injected += 1

        # 2. 이전 실행 결과를 previous_results로 system prompt에 전달
        # (s03_prompt에서 처리 — 여기서는 state에 이미 있으므로 패스)
        prev_count = len(state.previous_results)

        logger.info("[Memory] injected=%d messages (max_history=%s), previous_results=%d", injected, max_history, prev_count)
        return {
            "injected": injected,
            "previous_results": prev_count,
            "strategy": "default",
        }

    async def _execute_embedding_search(self, state: PipelineState) -> dict:
        """HP2 — memory_collections(복수) 병렬 검색·score 병합. 미지정 시 단일 collection(하위호환)."""
        top_k = int(self.get_param("memory_top_k", state, None) or 0)
        score_threshold = float(self.get_param("memory_score_threshold", state, None) or 0)
        collections = self._resolve_memory_collections(state)

        # 기본 전략도 함께 실행 (대화 이력 주입)
        base_result = await self._execute_default(state)

        # v0.11.25 — ServiceProvider.documents 만 의존. 엔진은 xgen-documents API
        # 스키마 (/api/retrieval/documents/search) 를 모른다. ServiceProvider 가 없거나
        # documents 가 등록 안 돼 있으면 graceful skip.
        services = state.metadata.get("services")
        doc_service = getattr(services, "documents", None) if services else None
        if not (doc_service and hasattr(doc_service, "search")):
            logger.info("[Memory] DocumentService not injected — embedding_search skipped")
            base_result["strategy"] = "embedding_search"
            base_result["embedding_results"] = 0
            return base_result

        import asyncio

        async def _one(coll: str) -> list:
            try:
                rows = await self._search_via_service(
                    doc_service, coll, state.user_input, top_k, score_threshold
                )
                for r in rows:
                    if isinstance(r, dict):
                        r.setdefault("collection", coll)
                return rows
            except Exception as e:
                logger.warning("[Memory] embedding search 실패 (collection=%s): %s", coll, e)
                return []

        results = await asyncio.gather(*[_one(c) for c in collections])
        memories: list = [m for rows in results for m in rows]
        memories.sort(key=lambda m: (m.get("score", 0) if isinstance(m, dict) else 0), reverse=True)
        if top_k > 0:
            memories = memories[:top_k]

        # 검색 결과를 시스템 프롬프트에 추가
        if memories:
            memory_text = "\n\n<relevant_memories>\n"
            for i, mem in enumerate(memories):
                score = mem.get("score", 0)
                content = mem.get("chunk_text", mem.get("content", ""))
                memory_text += f"[{i + 1}] (relevance: {score:.2f}) {content}\n\n"
            memory_text += "</relevant_memories>"
            state.system_prompt = state.system_prompt + memory_text
            logger.info("[Memory] embedding_search: %d memories injected (collections=%s)", len(memories), collections)

        base_result["strategy"] = "embedding_search"
        base_result["embedding_results"] = len(memories)
        base_result["memory_collections"] = collections
        return base_result

    def _resolve_memory_collections(self, state: PipelineState) -> list[str]:
        """memory_collections(복수) 우선, 없으면 memory_collection(단수). {user_id}/{interaction_id} 치환,
        치환할 값 없으면 해당 컬렉션 제외."""
        raw = self.get_param("memory_collections", state, None)
        if isinstance(raw, str):
            raw = [c.strip() for c in raw.split(",") if c.strip()]
        if not raw:
            raw = [self.get_param("memory_collection", state, "memory")]

        subs = {
            "user_id": str(getattr(state, "user_id", "") or ""),
            "interaction_id": str(getattr(state, "interaction_id", "") or ""),
        }
        out: list[str] = []
        for coll in raw:
            if not coll:
                continue
            name = str(coll)
            skip = False
            for token, val in subs.items():
                if "{" + token + "}" in name:
                    if not val:
                        skip = True
                        break
                    name = name.replace("{" + token + "}", val)
            if skip or not name:
                continue
            if name not in out:
                out.append(name)
        return out or ["memory"]

    async def _search_via_service(self, doc_service, collection: str, query: str, top_k: int, threshold: float) -> list:
        """ServiceProvider.documents 를 통한 검색.

        v0.11.25 — HTTP 폴백 제거. 엔진은 URL / API 스키마를 모른다. DocumentService
        구현체가 자체 전송 계층을 책임진다.
        """
        results = await doc_service.search(
            collection_name=collection,
            query_text=query,
            limit=top_k,
            score_threshold=threshold,
        )
        return results if isinstance(results, list) else []

    def list_strategies(self) -> list[StrategyInfo]:  # noqa: D401
        return [
            StrategyInfo("default", "이전 실행 결과 + 대화 이력 로드 (멀티턴 기억 유지)", is_default=True),
            StrategyInfo("embedding_search", "임베딩 기반 관련 기억만 골라 주입 (긴 대화에서 비용 절감)"),
            StrategyInfo("none", "이력 무시 — 매 turn 독립 실행 (단발 질의용, 가장 빠름)"),
        ]
