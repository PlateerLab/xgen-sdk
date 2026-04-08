"""
Config Serializer - 설정 값의 안전한 직렬화/역직렬화 유틸리티

JSON 이중 직렬화 문제 방지:
- 이미 JSON 문자열인 값을 다시 직렬화하지 않음
- 역직렬화 시 다중 이스케이프된 값을 안전하게 처리
"""
import json
import logging
from typing import Any, Union

logger = logging.getLogger("config-serializer")


def safe_serialize(value: Any, data_type: str = None) -> str:
    """
    설정 값을 DB 저장용 문자열로 안전하게 직렬화

    JSON 이중 직렬화 방지:
    - list/dict 타입만 JSON 직렬화
    - 이미 JSON 문자열인 경우 재직렬화하지 않음
    - 문자열은 그대로 저장

    Args:
        value: 직렬화할 값
        data_type: 데이터 타입 힌트 (선택적)

    Returns:
        DB 저장용 문자열
    """
    if value is None:
        return ""

    # 이미 문자열인 경우
    if isinstance(value, str):
        # JSON 문자열인지 확인 (list 또는 dict로 파싱 가능한지)
        if _is_json_string(value):
            # 이미 JSON 문자열이면 그대로 반환 (재직렬화 방지)
            logger.debug(f"Value is already JSON string, returning as-is: {value[:100]}...")
            return value
        # 일반 문자열은 그대로 반환
        return value

    # bool 타입 (int보다 먼저 체크해야 함 - Python에서 bool은 int의 서브클래스)
    if isinstance(value, bool):
        return str(value).lower()

    # 숫자 타입
    if isinstance(value, (int, float)):
        return str(value)

    # list 또는 dict 타입
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.warning(f"Failed to serialize {type(value).__name__}: {e}")
            return str(value)

    # 기타 타입
    return str(value)


def safe_deserialize(value: str, data_type: str = "string") -> Any:
    """
    DB에서 읽은 문자열 값을 실제 타입으로 안전하게 역직렬화

    다중 이스케이프된 JSON 문자열 처리:
    - 최대 10번까지 반복 역직렬화 시도
    - 무한 루프 방지
    - 파싱 실패 시 원본 문자열 반환

    Args:
        value: DB에서 읽은 문자열 값
        data_type: 타입 정보 (string, int, float, bool, list, dict)

    Returns:
        변환된 값
    """
    if value is None or value == "":
        return None

    # bool 타입
    if data_type == "bool":
        return str(value).lower() in ('true', '1', 'yes', 'on', 'enabled')

    # int 타입
    if data_type == "int":
        try:
            # 문자열이 따옴표로 감싸진 경우 처리
            clean_value = value.strip().strip('"').strip("'")
            return int(clean_value)
        except (ValueError, TypeError):
            logger.warning(f"Failed to convert to int: {value}")
            return 0

    # float 타입
    if data_type == "float":
        try:
            clean_value = value.strip().strip('"').strip("'")
            return float(clean_value)
        except (ValueError, TypeError):
            logger.warning(f"Failed to convert to float: {value}")
            return 0.0

    # list 타입
    if data_type == "list":
        return _safe_parse_json_list(value)

    # dict 타입
    if data_type == "dict":
        return _safe_parse_json_dict(value)

    # string 타입 (기본)
    # JSON 문자열이 이스케이프되어 저장된 경우 원래 문자열 복원
    if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return parsed
        except json.JSONDecodeError:
            pass
    return str(value)


def _is_json_string(value: str) -> bool:
    """
    문자열이 유효한 JSON (list 또는 dict)인지 확인

    Args:
        value: 확인할 문자열

    Returns:
        JSON list/dict로 파싱 가능하면 True
    """
    if not isinstance(value, str):
        return False

    value = value.strip()

    # list 또는 dict 형태인지 빠른 체크
    if not ((value.startswith('[') and value.endswith(']')) or
            (value.startswith('{') and value.endswith('}'))):
        return False

    try:
        parsed = json.loads(value)
        return isinstance(parsed, (list, dict))
    except json.JSONDecodeError:
        return False


def _safe_parse_json_list(value: str, max_depth: int = 10) -> list:
    """
    다중 이스케이프된 JSON 리스트를 안전하게 파싱

    다중 이스케이프 예시:
    - 원본: ["a", "b"]
    - 1회 이스케이프: "[\"a\", \"b\"]"
    - 2회 이스케이프: "\"[\\\"a\\\", \\\"b\\\"]\""
    - ...

    Args:
        value: 파싱할 문자열
        max_depth: 최대 역직렬화 반복 횟수 (무한 루프 방지)

    Returns:
        파싱된 리스트 또는 빈 리스트
    """
    if not isinstance(value, str) or not value.strip():
        return []

    current = value.strip()
    depth = 0

    while depth < max_depth:
        depth += 1

        # 이미 리스트면 반환
        if isinstance(current, list):
            return current

        # 문자열이 아니면 종료
        if not isinstance(current, str):
            break

        # JSON 파싱 시도
        try:
            parsed = json.loads(current)

            # 파싱 결과가 리스트면 성공
            if isinstance(parsed, list):
                logger.debug(f"Successfully parsed JSON list at depth {depth}")
                return parsed

            # 문자열이면 다시 파싱 시도 (다중 이스케이프 처리)
            if isinstance(parsed, str):
                # 파싱 결과가 이전과 같으면 무한 루프 방지
                if parsed == current:
                    break
                current = parsed
                continue

            # 다른 타입이면 종료
            break

        except json.JSONDecodeError:
            break

    # 파싱 실패 - 쉼표로 구분된 문자열 시도
    logger.warning(f"Failed to parse JSON list after {depth} attempts, trying comma-split: {value[:100]}...")
    try:
        # 대괄호 제거 후 쉼표로 분리
        clean_value = value.strip().strip('[]')
        if clean_value:
            return [item.strip().strip('"\'') for item in clean_value.split(',') if item.strip()]
    except Exception:
        pass

    return []


def _safe_parse_json_dict(value: str, max_depth: int = 10) -> dict:
    """
    다중 이스케이프된 JSON 딕셔너리를 안전하게 파싱

    Args:
        value: 파싱할 문자열
        max_depth: 최대 역직렬화 반복 횟수

    Returns:
        파싱된 딕셔너리 또는 빈 딕셔너리
    """
    if not isinstance(value, str) or not value.strip():
        return {}

    current = value.strip()
    depth = 0

    while depth < max_depth:
        depth += 1

        # 이미 딕셔너리면 반환
        if isinstance(current, dict):
            return current

        # 문자열이 아니면 종료
        if not isinstance(current, str):
            break

        # JSON 파싱 시도
        try:
            parsed = json.loads(current)

            # 파싱 결과가 딕셔너리면 성공
            if isinstance(parsed, dict):
                logger.debug(f"Successfully parsed JSON dict at depth {depth}")
                return parsed

            # 문자열이면 다시 파싱 시도 (다중 이스케이프 처리)
            if isinstance(parsed, str):
                # 파싱 결과가 이전과 같으면 무한 루프 방지
                if parsed == current:
                    break
                current = parsed
                continue

            # 다른 타입이면 종료
            break

        except json.JSONDecodeError:
            break

    logger.warning(f"Failed to parse JSON dict after {depth} attempts: {value[:100]}...")
    return {}


def normalize_config_value(value: Any, data_type: str = None) -> Any:
    """
    설정 값을 정규화 (이미 잘못 저장된 값 복구)

    DB에서 읽은 값이 다중 이스케이프되어 있는 경우 정상 값으로 복구

    Args:
        value: 정규화할 값
        data_type: 데이터 타입

    Returns:
        정규화된 값
    """
    if value is None:
        return None

    # 이미 올바른 타입이면 그대로 반환
    if isinstance(value, (list, dict)):
        return value

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)) and data_type in ("int", "float", None):
        return value

    # 문자열인 경우 역직렬화 시도
    if isinstance(value, str):
        if data_type == "list":
            return _safe_parse_json_list(value)
        elif data_type == "dict":
            return _safe_parse_json_dict(value)
        elif data_type == "bool":
            return str(value).lower() in ('true', '1', 'yes', 'on', 'enabled')
        elif data_type == "int":
            try:
                return int(value.strip().strip('"\''))
            except (ValueError, TypeError):
                return 0
        elif data_type == "float":
            try:
                return float(value.strip().strip('"\''))
            except (ValueError, TypeError):
                return 0.0

    return value
