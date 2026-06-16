"""s00_harness — Harness Planner 메타 스테이지 패키지.

v0.12.0 부터 각 Stage 를 **디렉토리 구조** 로 관리한다 (외부 기여자 플러그인
뺐다꼈다 대비). 다음과 같은 규약을 따른다:

    stages/s00_harness/
        __init__.py    — 퍼사드: 기존 경로 `from xgen_harness.stages.s00_harness import HarnessStage` 유지
        stage.py       — Stage ABC 구현체
        (옵션) strategies.py  — 이 Stage 전용 Strategy 구현
        (옵션) schema.py      — 파라미터 스키마 (stage_config 와 별도면)
        (옵션) README.md      — 외부 기여자용 로컬 문서 (src 밖으로 migrate 전 임시 허용)

이 레이아웃은 나중에 `pip install xgen-stage-harness-core` 같은 분리 패키지로
옮길 때 디렉토리 통째로 이동하면 된다. `registry.py` 는 import 경로를
`from ..stages.s00_harness import HarnessStage` 로 유지하므로 하위 호환 유지.
"""

from .stage import HarnessStage

__all__ = ["HarnessStage"]
