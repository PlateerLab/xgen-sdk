"""
External Inputs — 컴파일된 아티팩트가 런타임에 필요로 하는 외부 값.

두 통로 병행 (D1 합의):
  A. 선언형 — `HarnessConfig.external_inputs` 에 사용자가 직접 기재.
  B. 스캔형 — workflow_data / stage_params 의 문자열에서 `${VAR}` 추출해 후보 제시.

엔진 레지스트리 단일 진실 소스:
  - provider 별 API key env var → `providers.PROVIDER_API_KEY_MAP`
  - 그 외 타입은 힌트 키워드(url/http/endpoint → url, secret/key/token → secret)로 추정.

하드코딩 금지. 새 provider 는 `register_provider(..., api_key_env=...)` 한 줄로 자동 반영됨.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Iterable, Optional


# ${VAR} / ${VAR:default} / ${VAR|description}
PLACEHOLDER_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]{1,127})(?::([^}|]*))?(?:\|([^}]*))?\}")

# secret/url 힌트 — 키 이름 suffix 기반 (명시 선언이 없을 때만 폴백).
_SECRET_HINTS = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS")
_URL_HINTS = ("_URL", "_ENDPOINT", "_BASE_URL", "_HOST")


class InputType(str, Enum):
    SECRET = "secret"
    URL = "url"
    STRING = "string"
    INT = "int"
    BOOL = "bool"


@dataclass
class ExternalInputSpec:
    """단일 외부 입력 명세. UI 폼 자동 렌더 소스."""

    name: str
    type: str = InputType.STRING.value
    required: bool = True
    default: Any = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # 보안: SECRET 타입은 default(평문 시크릿)를 **절대 직렬화하지 않는다**.
        # 배포 산출물(spec.json/wheel)에 박히면 그대로 유출되고, 런타임이 env 대신
        # baked default 로 조용히 폴백한다. SECRET 은 항상 env/override 로만 주입.
        if str(self.type) == InputType.SECRET.value:
            data["default"] = None
            data["required"] = True
        return {k: v for k, v in data.items() if v is not None or k in {"required"}}

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> "ExternalInputSpec":
        t = str(raw.get("type", InputType.STRING.value))
        is_secret = (t == InputType.SECRET.value)
        # 값이 미등록 타입이면 그대로 유지 (외부 확장 여지) — 다만 validator 가 경고.
        return cls(
            name=name,
            type=t,
            required=True if is_secret else bool(raw.get("required", True)),
            default=None if is_secret else raw.get("default"),
            description=str(raw.get("description", "")),
        )


class MissingExternalInputError(ValueError):
    """required 외부 입력이 런타임에 누락됐을 때."""


# ────────────────────────────────────────────────────────────
# A. 선언형 → ExternalInputSpec 목록
# ────────────────────────────────────────────────────────────

def parse_declared(declared: dict[str, Any] | None) -> dict[str, ExternalInputSpec]:
    if not declared:
        return {}
    out: dict[str, ExternalInputSpec] = {}
    for name, raw in declared.items():
        if not name:
            continue
        if not isinstance(raw, dict):
            raw = {"type": InputType.STRING.value, "required": True}
        out[name] = ExternalInputSpec.from_dict(name, raw)
    return out


# ────────────────────────────────────────────────────────────
# B. 스캔형 — ${VAR} 플레이스홀더 추출
# ────────────────────────────────────────────────────────────

def _iter_strings(obj: Any) -> Iterable[str]:
    """dict/list/str 중첩에서 문자열만 추출 (재귀)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v)


def _guess_type(name: str) -> str:
    upper = name.upper()
    if any(upper.endswith(s) for s in _SECRET_HINTS):
        return InputType.SECRET.value
    if any(upper.endswith(s) for s in _URL_HINTS):
        return InputType.URL.value
    return InputType.STRING.value


def _provider_env_vars() -> set[str]:
    """엔진이 아는 provider API key 환경변수 집합 — 스캐너가 secret 로 확정하는 근거."""
    try:
        from ..providers import PROVIDER_API_KEY_MAP
    except Exception as _e:
        # providers 모듈 자체 로드 실패 — 스캐너는 env 추정 없이 제공된 선언에만 의존.
        return set()
    return set(PROVIDER_API_KEY_MAP.values())


def scan_placeholders(
    *payloads: Any,
    registered_api_key_envs: Optional[set[str]] = None,
) -> dict[str, ExternalInputSpec]:
    """여러 payload(dict/list/str) 를 훑어 `${VAR}` 추출 → ExternalInputSpec.

    중복된 VAR 는 첫 등장의 description/default 만 유지.
    provider 레지스트리에 등록된 env var 는 type=secret 으로 확정.
    """
    api_envs = registered_api_key_envs if registered_api_key_envs is not None else _provider_env_vars()
    found: dict[str, ExternalInputSpec] = {}

    for payload in payloads:
        for s in _iter_strings(payload):
            for m in PLACEHOLDER_RE.finditer(s):
                name, default, description = m.group(1), m.group(2), m.group(3)
                if name in found:
                    continue
                t = InputType.SECRET.value if name in api_envs else _guess_type(name)
                is_secret = (t == InputType.SECRET.value)
                # SECRET 은 인라인 default(${KEY:sk-LIVE})를 무시 — baking 금지 + 항상 required.
                has_default = default is not None and not is_secret
                found[name] = ExternalInputSpec(
                    name=name,
                    type=t,
                    required=True if is_secret else not has_default,
                    default=default if has_default else None,
                    description=(description or "").strip(),
                )
    return found


# ────────────────────────────────────────────────────────────
# 병합 (A+B) — 선언값 우선, 스캔값은 보완.
# ────────────────────────────────────────────────────────────

def merge_scanned(
    declared: dict[str, ExternalInputSpec],
    scanned: dict[str, ExternalInputSpec],
) -> dict[str, ExternalInputSpec]:
    """선언값 우선, 스캔값은 빈 자리만 보완. 결과는 name → spec."""
    out: dict[str, ExternalInputSpec] = {**scanned}
    out.update(declared)  # 선언이 이기도록 덮어씀
    return out


def specs_to_dict(specs: dict[str, ExternalInputSpec]) -> dict[str, dict[str, Any]]:
    return {name: spec.to_dict() for name, spec in specs.items()}


# ────────────────────────────────────────────────────────────
# 런타임 검증 (컴파일 산출물 내부에서 호출)
# ────────────────────────────────────────────────────────────

def collect_runtime_values(
    specs: dict[str, ExternalInputSpec],
    *,
    env: Optional[dict[str, str]] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """external_inputs 를 env + overrides 로 해석 — default 는 최후 폴백.

    우선순위: overrides > env > default.
    required 인데 아무것도 없으면 ``MissingExternalInputError``.
    """
    env_map = env if env is not None else os.environ
    ovr = overrides or {}
    resolved: dict[str, Any] = {}
    missing: list[str] = []

    for name, spec in specs.items():
        if name in ovr:
            resolved[name] = ovr[name]
            continue
        if name in env_map and env_map[name] != "":
            resolved[name] = env_map[name]
            continue
        # SECRET 은 baked default 로 폴백하지 않는다 — 누락이면 명시적으로 에러(env/override 강제).
        if spec.default is not None and str(spec.type) != InputType.SECRET.value:
            resolved[name] = spec.default
            continue
        if spec.required:
            missing.append(name)

    if missing:
        raise MissingExternalInputError(
            f"required external inputs missing: {sorted(missing)} "
            f"(provide via environment variables or overrides)"
        )
    return resolved


def validate_external_inputs(
    declared_raw: dict[str, Any] | None,
) -> list[str]:
    """선언된 external_inputs 가 알려진 타입인지 검증 — 경고 메시지 리스트 반환."""
    warnings: list[str] = []
    known = {t.value for t in InputType}
    for name, raw in (declared_raw or {}).items():
        if not isinstance(raw, dict):
            warnings.append(f"{name}: spec is not a dict")
            continue
        t = str(raw.get("type", InputType.STRING.value))
        if t not in known:
            warnings.append(f"{name}: unknown type '{t}' (will be treated as string)")
    return warnings
