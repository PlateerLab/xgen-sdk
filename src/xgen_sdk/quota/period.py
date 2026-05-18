"""
xgen_sdk.quota.period — quota 기간 경계 계산 (KST)

타임존은 시스템 표준과 동일 의미 — env `TIMEZONE` (기본 `Asia/Seoul`).
xgen_sdk.db.base_model.TIMEZONE 과 정확히 같은 정의를 사용하지만,
직접 import 하지 않는 이유:
    `from xgen_sdk.db.base_model import TIMEZONE` 는 `xgen_sdk.db.__init__.py`
    를 트리거하여 psycopg / pool_manager 등 무거운 모듈까지 끌어온다.
    quota 모듈은 "DB·HTTP 의존 0" 을 보장해야 하므로 동일 2 줄을 여기서 직접 둔다.
    base_model.py 의 TIMEZONE 정의가 바뀌면 본 파일도 같이 갱신할 것.
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from xgen_sdk.quota.types import QuotaPeriod

# 시스템 표준과 정렬 — xgen_sdk/db/base_model.py:15 와 동일한 정의 (DRY 의도적 회피, 위 docstring 참조)
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul"))


def today_kst() -> str:
    """오늘 (TIMEZONE 기준) ISO date 문자열 'YYYY-MM-DD'."""
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def period_bounds_kst(
    period: QuotaPeriod,
    now: Optional[datetime] = None,
) -> Tuple[str, str]:
    """
    quota period 의 시작/끝 날짜를 ISO 문자열로 반환.

        DAILY   : 오늘 ~ 오늘
        WEEKLY  : 이번 주 월요일 ~ 일요일 (Python weekday: Mon=0 .. Sun=6)
        MONTHLY : 이번 달 1일 ~ 말일

    Args:
        period: QuotaPeriod
        now:    None 이면 datetime.now(TIMEZONE).
                tz-naive 이거나 다른 tz 라도 안전하게 TIMEZONE 으로 변환.

    Returns:
        ('YYYY-MM-DD', 'YYYY-MM-DD')

    NULL / 알 수 없는 값 안전:
        - now 가 tz-naive → astimezone(TIMEZONE) 호출 전에 tz 부여 시도 없이
          .astimezone() 호출은 system tz 기준으로 해석됨. 안전하게 처리하기 위해
          tz-naive 면 TIMEZONE 부여로 간주.
        - period 가 enum 정의 외 값 → DAILY 로 fallback (throw 하지 않음).
    """
    if now is None:
        ref = datetime.now(TIMEZONE)
    elif now.tzinfo is None:
        # naive datetime — TIMEZONE 부여로 해석
        ref = now.replace(tzinfo=TIMEZONE)
    else:
        ref = now.astimezone(TIMEZONE)

    n = ref.date()

    if period == QuotaPeriod.DAILY:
        return (n.isoformat(), n.isoformat())

    if period == QuotaPeriod.WEEKLY:
        start = n - timedelta(days=n.weekday())   # Mon
        end = start + timedelta(days=6)           # Sun
        return (start.isoformat(), end.isoformat())

    if period == QuotaPeriod.MONTHLY:
        start = n.replace(day=1)
        # 다음 달 1일 - 1일 = 이번 달 말일
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = nxt - timedelta(days=1)
        return (start.isoformat(), end.isoformat())

    # 알 수 없는 period — DAILY fallback (enforcement throw 방지)
    return (n.isoformat(), n.isoformat())
