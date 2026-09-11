"""결재 원장이 DB 와 말하는 **유일한 통로**.

store·directory·notifier 가 같은 것을 쓴다. 한 곳에 모은 이유는 두 가지다:

1. **실패를 빈 목록으로 삼키지 않는다.** ``execute_raw_query`` 는 실패해도
   dict 를 돌려주는데, 그걸 그대로 ``.get("data") or []`` 로 읽으면 DB 오류가
   "결과 없음" 으로 둔갑한다. 결재에서 그건 "결재선에 아무도 없다" 나
   "그런 결재는 없다" 로 번역돼서, 사람이 승인을 못 하는데 이유를 알 수 없다.
2. notifier 가 store 를 import 하면 순환이 된다(store 가 notifier 를 부른다).
   둘 다 여기를 보게 하면 그 고리가 생기지 않는다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence


def rows(result: Any) -> List[Dict[str, Any]]:
    """``execute_raw_query`` 결과 → 행 목록. 실패는 예외로 올린다."""
    if not isinstance(result, dict):
        raise RuntimeError("DB 응답 형식이 올바르지 않습니다")
    if result.get("error"):
        raise RuntimeError(f"DB 오류: {result['error']}")
    return [dict(r) for r in (result.get("data") or [])]


def q(app_db, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    return rows(app_db.execute_raw_query(sql, tuple(params)))
