"""
기본 데이터 모델 클래스
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, Optional, List
import json
import logging
import os
from zoneinfo import ZoneInfo

logger = logging.getLogger("base-model")

# 환경변수에서 타임존 가져오기 (기본값: 서울 시간)
TIMEZONE = ZoneInfo(os.getenv('TIMEZONE', 'Asia/Seoul'))

# 타임존 설정 로그 (INFO 레벨로 강제 출력)
logger.warning("TIMEZONE ENV: %s", os.getenv('TIMEZONE', '---- USE_DEFAULT ---- Asia/Seoul'))
logger.warning("SET TIMEZONE: %s", str(TIMEZONE))
logger.warning("Time NOW (BaseModel): %s", datetime.now(TIMEZONE).isoformat())
logger.warning("Time NOW (UTC): %s", datetime.now().isoformat())
logger.warning("=== TIMEZONE DEBUG ===")

# DEBUG 레벨 로그도 보이도록 레벨 조정
logger.setLevel(logging.DEBUG)

class BaseModel(ABC):
    """모든 데이터 모델의 기본 클래스"""

    def __init__(self, **kwargs):
        self.id: Optional[int] = kwargs.get('id')
        self.created_at: Optional[datetime] = kwargs.get('created_at')
        self.updated_at: Optional[datetime] = kwargs.get('updated_at')

        # 추가 필드들을 동적으로 설정
        for key, value in kwargs.items():
            if not hasattr(self, key):
                setattr(self, key, value)

    @abstractmethod
    def get_table_name(self) -> str:
        """테이블 이름 반환"""
        pass

    @abstractmethod
    def get_schema(self) -> Dict[str, str]:
        """테이블 스키마 반환 (컬럼명: 타입)"""
        pass

    # 테이블 제약조건 접두사 (UNIQUE_, CHECK_)를 제외한 실제 컬럼명만 반환
    _CONSTRAINT_PREFIXES = ('UNIQUE_', 'CHECK_')

    # from_dict()에서 JSON 역직렬화 대상 TEXT 컬럼 캐시
    _text_columns_cache: Optional[frozenset] = None

    @classmethod
    def _get_text_columns(cls) -> frozenset:
        """스키마에서 TEXT로 시작하는 컬럼명을 반환 (클래스별 캐시)"""
        if cls._text_columns_cache is None:
            instance = cls()
            schema = instance.get_schema()
            cls._text_columns_cache = frozenset(
                k for k, v in schema.items()
                if isinstance(v, str) and v.strip().upper().startswith('TEXT')
            )
        return cls._text_columns_cache

    def get_column_names(self) -> List[str]:
        """get_schema()에서 제약조건 키를 제외한 실제 컬럼명 목록 반환"""
        return [k for k in self.get_schema().keys()
                if not k.startswith(self._CONSTRAINT_PREFIXES)]

    def get_indexes(self) -> List[tuple]:
        """테이블 인덱스 정의. 오버라이드해서 사용.
        Returns: [("인덱스명", "컬럼1, 컬럼2 DESC"), ...]
        """
        return []

    @classmethod
    def now(cls) -> datetime:
        """현재 시간을 설정된 타임존으로 반환"""
        return datetime.now(TIMEZONE)

    def to_dict(self) -> Dict[str, Any]:
        """객체를 딕셔너리로 변환 (API 응답 등 범용 — dict/list를 그대로 유지)"""
        result = {}
        for key, value in self.__dict__.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result

    def _to_db_dict(self) -> Dict[str, Any]:
        """DB 저장용 딕셔너리 변환 (dict/list → JSON 문자열 직렬화)"""
        result = {}
        for key, value in self.__dict__.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif isinstance(value, (list, dict)):
                result[key] = json.dumps(value) if value else None
            else:
                result[key] = value
        return result

    def to_api_dict(self) -> Dict[str, Any]:
        """객체를 딕셔너리로 변환 (API 응답용 — to_dict()와 동일, 호환성용 alias)"""
        return self.to_dict()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        """딕셔너리에서 객체 생성"""

        tz = TIMEZONE

        for field in ("created_at", "updated_at"):
            if field in data and isinstance(data[field], str):
                dt = datetime.fromisoformat(data[field])
                if dt.tzinfo is None:
                    # naive datetime → 지정된 TIMEZONE 기준으로 해석
                    dt = dt.replace(tzinfo=tz)
                else:
                    # aware datetime → 지정된 TIMEZONE 으로 변환
                    dt = dt.astimezone(tz)
                data[field] = dt

        # TEXT 컬럼에 저장된 JSON 문자열을 dict/list로 자동 역직렬화
        # to_dict()에서 json.dumps()로 직렬화한 값을 복원
        try:
            schema = cls._get_text_columns()
            for key in schema:
                val = data.get(key)
                if isinstance(val, str) and val:
                    first = val.lstrip()[0:1]
                    if first in ('{', '['):
                        try:
                            data[key] = json.loads(val)
                        except (json.JSONDecodeError, ValueError):
                            pass  # 순수 텍스트인 경우 그대로 유지
        except Exception:
            pass  # 스키마 파싱 실패 시 기존 동작 유지

        return cls(**data)

    def get_insert_query(self, db_type: str = "sqlite") -> tuple:
        """INSERT 쿼리 생성"""
        data = self._to_db_dict()
        # id와 타임스탬프 제외 (자동 생성)
        data.pop('id', None)
        data.pop('created_at', None)
        data.pop('updated_at', None)

        columns = list(data.keys())
        values = list(data.values())

        if db_type == "postgresql":
            placeholders = ["%s" for _ in range(len(values))]
        else:  # sqlite
            placeholders = ["?" for _ in range(len(values))]

        query = f"""
        INSERT INTO {self.get_table_name()} ({', '.join(columns)})
        VALUES ({', '.join(placeholders)})
        """

        return query.strip(), values

    def get_update_query(self, db_type: str = "sqlite") -> tuple:
        """UPDATE 쿼리 생성"""
        if not self.id:
            raise ValueError("Cannot update record without ID")

        data = self._to_db_dict()
        # id와 created_at 제외
        data.pop('id', None)
        data.pop('created_at', None)
        data['updated_at'] = self.now().isoformat()

        columns = list(data.keys())
        values = list(data.values())

        if db_type == "postgresql":
            set_clauses = [f"{col} = %s" for col in columns]
            where_placeholder = "%s"
        else:  # sqlite
            set_clauses = [f"{col} = ?" for col in columns]
            where_placeholder = "?"

        query = f"""
        UPDATE {self.get_table_name()}
        SET {', '.join(set_clauses)}
        WHERE id = {where_placeholder}
        """

        values.append(self.id)
        return query.strip(), values

    @classmethod
    def get_create_table_query(cls, db_type: str = "sqlite") -> str:
        """CREATE TABLE 쿼리 생성"""
        instance = cls()
        schema = instance.get_schema()

        # 기본 컬럼들 추가
        if db_type == "postgresql":
            base_columns = {
                'id': 'SERIAL PRIMARY KEY',
                'created_at': f'TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE \'{TIMEZONE.key}\')',
                'updated_at': f'TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE \'{TIMEZONE.key}\')'
            }
        else:  # sqlite
            base_columns = {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'created_at': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP',
                'updated_at': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'
            }

        # PostgreSQL의 경우 updated_at 자동 업데이트 트리거 필요
        all_columns = {**base_columns, **schema}

        columns_def = []
        for col_name, col_type in all_columns.items():
            # UNIQUE_, CHECK_ 접두사는 테이블 제약조건(CONSTRAINT)으로 렌더링
            if col_name.startswith('UNIQUE_') or col_name.startswith('CHECK_'):
                columns_def.append(f"CONSTRAINT {col_name.lower()} {col_type}")
            else:
                columns_def.append(f"{col_name} {col_type}")

        columns_str = ',\n            '.join(columns_def)
        query = f"""CREATE TABLE IF NOT EXISTS {instance.get_table_name()} (
            {columns_str}
        )"""

        return query.strip()
