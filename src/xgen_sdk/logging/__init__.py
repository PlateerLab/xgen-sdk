"""
xgen_sdk.logging — 통합 백엔드 로깅 모듈

모든 Python 컨테이너(xgen-core, xgen-workflow, xgen-documents)가
공통으로 사용하는 BackendLogger를 제공합니다.

사용 패턴:
    from xgen_sdk.logging import create_logger

    backend_log = create_logger(app_db, user_id=session["user_id"], request=request)
    backend_log.info("작업 시작", metadata={"param": value})
    backend_log.success("완료")
    backend_log.warn("경고 메시지")
    backend_log.error("에러 발생", exception=exc)
"""

from xgen_sdk.logging.backend_logger import BackendLogger, create_logger

__all__ = ["BackendLogger", "create_logger"]
