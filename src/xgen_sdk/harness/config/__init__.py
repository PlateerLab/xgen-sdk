"""
xgen_harness.config — 외부 설정 source 어댑터

v1.10.0 — HarnessConfig.resolve(sources=[...]) 의 source 구현 모음.

5 단계 resolution chain (위가 우선):
    1. 코드 인자 (HarnessConfig(...))
    2. ENV (XGEN_HARNESS_* prefix + `__` nested)
    3. Config file (toml/json/yaml)
    4. Cluster origin default (transpile 산출물의 DictConfigSource)
    5. SDK builtin default

deep merge — 사용자가 `stage_params.s06_context.top_k` 만 override 해도
다른 값 보존.

사용:
    from xgen_harness.config import DictConfigSource, EnvConfigSource, FileConfigSource

    config = HarnessConfig.resolve(sources=[
        EnvConfigSource(prefix="XGEN_HARNESS_"),
        FileConfigSource("./xgen-harness.toml"),
        DictConfigSource({...cluster origin default...}),
    ])
"""

from .sources import (
    ConfigSource,
    DictConfigSource,
    EnvConfigSource,
    FileConfigSource,
    deep_merge,
)

__all__ = [
    "ConfigSource",
    "DictConfigSource",
    "EnvConfigSource",
    "FileConfigSource",
    "deep_merge",
]
