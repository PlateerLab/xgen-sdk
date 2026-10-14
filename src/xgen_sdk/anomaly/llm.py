"""LLM 호출 결과(성공 · 시간 초과 · 오류 · 취소)를 시스템 알람 이벤트로 남긴다: workflow · documents 공용.

core 의 "외부 LLM 연속 시간 초과" 규칙은 같은 대상(provider · model · 주소)의 결과를 순서대로 본다. 대상은
:func:`target_key` 의 해시이고, 주소의 질의 문자열 · 사용자 정보 같은 자격 증명은 남기지 않는다.

결과 기록은 :mod:`xgen_sdk.anomaly.telemetry` 대기열에 넣기만 한다. LLM 응답 · 취소 · 스트림은 기록을 기다리지
않는다.

시간 초과 판정
--------------
에이전트 런타임은 SDK 읽기 상한을 길게 두고 실제 상한을 ``asyncio.wait_for`` (스트림은 청크마다)로 건다. 그
상한이 걸리면 감싼 호출 안에는 ``CancelledError`` 가 들어온다. 사람이 [정지]를 눌러도 같은 예외다. 둘을 가르는
것은 그 뒤다: ``wait_for`` 의 시간 상한은 자기가 건 취소를 거둬들인다(``Task.uncancel``). 그래서 취소를 받은
순간의 취소 횟수(``Task.cancelling``)를 기억해 두고, 스택이 다 풀린 다음 차례에 다시 본다. 줄었으면 시간
상한이 거둔 것이니 ``timeout``, 그대로면 바깥에서 멈춘 것이니 ``cancelled`` 다.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import inspect
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from .telemetry import record_event

logger = logging.getLogger("xgen-sdk.anomaly-llm")

#: 대화형 CLI 실행기 - 호출 단위가 아니라 세션이라 세지 않는다.
CLI_PROVIDERS = frozenset({"claude_code", "codex"})
#: 대상에 남기는 지역 · 배포 이름 키(라우팅을 가르는 것만).
REGION_KEYS = frozenset({
    "region_name", "region", "aws_region", "project", "project_id", "location",
    "azure_endpoint", "deployment_name",
})
_TIMEOUT_NAMES = frozenset({
    "APITimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout",
    "PoolTimeout", "ReadTimeoutError", "ConnectTimeoutError", "DeadlineExceeded",
})

_active: "contextvars.ContextVar[bool]" = contextvars.ContextVar("xgen_anomaly_llm_active", default=False)
_sink: Dict[str, Any] = {"db": None, "service": "unknown"}


def configure(app_db: Any, *, service: str) -> None:
    """기록할 DB 와 서비스 이름. 기동 때 한 번."""
    _sink["db"], _sink["service"] = app_db, str(service)


def outcome(error: Optional[BaseException]) -> str:
    if error is None:
        return "success"
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    seen = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError) or type(current).__name__ in _TIMEOUT_NAMES or (
            type(current).__name__ == "AppLLMError" and getattr(current, "code", None) == "timeout"
        ):
            return "timeout"
        current = current.__cause__ or current.__context__
    return "error"


def target_key(provider: Any, model: Any, base_url: Any = None, credentials: Optional[Dict[str, Any]] = None) -> str:
    try:
        endpoint = urlsplit(str(base_url or ""))
        route = (endpoint.scheme, endpoint.hostname, endpoint.port, endpoint.path)
    except ValueError:
        route = ("invalid-endpoint",)
    # 질의 문자열 · 사용자 정보에는 API 키가 들 수 있다. 라우팅을 가르는 값의 해시만 남긴다.
    region = {k: str(v) for k, v in (credentials or {}).items() if k in REGION_KEYS and v}
    return hashlib.sha256(json.dumps([provider, model, route, region], sort_keys=True).encode()).hexdigest()


def emit(provider: Any, model: Any, base_url: Any = None, credentials: Optional[Dict[str, Any]] = None,
         error: Optional[BaseException] = None, event_id: Optional[str] = None) -> bool:
    """결과 하나를 대기열에 넣는다. 무엇이 잘못돼도 호출자에게 예외를 넘기지 않는다."""
    db = _sink["db"]
    if db is None:
        return False
    try:
        return record_event(
            db, "llm", service=_sink["service"], provider=str(provider or "unknown")[:100],
            model=str(model or "unknown")[:200], target=target_key(provider, model, base_url, credentials),
            outcome=outcome(error), event_id=event_id,
        )
    except Exception:  # noqa: BLE001 - 기록 실패가 LLM 결과를 바꾸지 않는다
        logger.debug("LLM outcome not recorded", exc_info=True)
        return False


def _settle_cancel(provider: Any, model: Any, base_url: Any, credentials: Optional[Dict[str, Any]],
                   error: BaseException) -> None:
    """취소를 받았을 때: 시간 상한이 거둔 취소면 timeout, 아니면 cancelled (모듈 설명 참고)."""
    try:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
    except RuntimeError:
        task = None
    if task is None:
        emit(provider, model, base_url, credentials, error=error)
        return
    before = task.cancelling()

    def decide() -> None:
        timed_out = task.cancelling() < before
        emit(provider, model, base_url, credentials, error=TimeoutError() if timed_out else error)

    loop.call_soon(decide)


@asynccontextmanager
async def call_scope(provider: Any, model: Any, base_url: Any = None, credentials: Optional[Dict[str, Any]] = None):
    """논리 호출 하나를 감싼다. 안쪽에서 다시 감싼 호출은 세지 않는다(같은 문맥)."""
    if _active.get() or provider in CLI_PROVIDERS:
        yield
        return
    token = _active.set(True)
    error: Optional[BaseException] = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            _active.reset(token)
        except ValueError:
            # 다른 문맥에서 닫힌 async generator(GC). 표시는 그 문맥에 남지 않는다.
            pass
        if isinstance(error, asyncio.CancelledError):
            _settle_cancel(provider, model, base_url, credentials, error)
        else:
            emit(provider, model, base_url, credentials, error=error)


def wrap_method(bound: Any, provider: Any, base_url: Any = None, credentials: Optional[Dict[str, Any]] = None) -> Any:
    """런타임 클라이언트의 ``create_message`` · ``create_message_stream`` 을 감싼다."""
    if getattr(bound, "__anomaly_wrapped__", False):
        return bound

    def model_of(kwargs: Dict[str, Any]) -> Any:
        config = kwargs.get("model_config")
        return getattr(config, "model", None) or kwargs.get("model") or "unknown"

    if inspect.isasyncgenfunction(bound):
        @functools.wraps(bound)
        async def wrapped(*args, **kwargs):
            async with call_scope(provider, model_of(kwargs), base_url, credentials):
                iterator = bound(*args, **kwargs)
                try:
                    async for item in iterator:
                        yield item
                finally:
                    await iterator.aclose()
    elif inspect.iscoroutinefunction(bound):
        @functools.wraps(bound)
        async def wrapped(*args, **kwargs):
            async with call_scope(provider, model_of(kwargs), base_url, credentials):
                return await bound(*args, **kwargs)
    else:
        raise TypeError("Unsupported runtime LLM call interface")
    wrapped.__anomaly_wrapped__ = True
    return wrapped


def wrap_factory(factory: Any) -> Any:
    """``build_client(provider, api_key, base_url, *, credentials)`` 가 만든 클라이언트의 호출을 감싼다."""
    if getattr(factory, "__anomaly_wrapped__", False):
        return factory

    @functools.wraps(factory)
    def build(provider, api_key, base_url, *, credentials=None):
        client = factory(provider, api_key, base_url, credentials=credentials)
        for name in ("create_message", "create_message_stream"):
            bound = getattr(client, name, None)
            if bound is not None:
                try:
                    setattr(client, name, wrap_method(bound, provider, base_url, credentials))
                except Exception:  # noqa: BLE001 - 감싸지 못해도 클라이언트는 그대로 쓴다
                    logger.debug("LLM client method not wrapped: %s", name, exc_info=True)
        return client

    build.__anomaly_wrapped__ = True
    return build


def attach_langchain(llm: Any, *, provider: Any, model: Any, base_url: Any = None) -> Any:
    """LangChain 모델에 결과 콜백을 붙인다. 붙이지 못해도 모델은 그대로 돌려준다."""
    try:
        from langchain_core.callbacks import BaseCallbackHandler

        # 라우팅 칸만 읽는다. 모델 객체를 직렬화하지 않는다(API 키를 들고 있다).
        base_url = next((getattr(llm, name, None) for name in (
            "azure_endpoint", "openai_api_base", "anthropic_api_url", "endpoint_url"
        ) if getattr(llm, name, None)), base_url)
        credentials = {name: getattr(llm, name) for name in (
            "region_name", "aws_region", "project", "location", "deployment_name"
        ) if getattr(llm, name, None)}

        class Outcomes(BaseCallbackHandler):
            # 비동기 실행에서도 실행기 스레드로 넘기지 않고 그 자리에서 부른다(대기열에 넣기만 한다).
            # 이게 없으면 LangChain 이 스트림 토큰마다 공용 실행기를 한 번씩 거친다.
            run_inline = True

            def on_llm_end(self, response, *, run_id, **kwargs):
                emit(provider, model, base_url, credentials=credentials, event_id=f"langchain:{run_id}")

            def on_llm_error(self, error, *, run_id, **kwargs):
                emit(provider, model, base_url, credentials=credentials, error=error, event_id=f"langchain:{run_id}")

        callbacks = list(getattr(llm, "callbacks", None) or [])
        if not any(getattr(callback, "_anomaly_outcomes", False) for callback in callbacks):
            callback = Outcomes()
            callback._anomaly_outcomes = True
            llm.callbacks = [*callbacks, callback]
    except Exception:  # noqa: BLE001 - 관측을 못 붙여도 모델 기능은 그대로다
        logger.warning("LLM outcome callback not attached (provider=%s)", provider)
    return llm


__all__ = [
    "CLI_PROVIDERS",
    "REGION_KEYS",
    "attach_langchain",
    "call_scope",
    "configure",
    "emit",
    "outcome",
    "target_key",
    "wrap_factory",
    "wrap_method",
]
