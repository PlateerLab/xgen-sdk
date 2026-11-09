"""
SQL 식별자(테이블·컬럼·정렬 키) 검증 유틸리티 (SEC-12)

XgenDB 의 조건 기반 CRUD 메서드(find_by_condition / find_records_by_condition /
update_records_by_condition / delete_records_by_condition 등)는 값(value)은 모두
드라이버 파라미터(%s / ?)로 바인딩하지만, 테이블명·컬럼명·정렬 컬럼·조건 키 같은
"식별자"는 쿼리 문자열에 그대로 끼워 넣는다. 상위 서비스(xgen-workflow 등)가 요청
파라미터를 그대로 conditions 키나 orderby 로 넘기면 SQL 주입 지점이 되므로, 쿼리
조립 전에 이 모듈로 식별자를 화이트리스트(정규식) 검증한다.

허용 형식:
  - 식별자: ^[A-Za-z_][A-Za-z0-9_]{0,62}$   (PostgreSQL NAMEDATALEN-1 = 63)
  - 한정 식별자: alias.column (점 1개, 각 부분이 식별자)
  - 조건 키: column 또는 column + 연산자 접미사(__like__, __in__, ...)
  - 정렬: "col", "col DESC", "t.col ASC", 콤마 구분 다중 컬럼

검증 실패 시 UnsafeSqlIdentifierError(ValueError) 를 발생시켜 fail-closed 한다.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "UnsafeSqlIdentifierError",
    "CONDITION_OPERATORS",
    "is_safe_identifier",
    "validate_identifier",
    "validate_qualified_identifier",
    "validate_table_name",
    "validate_column_list",
    "split_condition_key",
    "validate_condition_key",
    "validate_orderby",
]

# PostgreSQL 식별자 최대 길이(63). SQLite 는 제한이 없지만 동일하게 제한한다.
_MAX_IDENTIFIER_LENGTH = 63
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# _build_where_clause 가 인식하는 연산자 접미사. 긴 것이 먼저 오도록 정렬(__notlike__ 가 __like__ 보다 먼저).
CONDITION_OPERATORS: Tuple[str, ...] = (
    "__notlike__",
    "__isnull__",
    "__notin__",
    "__like__",
    "__gte__",
    "__lte__",
    "__not__",
    "__in__",
    "__gt__",
    "__lt__",
)

_ORDER_DIRECTIONS = ("ASC", "DESC")
_NULLS_PLACEMENTS = ("FIRST", "LAST")


class UnsafeSqlIdentifierError(ValueError):
    """쿼리 식별자로 사용할 수 없는 값이 전달되었을 때 발생."""


def is_safe_identifier(name: object) -> bool:
    """단순 식별자(접두사·연산자 없음) 여부."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= _MAX_IDENTIFIER_LENGTH
        and _IDENTIFIER_RE.match(name) is not None
    )


def validate_identifier(name: object, kind: str = "identifier") -> str:
    """단순 식별자를 검증하고 그대로 반환. 실패 시 UnsafeSqlIdentifierError."""
    if not is_safe_identifier(name):
        raise UnsafeSqlIdentifierError(f"Unsafe SQL {kind}: {name!r}")
    return name  # type: ignore[return-value]


def validate_qualified_identifier(name: object, kind: str = "identifier") -> str:
    """`col` 또는 `alias.col` 형식의 식별자를 검증."""
    if not isinstance(name, str) or not name:
        raise UnsafeSqlIdentifierError(f"Unsafe SQL {kind}: {name!r}")
    parts = name.split(".")
    if len(parts) > 2 or not all(is_safe_identifier(p) for p in parts):
        raise UnsafeSqlIdentifierError(f"Unsafe SQL {kind}: {name!r}")
    return name


def validate_table_name(table_name: object) -> str:
    """테이블명 검증 (`schema.table` 허용)."""
    return validate_qualified_identifier(table_name, kind="table name")


def validate_column_list(columns: Optional[Iterable[object]], kind: str = "column") -> Optional[List[str]]:
    """컬럼 목록 검증. None/빈 목록은 그대로 돌려준다 (호출부 기본 동작 유지)."""
    if columns is None:
        return None
    validated = [validate_qualified_identifier(c, kind=kind) for c in columns]
    return validated


def split_condition_key(key: object) -> Tuple[str, str]:
    """
    조건 키를 (컬럼, 연산자 접미사) 로 분리하고 컬럼명을 검증.

    >>> split_condition_key("user_id__in__")
    ('user_id', '__in__')
    >>> split_condition_key("t.status")
    ('t.status', '')
    """
    if not isinstance(key, str) or not key:
        raise UnsafeSqlIdentifierError(f"Unsafe SQL condition key: {key!r}")
    for op in CONDITION_OPERATORS:
        if key.endswith(op):
            column = key[: -len(op)]
            validate_qualified_identifier(column, kind="condition column")
            return column, op
    validate_qualified_identifier(key, kind="condition column")
    return key, ""


def validate_condition_key(key: object) -> str:
    """조건 키 전체(컬럼 + 연산자)를 검증하고 그대로 반환."""
    split_condition_key(key)
    return key  # type: ignore[return-value]


def validate_orderby(orderby: object, default: str = "id") -> str:
    """
    ORDER BY 식(콤마 구분 다중 컬럼, 선택적 ASC/DESC)을 검증하고 정규화해 반환.

    허용 예: "id", "created_at DESC", "t.priority ASC, t.id DESC",
            "updated_at DESC NULLS LAST, id DESC"
    방향/NULLS 키워드는 대문자로 정규화되며, 그 외 토큰(함수 호출, 괄호, 서브쿼리,
    세미콜론, 주석 등)은 모두 거부된다.
    """
    if orderby is None or (isinstance(orderby, str) and not orderby.strip()):
        return default
    if not isinstance(orderby, str):
        raise UnsafeSqlIdentifierError(f"Unsafe SQL order by: {orderby!r}")

    normalized_parts: List[str] = []
    for raw_part in orderby.split(","):
        tokens = raw_part.strip().split()
        if not tokens or len(tokens) > 4:
            raise UnsafeSqlIdentifierError(f"Unsafe SQL order by: {orderby!r}")
        column = validate_qualified_identifier(tokens[0], kind="order by column")
        rest = [t.upper() for t in tokens[1:]]
        part = column

        # 선택적 방향
        if rest and rest[0] in _ORDER_DIRECTIONS:
            part += f" {rest.pop(0)}"

        # 선택적 NULLS FIRST | NULLS LAST
        if rest:
            if len(rest) == 2 and rest[0] == "NULLS" and rest[1] in _NULLS_PLACEMENTS:
                part += f" NULLS {rest[1]}"
            else:
                raise UnsafeSqlIdentifierError(f"Unsafe SQL order by: {orderby!r}")

        normalized_parts.append(part)
    return ", ".join(normalized_parts)


def ensure_all_identifiers(values: Sequence[object], kind: str) -> None:
    """여러 식별자를 한 번에 검증 (dict keys 등)."""
    for value in values:
        validate_qualified_identifier(value, kind=kind)
