"""Gemini 이미지 어댑터 — ``POST /v1beta/models/{model}:generateContent`` 이미지 출력.

참조 이미지는 입력 parts(``inlineData``)로 싣는다(편집). 키는 ``x-goog-api-key``
헤더로만 보낸다 — 주소(``?key=``)에 실으면 오류 · 로그에 섞일 수 있다.
이미지 출력은 후보 1개라 n 장은 요청 n 번을 동시에 보낸다.
"""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from xgen_sdk.image_generation import _http
from xgen_sdk.image_generation.errors import (
    CODE_NO_OUTPUT,
    CODE_SAFETY_BLOCKED,
    ImageGenerationError,
)
from xgen_sdk.image_generation.models import (
    capabilities_for,
    default_display_name,
    is_provider_image_model,
    mark_recommended,
)
from xgen_sdk.image_generation.types import (
    OPERATION_EDIT,
    OPERATION_GENERATE,
    PROVIDER_GEMINI,
    GeneratedImage,
    ImageRequest,
    ImageResult,
    ImageUsage,
    ModelInfo,
)

PROVIDER = PROVIDER_GEMINI
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_MODEL_PAGES = 5
_SAFETY_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "BLOCKLIST", "SPII", "IMAGE_PROHIBITED_CONTENT"}


def _base(base_url: Optional[str]) -> str:
    return (base_url or "").strip() or DEFAULT_BASE_URL


def _headers(api_key: str) -> Dict[str, str]:
    return {"x-goog-api-key": api_key}


def _model_path(model: str) -> str:
    mid = (model or "").strip()
    if mid.startswith("models/"):
        mid = mid[len("models/"):]
    return quote(mid, safe="-._")


def build_body(request: ImageRequest) -> Dict[str, Any]:
    parts: List[Dict[str, Any]] = []
    for ref in request.reference_images:
        parts.append({"inlineData": {
            "mimeType": ref.mime or _http.sniff_mime(ref.data),
            "data": base64.b64encode(ref.data).decode("ascii"),
        }})
    parts.append({"text": request.prompt})
    generation_config: Dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
    image_config: Dict[str, Any] = {}
    if request.size:
        image_config["aspectRatio"] = request.size
    if request.quality:
        image_config["imageSize"] = request.quality
    if image_config:
        generation_config["imageConfig"] = image_config
    return {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation_config}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_usage(body: Dict[str, Any]) -> Optional[ImageUsage]:
    meta = body.get("usageMetadata") if isinstance(body, dict) else None
    if not isinstance(meta, dict):
        return None
    image_tokens: Optional[int] = None
    for d in meta.get("candidatesTokensDetails") or []:
        if isinstance(d, dict) and str(d.get("modality") or "").upper() == "IMAGE":
            image_tokens = (image_tokens or 0) + (_int(d.get("tokenCount")) or 0)
    return ImageUsage(
        input_tokens=_int(meta.get("promptTokenCount")),
        output_tokens=_int(meta.get("candidatesTokenCount")),
        image_tokens=image_tokens,
    )


def parse_images(body: Any) -> Tuple[List[GeneratedImage], str]:
    """응답 → (이미지들, 같이 온 글). 막힘은 ImageGenerationError(safety_blocked)."""
    if not isinstance(body, dict):
        raise ImageGenerationError(CODE_NO_OUTPUT, detail="Provider returned a non-JSON response.", provider=PROVIDER)
    feedback = body.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise ImageGenerationError(CODE_SAFETY_BLOCKED, detail=f"Prompt blocked: {feedback.get('blockReason')}", provider=PROVIDER)
    images: List[GeneratedImage] = []
    texts: List[str] = []
    finish_reasons: List[str] = []
    for cand in body.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        if cand.get("finishReason"):
            finish_reasons.append(str(cand.get("finishReason")))
        content = cand.get("content") if isinstance(cand.get("content"), dict) else {}
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                data = _http.b64decode(inline["data"])
                mime = inline.get("mimeType") or inline.get("mime_type") or _http.sniff_mime(data)
                if str(mime).startswith("image/"):
                    images.append(GeneratedImage(data=data, mime=str(mime)))
            elif isinstance(part.get("text"), str) and not part.get("thought"):
                texts.append(part["text"])
    text = "\n".join(t for t in texts if t.strip())
    if not images:
        if any(r.upper() in _SAFETY_FINISH for r in finish_reasons):
            raise ImageGenerationError(
                CODE_SAFETY_BLOCKED, detail=f"Generation stopped: {', '.join(finish_reasons)}", provider=PROVIDER,
            )
        detail = "Provider returned no image."
        if text:
            detail += " Model said: " + _http.clip(text, 500)
        raise ImageGenerationError(CODE_NO_OUTPUT, detail=detail, provider=PROVIDER)
    return images, text


async def _one(client: httpx.AsyncClient, url: str, key: str, body: Dict[str, Any]) -> Tuple[List[GeneratedImage], Optional[ImageUsage], str]:
    try:
        response = await client.post(url, headers=_headers(key), json=body)
    except httpx.HTTPError as exc:
        raise _http.classify_transport_error(PROVIDER, exc) from None
    if response.status_code >= 400:
        raise _http.classify_http_error(PROVIDER, response)
    payload = _http.response_json(response)
    images, text = parse_images(payload)
    return images, parse_usage(payload), text


async def generate(
    request: ImageRequest,
    *,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str] = None,
    timeout_s: float = 180.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> ImageResult:
    """생성(참조 없음) 또는 편집(참조 = 입력 parts). 일부 요청만 실패하면 받은 것만 돌려준다."""
    key = _http.require_key(PROVIDER, api_key)
    _http.check_prompt(PROVIDER, request.prompt)
    started = time.monotonic()
    n = max(1, int(request.n or 1))
    url = _http.join_url(_base(base_url), f"/v1beta/models/{_model_path(model)}:generateContent")
    body = build_body(request)
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport, follow_redirects=False) as client:
        outcomes = await asyncio.gather(*[_one(client, url, key, body) for _ in range(n)], return_exceptions=True)
    images: List[GeneratedImage] = []
    usage: Optional[ImageUsage] = None
    texts: List[str] = []
    errors: List[BaseException] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            errors.append(outcome)
            continue
        imgs, u, text = outcome
        images.extend(imgs)
        usage = u if usage is None else usage.add(u)
        if text:
            texts.append(text)
    if not images:
        # 예상 밖 예외도 그대로 올린다(호출부 버그를 감추지 않는다)
        raise errors[0] if errors else ImageGenerationError(CODE_NO_OUTPUT, provider=PROVIDER)
    metadata: Dict[str, Any] = {}
    if texts:
        metadata["text"] = _http.clip("\n".join(texts), 2000)
    if errors:
        metadata["partial_errors"] = [getattr(e, "code", type(e).__name__) for e in errors]
    return ImageResult(
        images=images,
        usage=usage,
        model=model,
        latency_ms=int((time.monotonic() - started) * 1000),
        provider=PROVIDER,
        operation=OPERATION_EDIT if request.reference_images else OPERATION_GENERATE,
        metadata=metadata,
    )


async def fetch_models(
    *,
    api_key: Optional[str],
    base_url: Optional[str] = None,
    timeout_s: float = 10.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> List[ModelInfo]:
    """``GET /v1beta/models`` 에서 generateContent 를 지원하는 ``gemini-*image*`` 만."""
    key = _http.require_key(PROVIDER, api_key)
    url = _http.join_url(_base(base_url), "/v1beta/models")
    out: List[ModelInfo] = []
    seen = set()
    page_token: Optional[str] = None
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport, follow_redirects=False) as client:
        for _ in range(MAX_MODEL_PAGES):
            params: Dict[str, Any] = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            try:
                response = await client.get(url, headers=_headers(key), params=params)
            except httpx.HTTPError as exc:
                raise _http.classify_transport_error(PROVIDER, exc) from None
            if response.status_code >= 400:
                raise _http.classify_http_error(PROVIDER, response)
            body = _http.response_json(response)
            if not isinstance(body, dict):
                raise ImageGenerationError("connection", detail="Model list response is not JSON.", provider=PROVIDER)
            for item in body.get("models") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                mid = name[len("models/"):] if name.startswith("models/") else name
                methods = item.get("supportedGenerationMethods") or []
                if not mid or mid in seen or "generateContent" not in methods:
                    continue
                if not is_provider_image_model(PROVIDER, mid):
                    continue
                seen.add(mid)
                label = item.get("displayName") if isinstance(item.get("displayName"), str) else ""
                out.append(ModelInfo(
                    provider=PROVIDER,
                    model_id=mid,
                    display_name=label.strip() or default_display_name(PROVIDER, mid),
                    capabilities=capabilities_for(PROVIDER, mid),
                    source="sync",
                ))
            page_token = body.get("nextPageToken") or None
            if not page_token:
                break
    out.sort(key=lambda m: m.model_id)
    return mark_recommended(PROVIDER, out)


__all__ = ["DEFAULT_BASE_URL", "build_body", "fetch_models", "generate", "parse_images", "parse_usage"]
