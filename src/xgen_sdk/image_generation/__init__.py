"""xgen_sdk.image_generation — 이미지 생성 제공자(openai · gemini · comfyui) 공용 계층 (2.31.0+).

xgen-core(관리 화면: 제공자 · 모델 카탈로그 · 연결 확인 · 동기화)와 xgen-workflow
(``tools/image_generation`` 노드)가 같은 규칙을 본다.

    types.py           ImageRequest · ImageResult · Credentials · Capabilities · ModelInfo
    errors.py          ImageGenerationError (code 7종 + 한국어 사용자 문구)
    models.py          이미지 모델 판별 · capabilities · 정적 FALLBACK
    credentials.py     resolve_credentials (개별 키 → LLM 설정 키), seal/open_api_key
    catalog.py         list_models · fetch_models · test_connection · generate_image
    openai_adapter     images.generate / images.edit (gpt-image 계열)
    gemini_adapter     generateContent 이미지 출력(참조 이미지 = 입력 parts)
    comfyui_adapter    comfyui.run_workflow 감싸기(매핑 v1 은 편집 미지원)

Usage:
    from xgen_sdk.image_generation import ImageRequest, resolve_credentials, generate_image

    creds = resolve_credentials("openai", app_db)
    result = await generate_image("openai", ImageRequest(prompt="a cat", size="1024x1024"),
                                  model="gpt-image-1", credentials=creds)
    result.images[0].data, result.usage.image_tokens, result.latency_ms
"""
from xgen_sdk.image_generation import comfyui_adapter, gemini_adapter, openai_adapter
from xgen_sdk.image_generation.catalog import (
    fetch_models,
    generate_image,
    list_models,
    test_connection,
    validate_request,
)
from xgen_sdk.image_generation.comfyui_adapter import comfyui_capabilities, supports_edit as comfyui_supports_edit
from xgen_sdk.image_generation.credentials import (
    CREDENTIAL_MODE_CUSTOM,
    CREDENTIAL_MODE_LLM,
    SecretUnreadable,
    llm_credentials,
    open_api_key,
    resolve_credentials,
    seal_api_key,
)
from xgen_sdk.image_generation.errors import (
    ERROR_CODES,
    USER_MESSAGES,
    ImageGenerationError,
    from_comfyui_error,
    user_message,
)
from xgen_sdk.image_generation.models import (
    FALLBACK_MODELS,
    capabilities_for,
    fallback_models,
    is_image_model,
    is_provider_image_model,
)
from xgen_sdk.image_generation.types import (
    OPERATION_EDIT,
    OPERATION_GENERATE,
    PROVIDER_COMFYUI,
    PROVIDER_DISPLAY_NAMES,
    PROVIDER_GEMINI,
    PROVIDER_KINDS,
    PROVIDER_OPENAI,
    PROVIDER_SORT_ORDER,
    PROVIDERS,
    Capabilities,
    Credentials,
    GeneratedImage,
    ImageRequest,
    ImageResult,
    ImageUsage,
    ModelInfo,
    ReferenceImage,
)

__all__ = [
    # 타입
    "Capabilities",
    "Credentials",
    "GeneratedImage",
    "ImageRequest",
    "ImageResult",
    "ImageUsage",
    "ModelInfo",
    "ReferenceImage",
    "PROVIDERS",
    "PROVIDER_OPENAI",
    "PROVIDER_GEMINI",
    "PROVIDER_COMFYUI",
    "PROVIDER_KINDS",
    "PROVIDER_DISPLAY_NAMES",
    "PROVIDER_SORT_ORDER",
    "OPERATION_GENERATE",
    "OPERATION_EDIT",
    # 오류
    "ERROR_CODES",
    "USER_MESSAGES",
    "ImageGenerationError",
    "from_comfyui_error",
    "user_message",
    # 모델
    "FALLBACK_MODELS",
    "capabilities_for",
    "fallback_models",
    "is_image_model",
    "is_provider_image_model",
    "fetch_models",
    "list_models",
    "test_connection",
    # 자격증명
    "CREDENTIAL_MODE_CUSTOM",
    "CREDENTIAL_MODE_LLM",
    "SecretUnreadable",
    "llm_credentials",
    "open_api_key",
    "resolve_credentials",
    "seal_api_key",
    # 실행
    "generate_image",
    "validate_request",
    "comfyui_capabilities",
    "comfyui_supports_edit",
    "openai_adapter",
    "gemini_adapter",
    "comfyui_adapter",
]
