"""
xgen_sdk.notification.publisher — DB insert helper

caller flow 가 어떤 이유로도 throw 되지 않도록 try/except + 경고 로그.
notification 모델은 lazy import — SDK 가 xgen-core 패키지에 hard-dependency 를 갖지
않도록 한다 (의존 방향 보존). 모델이 없으면 조용히 skip.
"""
from __future__ import annotations
import logging
from typing import Optional

from xgen_sdk.notification.types import NotificationPayload

logger = logging.getLogger("xgen-notify")


def publish(db, payload: NotificationPayload) -> Optional[int]:
    """단일 알림을 DB 에 기록.

    Args:
        db: XgenDB-like 인스턴스 (insert(model) 지원).
        payload: NotificationPayload

    Returns:
        생성된 row id. 실패 시 None.

    실패 케이스 (모두 main flow 를 throw 시키지 않음):
        - notification 모델 패키지가 없음 (SDK 단독 환경)
        - db 가 None
        - payload 가 None
        - DB insert 실패
    """
    if payload is None:
        logger.warning("notify skipped — payload is None")
        return None
    if db is None:
        logger.warning("notify skipped — db is None (user_id=%s, category=%s)",
                       getattr(payload, "user_id", None),
                       getattr(payload, "category", None))
        return None

    # Lazy import — SDK 가 xgen-core 모델에 hard-dep 갖지 않음
    try:
        from service.database.models.notification import Notification  # type: ignore
    except ImportError:
        logger.warning(
            "notification model not available — skipping notify(user=%s, category=%s)",
            payload.user_id, payload.category,
        )
        return None

    try:
        row = Notification(
            user_id=payload.user_id,
            category=payload.category.value if hasattr(payload.category, "value") else str(payload.category),
            severity=payload.severity.value if hasattr(payload.severity, "value") else str(payload.severity),
            title=payload.title or "",
            body=payload.body or "",
            link=payload.link,
            metadata=payload.metadata or {},
            read_at=None,
        )
        result_id = db.insert(row)
        return result_id
    except Exception as e:
        logger.warning(
            "notify failed (user=%s, category=%s): %s",
            payload.user_id, payload.category, e,
        )
        return None
