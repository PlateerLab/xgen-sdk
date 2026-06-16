"""
ConfigSource Protocol + 3 builtin (Dict / Env / File)

각 source 는 `load() -> dict` 를 구현. HarnessConfig.resolve(sources=[...]) 가
순서대로 호출 + deep merge.

deep merge 규칙:
    - nested dict 는 재귀 merge — 위 source 가 키 일부만 가져도 아래 source 의 다른 키 보존
    - list / scalar 는 위 source 가 통째로 override (덮어쓰기)
    - None / 빈 dict 는 의미 없음 (skip)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("harness.config.sources")


@runtime_checkable
class ConfigSource(Protocol):
    """설정 source 인터페이스.

    구현체는 `load() -> dict` 만 박으면 끝. HarnessConfig.resolve 가 sources 순서대로
    호출 + deep merge.
    """

    def load(self) -> dict[str, Any]:
        """source 에서 설정 dict 로드. 비어있으면 {} 반환 (None X)."""
        ...


# ─────────────────────────────────────────────────────────
# DictConfigSource
# ─────────────────────────────────────────────────────────


class DictConfigSource:
    """명시적 dict source. transpile 산출물의 CLUSTER_DEFAULTS 박을 때 사용.

    Args:
        data: 설정 dict (HarnessConfig 필드 셋과 매핑)
    """

    def __init__(self, data: dict[str, Any]):
        self._data = data or {}

    def load(self) -> dict[str, Any]:
        return dict(self._data)


# ─────────────────────────────────────────────────────────
# EnvConfigSource
# ─────────────────────────────────────────────────────────


class EnvConfigSource:
    """환경변수 source.

    Pydantic settings 패턴 — prefix + `__` (double underscore) 가 nested 구분자.
    값은 JSON 으로 디코드 시도, 실패 시 raw string.

    예시:
        XGEN_HARNESS_SYSTEM_PROMPT="당신은 ..."
            → {"system_prompt": "당신은 ..."}

        XGEN_HARNESS_STAGE_PARAMS__S06_CONTEXT__TOP_K=20
            → {"stage_params": {"s06_context": {"top_k": 20}}}

        XGEN_HARNESS_RAG_COLLECTIONS='["my-coll"]'
            → {"rag_collections": ["my-coll"]}

        XGEN_HARNESS_RUNTIME_DEFAULTS__SYNTH_RAW_THRESHOLD=5000
            → {"runtime_defaults": {"synth_raw_threshold": 5000}}

    Args:
        prefix: 환경변수 prefix (default "XGEN_HARNESS_")
        nested_separator: nested 키 구분자 (default "__")
    """

    def __init__(
        self,
        prefix: str = "XGEN_HARNESS_",
        *,
        nested_separator: str = "__",
        env: Optional[dict[str, str]] = None,
    ):
        self._prefix = prefix
        self._sep = nested_separator
        # env 명시 시 그것 사용 (테스트용), 아니면 os.environ
        self._env = env if env is not None else dict(os.environ)

    def load(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        prefix_len = len(self._prefix)
        for key, value in self._env.items():
            if not key.startswith(self._prefix):
                continue
            stripped = key[prefix_len:]
            if not stripped:
                continue
            # nested separator 로 분할 → 소문자 키
            parts = [p.lower() for p in stripped.split(self._sep) if p]
            if not parts:
                continue
            # 값 파싱: JSON 시도 → 실패 시 raw
            parsed = self._parse_value(value)
            # nested dict 에 박기
            self._set_nested(result, parts, parsed)
        return result

    @staticmethod
    def _parse_value(value: str) -> Any:
        """env value 를 JSON 으로 디코드 시도. 실패 시 raw string."""
        # bool / int / float 자주 쓰이는 케이스 우선
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value

    @staticmethod
    def _set_nested(d: dict, parts: list[str], value: Any) -> None:
        """nested dict 에 parts 경로로 값 박음."""
        current = d
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value


# ─────────────────────────────────────────────────────────
# FileConfigSource
# ─────────────────────────────────────────────────────────


class FileConfigSource:
    """파일 source. toml / json / yaml 자동 감지 (확장자).

    토 ml 우선 (Python 표준, pyproject.toml 친숙). yaml 은 pyyaml 설치 시만.

    Args:
        path: 파일 경로 (Path 또는 str)
        format: "toml" / "json" / "yaml" / "auto" (default — 확장자 감지)
        missing_ok: 파일 없으면 빈 dict 반환 (default True)
    """

    def __init__(
        self,
        path: str | Path,
        *,
        format: str = "auto",
        missing_ok: bool = True,
    ):
        self._path = Path(path)
        self._format = format
        self._missing_ok = missing_ok

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            if self._missing_ok:
                return {}
            raise FileNotFoundError(f"FileConfigSource: {self._path} not found")
        fmt = self._format
        if fmt == "auto":
            ext = self._path.suffix.lower()
            if ext == ".toml":
                fmt = "toml"
            elif ext == ".json":
                fmt = "json"
            elif ext in (".yaml", ".yml"):
                fmt = "yaml"
            else:
                # 기본 toml — 사용자가 명시적으로 박지 않은 경우
                fmt = "toml"
        text = self._path.read_text(encoding="utf-8")
        if fmt == "toml":
            return self._load_toml(text)
        elif fmt == "json":
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        elif fmt == "yaml":
            return self._load_yaml(text)
        else:
            raise ValueError(f"FileConfigSource: unknown format {fmt!r}")

    @staticmethod
    def _load_toml(text: str) -> dict[str, Any]:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                raise ImportError(
                    "FileConfigSource(toml): Python 3.11+ 또는 `pip install tomli` 필요"
                )
        data = tomllib.loads(text)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _load_yaml(text: str) -> dict[str, Any]:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            raise ImportError(
                "FileConfigSource(yaml): `pip install pyyaml` 필요"
            )
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}


# ─────────────────────────────────────────────────────────
# deep_merge — sources 결과 합치는 핵심 유틸
# ─────────────────────────────────────────────────────────


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """overlay 가 base 위에 박힘. overlay 가 우선.

    - nested dict 는 재귀 merge — overlay 가 일부 키만 가져도 base 의 다른 키 보존
    - list / scalar / None 은 overlay 통째로 override
    - overlay 가 빈 dict / None 은 base 그대로

    예:
        base    = {"stage_params": {"s06": {"top_k": 10, "filter": {...}}}}
        overlay = {"stage_params": {"s06": {"top_k": 20}}}
        result  = {"stage_params": {"s06": {"top_k": 20, "filter": {...}}}}
    """
    if not overlay:
        return dict(base)
    result = dict(base)
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = deep_merge(base_value, overlay_value)
        else:
            result[key] = overlay_value
    return result
