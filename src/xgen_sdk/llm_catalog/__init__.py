"""
xgen_sdk.llm_catalog — LLM 모델 카탈로그 동적 조회 + module-level TTL 캐시.

전체 설계: LLM_MODEL_CATALOG_PLAN.md §2 참조.

기능:
    - get_models(provider, capability) — provider 의 chat/vision 모델 dict 리스트
    - invalidate(provider=None) — API 키 변경 시 캐시 무효화

캐시:
    in-process dict, key=(provider, capability), TTL=1h
    pod 별 독립. Redis 미사용 — 단순화 + 호출 빈도 낮음.

Fallback 체인:
    1. cache hit (TTL 신선) → 반환
    2. API key 존재 + provider API 호출 → 성공 시 캐시 저장 후 반환
    3. provider API 실패 → in-process stale 캐시 (있으면) 반환
    4. stale 캐시도 없음 → 하드코딩 FALLBACK 반환

대상 provider:
    'openai' / 'anthropic' / 'gemini'
    (vLLM / AWS Bedrock / SGL 은 list API 가 없어 대상 외 — 호출 시 fallback 만 반환)

NULL/예외 안전:
    어떤 경우에도 빈 리스트 대신 fallback 을 반환 (UI dropdown 빈 채로 떨어지는 회귀 방지).
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("xgen-sdk.llm_catalog")

TTL_SEC = 3600   # 1 시간
HTTP_TIMEOUT = 5.0

# ─── 모듈 캐시 ───────────────────────────────────────────────────
# {"openai:chat" -> {"data": [...], "ts": 1234.5}}
_cache: Dict[str, Dict] = {}

# ─── ConfigComposer / ConfigClient 등록 ───────────────────────────
# app (xgen-core / xgen-workflow / xgen-documents) 가 자신의 composer 인스턴스를
# 여기에 등록하면 _resolve_api_key 가 PersistentConfig 에서 API 키 조회.
# 등록 안 됐을 때는 env var 만 사용 (OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY).
_registered_composer = None


def register_config_composer(composer) -> None:
    """app startup 시 호출 — 자신의 ConfigComposer / ConfigClient 인스턴스를 등록.

    composer 는 `get_config_by_name(env_name) -> PersistentConfig` 메서드를 가진
    duck-typed 객체. ConfigComposer (xgen-core) / ConfigClient (xgen-workflow) 모두 호환.
    """
    global _registered_composer  # pylint: disable=global-statement
    _registered_composer = composer
    logger.info("llm_catalog: registered composer (type=%s)", type(composer).__name__)

# ─── 정적 fallback (provider API 모두 실패 시 사용) ────────────
# 2026-05-18 기준 각 provider 의 가용 모델 종합 리스트. 정상 키 + 네트워크 환경에서는
# provider API 가 동적 카탈로그 반환. fallback 정확도 유지를 위해 release 시점마다 본 리스트 최신화 권장.
FALLBACK_CHAT: Dict[str, List[Dict[str, str]]] = {
    # OpenAI — GPT-5.x family + o-series reasoning + GPT-4 line.
    "openai": [
        # GPT-5.x family (latest)
        {"value": "gpt-5.5",        "label": "GPT-5.5"},
        {"value": "gpt-5.4",        "label": "GPT-5.4"},
        {"value": "gpt-5.4-mini",   "label": "GPT-5.4 Mini"},
        {"value": "gpt-5.4-nano",   "label": "GPT-5.4 Nano"},
        {"value": "gpt-5.3",        "label": "GPT-5.3"},
        {"value": "gpt-5.2",        "label": "GPT-5.2"},
        {"value": "gpt-5.1",        "label": "GPT-5.1"},
        {"value": "gpt-5",          "label": "GPT-5"},
        {"value": "gpt-5-mini",     "label": "GPT-5 Mini"},
        {"value": "gpt-5-nano",     "label": "GPT-5 Nano"},
        # o-series (reasoning)
        {"value": "o4-mini",        "label": "o4 Mini"},
        {"value": "o3",             "label": "o3"},
        {"value": "o3-mini",        "label": "o3 Mini"},
        {"value": "o1",             "label": "o1"},
        {"value": "o1-mini",        "label": "o1 Mini"},
        # GPT-4 family (API 잔존, ChatGPT 앱 retire 와 별개)
        {"value": "gpt-4.1",        "label": "GPT-4.1"},
        {"value": "gpt-4.1-mini",   "label": "GPT-4.1 Mini"},
        {"value": "gpt-4o",         "label": "GPT-4o"},
        {"value": "gpt-4o-mini",    "label": "GPT-4o Mini"},
        {"value": "gpt-4-turbo",    "label": "GPT-4 Turbo"},
        {"value": "gpt-4",          "label": "GPT-4"},
        # Legacy
        {"value": "gpt-3.5-turbo",  "label": "GPT-3.5 Turbo"},
    ],
    # Anthropic — Claude 4.x family (Opus/Sonnet/Haiku) + Claude 3.x line.
    "anthropic": [
        # Claude 4.7 (current flagship)
        {"value": "claude-opus-4-7",                 "label": "Claude Opus 4.7"},
        # Claude 4.6
        {"value": "claude-opus-4-6",                 "label": "Claude Opus 4.6"},
        {"value": "claude-sonnet-4-6",               "label": "Claude Sonnet 4.6"},
        # Claude 4.5
        {"value": "claude-opus-4-5-20251101",        "label": "Claude Opus 4.5"},
        {"value": "claude-sonnet-4-5-20250929",      "label": "Claude Sonnet 4.5"},
        {"value": "claude-haiku-4-5-20251001",       "label": "Claude Haiku 4.5"},
        # Claude 4.1 / 4
        {"value": "claude-opus-4-1-20250805",        "label": "Claude Opus 4.1"},
        {"value": "claude-opus-4-20250514",          "label": "Claude Opus 4"},
        {"value": "claude-sonnet-4-20250514",        "label": "Claude Sonnet 4"},
        # Claude 3.x
        {"value": "claude-3-7-sonnet-20250219",      "label": "Claude 3.7 Sonnet"},
        {"value": "claude-3-5-sonnet-20241022",      "label": "Claude 3.5 Sonnet"},
        {"value": "claude-3-5-haiku-20241022",       "label": "Claude 3.5 Haiku"},
    ],
    # Google Gemini — 3.x family + 2.5 / 2.0 line.
    "gemini": [
        # Gemini 3.x (latest)
        {"value": "gemini-3.1-pro",          "label": "Gemini 3.1 Pro"},
        {"value": "gemini-3.1-flash-lite",   "label": "Gemini 3.1 Flash Lite"},
        {"value": "gemini-3-pro",            "label": "Gemini 3 Pro"},
        {"value": "gemini-3-flash",          "label": "Gemini 3 Flash"},
        # Gemini 2.5
        {"value": "gemini-2.5-pro",          "label": "Gemini 2.5 Pro"},
        {"value": "gemini-2.5-flash",        "label": "Gemini 2.5 Flash"},
        {"value": "gemini-2.5-flash-lite",   "label": "Gemini 2.5 Flash Lite"},
        # Gemini 2.0 (legacy)
        {"value": "gemini-2.0-flash",        "label": "Gemini 2.0 Flash"},
        {"value": "gemini-2.0-flash-lite",   "label": "Gemini 2.0 Flash Lite"},
    ],
}

# Vision-capable 필터링된 fallback.
# OpenAI: gpt-5/gpt-4o/gpt-4-turbo/gpt-4.1/o-series 모두 vision 지원.
# Anthropic: Claude 3.5+ 모두 vision 지원.
# Gemini: Gemini 2+ 모두 vision 지원.
FALLBACK_VISION: Dict[str, List[Dict[str, str]]] = {
    "openai": [
        # GPT-5.x family (vision)
        {"value": "gpt-5.5",        "label": "GPT-5.5"},
        {"value": "gpt-5.4",        "label": "GPT-5.4"},
        {"value": "gpt-5.4-mini",   "label": "GPT-5.4 Mini"},
        {"value": "gpt-5.3",        "label": "GPT-5.3"},
        {"value": "gpt-5",          "label": "GPT-5"},
        {"value": "gpt-5-mini",     "label": "GPT-5 Mini"},
        # o-series (vision)
        {"value": "o4-mini",        "label": "o4 Mini"},
        {"value": "o3",             "label": "o3"},
        # GPT-4 vision
        {"value": "gpt-4.1",        "label": "GPT-4.1"},
        {"value": "gpt-4.1-mini",   "label": "GPT-4.1 Mini"},
        {"value": "gpt-4o",         "label": "GPT-4o"},
        {"value": "gpt-4o-mini",    "label": "GPT-4o Mini"},
        {"value": "gpt-4-turbo",    "label": "GPT-4 Turbo"},
    ],
    "anthropic": [
        # Claude 4.x (all vision)
        {"value": "claude-opus-4-7",                 "label": "Claude Opus 4.7"},
        {"value": "claude-opus-4-6",                 "label": "Claude Opus 4.6"},
        {"value": "claude-sonnet-4-6",               "label": "Claude Sonnet 4.6"},
        {"value": "claude-opus-4-5-20251101",        "label": "Claude Opus 4.5"},
        {"value": "claude-sonnet-4-5-20250929",      "label": "Claude Sonnet 4.5"},
        {"value": "claude-haiku-4-5-20251001",       "label": "Claude Haiku 4.5"},
        {"value": "claude-opus-4-1-20250805",        "label": "Claude Opus 4.1"},
        {"value": "claude-opus-4-20250514",          "label": "Claude Opus 4"},
        {"value": "claude-sonnet-4-20250514",        "label": "Claude Sonnet 4"},
        # Claude 3.5+ (vision)
        {"value": "claude-3-7-sonnet-20250219",      "label": "Claude 3.7 Sonnet"},
        {"value": "claude-3-5-sonnet-20241022",      "label": "Claude 3.5 Sonnet"},
    ],
    "gemini": [
        # Gemini 3.x (vision)
        {"value": "gemini-3.1-pro",          "label": "Gemini 3.1 Pro"},
        {"value": "gemini-3.1-flash-lite",   "label": "Gemini 3.1 Flash Lite"},
        {"value": "gemini-3-pro",            "label": "Gemini 3 Pro"},
        {"value": "gemini-3-flash",          "label": "Gemini 3 Flash"},
        # Gemini 2.5 (vision)
        {"value": "gemini-2.5-pro",          "label": "Gemini 2.5 Pro"},
        {"value": "gemini-2.5-flash",        "label": "Gemini 2.5 Flash"},
        {"value": "gemini-2.5-flash-lite",   "label": "Gemini 2.5 Flash Lite"},
        # Gemini 2.0 (vision)
        {"value": "gemini-2.0-flash",        "label": "Gemini 2.0 Flash"},
    ],
}

# 비전 capability 필터 패턴 (provider API 응답에서 추가 필터)
_VISION_PATTERNS = {
    "openai": [re.compile(r"^gpt-4o"), re.compile(r"^gpt-4-vision"),
               re.compile(r"^gpt-4-turbo"), re.compile(r"^gpt-4\.1"),
               re.compile(r"^o\d")],
    "anthropic": [re.compile(r"^claude-3"), re.compile(r"^claude-(opus|sonnet|haiku)-4")],
    "gemini": [re.compile(r"^gemini-(2|1\.5|3)")],
}

# OpenAI chat 모델 필터 — 임베딩/이미지/오디오/whisper 등 제외
_OPENAI_CHAT_EXCLUDE = re.compile(
    r"realtime|audio|transcribe|tts|dall-e|whisper|moderation|embedding|"
    r"davinci|babbage|curie|instruct|search|edit"
)
_OPENAI_CHAT_INCLUDE = re.compile(r"^(gpt-|o\d|chatgpt-)")

# OpenAI label 친근화 (id → display)
_OPENAI_LABEL_OVERRIDES = {
    "gpt-4o": "GPT-4o",
    "gpt-4o-mini": "GPT-4o Mini",
    "gpt-4-turbo": "GPT-4 Turbo",
    "gpt-4": "GPT-4",
    "gpt-4.1": "GPT-4.1",
    "gpt-4.1-mini": "GPT-4.1 Mini",
    "gpt-3.5-turbo": "GPT-3.5 Turbo",
    "o1": "o1",
    "o1-mini": "o1 Mini",
    "o1-preview": "o1 Preview",
    "o3": "o3",
    "o3-mini": "o3 Mini",
    "o4-mini": "o4 Mini",
    "chatgpt-4o-latest": "ChatGPT-4o Latest",
}


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def get_models(provider: str, capability: str = "chat") -> List[Dict[str, str]]:
    """provider + capability ('chat' | 'vision') 에 해당하는 모델 리스트.

    반환 형식: [{"value": "gpt-4o-mini", "label": "GPT-4o Mini"}, ...]

    NULL/예외 안전: 어떤 경우에도 빈 리스트 대신 fallback 반환.
    """
    provider = (provider or "").strip().lower()
    capability = (capability or "chat").strip().lower()
    if capability not in ("chat", "vision"):
        capability = "chat"

    # 대상 provider 아니면 즉시 빈 fallback (vLLM 등은 정적 유지)
    if provider not in ("openai", "anthropic", "gemini"):
        return _fallback_for(provider, capability)

    key = f"{provider}:{capability}"
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached["ts"]) < TTL_SEC:
        # defensive copy — caller 가 mutate 해도 캐시는 보존
        return [dict(m) for m in cached["data"]]

    data = _fetch_with_fallback(provider, capability)
    _cache[key] = {"data": data, "ts": now}
    return [dict(m) for m in data]


def invalidate(provider: Optional[str] = None) -> None:
    """캐시 무효화. provider=None 이면 전체 비움.

    API 키 변경 시 admin 측에서 호출 권장 (config PUT 직후).
    """
    if provider is None:
        _cache.clear()
        return
    p = provider.strip().lower()
    for k in list(_cache.keys()):
        if k.startswith(f"{p}:"):
            _cache.pop(k, None)


# ═══════════════════════════════════════════════════════════════
# 내부 — API key 조회
# ═══════════════════════════════════════════════════════════════

def _resolve_api_key(provider: str) -> Optional[str]:
    """등록된 composer → env var fallback.

    app 이 register_config_composer() 로 자신의 composer 를 등록했다면 그것에서 조회.
    실패 또는 미등록 시 env var. 어떤 단계에서도 raise 하지 않음.
    """
    env_name_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    env_name = env_name_map.get(provider)
    if not env_name:
        return None

    # 1) 등록된 composer 시도 (xgen-core: ConfigComposer / xgen-workflow: ConfigClient)
    composer = _registered_composer
    if composer is not None:
        try:
            cfg = composer.get_config_by_name(env_name)
            if cfg is not None:
                val = getattr(cfg, "value", None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        except Exception as e:  # pylint: disable=broad-except
            logger.debug("composer lookup failed for %s: %s", env_name, e)

    # 2) env var fallback (openai_config.py 가 자동 export. anthropic/gemini 는 composer 경로 사용)
    v = os.environ.get(env_name)
    if v and v.strip():
        return v.strip()
    return None


# ═══════════════════════════════════════════════════════════════
# 내부 — provider 별 API 호출
# ═══════════════════════════════════════════════════════════════

def _fallback_for(provider: str, capability: str) -> List[Dict[str, str]]:
    table = FALLBACK_VISION if capability == "vision" else FALLBACK_CHAT
    return [dict(m) for m in table.get(provider, [])]


def _fetch_with_fallback(provider: str, capability: str) -> List[Dict[str, str]]:
    api_key = _resolve_api_key(provider)
    if not api_key:
        logger.debug("no API key for %s — using fallback", provider)
        return _fallback_for(provider, capability)

    try:
        if provider == "openai":
            models = _fetch_openai(api_key)
        elif provider == "anthropic":
            models = _fetch_anthropic(api_key)
        elif provider == "gemini":
            models = _fetch_gemini(api_key)
        else:
            return _fallback_for(provider, capability)
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("catalog fetch failed (%s/%s): %s", provider, capability, e)
        # 기존 stale 캐시가 있으면 (위 호출자가 캐시 hit 못한 경우는 만료된 경우)
        # — 모듈 캐시는 만료된 항목도 유지하므로 stale 반환 시도
        stale = _cache.get(f"{provider}:{capability}")
        if stale and stale.get("data"):
            logger.info("returning stale cache for %s/%s", provider, capability)
            return stale["data"]
        return _fallback_for(provider, capability)

    # capability 필터
    if capability == "vision":
        filtered = _filter_vision(provider, models)
    else:
        filtered = _filter_chat(provider, models)

    return filtered or _fallback_for(provider, capability)


def _fetch_openai(api_key: str) -> List[Dict[str, str]]:
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.get("https://api.openai.com/v1/models", headers=headers)
        r.raise_for_status()
        data = r.json()
    out: List[Dict[str, str]] = []
    for m in data.get("data", []):
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append({"value": mid, "label": _openai_label(mid)})
    return out


def _fetch_anthropic(api_key: str) -> List[Dict[str, str]]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.get(
            "https://api.anthropic.com/v1/models",
            headers=headers,
            params={"limit": 1000},
        )
        r.raise_for_status()
        data = r.json()
    out: List[Dict[str, str]] = []
    for m in data.get("data", []):
        mid = m.get("id") or ""
        if not mid:
            continue
        label = m.get("display_name") or mid
        out.append({"value": mid, "label": label})
    return out


def _fetch_gemini(api_key: str) -> List[Dict[str, str]]:
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": api_key, "pageSize": 200},
        )
        r.raise_for_status()
        data = r.json()
    out: List[Dict[str, str]] = []
    for m in data.get("models", []):
        name = m.get("name") or ""
        if not name.startswith("models/"):
            continue
        mid = name[len("models/"):]
        if not mid.startswith("gemini-"):
            continue
        # generateContent 지원 모델만 (chat-capable)
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        label = m.get("displayName") or mid
        out.append({"value": mid, "label": label})
    return out


# ═══════════════════════════════════════════════════════════════
# 내부 — capability 필터
# ═══════════════════════════════════════════════════════════════

def _filter_chat(provider: str, models: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if provider == "openai":
        out: List[Dict[str, str]] = []
        for m in models:
            mid = m.get("value", "")
            if _OPENAI_CHAT_EXCLUDE.search(mid):
                continue
            if not _OPENAI_CHAT_INCLUDE.search(mid):
                continue
            out.append(m)
        return out
    # anthropic / gemini — fetcher 단에서 이미 필터됨
    return list(models)


def _filter_vision(provider: str, models: List[Dict[str, str]]) -> List[Dict[str, str]]:
    patterns = _VISION_PATTERNS.get(provider, [])
    if not patterns:
        return list(models)
    out: List[Dict[str, str]] = []
    for m in models:
        mid = m.get("value", "")
        if any(p.search(mid) for p in patterns):
            out.append(m)
    return out


def _openai_label(model_id: str) -> str:
    if model_id in _OPENAI_LABEL_OVERRIDES:
        return _OPENAI_LABEL_OVERRIDES[model_id]
    return model_id


__all__ = [
    "get_models",
    "invalidate",
    "register_config_composer",
    "FALLBACK_CHAT",
    "FALLBACK_VISION",
    "TTL_SEC",
]
