"""제공자 묶음 — 모델 목록(sync + FALLBACK), 연결 확인, 생성 · 편집 진입점."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

import httpx

from xgen_sdk.image_generation import gemini_adapter, openai_adapter
from xgen_sdk.image_generation.errors import (
    CODE_INVALID_INPUT,
    MSG_EDIT_UNSUPPORTED,
    MSG_TOO_MANY_REFERENCES,
    MSG_UNKNOWN_PROVIDER,
    ImageGenerationError,
)
from xgen_sdk.image_generation.models import capabilities_for, fallback_models
from xgen_sdk.image_generation.types import (
    PROVIDER_COMFYUI,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    Capabilities,
    Credentials,
    ImageRequest,
    ImageResult,
    ModelInfo,
)

logger = logging.getLogger("xgen-sdk.image_generation")

_ADAPTERS = {PROVIDER_OPENAI: openai_adapter, PROVIDER_GEMINI: gemini_adapter}
CredsLike = Union[Credentials, Dict[str, Any], None]


def _creds(creds: CredsLike) -> Credentials:
    if isinstance(creds, Credentials):
        return creds
    if isinstance(creds, dict):
        return Credentials(api_key=creds.get("api_key"), base_url=creds.get("base_url"), source=str(creds.get("source") or "none"))
    return Credentials()


def _caps(value: Union[Capabilities, Dict[str, Any], None], provider: str, model: str) -> Capabilities:
    if isinstance(value, Capabilities):
        return value
    if isinstance(value, dict):
        base = capabilities_for(provider, model)
        return Capabilities(
            generate=bool(value.get("generate", base.generate)),
            edit=bool(value.get("edit", base.edit)),
            sizes=list(value.get("sizes") or []),
            qualities=list(value.get("qualities") or []),
            max_n=int(value.get("max_n") or base.max_n),
            max_reference_images=int(value.get("max_reference_images") or 0),
        )
    return capabilities_for(provider, model)


async def fetch_models(
    provider: str,
    creds: CredsLike,
    *,
    timeout_s: float = 10.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> List[ModelInfo]:
    """제공자 API 로 모델 목록 조회(source ``sync``). 실패하면 ImageGenerationError — 연결 확인용.

    comfyui 는 모델 대신 등록 워크플로우라 빈 목록.
    """
    p = (provider or "").strip().lower()
    if p == PROVIDER_COMFYUI:
        return []
    adapter = _ADAPTERS.get(p)
    if adapter is None:
        raise ImageGenerationError(CODE_INVALID_INPUT, MSG_UNKNOWN_PROVIDER, detail=f"Unknown provider: {provider}")
    c = _creds(creds)
    return await adapter.fetch_models(api_key=c.api_key, base_url=c.base_url, timeout_s=timeout_s, transport=transport)


async def list_models(
    provider: str,
    creds: CredsLike = None,
    *,
    timeout_s: float = 10.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> List[ModelInfo]:
    """capabilities 포함 모델 목록. 키가 없거나 조회가 실패하거나 결과가 비면 정적 FALLBACK
    (source ``fallback``). 예외를 올리지 않는다. 한 모델에 ``recommended=True``.
    """
    p = (provider or "").strip().lower()
    if p not in _ADAPTERS:
        return []
    c = _creds(creds)
    if not c.api_key:
        return fallback_models(p)
    try:
        models = await fetch_models(p, c, timeout_s=timeout_s, transport=transport)
    except ImageGenerationError as exc:
        logger.warning("image_generation: model list failed (%s): %s", p, exc.code)
        return fallback_models(p)
    return models or fallback_models(p)


async def test_connection(
    provider: str,
    creds: CredsLike,
    *,
    timeout_s: float = 10.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, Any]:
    """연결 확인 — 모델 목록 1회 조회. ``{ok, model_count, code, message}`` (message 는 사용자 문구)."""
    try:
        models = await fetch_models(provider, creds, timeout_s=timeout_s, transport=transport)
    except ImageGenerationError as exc:
        return {"ok": False, "model_count": 0, "code": exc.code, "message": exc.message}
    return {"ok": True, "model_count": len(models), "code": None, "message": "이미지 생성 서비스에 연결했습니다."}


def validate_request(
    provider: str,
    model: str,
    request: ImageRequest,
    capabilities: Union[Capabilities, Dict[str, Any], None] = None,
) -> Capabilities:
    """capabilities 로 요청을 검사한다. 어기면 invalid_input (detail 은 모델이 읽을 영어)."""
    caps = _caps(capabilities, provider, model)
    n = int(request.n or 1)
    if n < 1 or n > max(1, caps.max_n):
        raise ImageGenerationError(
            CODE_INVALID_INPUT, detail=f"n must be between 1 and {max(1, caps.max_n)}.", provider=provider,
        )
    if request.size and caps.sizes and request.size not in caps.sizes:
        raise ImageGenerationError(
            CODE_INVALID_INPUT, detail=f"Unsupported size '{request.size}'. Allowed: {', '.join(caps.sizes)}.", provider=provider,
        )
    if request.size and not caps.sizes:
        raise ImageGenerationError(CODE_INVALID_INPUT, detail="This model does not accept a size option.", provider=provider)
    if request.quality and caps.qualities and request.quality not in caps.qualities:
        raise ImageGenerationError(
            CODE_INVALID_INPUT,
            detail=f"Unsupported quality '{request.quality}'. Allowed: {', '.join(caps.qualities)}.", provider=provider,
        )
    if request.quality and not caps.qualities:
        raise ImageGenerationError(CODE_INVALID_INPUT, detail="This model does not accept a quality option.", provider=provider)
    refs = request.reference_images
    if refs:
        if not caps.edit:
            raise ImageGenerationError(
                CODE_INVALID_INPUT, MSG_EDIT_UNSUPPORTED, detail="This model does not support reference_images.", provider=provider,
            )
        if len(refs) > caps.max_reference_images:
            raise ImageGenerationError(
                CODE_INVALID_INPUT, MSG_TOO_MANY_REFERENCES,
                detail=f"At most {caps.max_reference_images} reference images are allowed.", provider=provider,
            )
        if any(not r.data for r in refs):
            raise ImageGenerationError(CODE_INVALID_INPUT, detail="A reference image is empty.", provider=provider)
    elif not caps.generate:
        raise ImageGenerationError(CODE_INVALID_INPUT, detail="This model only edits images; pass reference_images.", provider=provider)
    return caps


async def generate_image(
    provider: str,
    request: ImageRequest,
    *,
    model: str,
    credentials: CredsLike,
    capabilities: Union[Capabilities, Dict[str, Any], None] = None,
    timeout_s: float = 180.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> ImageResult:
    """모델형 제공자(openai · gemini)로 생성 · 편집. 참조 이미지가 있으면 편집.

    ``capabilities`` 는 모델 카탈로그 행의 값(비우면 ``capabilities_for``). comfyui 는
    ``xgen_sdk.image_generation.comfyui_adapter.generate`` 를 쓴다.

    Raises:
        ImageGenerationError: 7가지 code 중 하나.
    """
    p = (provider or "").strip().lower()
    adapter = _ADAPTERS.get(p)
    if adapter is None:
        raise ImageGenerationError(
            CODE_INVALID_INPUT, MSG_UNKNOWN_PROVIDER,
            detail=f"Provider '{provider}' is not a model provider (comfyui uses comfyui_adapter.generate).",
        )
    validate_request(p, model, request, capabilities)
    c = _creds(credentials)
    return await adapter.generate(
        request, model=model, api_key=c.api_key, base_url=c.base_url, timeout_s=timeout_s, transport=transport,
    )


__all__ = ["fetch_models", "generate_image", "list_models", "test_connection", "validate_request"]
