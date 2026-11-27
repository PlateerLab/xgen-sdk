"""이미지 모델 판별 · capabilities · 정적 FALLBACK.

네트워크는 쓰지 않는다(조회는 각 어댑터의 ``fetch_models``, 묶음은 ``catalog.list_models``).
``xgen_sdk.llm_catalog`` 도 ``is_image_model`` 로 채팅 목록에서 이미지 전용 모델을 뺀다.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from xgen_sdk.image_generation.types import (
    PROVIDER_COMFYUI,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    Capabilities,
    ModelInfo,
)

# gpt-image-1 · chatgpt-image-latest · gemini-2.5-flash-image · gemini-3-pro-image-preview ·
# gemini-2.0-flash-preview-image-generation · imagen-4.0-generate-001 · dall-e-3
_IMAGE_MODEL_RE = re.compile(r"(^|[-_./])image([-_.]|$)|^imagen|^dall-e", re.IGNORECASE)
_OPENAI_IMAGE_RE = re.compile(r"^(gpt-image-|chatgpt-image)", re.IGNORECASE)
_GEMINI_IMAGE_RE = re.compile(r"^gemini-.*image", re.IGNORECASE)

OPENAI_SIZES = ["auto", "1024x1024", "1536x1024", "1024x1536"]
OPENAI_QUALITIES = ["auto", "low", "medium", "high"]
OPENAI_MAX_N = 10
OPENAI_MAX_REFERENCES = 16

GEMINI_ASPECT_RATIOS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
GEMINI_IMAGE_SIZES = ["1K", "2K", "4K"]
GEMINI_MAX_N = 4  # 이미지 출력은 후보 1개 — n 장은 요청 n 번으로 만든다

# 동기화 · fallback 목록에서 '권장 기본 모델' 1개를 고르는 순서
RECOMMENDED_ORDER: Dict[str, List[str]] = {
    PROVIDER_OPENAI: ["gpt-image-1", "gpt-image-1.5", "gpt-image-1-mini"],
    PROVIDER_GEMINI: ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"],
}

_OPENAI_LABELS = {
    "gpt-image-1": "GPT Image 1",
    "gpt-image-1.5": "GPT Image 1.5",
    "gpt-image-1-mini": "GPT Image 1 Mini",
    "chatgpt-image-latest": "ChatGPT Image (latest)",
}
_GEMINI_LABELS = {
    "gemini-2.5-flash-image": "Nano Banana (Gemini 2.5 Flash Image)",
    "gemini-3-pro-image-preview": "Nano Banana Pro (Gemini 3 Pro Image Preview)",
}

# 정적 FALLBACK — 제공자 API 조회가 안 될 때(키 없음 · 실패 · 결과 없음)
FALLBACK_MODELS: Dict[str, List[Dict[str, str]]] = {
    PROVIDER_OPENAI: [
        {"model_id": "gpt-image-1", "display_name": _OPENAI_LABELS["gpt-image-1"]},
        {"model_id": "gpt-image-1.5", "display_name": _OPENAI_LABELS["gpt-image-1.5"]},
        {"model_id": "gpt-image-1-mini", "display_name": _OPENAI_LABELS["gpt-image-1-mini"]},
    ],
    PROVIDER_GEMINI: [
        {"model_id": "gemini-2.5-flash-image", "display_name": _GEMINI_LABELS["gemini-2.5-flash-image"]},
        {"model_id": "gemini-3-pro-image-preview", "display_name": _GEMINI_LABELS["gemini-3-pro-image-preview"]},
    ],
    PROVIDER_COMFYUI: [],
}


def is_image_model(model_id: Optional[str]) -> bool:
    """이미지 전용 모델인가 — ``gpt-image-*`` · ``*-image*`` · ``imagen*`` · ``dall-e*``."""
    return bool(model_id) and bool(_IMAGE_MODEL_RE.search(str(model_id).strip()))


def is_provider_image_model(provider: str, model_id: Optional[str]) -> bool:
    """이 SDK 어댑터가 다룰 수 있는 이미지 모델인가.

    openai: gpt-image 계열(dall-e 는 제외). gemini: generateContent 로 이미지를 내는
    ``gemini-*image*`` (imagen 은 predict API 라 대상 밖).
    """
    mid = (model_id or "").strip()
    if provider == PROVIDER_OPENAI:
        return bool(_OPENAI_IMAGE_RE.search(mid))
    if provider == PROVIDER_GEMINI:
        return bool(_GEMINI_IMAGE_RE.search(mid))
    return False


def default_display_name(provider: str, model_id: str) -> str:
    if provider == PROVIDER_OPENAI:
        return _OPENAI_LABELS.get(model_id, model_id)
    if provider == PROVIDER_GEMINI:
        return _GEMINI_LABELS.get(model_id, model_id)
    return model_id


def capabilities_for(provider: str, model_id: str) -> Capabilities:
    """모델 id 로 capabilities 를 정한다(제공자 목록 API 는 이 정보를 주지 않는다)."""
    mid = (model_id or "").strip().lower()
    if provider == PROVIDER_OPENAI:
        return Capabilities(
            generate=True,
            edit=True,
            sizes=list(OPENAI_SIZES),
            qualities=list(OPENAI_QUALITIES),
            max_n=OPENAI_MAX_N,
            max_reference_images=OPENAI_MAX_REFERENCES,
        )
    if provider == PROVIDER_GEMINI:
        if mid.startswith("gemini-2.0"):
            # 2.0 실험 이미지 모델은 imageConfig 를 받지 않는다
            return Capabilities(generate=True, edit=True, sizes=[], qualities=[], max_n=GEMINI_MAX_N, max_reference_images=3)
        if mid.startswith("gemini-2"):
            return Capabilities(
                generate=True, edit=True, sizes=list(GEMINI_ASPECT_RATIOS), qualities=[],
                max_n=GEMINI_MAX_N, max_reference_images=3,
            )
        # gemini-3 이후(Nano Banana Pro 등): 해상도 1K · 2K · 4K, 참조 최대 14장
        return Capabilities(
            generate=True, edit=True, sizes=list(GEMINI_ASPECT_RATIOS), qualities=list(GEMINI_IMAGE_SIZES),
            max_n=GEMINI_MAX_N, max_reference_images=14,
        )
    return Capabilities(generate=True, edit=False, sizes=[], qualities=[], max_n=1, max_reference_images=0)


def mark_recommended(provider: str, models: List[ModelInfo]) -> List[ModelInfo]:
    """목록에서 권장 기본 모델 하나에만 ``recommended=True``."""
    for m in models:
        m.recommended = False
    if not models:
        return models
    ids = {m.model_id: m for m in models}
    for candidate in RECOMMENDED_ORDER.get(provider, []):
        if candidate in ids:
            ids[candidate].recommended = True
            return models
    models[0].recommended = True
    return models


def fallback_models(provider: str) -> List[ModelInfo]:
    out = [
        ModelInfo(
            provider=provider,
            model_id=row["model_id"],
            display_name=row["display_name"],
            capabilities=capabilities_for(provider, row["model_id"]),
            source="fallback",
        )
        for row in FALLBACK_MODELS.get(provider, [])
    ]
    return mark_recommended(provider, out)


__all__ = [
    "FALLBACK_MODELS",
    "GEMINI_ASPECT_RATIOS",
    "GEMINI_IMAGE_SIZES",
    "GEMINI_MAX_N",
    "OPENAI_MAX_N",
    "OPENAI_MAX_REFERENCES",
    "OPENAI_QUALITIES",
    "OPENAI_SIZES",
    "RECOMMENDED_ORDER",
    "capabilities_for",
    "default_display_name",
    "fallback_models",
    "is_image_model",
    "is_provider_image_model",
    "mark_recommended",
]
