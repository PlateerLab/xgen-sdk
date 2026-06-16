"""
ServiceProvider — 플러거블 서비스 프로토콜

xgen 서비스(DB, MCP, Documents, Config)를 추상화하여
하네스가 독립 실행(서비스 없음)과 xgen 연동(서비스 있음) 모두 지원.

설계 원칙:
  - 각 서비스는 Optional: 없으면 graceful fallback
  - ServiceProvider가 서비스 묶음을 관리
  - Stage는 state.services.xxx로 접근
  - xgen 환경이 아니면 NullServiceProvider 사용 (모든 서비스 None)
"""

from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, runtime_checkable


# ──────────────────────────────────────────────
# 1. DatabaseService — DB CRUD
# ──────────────────────────────────────────────

@runtime_checkable
class DatabaseService(Protocol):
    """DB 저장/조회 프로토콜.

    xgen-workflow의 DatabaseClient와 호환.
    s11_save, session, artifact에서 사용.
    """

    async def insert_record(self, table: str, record: dict[str, Any]) -> Optional[int]:
        """레코드 삽입. 성공 시 ID 반환."""
        ...

    async def find_records(
        self, table: str, conditions: dict[str, Any], limit: int = 10
    ) -> list[dict[str, Any]]:
        """조건부 레코드 조회."""
        ...

    async def upsert_record(
        self, table: str, match: dict[str, Any], record: dict[str, Any]
    ) -> bool:
        """INSERT or UPDATE."""
        ...

    async def get_schema_summary(
        self, connection_name: str, max_tables: int = 20
    ) -> str:
        """다중 DB 연결의 스키마 요약을 사람이 읽을 수 있는 한 줄 텍스트로 반환.

        s06_context 가 시스템 프롬프트에 컨텍스트로 주입할 때 사용.
        라이브러리는 connection_name 같은 추상 식별자만 다루고,
        실제 SQL/엔진별 해석(information_schema, SHOW TABLES 등)은 구현체 책임.

        실패/미지원 시 빈 문자열 반환 — 호출자는 graceful skip.
        """
        ...


# ──────────────────────────────────────────────
# 2. ConfigService — 설정/API 키 조회
# ──────────────────────────────────────────────

@runtime_checkable
class ConfigService(Protocol):
    """설정 값 조회 프로토콜.

    xgen-core의 persistent_configs 테이블 → Redis → 환경변수.
    s01_input에서 API 키 해석에 사용.
    """

    async def get_value(self, key: str, default: str = "") -> str:
        """설정 값 조회. 없으면 default."""
        ...

    async def get_api_key(self, provider: str) -> Optional[str]:
        """프로바이더별 API 키 조회.
        환경변수 → config store → 폴백 순서.
        """
        ...

    async def get_setting(
        self, key: str, *, default: Optional[str] = None
    ) -> Optional[str]:
        """일반 설정 조회 — Redis(xgen-core) → .env → default 순서 강제.

        정책: 관리자 UI 에서 런타임 변경한 Redis 값이 부팅 고정 .env 보다 우선.
        구현체는 ServiceLookupEvent 를 발행해 어느 source 에서 히트했는지 가시화해야 함.
        """
        ...


# ──────────────────────────────────────────────
# 3. MCPService — MCP 도구 디스커버리/실행
# ──────────────────────────────────────────────

@runtime_checkable
class MCPService(Protocol):
    """MCP 도구 관리 프로토콜.

    xgen-mcp-station HTTP API 래핑.
    s01_input(디스커버리), s08_act(실행)에서 사용.
    """

    async def list_sessions(self) -> list[dict[str, Any]]:
        """활성 MCP 세션 목록."""
        ...

    async def list_tools(self, session_id: str) -> list[dict[str, Any]]:
        """세션의 도구 목록. 각 항목: {name, description, inputSchema}"""
        ...

    async def call_tool(
        self, session_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        """도구 호출. 결과 텍스트 반환."""
        ...


# ──────────────────────────────────────────────
# 4. DocumentService — RAG/문서 검색
# ──────────────────────────────────────────────

@runtime_checkable
class DocumentService(Protocol):
    """문서 검색 프로토콜.

    xgen-documents의 Hybrid RAG, Ontology GraphRAG.
    s03_prompt(RAG 프롬프트), s06_context(컨텍스트 보강)에서 사용.
    """

    async def search(
        self,
        query: str,
        collection_id: str,
        limit: int = 5,
        user_id: str = "",
        score_threshold: float = 0.0,
        filter: dict[str, Any] | None = None,
        rerank: bool = False,
        rerank_top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """문서 검색. 각 항목: {content, source, score, ...}

        score_threshold: 결과에 포함시킬 최소 유사도. 0.0 이면 필터링 없음.
        filter: 메타데이터 필터. 예: {"file_name": "products.csv"}. None 이면 비적용.
        rerank: True 면 서버에서 재순위 수행. (구현체가 지원하는 경우)
        rerank_top_k: rerank=True 일 때 유지할 상위 k 개. None 이면 서버 기본값.
        """
        ...

    async def list_collections(self, user_id: str = "") -> list[dict[str, Any]]:
        """사용 가능한 컬렉션 목록."""
        ...

    async def embed_query(self, text: str, user_id: str = "") -> list[float]:
        """쿼리 임베딩 벡터. 없으면 빈 리스트."""
        ...

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 5,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        """문서 리랭크. 각 항목: {index, score, content}"""
        ...

    async def list_folders(self, user_id: str = "") -> list[dict[str, Any]]:
        """폴더 목록."""
        ...

    async def ontology_query(
        self,
        collection_id: str,
        query: str,
        user_id: str = "",
    ) -> dict[str, Any]:
        """GraphRAG / Ontology 질의. {nodes, relations, ...}"""
        ...


# ──────────────────────────────────────────────
# 5. ServiceProvider — 서비스 묶음
# ──────────────────────────────────────────────

class ServiceProvider:
    """플러거블 서비스 컨테이너.

    각 서비스는 Optional — None이면 해당 기능은 graceful skip.
    Stage에서 접근: state.services.database, state.services.mcp 등.

    사용 예:
        # xgen 환경
        provider = XgenServiceProvider(db_manager, config_client)
        pipeline.run(state, services=provider)

        # 독립 실행 (서비스 없음)
        pipeline.run(state)  # NullServiceProvider 자동 적용
    """

    def __init__(
        self,
        database: Optional[DatabaseService] = None,
        config: Optional[ConfigService] = None,
        mcp: Optional[MCPService] = None,
        documents: Optional[DocumentService] = None,
    ):
        self._database = database
        self._config = config
        self._mcp = mcp
        self._documents = documents

    @property
    def database(self) -> Optional[DatabaseService]:
        return self._database

    @property
    def config(self) -> Optional[ConfigService]:
        return self._config

    @property
    def mcp(self) -> Optional[MCPService]:
        return self._mcp

    @property
    def documents(self) -> Optional[DocumentService]:
        return self._documents

    def has(self, service_name: str) -> bool:
        """서비스 사용 가능 여부 확인."""
        return getattr(self, f"_{service_name}", None) is not None

    def describe(self) -> dict[str, bool]:
        """각 서비스 활성 상태."""
        return {
            "database": self._database is not None,
            "config": self._config is not None,
            "mcp": self._mcp is not None,
            "documents": self._documents is not None,
        }


# ──────────────────────────────────────────────
# 6. NullServiceProvider — 서비스 없이 독립 실행
# ──────────────────────────────────────────────

class NullServiceProvider(ServiceProvider):
    """모든 서비스가 None인 기본 프로바이더.
    하네스가 xgen 없이 독립 실행될 때 사용.
    """

    def __init__(self):
        super().__init__()
