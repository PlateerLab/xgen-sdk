import logging
import inspect
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Dict, Optional, Any
from uuid import UUID
from fastapi import Request
from xgen_sdk.db import XgenDB

logger = logging.getLogger("backend-logger")


def _json_default(value: Any) -> Any:
    """json.dumps 가 모르는 타입을 문자열류로 흘려보낸다."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (set, frozenset)):
        return list(value)
    return str(value)


def _dump_metadata(metadata: Optional[Dict]) -> str:
    """metadata 직렬화. 어떤 입력이 와도 예외를 던지지 않는다.

    _log 의 except Exception 이 직렬화 실패를 삼키면 metadata 하나 때문에
    로그 레코드 자체가 DB 에 안 남는다. 여기서 끝내고 로그는 반드시 남긴다.
    """
    if not metadata:
        return '{}'
    try:
        return json.dumps(metadata, ensure_ascii=False, default=_json_default)
    except (TypeError, ValueError) as e:
        # default 로도 못 막는 경우: 직렬화 불가 dict 키, 순환 참조 등
        logger.warning(f"metadata serialization fallback: {str(e)}")
        try:
            return json.dumps(
                {"_serialization_error": str(e), "_metadata_repr": repr(metadata)},
                ensure_ascii=False,
                default=_json_default,
            )
        except Exception:
            return '{"_serialization_error": "metadata could not be serialized"}'


class BackendLogger:
    """백엔드 DB 로거 — backend_logs 테이블에 로그를 기록"""

    def __init__(self, app_db: XgenDB, user_id: Optional[int] = None,
                 request: Optional[Request] = None):
        self.app_db = app_db
        self.user_id = user_id
        self.request = request
        self._function_name: Optional[str] = None
        self._api_endpoint: Optional[str] = None

        self._extract_context_info()

    def _extract_context_info(self):
        try:
            if self.request:
                path = self.request.url.path
                if path.startswith('/api/v1/'):
                    self._api_endpoint = path[8:]
                elif path.startswith('/'):
                    self._api_endpoint = path[1:]
                else:
                    self._api_endpoint = path
        except Exception as e:
            logger.warning(f"Could not extract context info: {str(e)}")

    def _log(self, level: str, message: str, metadata: Optional[Dict] = None,
             function_name: Optional[str] = None, api_endpoint: Optional[str] = None):
        try:
            func_name = function_name or self._function_name or ''
            endpoint = api_endpoint or self._api_endpoint or ''
            log_id = f"LOG__{self.user_id}__{func_name}"

            log_data = {
                'user_id': self.user_id,
                'log_id': log_id,
                'log_level': level,
                'message': message,
                'function_name': func_name,
                'api_endpoint': endpoint,
                'metadata': _dump_metadata(metadata)
            }
            self.app_db.insert_record('backend_logs', log_data)
            logger.info(f"Logged backend data with log_id: {log_id}")
        except Exception as e:
            logger.error(f"Error logging backend data: {str(e)}")

    def success(self, message: str, metadata: Optional[Dict] = None,
                function_name: Optional[str] = None, api_endpoint: Optional[str] = None):
        self._log("INFO", f"SUCCESS: {message}", metadata, function_name, api_endpoint)

    def info(self, message: str, metadata: Optional[Dict] = None,
             function_name: Optional[str] = None, api_endpoint: Optional[str] = None):
        self._log("INFO", message, metadata, function_name, api_endpoint)

    def warn(self, message: str, metadata: Optional[Dict] = None,
             function_name: Optional[str] = None, api_endpoint: Optional[str] = None):
        self._log("WARN", message, metadata, function_name, api_endpoint)

    def warning(self, message: str, metadata: Optional[Dict] = None,
                function_name: Optional[str] = None, api_endpoint: Optional[str] = None):
        self.warn(message, metadata, function_name, api_endpoint)

    def error(self, message: str, metadata: Optional[Dict] = None,
              function_name: Optional[str] = None, api_endpoint: Optional[str] = None,
              exception: Optional[Exception] = None):
        error_message = message
        if exception:
            error_message = f"{message}: {str(exception)}"
            if metadata is None:
                metadata = {}
            metadata['exception_type'] = type(exception).__name__
            metadata['exception_details'] = str(exception)
        self._log("ERROR", error_message, metadata, function_name, api_endpoint)

    def debug(self, message: str, metadata: Optional[Dict] = None,
              function_name: Optional[str] = None, api_endpoint: Optional[str] = None):
        self._log("DEBUG", message, metadata, function_name, api_endpoint)


def create_logger(app_db: XgenDB, user_id: Optional[int] = None,
                  request: Optional[Request] = None) -> BackendLogger:
    """백엔드 로거 생성 팩토리 — 호출자 함수명 자동 추출"""
    caller_function_name = None
    try:
        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_function_name = frame.f_back.f_code.co_name
    except Exception:
        pass

    logger_instance = BackendLogger(app_db, user_id, request)
    if caller_function_name:
        logger_instance._function_name = caller_function_name

    return logger_instance
