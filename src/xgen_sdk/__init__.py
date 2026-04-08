"""
xgen-sdk: XGen Platform Shared SDK

모든 Python 컨테이너(xgen-core, xgen-workflow, xgen-documents)가
공통으로 사용하는 인프라 모듈을 제공합니다.

모듈:
    - xgen_sdk.db: PostgreSQL 직접 연결 (psycopg3 ConnectionPool)
    - xgen_sdk.config: Redis 설정 관리 + Local fallback
    - xgen_sdk.storage: MinIO 오브젝트 스토리지
    - xgen_sdk.auth: 인증/인가 (ABAC 권한, 게이트웨이 헤더)
    - xgen_sdk.redis: 범용 Redis 클라이언트
"""

__version__ = "1.0.0"
