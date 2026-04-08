"""
Gateway 헤더 파서

Gateway(xgen-backend-gateway)가 주입한 HTTP 헤더를 파싱하여
사용자 정보를 Dict로 변환합니다.
"""

import logging
from typing import Dict, Any, Optional, Union, List
from urllib.parse import unquote

from fastapi import HTTPException, Request

logger = logging.getLogger("xgen-sdk.auth.gateway")


def get_user_info_by_gateway(request: Request) -> Dict[str, Any]:
    """Gateway가 주입한 헤더에서 사용자 정보를 추출한다.

    Headers:
        X-User-ID: 사용자 ID
        X-User-Name: URL-encoded 사용자 이름
        X-User-Superuser: "true" | "false"
        X-User-Roles: 쉼표 구분 역할 이름 (예: "developer,designer")
        X-User-Permissions: 쉼표 구분 권한 문자열 (예: "workflow:create,document:read")
        X-User-Groups: 쉼표 구분 그룹 이름
        X-User-Supervision-Full: 쉼표 구분 대상 역할 이름
        X-User-Supervision-Monitor: 쉼표 구분 대상 역할 이름
        X-User-Supervision-Audit: 쉼표 구분 대상 역할 이름

    Returns:
        Dict with: user_id, user_name, is_superuser, roles, permissions, groups, supervision
    """
    user_id = request.headers.get("X-User-ID")
    user_name_encoded = request.headers.get("X-User-Name")
    logger.info(f"헤더에서 사용자 정보 추출 시도: X-User-ID={user_id}, X-User-Name={user_name_encoded}")
    user_name = unquote(user_name_encoded) if user_name_encoded else None
    is_superuser = request.headers.get("X-User-Superuser", "false").lower() == "true"

    logger.info(f"사용자 인증 정보: user_id={user_id}, user_name={user_name}, is_superuser={is_superuser}")

    if not user_id or not user_name:
        raise HTTPException(
            status_code=401,
            detail="사용자 인증이 필요합니다"
        )

    normalized_user_id = normalize_user_id(user_id)
    if normalized_user_id is None:
        raise HTTPException(
            status_code=401,
            detail="유효하지 않은 사용자 ID 형식입니다"
        )

    # 역할/권한/그룹 파싱
    roles = _parse_comma_list(request.headers.get("X-User-Roles", ""))
    permissions = set(_parse_comma_list(request.headers.get("X-User-Permissions", "")))
    groups = _parse_comma_list(request.headers.get("X-User-Groups", ""))

    if is_superuser:
        permissions = {"*:*"}

    # 감독 범위 파싱
    supervision = {
        "full": _parse_comma_list(request.headers.get("X-User-Supervision-Full", "")),
        "monitor": _parse_comma_list(request.headers.get("X-User-Supervision-Monitor", "")),
        "audit": _parse_comma_list(request.headers.get("X-User-Supervision-Audit", "")),
    }

    return {
        "user_id": normalized_user_id,
        "user_name": user_name,
        "is_superuser": is_superuser,
        "roles": roles,
        "permissions": permissions,
        "groups": groups,
        "supervision": supervision,
    }


def normalize_user_id(user_id: Union[int, str, None]) -> Optional[int]:
    """
    user_id를 int로 변환합니다.
    str인 경우 숫자로만 구성되어 있으면 int로 변환하고, 그렇지 않으면 None을 반환합니다.
    """
    if user_id is None:
        return None
    if isinstance(user_id, int):
        return user_id
    if isinstance(user_id, str):
        user_id = user_id.strip()
        if user_id.isdigit():
            return int(user_id)
    return None


def _parse_comma_list(value: str) -> List[str]:
    """쉼표 구분 문자열을 리스트로 파싱. 빈 값 필터링."""
    if not value or not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
