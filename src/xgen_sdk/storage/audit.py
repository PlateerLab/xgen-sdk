"""
xgen_sdk.storage.audit — MinIO 업로드/다운로드 감사(audit) 훅

storage 계층에서 일어나는 모든 upload/download 를 서비스 DB(minio_logs 테이블)에
기록하기 위한 경량 훅. SDK 는 DB 를 모르므로(스토리지 모듈은 DB 무의존), 각 서비스가
부팅 시 로거 콜러블을 등록하면 SDK 가 작업 후 그 콜러블을 best-effort 로 호출한다.

두 축:
    1) 로거 등록 — set_storage_audit_logger(fn):
       fn(event: dict) 형태. SDK 가 upload/download 직후 event 를 넘긴다.
       서비스는 event 를 minio_logs 로 insert (service 이름 등 자기 컨텍스트 부착).

    2) 컨텍스트 부착 — storage_audit_context(**fields):
       ContextVar 기반 with-블록. 이 블록 안에서 수행된 storage 작업의 event 에
       user_id/session_id/interaction_id/source 등 서비스 레벨 컨텍스트를 병합한다.
       (SDK 는 storage 사실만 알고 요청 컨텍스트는 모르므로, 상관관계용 키를
        서비스가 이 블록으로 흘려보낸다 — 예: 지식 컬렉션 업로드 이력과의 조인.)

원칙:
    - best-effort — 로거/컨텍스트 처리 중 어떤 예외도 storage 작업을 깨뜨리지 않는다.
    - 로거 미등록 시 완전 no-op (오버헤드 0).
    - 이 모듈은 minio/pycryptodome 무의존 (순수 파이썬).
"""
from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 서비스가 등록하는 로거 콜러블. event(dict) 1개를 받는다.
_AUDIT_LOGGER: Optional[Callable[[Dict[str, Any]], None]] = None

# 서비스 레벨 컨텍스트 (요청별). storage_audit_context 로 push/pop.
_AUDIT_CONTEXT: ContextVar[Dict[str, Any]] = ContextVar(
    "xgen_storage_audit_context", default={}
)

# event 에 병합 허용되는 컨텍스트 키 (화이트리스트 — 예기치 않은 키 유입 차단).
_CONTEXT_KEYS = ("user_id", "session_id", "interaction_id", "source", "service")

# 내부 제어 키 (event 에 노출되지 않음).
_SUPPRESS_KEY = "_suppress_audit"


def set_storage_audit_logger(fn: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """storage 감사 로거 등록. None 이면 해제.

    Args:
        fn: event(dict) 를 받아 저장하는 콜러블. 예외를 던져도 무방(SDK 가 삼킴).
            서비스는 여기서 minio_logs 로 insert 하고 service 이름 등을 붙인다.
    """
    global _AUDIT_LOGGER
    if fn is not None and not callable(fn):
        raise TypeError("storage audit logger 는 callable 또는 None 이어야 합니다.")
    _AUDIT_LOGGER = fn


@contextlib.contextmanager
def storage_audit_context(suppress: bool = False, **fields: Any):
    """이 블록 안 storage 작업의 audit event 에 서비스 컨텍스트를 병합.

    허용 키: user_id, session_id, interaction_id, source, service.
    None 값은 무시(기존 컨텍스트를 덮어쓰지 않음). 중첩 시 상위 컨텍스트에 누적.

    Args:
        suppress: True 면 이 블록 안 storage 작업의 emit 이 no-op 이 된다.
            호출부가 minio_logs 를 **명시적으로** 기록할 때 SDK 훅의 중복 기록을
            막기 위한 용도 (예: 문서 업로드 플로우가 async 컨텍스트에서 직접 write).

    예:
        with storage_audit_context(user_id=uid, session_id=sid, source="collection"):
            upload_file(client, path, obj, bucket)   # event 에 위 필드가 붙는다
    """
    merged = dict(_AUDIT_CONTEXT.get() or {})
    for k, v in fields.items():
        if v is not None and k in _CONTEXT_KEYS:
            merged[k] = v
    if suppress:
        merged[_SUPPRESS_KEY] = True
    token = _AUDIT_CONTEXT.set(merged)
    try:
        yield
    finally:
        _AUDIT_CONTEXT.reset(token)


def audit_context_snapshot() -> Dict[str, Any]:
    """현재 컨텍스트 사본 (디버깅/테스트용)."""
    return dict(_AUDIT_CONTEXT.get() or {})


def emit_storage_audit(
    operation: str,
    bucket_name: str,
    object_name: str,
    *,
    size_bytes: Optional[int] = None,
    plaintext_size_bytes: Optional[int] = None,
    encrypted: bool = False,
    encryption_algorithm: Optional[str] = None,
    content_type: Optional[str] = None,
    status: str = "success",
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """storage 작업 1건을 감사 event 로 방출. 로거 미등록 시 no-op.

    어떤 예외도 호출부(storage 작업)로 전파되지 않는다 (best-effort).
    """
    fn = _AUDIT_LOGGER
    if fn is None:
        return
    try:
        ctx = _AUDIT_CONTEXT.get() or {}
        if ctx.get(_SUPPRESS_KEY):
            return  # 호출부가 명시적으로 기록 — SDK 훅 중복 방지
        event: Dict[str, Any] = {
            "operation": operation,
            "bucket_name": bucket_name,
            "object_name": object_name,
            "object_path": (f"{bucket_name}/{object_name}" if bucket_name else object_name),
            "size_bytes": size_bytes,
            "plaintext_size_bytes": plaintext_size_bytes,
            "encrypted": bool(encrypted),
            "encryption_algorithm": encryption_algorithm,
            "content_type": content_type,
            "status": status,
            "error_message": error_message,
            "duration_ms": duration_ms,
            # 서비스 컨텍스트 (없으면 None)
            "user_id": ctx.get("user_id"),
            "session_id": ctx.get("session_id"),
            "interaction_id": ctx.get("interaction_id"),
            "source": ctx.get("source"),
            "service": ctx.get("service"),
        }
        fn(event)
    except Exception as e:  # pylint: disable=broad-except
        logger.debug("[storage.audit] emit failed (ignored): %s", e)
