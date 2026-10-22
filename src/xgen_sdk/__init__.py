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
    - xgen_sdk.comfyui: ComfyUI 워크플로우 매핑 규칙 + async 클라이언트 (2.4.0+)
    - xgen_sdk.anomaly: 시스템 알람 운영 이벤트 적재 · LLM 호출 결과 기록 (2.11.0+)
    - xgen_sdk.approval.followup: 같은 문서에 후속 결재선 이어 붙이기 (2.12.0+)
    - xgen_sdk.approval.directory.list_users(offset=): 결재자 목록 이어 받기 (2.13.0+)
    - xgen_sdk.storage.transfer: 큰 업로드를 디스크로 받기 · 저장소 파일을 메모리 없이 응답 (2.14.0+)
    - xgen_sdk.approval: [파일 첨부] 칸 기본 상한 1GiB · 옛 기본값(50MB) 올리기 raise_legacy_attachment_limits (2.15.0+)
    - xgen_sdk.storage.folder_zip: 폴더 하나를 zip 으로 흘려 보내기(plan_tree · stream), 파일 저장소 · 에이전트 스토리지 공용 (2.16.0+)
    - xgen_sdk.config: Redis 를 설정해 둔 프로세스는 기동 순간 Redis 가 없어도 메모리 설정에 묶이지 않고 다시 붙는다 (2.17.2+)

Quick Start:
    from xgen_sdk import XgenApp

    xgen = XgenApp()
    xgen.boot()

    db = xgen.db           # XgenDB 인스턴스
    config = xgen.config   # RedisConfigManager 또는 LocalConfigManager
"""

__version__ = "2.20.0"

from xgen_sdk.app import XgenApp

__all__ = ["XgenApp", "__version__"]
