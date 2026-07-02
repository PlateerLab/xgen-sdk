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
    - xgen_sdk.logging: 백엔드 DB 로깅 (BackendLogger)
    - xgen_sdk.quota: 토큰 한도(quota) 정책 평가 (1.13.0+)
    - xgen_sdk.notification: 일반화된 in-app notification (1.13.1+)
    - xgen_sdk.llm_catalog: LLM 모델 카탈로그 동적 조회 + 캐시 (1.14.0+)
    - xgen_sdk.harness: 내장 하네스 엔진(LangChain-free) — DB 세션영속·config 키·logging 연동 + add/delete step (1.17.0+ 엔진 소스 내장)

Quick Start:
    from xgen_sdk import XgenApp

    xgen = XgenApp()
    xgen.boot()

    db = xgen.db           # XgenDB 인스턴스
    config = xgen.config   # RedisConfigManager 또는 LocalConfigManager
"""

__version__ = "1.27.8"

from xgen_sdk.app import XgenApp

__all__ = ["XgenApp", "__version__"]
