"""
ExecutionContext — 실행별 격리된 컨텍스트

os.environ 대신 contextvars를 사용하여 동시 실행 시 API 키/설정이 섞이지 않도록 격리.
"""
import contextvars
from typing import Any, Optional

# Per-execution context variables
_api_key_var: contextvars.ContextVar[str] = contextvars.ContextVar("harness_api_key", default="")
_provider_var: contextvars.ContextVar[str] = contextvars.ContextVar("harness_provider", default="")
_model_var: contextvars.ContextVar[str] = contextvars.ContextVar("harness_model", default="")
_extra_vars: contextvars.ContextVar[dict] = contextvars.ContextVar("harness_extra")


def set_execution_context(api_key: str, provider: str = "", model: str = "", **extra: Any) -> None:
    """현재 실행 컨텍스트에 API 키/프로바이더/모델 설정.

    contextvars 기반이므로 asyncio.create_task() 각각에 자동 격리됨.
    """
    _api_key_var.set(api_key)
    if provider:
        _provider_var.set(provider)
    if model:
        _model_var.set(model)
    if extra:
        _extra_vars.set(extra)


def get_api_key() -> str:
    """현재 컨텍스트의 API 키 반환. 없으면 빈 문자열."""
    return _api_key_var.get("")


def get_provider() -> str:
    """현재 컨텍스트의 프로바이더 이름 반환."""
    return _provider_var.get("")


def get_model() -> str:
    """현재 컨텍스트의 모델 이름 반환."""
    return _model_var.get("")


def get_extra(key: str, default: Any = None) -> Any:
    """현재 컨텍스트의 추가 설정값 반환."""
    try:
        extras = _extra_vars.get()
        return extras.get(key, default)
    except LookupError:
        return default


def clear_execution_context() -> None:
    """현재 컨텍스트 초기화. 테스트 등에서 사용."""
    for var in (_api_key_var, _provider_var, _model_var, _extra_vars):
        try:
            var.set(var.default if hasattr(var, 'default') else "")  # type: ignore
        except (AttributeError, TypeError):
            pass
    _api_key_var.set("")
    _provider_var.set("")
    _model_var.set("")


# ── xgen 서비스 간 호출 인증 헤더 (v0.11.24) ────────────────────────────
# 엔진은 요청자 권한을 함부로 승격하지 않는다 — 호스트(xgen-workflow gateway) 가
# 인증한 사용자 컨텍스트를 ExecutionContext 에 담아 내려주고, 엔진은 그 값만 전파.
# ExecutionContext 에 값이 없으면 빈 익명 헤더로 나가며, 서버측에서 거부된다.
# 기존 하드코딩 `"true"` 기본값은 v0.11.24 에서 `"false"` 로 교정 — 명시 주입 없이
# admin 권한을 얻는 경로를 닫는다.

def get_xgen_auth_headers(user_id: str = "") -> dict:
    """xgen 내부 서비스(documents / core / mcp) 호출용 인증 헤더.

    우선순위: 인자 `user_id` > ExecutionContext `user_id` > 빈 값.
    admin/superuser 플래그는 명시 주입 시에만 true. 기본값은 모두 false.
    """
    ctx_uid = get_extra("user_id", "") or ""
    ctx_admin = str(get_extra("user_is_admin", "false")).lower()
    ctx_super = str(get_extra("user_is_superuser", "false")).lower()
    uid = user_id or ctx_uid
    return {
        "x-user-id": str(uid),
        "x-user-name": get_extra("user_name", "harness") or "harness",
        "x-user-admin": "true" if ctx_admin == "true" else "false",
        "x-user-superuser": "true" if ctx_super == "true" else "false",
    }
