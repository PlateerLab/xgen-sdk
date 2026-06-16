"""
LLM Providers — 프로바이더 레지스트리 + 팩토리

새 프로바이더 추가:
    from xgen_harness.providers import register_provider
    register_provider("bedrock", BedrockProvider)

사용:
    from xgen_harness.providers import create_provider
    provider = create_provider("anthropic", api_key, model)
"""

import logging
import os
from typing import Optional, Type

from .base import LLMProvider, ProviderEvent, ProviderEventType

logger = logging.getLogger("harness.providers")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  프로바이더 레지스트리
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_REGISTRY: dict[str, Type[LLMProvider]] = {}

# API 키 환경변수 매핑 — 단일 진실 소스
PROVIDER_API_KEY_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "bedrock": "AWS_ACCESS_KEY_ID",
    "vllm": "VLLM_API_KEY",
}

# 프로바이더별 기본 모델
PROVIDER_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.0-flash",
}

# 프로바이더별 컨텍스트 한도 (문자 수 기준, ~4 chars/token 추정).
# s07_llm 의 중간축약 기준. 외부 프로바이더는 register_provider(..., context_limit=…) 로 등록.
PROVIDER_CONTEXT_LIMITS: dict[str, int] = {
    "anthropic": 500_000,
    "openai": 500_000,
    "google": 500_000,
    "bedrock": 500_000,
    "vllm": 50_000,
}

# 컨텍스트 한도 미등록 프로바이더용 공통 기본값.
# 외부에서 os.environ["XGEN_HARNESS_DEFAULT_CONTEXT_LIMIT"] 로 override 가능.
DEFAULT_CONTEXT_LIMIT_CHARS = 500_000

# 프로바이더별 추가 모델 목록 — UI 드롭다운 동적 렌더용.
# 기본 모델(PROVIDER_DEFAULT_MODEL)은 자동으로 맨 앞에 포함됨.
# 새 provider 등록 시 이 dict 에 append → stage_config / harness.py 가 자동 반영.
PROVIDER_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-haiku-4-5-20251001",
    ],
    "openai": [
        "gpt-4o-mini",
        "gpt-4o",
        "o3-mini",
    ],
    "google": [
        "gemini-2.0-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
    "bedrock": [],
    "vllm": [],
}


# 프로바이더별 기본 base_url — OpenAI 호환 엔드포인트.
#
# ⚠️ google/bedrock/vllm 은 기본적으로 OpenAIProvider(httpx) 가 OpenAI 호환
# 엔드포인트로 호출하는 **shim** 이다 (네이티브 SDK 아님). 이 매핑이 없으면
# create_provider("google") 가 base_url=None → api.openai.com 으로 떨어져
# Gemini 모델명을 OpenAI 에 보내는 조용한 오작동이 난다 (연동성 결함).
#
# 네이티브 통합(예: Gemini thinking blocks, Bedrock SigV4)이 필요하면
# `register_provider("google", NativeGeminiProvider)` 또는 entry_points
# `xgen_harness.providers` 로 **코어 수정 없이** 교체한다 — 그때 이 매핑은
# 무시된다. 즉 기본은 compat-shim, 고급은 plugin override (PHILOSOPHY ②연동성).
PROVIDER_DEFAULT_BASE_URL: dict[str, str] = {
    # Gemini OpenAI 호환 엔드포인트 (Google AI Studio).
    # ⚠️ 반드시 .../chat/completions 까지 전체 경로 — normalize_base_url 이 짧은 base 에
    #    `/v1/chat/completions` 를 붙이는데 Gemini 경로는 `/v1beta/openai/chat/completions`
    #    라 `/v1/` 가 잘못 끼어 404 가 난다 (v1.18.1 에서 `.../openai/` 만 줘서 깨졌던 것).
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    # vLLM 은 배포 위치가 환경마다 달라 기본값을 박지 않음 — VLLM_API_BASE_URL
    # 또는 register_provider(base_url=) 로 주입. (로컬 기본은 의도적으로 비움)
    "vllm": "",
    # Bedrock 은 직접 호출이 SigV4 라 OpenAI 호환 프록시 URL 이 필요 —
    # BEDROCK_API_BASE_URL 로 주입. 기본값 없음.
    "bedrock": "",
}


def get_provider_base_url(provider: str) -> Optional[str]:
    """프로바이더의 base_url 해석 (create_provider 가 base_url 미지정 시 사용).

    우선순위:
      1) ``{PROVIDER_UPPER}_API_BASE_URL`` env (예: ``GOOGLE_API_BASE_URL``)
      2) ``PROVIDER_DEFAULT_BASE_URL`` 레지스트리 (register_provider 로 외부 등록)
      3) None → 호출 provider 클래스의 자체 기본값 (OpenAIProvider=api.openai.com)

    엔진은 이 헬퍼만 사용 — 하드코딩 URL 직접 접근 금지 (PHILOSOPHY ③무하드코딩).
    """
    key = (provider or "").lower()
    env_val = os.environ.get(f"{key.upper()}_API_BASE_URL", "").strip()
    if env_val:
        return env_val
    registry_val = PROVIDER_DEFAULT_BASE_URL.get(key, "").strip()
    return registry_val or None


def get_provider_models(provider: str) -> list[str]:
    """프로바이더별 모델 목록 (기본 모델 포함, 중복 제거).

    UI 가 이 목록을 드롭다운으로 렌더. 새 provider 추가 시
    PROVIDER_MODELS 에 append → 자동 반영.
    """
    models: list[str] = []
    default = get_default_model(provider)  # env override 존중
    if default:
        models.append(default)
    for m in PROVIDER_MODELS.get(provider.lower(), []):
        if m and m not in models:
            models.append(m)
    return models


def register_provider(
    name: str,
    cls: Type[LLMProvider],
    *,
    default_model: Optional[str] = None,
    models: Optional[list[str]] = None,
    api_key_env: Optional[str] = None,
    context_limit: Optional[int] = None,
    base_url: Optional[str] = None,
) -> None:
    """프로바이더 등록. 기존 이름이면 덮어씀.

    외부 패키지(예: xgen-bedrock-provider) 는 단 한번의 호출로 UI 드롭다운 /
    API key 탐색 / 컨텍스트 한도 / 기본 base_url 까지 선언할 수 있다. 레지스트리만
    건드리므로 엔진 소스 수정 0.

    Args:
        name: 프로바이더 식별자 (소문자 권장).
        cls: LLMProvider 서브클래스.
        default_model: UI/하네스 기본값. None 이면 기존 매핑 유지.
        models: UI 드롭다운에 표시할 추가 모델 목록.
        api_key_env: API 키 환경변수명 (예: ``MY_PROVIDER_API_KEY``).
        context_limit: 문자 수 기준 컨텍스트 한도 (s07 중간축약 기준).
        base_url: 기본 base_url (OpenAI 호환 shim 의 엔드포인트). 네이티브
            provider 클래스면 보통 불필요 — 클래스가 자체 URL 을 안다.
    """
    key = name.lower()
    _REGISTRY[key] = cls
    if default_model is not None:
        PROVIDER_DEFAULT_MODEL[key] = default_model
    if models is not None:
        PROVIDER_MODELS[key] = list(models)
    if api_key_env is not None:
        PROVIDER_API_KEY_MAP[key] = api_key_env
    if context_limit is not None:
        PROVIDER_CONTEXT_LIMITS[key] = int(context_limit)
    if base_url is not None:
        PROVIDER_DEFAULT_BASE_URL[key] = base_url
    logger.debug("Provider registered: %s → %s", name, cls.__name__)


def get_context_limit(provider: str) -> int:
    """프로바이더의 컨텍스트 한도(문자 수) 조회.

    우선순위:
      1) PROVIDER_CONTEXT_LIMITS 에 등록된 값
      2) XGEN_HARNESS_DEFAULT_CONTEXT_LIMIT env
      3) DEFAULT_CONTEXT_LIMIT_CHARS

    엔진은 이 헬퍼만 사용 — 하드코딩 딕셔너리에 직접 접근 금지 (PHILOSOPHY §2 s07).
    """
    key = (provider or "").lower()
    if key in PROVIDER_CONTEXT_LIMITS:
        return int(PROVIDER_CONTEXT_LIMITS[key])
    env_default = os.environ.get("XGEN_HARNESS_DEFAULT_CONTEXT_LIMIT", "").strip()
    if env_default.isdigit():
        return int(env_default)
    return DEFAULT_CONTEXT_LIMIT_CHARS


def create_provider(
    name: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
) -> LLMProvider:
    """프로바이더 인스턴스 생성. 레지스트리에서 조회."""
    key = name.lower()

    if key not in _REGISTRY:
        _register_defaults()

    cls = _REGISTRY.get(key)
    if cls is None:
        logger.warning("Unknown provider '%s', falling back to OpenAI-compatible", name)
        cls = _REGISTRY.get("openai")
        if cls is None:
            raise ValueError(f"Provider '{name}' not registered and no OpenAI fallback")

    if base_url is None:
        # env > PROVIDER_DEFAULT_BASE_URL 레지스트리 > None(클래스 자체 기본).
        # google/vllm/bedrock 같은 OpenAI 호환 shim 이 엔드포인트를 못 찾아
        # api.openai.com 으로 떨어지던 조용한 오작동을 막는다.
        base_url = get_provider_base_url(name)

    return cls(api_key, model, base_url)


def get_api_key_env(provider: str) -> str:
    """프로바이더의 API 키 환경변수명 반환."""
    return PROVIDER_API_KEY_MAP.get(provider.lower(), f"{provider.upper()}_API_KEY")


def resolve_api_key_from_file(provider: str) -> Optional[str]:
    """파일 기반 API 키 폴백 (Docker 환경 등 env 주입이 어려울 때).

    기본 경로: ``/app/config/{env_var.lower()}.txt``.
    ``XGEN_HARNESS_API_KEY_FILE_DIR`` 환경변수로 override 가능 — 이식 측(Docker 외)이
    다른 경로를 쓸 때 엔진 코드 수정 없이 재지정. 파일 없으면 None.

    Stage 에서 직접 경로를 박지 않고 이 레지스트리 헬퍼를 호출하는 단일 경로를 둠
    (PHILOSOPHY §1 "책임 침범 금지" — API key 해석 경로는 providers 레지스트리가 소유).
    """
    env_var = get_api_key_env(provider)
    base_dir = os.environ.get("XGEN_HARNESS_API_KEY_FILE_DIR", "/app/config").rstrip("/")
    filepath = f"{base_dir}/{env_var.lower()}.txt"
    try:
        if os.path.exists(filepath):
            with open(filepath) as f:
                return f.read().strip() or None
    except OSError as e:
        logger.debug("[providers] API key file read failed (%s): %s", filepath, e)
    return None


def get_default_model(provider: str) -> str:
    """프로바이더의 기본 모델명 반환.

    v0.22.0 — env override 우선. 모델 업그레이드를 코드 수정 없이 런타임에 반영.

    우선순위:
      1) ``XGEN_HARNESS_{PROVIDER_UPPER}_DEFAULT_MODEL`` env
         (예: ``XGEN_HARNESS_ANTHROPIC_DEFAULT_MODEL=claude-sonnet-4-6-20260101``)
      2) ``PROVIDER_DEFAULT_MODEL`` 레지스트리 (register_provider 로 외부 교체 가능)
      3) 빈 문자열 (상위 호출자가 폴백 결정)
    """
    key = (provider or "").lower()
    if key:
        env_name = f"XGEN_HARNESS_{key.upper()}_DEFAULT_MODEL"
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
    return PROVIDER_DEFAULT_MODEL.get(key, "")


def list_providers() -> list[str]:
    """등록된 프로바이더 이름 목록."""
    _register_defaults()
    return list(_REGISTRY.keys())


def get_default_provider() -> str:
    """하네스 런타임 기본 프로바이더 해석.

    우선순위:
      1) ``XGEN_HARNESS_DEFAULT_PROVIDER`` env (등록된 프로바이더일 때만)
      2) 선호 목록(openai → anthropic) 중 등록된 첫 항목
      3) 레지스트리 첫 항목
      4) 최종 fallback ``"openai"``

    사용자 선호(2026-04-20): 기본값은 열린 상태로 유지하되,
    명시 설정이 없을 때 openai 를 우선한다. 선택권은 UI 에서 항상 열려있음.
    """
    _register_defaults()

    env = os.environ.get("XGEN_HARNESS_DEFAULT_PROVIDER", "").strip().lower()
    if env and env in _REGISTRY:
        return env

    for preferred in ("openai", "anthropic"):
        if preferred in _REGISTRY:
            return preferred

    if _REGISTRY:
        return next(iter(_REGISTRY.keys()))

    return "openai"


def _register_defaults() -> None:
    """빌트인 프로바이더 등록.

    - anthropic: Anthropic Messages API (httpx SSE) — **네이티브**
    - openai:    OpenAI Chat Completions API (httpx SSE) — **네이티브**
    - google:    Gemini → OpenAI 호환 엔드포인트 — **compat shim** (OpenAIProvider)
    - bedrock:   AWS Bedrock → OpenAI 호환 (프록시) — **compat shim**
    - vllm:      vLLM → OpenAI 호환 엔드포인트 — **compat shim**

    ⚠️ google/bedrock/vllm 은 OpenAIProvider 를 base_url 만 바꿔 재사용하는 shim 이다.
    Gemini thinking blocks·Bedrock SigV4 같은 네이티브 기능이 필요하면 별도 패키지가
    `register_provider("google", NativeGeminiProvider)` 또는 entry_points
    `xgen_harness.providers` 로 **코어 수정 없이** 교체한다 (PHILOSOPHY ②연동성:
    엔진은 provider-agnostic, 네이티브는 plugin). 기본 base_url 은
    PROVIDER_DEFAULT_BASE_URL / get_provider_base_url() 가 해석.

    새 프로바이더 추가: register_provider("name", ProviderClass, base_url=...)
    v0.15.1 — entry_points 그룹 `xgen_harness.providers` 자동 발견 추가.
    """
    if _REGISTRY:
        # 이미 등록된 상태라도 entry_points 재스캔은 idempotent 한 번 시도.
        _discover_from_entry_points_once()
        return
    from .anthropic import AnthropicProvider
    from .openai import OpenAIProvider

    _REGISTRY["anthropic"] = AnthropicProvider
    _REGISTRY["openai"] = OpenAIProvider
    # OpenAI 호환 프로바이더 — 동일 클래스, base_url만 다름
    _REGISTRY["google"] = OpenAIProvider
    _REGISTRY["bedrock"] = OpenAIProvider
    _REGISTRY["vllm"] = OpenAIProvider

    _discover_from_entry_points_once()


_ENTRY_POINTS_DISCOVERED = False


def _discover_from_entry_points_once() -> None:
    """외부 패키지가 `xgen_harness.providers` 그룹에 provider 를 노출하면 자동 등록.

    entry_point 의 반환은 다음 중 하나 허용:
      - LLMProvider 서브클래스 → 모듈명/ep 이름으로 등록
      - dict: {name, cls, default_model?, models?, api_key_env?, context_limit?}
      - list[dict]: 여러 개 한 번에

    v0.15.1 자동 연동 자동 확장성 — pip install xgen-bedrock-provider 로 새
    Provider 가 첫 list_providers/get_default_provider 호출 시점에 합류.
    """
    global _ENTRY_POINTS_DISCOVERED
    if _ENTRY_POINTS_DISCOVERED:
        return
    _ENTRY_POINTS_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.providers"
        if hasattr(eps, "select"):
            items = eps.select(group=group)
        else:
            items = eps.get(group, [])
        for ep in items:
            try:
                factory = ep.load()
                result = factory() if callable(factory) else factory
                _register_from_entry_point(ep.name, result)
            except Exception:
                continue
    except Exception:
        return


def _register_from_entry_point(ep_name: str, result) -> None:
    """entry_point 반환값 해석 후 register_provider 호출."""
    # case 1: LLMProvider 서브클래스
    try:
        if isinstance(result, type) and issubclass(result, LLMProvider):
            register_provider(ep_name, result)
            return
    except Exception:
        pass
    # case 2: dict 단건
    if isinstance(result, dict) and result.get("name") and result.get("cls"):
        register_provider(
            result["name"],
            result["cls"],
            default_model=result.get("default_model"),
            models=result.get("models"),
            api_key_env=result.get("api_key_env"),
            context_limit=result.get("context_limit"),
        )
        return
    # case 3: list/iterable 다건
    if isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, dict) and item.get("name") and item.get("cls"):
                register_provider(
                    item["name"],
                    item["cls"],
                    default_model=item.get("default_model"),
                    models=item.get("models"),
                    api_key_env=item.get("api_key_env"),
                    context_limit=item.get("context_limit"),
                )


__all__ = [
    "LLMProvider", "ProviderEvent", "ProviderEventType",
    "register_provider", "create_provider",
    "get_api_key_env", "get_default_model", "list_providers",
    "get_default_provider",
    "get_provider_models",
    "get_context_limit",
    "get_provider_base_url",
    "resolve_api_key_from_file",
    "PROVIDER_API_KEY_MAP", "PROVIDER_DEFAULT_MODEL", "PROVIDER_MODELS",
    "PROVIDER_CONTEXT_LIMITS", "DEFAULT_CONTEXT_LIMIT_CHARS",
    "PROVIDER_DEFAULT_BASE_URL",
]
