"""
xgen_sdk.notification — 일반화된 in-app notification 시스템

quota 외 다른 도메인(governance / execution / system)도 동일 channel 을 통해
사용자에게 알림을 전달할 수 있는 1급 시민 인터페이스.

전체 설계: TOKEN_QUOTA_POLICY_PLAN.md §15 참조.

설계 원칙:
    - Generic   : payload 만 정의. 도메인 지식 0.
    - Per-user  : 모든 알림은 1 user 직접 전달.
    - Persistent: DB 영속. 읽음 여부 추적 (read_at).
    - Safe      : 알림 발송 실패가 호출자 main flow 를 깨지 않음.
    - 확장 가능 : 추가 metadata 는 JSON 으로 자유 적재.

Quick start:
    from xgen_sdk.notification import (
        NotificationPayload, NotificationCategory, NotificationSeverity, publish,
    )

    publish(app_db, NotificationPayload(
        user_id=123,
        category=NotificationCategory.QUOTA,
        severity=NotificationSeverity.ERROR,
        title='토큰 한도 초과',
        body='월간 한도 1,000,000 토큰을 초과했습니다.',
        link='/mypage?tab=quota',
        metadata={'policy_id': 7, 'used': 1234567, 'limit': 1000000},
    ))
"""
from xgen_sdk.notification.types import (
    NotificationCategory,
    NotificationPayload,
    NotificationSeverity,
)
from xgen_sdk.notification.publisher import publish

__all__ = [
    "NotificationCategory",
    "NotificationPayload",
    "NotificationSeverity",
    "publish",
]
