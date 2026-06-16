"""
ServiceRegistry — 외부 서비스 엔드포인트 레지스트리

하네스 라이브러리는 특정 인프라(xgen, AWS 등)를 가정하지 않는다.
실행기(executor)가 부팅 시 register_service()로 필요한 서비스를 등록하고,
각 Stage는 get_service_url()로 조회한다.

등록되지 않은 서비스를 조회하면 None을 반환 — 해당 기능은 graceful skip.

사용 예 (실행기 측):
    from xgen_harness.core.service_registry import register_service
    register_service("documents", "http://xgen-documents:8000")
    register_service("mcp", "http://xgen-mcp-station:8000")
    register_service("config", "http://xgen-core:8000")

사용 예 (Stage 측):
    from xgen_harness.core.service_registry import get_service_url
    url = get_service_url("documents")
    if not url:
        logger.info("documents service not registered, skipping RAG")
        return
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_SERVICES: dict[str, str] = {}

# 환경변수 매핑 — 서비스 이름 → 환경변수명
# 실행기가 register_service()를 안 해도 환경변수로 폴백 가능
_ENV_MAP: dict[str, list[str]] = {}


def register_service(name: str, url: str) -> None:
    """서비스 엔드포인트 등록. 실행기가 부팅 시 호출."""
    _SERVICES[name] = url.rstrip("/")


def register_env_mapping(name: str, *env_vars: str) -> None:
    """서비스 이름에 대한 환경변수 폴백 등록.

    register_service() 없이도 환경변수가 있으면 자동 해석.
    실행기가 부팅 시 호출.

    예: register_env_mapping("documents", "XGEN_DOCUMENTS_URL", "DOCUMENTS_SERVICE_BASE_URL")
    """
    _ENV_MAP[name] = list(env_vars)


def get_service_url(name: str) -> Optional[str]:
    """서비스 URL 조회.

    우선순위: 명시적 등록 > 환경변수 매핑 > None
    None이면 해당 기능은 skip (하네스가 특정 인프라를 강제하지 않음).
    """
    # 1. 명시적 등록
    if name in _SERVICES:
        return _SERVICES[name]

    # 2. 환경변수 매핑
    for env_var in _ENV_MAP.get(name, []):
        value = os.environ.get(env_var)
        if value:
            return value.rstrip("/")

    # 3. 범용 환경변수 추측 (SERVICE_NAME_URL)
    env_guess = f"{name.upper().replace('-', '_')}_URL"
    value = os.environ.get(env_guess)
    if value:
        return value.rstrip("/")

    return None


def list_services() -> dict[str, Optional[str]]:
    """등록된 모든 서비스 엔드포인트 반환."""
    result = {}
    for name in set(list(_SERVICES.keys()) + list(_ENV_MAP.keys())):
        result[name] = get_service_url(name)
    return result


def clear_services() -> None:
    """모든 서비스 등록 초기화. 테스트용."""
    _SERVICES.clear()
    _ENV_MAP.clear()
