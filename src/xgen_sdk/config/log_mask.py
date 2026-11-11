"""config 값을 로그에 남길 때 쓰는 마스킹.

base_config 에 있던 것을 옮겼다. redis_config · local_config 의 업데이트 로그도 같은 규칙을
써야 하는데, base_config 는 redis_config 를 import 하므로 거기서 가져오면 순환한다.
"""
from typing import Any

#: 값을 로그에 그대로 남기면 안 되는 config 이름 조각.
#: config 로더는 부팅/refresh 마다 값을 INFO 로 남기는데, 여기에 API 키·
#: 비밀번호가 평문으로 찍히면 로그 수집기(Loki 등)에 그대로 적재된다.
_SENSITIVE_NAME_PARTS = (
    "KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL",
    "PRIVATE", "SALT", "CERT", "DSN",
)


def _mask_config_value(env_name: str, value: Any) -> Any:
    """민감한 이름의 config 값을 로그용으로 마스킹.

    값의 존재/길이는 남긴다 — 운영에서 "값이 비어 있어서 생긴 문제" 를
    진단하려면 그 정도는 필요하고, 그 이상은 유출이다.
    """
    name = (env_name or "").upper()
    if not any(part in name for part in _SENSITIVE_NAME_PARTS):
        return value
    if value is None:
        return "<unset>"
    text = str(value)
    if not text:
        return "<empty>"
    return f"<redacted len={len(text)}>"
