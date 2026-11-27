"""OpenAI 이미지 어댑터 — ``POST /v1/images/generations`` · ``POST /v1/images/edits`` (gpt-image 계열).

gpt-image 모델은 결과를 항상 ``b64_json`` 으로 준다(``response_format`` 을 보내지 않는다).
url 만 오면 인증 헤더 없이 받아 온다.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from xgen_sdk.image_generation import _http
from xgen_sdk.image_generation.errors import CODE_NO_OUTPUT, ImageGenerationError
from xgen_sdk.image_generation.models import (
    capabilities_for,
    default_display_name,
    is_provider_image_model,
    mark_recommended,
)
from xgen_sdk.image_generation.types import (
    OPERATION_EDIT,
    OPERATION_GENERATE,
    PROVIDER_OPENAI,
    GeneratedImage,
    ImageRequest,
    ImageResult,
    ImageUsage,
    ModelInfo,
)

PROVIDER = PROVIDER_OPENAI
DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_DOWNLOAD_BYTES = 50_000_000
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def _base(base_url: Optional[str]) -> str:
    return (base_url or "").strip() or DEFAULT_BASE_URL


def _headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_usage(body: Dict[str, Any]) -> Optional[ImageUsage]:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None
    out_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    output_tokens = _int(usage.get("output_tokens"))
    image_tokens = _int(out_details.get("image_tokens"))
    if image_tokens is None:
        image_tokens = output_tokens
    return ImageUsage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=output_tokens,
        image_tokens=image_tokens,
    )


async def _collect_images(client: httpx.AsyncClient, body: Any) -> List[GeneratedImage]:
    items = body.get("data") if isinstance(body, dict) else None
    fmt = body.get("output_format") if isinstance(body, dict) else None
    default_mime = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}.get(
        str(fmt or "").lower(), "image/png"
    )
    images: List[GeneratedImage] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        data: Optional[bytes] = None
        if isinstance(item.get("b64_json"), str) and item["b64_json"]:
            data = _http.b64decode(item["b64_json"])
        elif isinstance(item.get("url"), str) and item["url"]:
            data = await _download(client, item["url"])
        if not data:
            continue
        revised = item.get("revised_prompt") if isinstance(item.get("revised_prompt"), str) else None
        images.append(GeneratedImage(data=data, mime=_http.sniff_mime(data, default_mime), revised_prompt=revised))
    return images


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    # 결과 url 은 서명된 공개 주소 — 인증 헤더를 싣지 않는다
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise ImageGenerationError(CODE_NO_OUTPUT, detail=f"Result download failed: HTTP {response.status_code}", provider=PROVIDER)
            chunks: List[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ImageGenerationError(CODE_NO_OUTPUT, detail="Result image is too large.", provider=PROVIDER)
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise _http.classify_transport_error(PROVIDER, exc) from None


def _ref_files(request: ImageRequest) -> List[Tuple[str, Tuple[str, bytes, str]]]:
    field = "image" if len(request.reference_images) == 1 else "image[]"
    files = []
    for i, ref in enumerate(request.reference_images, start=1):
        mime = ref.mime or _http.sniff_mime(ref.data)
        files.append((field, (f"reference_{i}.{_EXT.get(mime, 'png')}", ref.data, mime)))
    return files


async def generate(
    request: ImageRequest,
    *,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str] = None,
    timeout_s: float = 180.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> ImageResult:
    """생성(참조 이미지 없음) 또는 편집(있음). 입력 검증은 호출하는 쪽(``generate_image``)이 한다."""
    key = _http.require_key(PROVIDER, api_key)
    _http.check_prompt(PROVIDER, request.prompt)
    started = time.monotonic()
    edit = bool(request.reference_images)
    fields: Dict[str, Any] = {"model": model, "prompt": request.prompt, "n": max(1, int(request.n or 1))}
    if request.size:
        fields["size"] = request.size
    if request.quality:
        fields["quality"] = request.quality
    base = _base(base_url)
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport, follow_redirects=False) as client:
        try:
            if edit:
                url = _http.join_url(base, "/v1/images/edits")
                data = {k: str(v) for k, v in fields.items()}
                response = await client.post(url, headers=_headers(key), data=data, files=_ref_files(request))
            else:
                url = _http.join_url(base, "/v1/images/generations")
                response = await client.post(url, headers=_headers(key), json=fields)
        except httpx.HTTPError as exc:
            raise _http.classify_transport_error(PROVIDER, exc) from None
        if response.status_code >= 400:
            raise _http.classify_http_error(PROVIDER, response)
        body = _http.response_json(response)
        if not isinstance(body, dict):
            raise ImageGenerationError(CODE_NO_OUTPUT, detail="Provider returned a non-JSON response.", provider=PROVIDER)
        images = await _collect_images(client, body)
    if not images:
        raise ImageGenerationError(CODE_NO_OUTPUT, detail="Provider returned no images.", provider=PROVIDER)
    return ImageResult(
        images=images,
        usage=parse_usage(body),
        model=model,
        latency_ms=int((time.monotonic() - started) * 1000),
        provider=PROVIDER,
        operation=OPERATION_EDIT if edit else OPERATION_GENERATE,
    )


async def fetch_models(
    *,
    api_key: Optional[str],
    base_url: Optional[str] = None,
    timeout_s: float = 10.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> List[ModelInfo]:
    """``GET /v1/models`` 에서 gpt-image 계열만. 실패하면 ImageGenerationError."""
    key = _http.require_key(PROVIDER, api_key)
    url = _http.join_url(_base(base_url), "/v1/models")
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport, follow_redirects=False) as client:
        try:
            response = await client.get(url, headers=_headers(key))
        except httpx.HTTPError as exc:
            raise _http.classify_transport_error(PROVIDER, exc) from None
    if response.status_code >= 400:
        raise _http.classify_http_error(PROVIDER, response)
    body = _http.response_json(response)
    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ImageGenerationError("connection", detail="Model list response has no data array.", provider=PROVIDER)
    seen = set()
    out: List[ModelInfo] = []
    for item in items:
        mid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(mid, str) or mid in seen or not is_provider_image_model(PROVIDER, mid):
            continue
        seen.add(mid)
        out.append(ModelInfo(
            provider=PROVIDER,
            model_id=mid,
            display_name=default_display_name(PROVIDER, mid),
            capabilities=capabilities_for(PROVIDER, mid),
            source="sync",
        ))
    out.sort(key=lambda m: m.model_id)
    return mark_recommended(PROVIDER, out)


__all__ = ["DEFAULT_BASE_URL", "fetch_models", "generate", "parse_usage"]
