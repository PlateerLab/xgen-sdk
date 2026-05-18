"""
xgen_sdk.notification.types — Enum + dataclass

DB/HTTP 의존이 없는 순수 타입.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class NotificationCategory(str, Enum):
    """알림 카테고리 — 신규 도메인 추가 시 enum 값 1 개만 늘리면 됨."""
    QUOTA = "quota"
    GOVERNANCE = "governance"
    EXECUTION = "execution"
    SYSTEM = "system"

    @classmethod
    def coerce(cls, value: Optional[str]) -> "NotificationCategory":
        """알 수 없는 값은 SYSTEM 으로 fallback."""
        if not value:
            return cls.SYSTEM
        try:
            return cls(value)
        except ValueError:
            return cls.SYSTEM


class NotificationSeverity(str, Enum):
    """알림 심각도."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @classmethod
    def coerce(cls, value: Optional[str]) -> "NotificationSeverity":
        if not value:
            return cls.INFO
        try:
            return cls(value)
        except ValueError:
            return cls.INFO


@dataclass(frozen=True)
class NotificationPayload:
    """알림 1 건 — DB 행과 1:1 매핑.

    producer 가 publish() 에 넘기는 immutable spec.

    Args:
        user_id:   수신자 user.id
        category:  도메인 카테고리 (quota / governance / ...)
        severity:  info | warning | error | critical
        title:     알림 제목 (사용자 표시)
        body:      알림 본문 (사용자 표시)
        link:      클릭 시 이동할 인앱 경로 (선택, 예: '/main?view=...')
        metadata:  자유 적재 — producer 가 필요에 따라 추가 정보. JSON 직렬화 가능 dict.
    """
    user_id: int
    category: NotificationCategory
    severity: NotificationSeverity
    title: str
    body: str
    link: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
