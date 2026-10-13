"""통제 정책 스위치: 정책 종류 두 가지와 적용 자리 네 곳.

관리자 화면 [AI 거버넌스 > 통제 정책 관리] 는 종류마다 [탐지 기능 사용] 스위치를 두고, 켜면 아래
[세부설정] 에서 어디에 쓸지 고른다. 실제 설정 키는 core 가 PersistentConfig 로 만든다
(``core/config/sub_config/guarder_config.py`` 가 :func:`scope_settings` 를 그대로 쓴다).

    종류            탐지 스위치                    자리 스위치 앞부분
    개인정보(pii)   IS_AVAILABLE_PIIS              PII_
    금칙어          IS_AVAILABLE_FORBIDDEN_WORDS   FORBIDDEN_WORDS_

    자리                      뜻                                                        기본
    BLOCK_CHAT_INPUT          채팅으로 들어온 사용자 글에 걸리면 그 메시지를 받지 않는다   꺼짐
    BLOCK_MODEL_INPUT         에이전트가 모델에 넘기는 글(입력·참조 문서·바깥 도구 결과)을 가린다  켜짐
    STRIP_CHAT_UPLOAD         채팅에 올린 파일에서 걸린 부분을 지운 뒤 저장한다            꺼짐
    STRIP_FILESTORE_UPLOAD    파일 저장소에 올린 파일에서 걸린 부분을 지운 뒤 저장한다      꺼짐

기본값은 이 설정이 생기기 전 동작 그대로다(탐지를 켜면 모델에 가는 글만 가렸다). 한 자리는 탐지 스위치와
자리 스위치가 **둘 다** 켜져 있어야 쓰인다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

PII = "pii"
FORBIDDEN_WORD = "forbidden_word"


@dataclass(frozen=True)
class PolicyKind:
    """정책 종류 하나: 표, 탐지 스위치, 자리 스위치 앞부분."""

    policy_type: str
    table: str
    enable_flag: str
    scope_prefix: str
    label: str


#: 적용 차례도 이 순서다(개인정보 먼저: 개인정보가 가린 자리는 금칙어에 다시 걸리지 않는다).
KINDS: Tuple[PolicyKind, ...] = (
    PolicyKind(PII, "piis", "IS_AVAILABLE_PIIS", "PII", "개인정보"),
    PolicyKind(FORBIDDEN_WORD, "forbidden_words", "IS_AVAILABLE_FORBIDDEN_WORDS", "FORBIDDEN_WORDS", "금칙어"),
)

SCOPE_CHAT_INPUT = "BLOCK_CHAT_INPUT"
SCOPE_MODEL_INPUT = "BLOCK_MODEL_INPUT"
SCOPE_CHAT_UPLOAD = "STRIP_CHAT_UPLOAD"
SCOPE_FILESTORE_UPLOAD = "STRIP_FILESTORE_UPLOAD"

#: (자리, 기본값, 화면 이름): 화면 [세부설정] 의 차례와 같다.
SCOPES: Tuple[Tuple[str, bool, str], ...] = (
    (SCOPE_CHAT_INPUT, False, "채팅 입력시 차단"),
    (SCOPE_MODEL_INPUT, True, "모델에 전달 차단"),
    (SCOPE_CHAT_UPLOAD, False, "채팅 파일 업로드시 제거"),
    (SCOPE_FILESTORE_UPLOAD, False, "파일 저장소 업로드시 제거"),
)
SCOPE_DEFAULTS = {scope: default for scope, default, _label in SCOPES}

FlagReader = Callable[[str, bool], bool]


def kind_of(policy_type: str) -> Optional[PolicyKind]:
    for kind in KINDS:
        if kind.policy_type == policy_type:
            return kind
    return None


def scope_key(kind: PolicyKind, scope: str) -> str:
    """자리 스위치 설정 키: 예: ``PII_BLOCK_CHAT_INPUT``."""
    if scope not in SCOPE_DEFAULTS:
        raise ValueError(f"알 수 없는 적용 자리: {scope}")
    return f"{kind.scope_prefix}_{scope}"


def scope_settings() -> List[Tuple[str, str, bool, str]]:
    """core 가 만들 설정: ``(env_name, config_path, 기본값, 화면 이름)`` 여덟 개."""
    out: List[Tuple[str, str, bool, str]] = []
    for kind in KINDS:
        for scope, default, label in SCOPES:
            out.append((
                scope_key(kind, scope),
                f"guarder.{kind.table}.{scope.lower()}",
                default,
                f"{kind.label} {label}",
            ))
    return out


_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def as_bool(value: Any, default: bool) -> bool:
    """설정 값 → 참/거짓. 알 수 없는 값은 기본값(오타가 보호를 켜거나 끄지 않게)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def config_flag(name: str, default: bool = False, *, config: Any = None) -> bool:
    """관리자 설정 → 환경변수 → 기본값. 설정을 읽지 못하면 다음 단으로 내려간다."""
    value = None
    try:
        if config is None:
            from xgen_sdk.config.config_client import get_config_client

            config = get_config_client()
        cfg = config.get_config_by_name(name)
        value = getattr(cfg, "value", None) if cfg is not None else None
    except Exception:  # noqa: BLE001, 설정 저장소 장애가 호출자를 막지 않는다
        value = None
    if value is None or (isinstance(value, str) and not value.strip()):
        value = os.getenv(name)
    return as_bool(value, default)


def _reader(flag: Optional[FlagReader], config: Any) -> FlagReader:
    if flag is not None:
        return flag
    return lambda name, default: config_flag(name, default, config=config)


def scope_on(kind: PolicyKind, scope: str, *, flag: Optional[FlagReader] = None, config: Any = None) -> bool:
    """이 종류를 이 자리에 쓰는가: 탐지 스위치와 자리 스위치가 둘 다 켜져 있어야 한다."""
    read = _reader(flag, config)
    return read(kind.enable_flag, False) and read(scope_key(kind, scope), SCOPE_DEFAULTS[scope])


def kinds_on(scope: str, *, flag: Optional[FlagReader] = None, config: Any = None) -> List[PolicyKind]:
    """이 자리에 켜진 종류(적용 차례대로)."""
    read = _reader(flag, config)
    return [kind for kind in KINDS if scope_on(kind, scope, flag=read)]
