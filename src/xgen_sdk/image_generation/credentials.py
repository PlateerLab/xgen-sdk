"""제공자 자격증명 해석 — xgen-core(연결 확인 · 동기화)와 xgen-workflow(도구 실행)가 같이 쓴다.

순서 (계약 §2):
  1. comfyui → 쓰지 않음(연결 카탈로그가 대신함) — source ``none``.
  2. ``image_provider_catalog`` 행이 ``credential_mode='custom'`` 이고 secret 이 있으면 그 키
     (+ 행의 base_url, 없으면 LLM 설정 / 기본 주소) — source ``custom``.
  3. 아니면 LLM 설정 키: openai = ``OPENAI_API_KEY`` (+ ``OPENAI_API_BASE_URL``),
     gemini = ``GEMINI_API_KEY`` — source ``llm``. 읽는 방식은 ``xgen_sdk.llm_catalog`` 와
     같다(등록된 ConfigComposer/ConfigClient → 환경변수).
  4. 키가 없으면 source ``none``.

개별 키 암호문은 ComfyUI 연결 비밀과 같은 방식이다 — AES-256-GCM, 키 =
SHA-256(``DB_CREDENTIAL_SECRET``), 저장 = urlsafe-base64(iv12 + ciphertext + tag)
(xgen-core / xgen-workflow ``service/db_connections/crypto.py`` 와 상호 복호화).
평문은 ``{"api_key": "..."}`` JSON 을 권장하고, 키 문자열 그대로도 읽는다.

키 원문은 반환값의 ``api_key`` 에만 있다 — 로그 · 예외 · repr 어디에도 싣지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any, Callable, Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from xgen_sdk.image_generation import gemini_adapter, openai_adapter
from xgen_sdk.image_generation.types import (
    PROVIDER_COMFYUI,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    Credentials,
)

logger = logging.getLogger("xgen-sdk.image_generation")

TABLE = "image_provider_catalog"
CREDENTIAL_MODE_LLM = "llm"
CREDENTIAL_MODE_CUSTOM = "custom"
SOURCE_CUSTOM = "custom"
SOURCE_LLM = "llm"
SOURCE_NONE = "none"

SECRET_ENV = "DB_CREDENTIAL_SECRET"
# xgen-core / xgen-workflow crypto.py 의 기본값과 같아야 상호 복호화된다
_DEFAULT_SECRET = "xgen-db-credential-secret-change-in-production"
_IV_LENGTH = 12

LLM_KEY_SETTINGS: Dict[str, str] = {
    PROVIDER_OPENAI: "OPENAI_API_KEY",
    PROVIDER_GEMINI: "GEMINI_API_KEY",
}
LLM_BASE_URL_SETTINGS: Dict[str, str] = {
    PROVIDER_OPENAI: "OPENAI_API_BASE_URL",
}
DEFAULT_BASE_URLS: Dict[str, str] = {
    PROVIDER_OPENAI: openai_adapter.DEFAULT_BASE_URL,
    PROVIDER_GEMINI: gemini_adapter.DEFAULT_BASE_URL,
}


class SecretUnreadable(Exception):
    """암호문을 풀 수 없다(키가 바뀌었거나 손상). 원인 문구는 싣지 않는다."""


# ─── 암호문 ───────────────────────────────────────────────────────

def _aesgcm() -> AESGCM:
    secret = os.getenv(SECRET_ENV, _DEFAULT_SECRET)
    return AESGCM(hashlib.sha256(secret.encode("utf-8")).digest())


def seal_api_key(api_key: str) -> str:
    """API 키 → ``secret_ciphertext`` 에 저장할 암호문(평문은 ``{"api_key": ...}``)."""
    key = (api_key or "").strip()
    if not key:
        raise ValueError("api_key is empty")
    plaintext = json.dumps({"api_key": key}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    iv = os.urandom(_IV_LENGTH)
    return base64.urlsafe_b64encode(iv + _aesgcm().encrypt(iv, plaintext, None)).decode("utf-8")


def _decrypt(ciphertext: str) -> str:
    blob = base64.urlsafe_b64decode(ciphertext.encode("utf-8"))
    if len(blob) < _IV_LENGTH + 16:
        raise ValueError("ciphertext too short")
    return _aesgcm().decrypt(blob[:_IV_LENGTH], blob[_IV_LENGTH:], None).decode("utf-8")


def _key_from_plaintext(plaintext: str) -> Optional[str]:
    text = (plaintext or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for name in ("api_key", "token", "key"):
                value = data.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None
    return text or None


def open_api_key(ciphertext: Optional[str], *, decrypt: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """암호문 → API 키. 비었으면 None, 풀 수 없으면 SecretUnreadable.

    ``decrypt`` 를 주면 그 함수로 푼다(예: core 의 ``decrypt_credential``).
    """
    if not ciphertext:
        return None
    try:
        plaintext = (decrypt or _decrypt)(str(ciphertext))
    except Exception:  # noqa: BLE001 — 원인 문구에 비밀이 섞일 수 있어 싣지 않는다
        raise SecretUnreadable() from None
    return _key_from_plaintext(plaintext)


# ─── 설정 읽기 ────────────────────────────────────────────────────

def _registered_composer() -> Any:
    try:
        from xgen_sdk import llm_catalog

        return getattr(llm_catalog, "_registered_composer", None)
    except Exception:  # noqa: BLE001
        return None


def read_llm_setting(env_name: str, composer: Any = None) -> Optional[str]:
    """LLM 설정 값 하나 — composer(인자 → llm_catalog 에 등록된 것) → 환경변수."""
    comp = composer if composer is not None else _registered_composer()
    if comp is not None:
        try:
            cfg = comp.get_config_by_name(env_name)
            value = getattr(cfg, "value", None) if cfg is not None else None
            if isinstance(value, str) and value.strip():
                return value.strip()
        except Exception:  # noqa: BLE001
            logger.debug("image_generation: config lookup failed for %s", env_name)
    value = os.environ.get(env_name)
    return value.strip() if value and value.strip() else None


def llm_credentials(provider_id: str, composer: Any = None) -> Credentials:
    provider = (provider_id or "").strip().lower()
    base = None
    if provider in LLM_BASE_URL_SETTINGS:
        base = read_llm_setting(LLM_BASE_URL_SETTINGS[provider], composer)
    base = base or DEFAULT_BASE_URLS.get(provider)
    setting = LLM_KEY_SETTINGS.get(provider)
    key = read_llm_setting(setting, composer) if setting else None
    return Credentials(api_key=key, base_url=base, source=SOURCE_LLM if key else SOURCE_NONE)


def provider_row(db: Any, provider_id: str) -> Optional[Dict[str, Any]]:
    """``image_provider_catalog`` 한 행(credential_mode · secret_ciphertext · base_url). 못 읽으면 None."""
    if db is None:
        return None
    manager = getattr(db, "config_db_manager", None) or db
    execute = getattr(manager, "execute_query", None)
    if execute is None:
        return None
    try:
        rows = execute(
            f"SELECT provider_id, credential_mode, secret_ciphertext, base_url FROM {TABLE} WHERE provider_id = %s LIMIT 1",
            (provider_id,),
        )
    except Exception:  # noqa: BLE001 — 표가 아직 없을 수 있다(부팅 동기화 전)
        logger.debug("image_generation: provider row lookup failed for %s", provider_id)
        return None
    if not rows:
        return None
    return dict(rows[0])


def resolve_credentials(
    provider_id: str,
    db: Any = None,
    *,
    composer: Any = None,
    decrypt: Optional[Callable[[str], str]] = None,
    row: Optional[Dict[str, Any]] = None,
) -> Credentials:
    """제공자 자격증명 → ``Credentials{api_key, base_url, source, error}``.

    Args:
        provider_id: ``openai`` · ``gemini`` · ``comfyui``.
        db: ``execute_query(sql, params)`` 를 가진 DB 관리자(또는 ``config_db_manager`` 를 가진 앱 DB).
            None 이면 행을 읽지 않는다(LLM 설정만).
        composer: LLM 설정을 읽을 ConfigComposer/ConfigClient. 비우면 ``llm_catalog`` 에 등록된 것 → 환경변수.
        decrypt: 암호문을 풀 함수(비우면 SDK 의 같은 AES-GCM 방식).
        row: 이미 읽은 ``image_provider_catalog`` 행(있으면 DB 를 다시 읽지 않는다).

    개별 키가 설정돼 있는데 풀 수 없으면 LLM 키로 넘어가지 않고 ``source='none'``,
    ``error='secret_unreadable'`` (관리자가 고른 키 대신 다른 키를 몰래 쓰지 않는다).
    """
    provider = (provider_id or "").strip().lower()
    if provider == PROVIDER_COMFYUI or provider not in LLM_KEY_SETTINGS:
        return Credentials(api_key=None, base_url=None, source=SOURCE_NONE)
    data = row if row is not None else provider_row(db, provider)
    if data and str(data.get("credential_mode") or "").strip().lower() == CREDENTIAL_MODE_CUSTOM and data.get("secret_ciphertext"):
        llm = llm_credentials(provider, composer)
        base = (data.get("base_url") or "").strip() if isinstance(data.get("base_url"), str) else ""
        try:
            key = open_api_key(data.get("secret_ciphertext"), decrypt=decrypt)
        except SecretUnreadable:
            logger.warning("image_generation: custom key for %s is unreadable", provider)
            return Credentials(api_key=None, base_url=base or llm.base_url, source=SOURCE_NONE, error="secret_unreadable")
        if key:
            return Credentials(api_key=key, base_url=base or llm.base_url, source=SOURCE_CUSTOM)
    return llm_credentials(provider, composer)


__all__ = [
    "CREDENTIAL_MODE_CUSTOM",
    "CREDENTIAL_MODE_LLM",
    "DEFAULT_BASE_URLS",
    "LLM_BASE_URL_SETTINGS",
    "LLM_KEY_SETTINGS",
    "SOURCE_CUSTOM",
    "SOURCE_LLM",
    "SOURCE_NONE",
    "SecretUnreadable",
    "TABLE",
    "llm_credentials",
    "open_api_key",
    "provider_row",
    "read_llm_setting",
    "resolve_credentials",
    "seal_api_key",
]
