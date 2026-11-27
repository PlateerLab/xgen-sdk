"""제공자 어댑터가 같이 쓰는 HTTP 도우미 — 주소 잇기, MIME 판별, 오류 변환."""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from xgen_sdk.image_generation.errors import (
    CODE_AUTH,
    CODE_CONNECTION,
    CODE_INVALID_INPUT,
    CODE_QUOTA,
    CODE_SAFETY_BLOCKED,
    CODE_TIMEOUT,
    ImageGenerationError,
)

DETAIL_LIMIT = 1000
_KEYISH = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|AIza[0-9A-Za-z_\-]{10,}|key=[^&\s\"']+)")


def join_url(base_url: str, path: str) -> str:
    """base + path — 겹치는 마디를 두 번 붙이지 않는다(``.../v1`` + ``/v1/x`` → ``.../v1/x``).

    xgen-core ``service/llm/connection_probe.join_url`` 과 같은 규칙.
    """
    base = (base_url or "").strip().rstrip("/")
    seg = [p for p in (path or "").split("/") if p]
    if not base:
        return "/" + "/".join(seg)
    if not seg:
        return base
    tail = [p for p in (urlsplit(base).path or "").split("/") if p]
    overlap = 0
    for n in range(min(len(tail), len(seg)), 0, -1):
        if tail[-n:] == seg[:n]:
            overlap = n
            break
    return base + "/" + "/".join(seg[overlap:]) if seg[overlap:] else base


def redact(text: Any) -> str:
    """키처럼 보이는 조각을 가린다(제공자가 오류에 키 일부를 되돌려 주는 경우 대비)."""
    s = text if isinstance(text, str) else str(text)
    return _KEYISH.sub("***", s)


def clip(text: Any, limit: int = DETAIL_LIMIT) -> str:
    s = redact(text)
    return s if len(s) <= limit else s[:limit] + "…"


def sniff_mime(data: bytes, default: str = "image/png") -> str:
    head = data[:16] if data else b""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return default


def b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenerationError("no_output", detail="Provider returned an undecodable image payload.") from exc


def response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return None


def error_text(body: Any, response: Optional[httpx.Response] = None) -> str:
    """오류 본문에서 사람이 읽을 문장 하나."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("status") or err.get("code")
            if msg:
                return clip(msg)
        elif isinstance(err, str):
            return clip(err)
        if body.get("message"):
            return clip(body["message"])
    if response is not None:
        try:
            return clip(response.text)
        except Exception:  # noqa: BLE001
            return ""
    return ""


_SAFETY_HINTS = ("moderation", "safety", "content_policy", "content policy", "blocked", "prohibited")
_QUOTA_HINTS = ("insufficient_quota", "quota", "billing", "rate limit", "rate_limit", "resource_exhausted")
_AUTH_HINTS = ("api key not valid", "api_key_invalid", "invalid api key", "incorrect api key", "invalid_api_key")


def classify_http_error(provider: str, response: httpx.Response) -> ImageGenerationError:
    """HTTP 오류 응답 → 공통 오류."""
    status = response.status_code
    body = response_json(response)
    text = error_text(body, response)
    code_field = ""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        code_field = " ".join(str(err.get(k) or "") for k in ("code", "type", "status", "param")).lower()
    hay = (code_field + " " + text).lower()

    if status in (401, 403) or any(h in hay for h in _AUTH_HINTS):
        code = CODE_AUTH
    elif status == 429 or any(h in hay for h in _QUOTA_HINTS):
        code = CODE_QUOTA
    elif status in (400, 404, 409, 413, 415, 422):
        code = CODE_SAFETY_BLOCKED if any(h in hay for h in _SAFETY_HINTS) else CODE_INVALID_INPUT
    elif status in (408, 504):
        code = CODE_TIMEOUT
    else:
        code = CODE_CONNECTION
    return ImageGenerationError(code, detail=text or f"HTTP {status}", status_code=status, provider=provider)


def classify_transport_error(provider: str, exc: Exception) -> ImageGenerationError:
    if isinstance(exc, httpx.TimeoutException):
        return ImageGenerationError(CODE_TIMEOUT, detail=type(exc).__name__, provider=provider)
    # 요청 URL · 헤더가 섞일 수 있어 원문 대신 종류만 싣는다
    return ImageGenerationError(CODE_CONNECTION, detail=type(exc).__name__, provider=provider)


def require_key(provider: str, api_key: Optional[str]) -> str:
    from xgen_sdk.image_generation.errors import MSG_NO_KEY

    if not api_key or not str(api_key).strip():
        raise ImageGenerationError(CODE_AUTH, MSG_NO_KEY, detail="API key is not configured.", provider=provider)
    return str(api_key).strip()


def check_prompt(provider: str, prompt: Any) -> str:
    from xgen_sdk.image_generation.errors import MSG_EMPTY_PROMPT

    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageGenerationError(CODE_INVALID_INPUT, MSG_EMPTY_PROMPT, detail="prompt must be a non-empty string.", provider=provider)
    return prompt


__all__ = [
    "b64decode",
    "check_prompt",
    "classify_http_error",
    "classify_transport_error",
    "clip",
    "error_text",
    "join_url",
    "redact",
    "require_key",
    "response_json",
    "sniff_mime",
]
