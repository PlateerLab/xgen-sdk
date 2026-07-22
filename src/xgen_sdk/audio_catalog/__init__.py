"""
xgen_sdk.audio_catalog — 오디오(STT/TTS) 모델·보이스 카탈로그 동적 조회 + TTL 캐시.

``xgen_sdk.llm_catalog`` 와 동일한 패턴:
    - get_stt_models(provider)  — provider 의 STT 모델 dict 리스트
    - get_tts_voices(provider)  — provider 의 TTS 보이스(profile) dict 리스트
    - invalidate(provider=None) — 엔드포인트/키 변경 시 캐시 무효화
    - register_config_composer(composer) — base_url/api_key 를 config 에서 해석

동적 소스(엔드포인트 probe):
    - STT: OpenAI 호환 ``GET {base}/v1/models`` (vLLM whisper 포함) → model id 리스트
    - TTS: xgen-audio-service OmniVoice ``GET {base}/voices`` → {id, name} 리스트

Fallback 체인(llm_catalog 와 동일 철학 — UI dropdown 빈 채로 떨어지는 회귀 방지):
    1. cache hit(TTL 신선) → 반환
    2. 엔드포인트 probe 성공 → 캐시 저장 후 반환
    3. probe 실패 → stale 캐시(있으면) → 없으면 하드코딩 FALLBACK

provider 값(오디오 config 와 일치):
    'openai'  — 정적 목록(공식 모델/보이스)
    'geny'    — xgen-audio-service (OmniVoice TTS / OpenAI 호환 whisper STT) 엔드포인트
    'custom'  — 임의 OpenAI 호환 엔드포인트
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("xgen-sdk.audio_catalog")

TTL_SEC = 3600          # 1 시간
HTTP_TIMEOUT = 5.0

# ─── 모듈 캐시 ──────────────────────────────────────────────────
# {"stt:geny" -> {"data": [...], "ts": 1234.5}}
_cache: Dict[str, Dict] = {}

# ─── ConfigComposer / ConfigClient 등록 ──────────────────────────
# app 이 자신의 composer 를 등록하면 base_url/api_key 를 PersistentConfig 에서 조회.
# 미등록 시 조회는 None 을 반환하고 정적 FALLBACK 을 사용.
_registered_composer = None


def register_config_composer(composer) -> None:
    """app startup 시 호출 — ``get_config_by_name(env_name) -> PersistentConfig`` 를
    가진 duck-typed composer 를 등록(ConfigComposer/ConfigClient 호환)."""
    global _registered_composer  # pylint: disable=global-statement
    _registered_composer = composer
    logger.info("audio_catalog: registered composer (type=%s)", type(composer).__name__)


def _config_value(env_name: str) -> Optional[str]:
    """등록된 composer 에서 env_name 의 현재 값을 조회(없으면 None)."""
    comp = _registered_composer
    if comp is None:
        return None
    try:
        cfg = comp.get_config_by_name(env_name)
        val = getattr(cfg, "value", None)
        return str(val) if val not in (None, "") else None
    except Exception:  # noqa: BLE001 — 조회 실패는 fallback 으로 흡수
        return None


# ─── 엔드포인트별 base_url env 이름 ───────────────────────────────
_STT_BASE_URL_ENV = {"geny": "GENY_STT_BASE_URL", "custom": "CUSTOM_STT_BASE_URL"}
_STT_API_KEY_ENV = {"geny": "GENY_STT_API_KEY", "custom": "CUSTOM_STT_API_KEY"}
_TTS_BASE_URL_ENV = {"geny": "GENY_TTS_BASE_URL", "custom": "CUSTOM_TTS_BASE_URL"}


# ─── 정적 fallback ────────────────────────────────────────────────
FALLBACK_STT: Dict[str, List[Dict[str, str]]] = {
    "openai": [
        {"value": "gpt-4o-transcribe", "label": "GPT-4o Transcribe"},
        {"value": "gpt-4o-mini-transcribe", "label": "GPT-4o Mini Transcribe"},
        {"value": "whisper-1", "label": "Whisper v2 (whisper-1)"},
    ],
    # geny/custom 은 xgen-audio-service whisper 기본
    "_endpoint": [
        {"value": "openai/whisper-large-v3", "label": "Whisper large-v3"},
        {"value": "openai/whisper-large-v3-turbo", "label": "Whisper large-v3 turbo"},
    ],
}

FALLBACK_TTS_VOICES: Dict[str, List[Dict[str, str]]] = {
    "openai": [
        {"value": v, "label": v.capitalize()}
        for v in ("alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer")
    ],
    # geny/custom 은 xgen-audio-service 기본 프로파일
    "_endpoint": [
        {"value": "paimon_ko", "label": "Paimon (KO)"},
        {"value": "ellen_joe", "label": "Ellen Joe"},
        {"value": "mao_pro", "label": "Mao"},
        {"value": "ruan_mei", "label": "Ruan Mei"},
    ],
}


def _normalize_base(url: str) -> str:
    """base_url 끝의 ``/`` 와 ``/v1`` 를 정리해 일관된 루트를 만든다."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3]
    return u


def _cache_get(key: str) -> Optional[List[Dict[str, str]]]:
    ent = _cache.get(key)
    if ent and (time.time() - ent["ts"] < TTL_SEC):
        return ent["data"]
    return None


def _cache_put(key: str, data: List[Dict[str, str]]) -> None:
    _cache[key] = {"data": data, "ts": time.time()}


def _stale(key: str) -> Optional[List[Dict[str, str]]]:
    ent = _cache.get(key)
    return ent["data"] if ent else None


# ─── STT ─────────────────────────────────────────────────────────
def _fetch_stt_models(base_url: str, api_key: Optional[str]) -> List[Dict[str, str]]:
    """OpenAI 호환 ``GET {base}/v1/models`` 에서 모델 id 목록을 가져온다."""
    root = _normalize_base(base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.get(f"{root}/v1/models", headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data", []) or []
    out = [{"value": m["id"], "label": m["id"]} for m in data if m.get("id")]
    return out or FALLBACK_STT["_endpoint"]


def get_stt_models(provider: str) -> List[Dict[str, str]]:
    provider = (provider or "openai").strip().lower()
    if provider == "openai":
        return FALLBACK_STT["openai"]
    key = f"stt:{provider}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    base = _config_value(_STT_BASE_URL_ENV.get(provider, ""))
    if base:
        try:
            models = _fetch_stt_models(base, _config_value(_STT_API_KEY_ENV.get(provider, "")))
            _cache_put(key, models)
            return models
        except Exception as e:  # noqa: BLE001
            logger.warning("audio_catalog stt probe failed (%s): %s", provider, e)
    return _stale(key) or FALLBACK_STT["_endpoint"]


# ─── TTS ─────────────────────────────────────────────────────────
def _fetch_tts_voices(base_url: str) -> List[Dict[str, str]]:
    """xgen-audio-service OmniVoice ``GET {base}/voices`` 에서 프로파일 목록."""
    root = _normalize_base(base_url)
    r = httpx.get(f"{root}/voices", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    voices = r.json().get("voices", []) or []
    out = [
        {"value": v["id"], "label": v.get("name") or v["id"]}
        for v in voices if v.get("id")
    ]
    return out or FALLBACK_TTS_VOICES["_endpoint"]


def get_tts_voices(provider: str) -> List[Dict[str, str]]:
    provider = (provider or "geny").strip().lower()
    if provider == "openai":
        return FALLBACK_TTS_VOICES["openai"]
    key = f"tts:{provider}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    base = _config_value(_TTS_BASE_URL_ENV.get(provider, ""))
    if base:
        try:
            voices = _fetch_tts_voices(base)
            _cache_put(key, voices)
            return voices
        except Exception as e:  # noqa: BLE001
            logger.warning("audio_catalog tts probe failed (%s): %s", provider, e)
    return _stale(key) or FALLBACK_TTS_VOICES["_endpoint"]


def invalidate(provider: Optional[str] = None) -> None:
    """엔드포인트/키 변경 시 캐시 무효화. provider 지정 시 해당 항목만."""
    if provider is None:
        _cache.clear()
        return
    for k in [k for k in _cache if k.endswith(f":{provider}")]:
        _cache.pop(k, None)


__all__ = [
    "get_stt_models",
    "get_tts_voices",
    "invalidate",
    "register_config_composer",
]
