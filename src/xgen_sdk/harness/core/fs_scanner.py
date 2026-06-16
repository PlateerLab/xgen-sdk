"""
Filesystem Scanner — "파일 구조가 곧 카탈로그" 의 실전 구현 (v0.15.2).

철학:
  - 엔진 안에 Stage 이름을 리터럴로 박지 않는다.
  - `stages/` 디렉토리를 훑어 각 `sNN_xxx/` 패키지를 import 하고, 패키지가 export 한
    `Stage` 서브클래스를 자동 register.
  - 외부 기여자는 `stages/s04_tool_lotte/` 처럼 디렉토리만 만들고 `stage.py` 에
    HarnessStage 서브클래스를 두면 엔진 코드 수정 없이 합류.
  - LLM 에게는 `stages[].source_file` 로 실제 파일 경로도 노출 → "구조를 보고 판단" 가능.

외부 플러그인은 여전히 entry_points `xgen_harness.stages` 도 지원 (registry.py 에서 처리).
여기는 **엔진 본체 내부의 기본 Stage** 가 수동 import 를 벗어나게 하는 것이 목표.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Type

if TYPE_CHECKING:
    from .registry import ArtifactRegistry
    from .stage import Stage

logger = logging.getLogger("harness.fs_scanner")


# 규약: stages/sNN_name 디렉토리 이름 패턴. 숫자 2자리 + 밑줄 + 이름.
# 외부 기여자도 이 패턴만 지키면 자동 합류.
_STAGE_DIR_RE = re.compile(r"^s\d{2}_[a-z0-9_]+$")

# vendored 위치 독립 — 이 모듈(harness.core.fs_scanner)의 부모가 곧 하네스 루트 패키지.
_HARNESS_ROOT = __package__.rsplit(".", 1)[0]


def scan_default_stages(registry: "ArtifactRegistry") -> int:
    """`xgen_harness/stages/` 디렉토리를 훑어 기본 Stage 를 자동 등록.

    각 `sNN_xxx/` 디렉토리:
      1. `xgen_harness.stages.sNN_xxx` 패키지 import
      2. 패키지 네임스페이스에서 `Stage` 서브클래스 중 하나 탐색
      3. `registry.register(stage_id=sNN_xxx, artifact="default", cls)` 호출

    반환: 등록된 Stage 수.

    주의:
      - 디렉토리 이름 = stage_id. Stage 클래스의 `stage_id` property 와 일치해야 함.
      - 여러 클래스가 있으면 순회 순서상 첫 번째를 채택. artifact 변형은 entry_points
        경로(`stages/sNN_xxx__alt`) 로 분리.
    """
    from .stage import Stage

    stages_dir = _locate_stages_dir()
    if stages_dir is None or not stages_dir.exists():
        logger.debug("[fs_scanner] stages directory not found, skipping")
        return 0

    count = 0
    for entry in sorted(stages_dir.iterdir()):
        if not entry.is_dir():
            continue
        if not _STAGE_DIR_RE.match(entry.name):
            continue

        module_name = f"{_HARNESS_ROOT}.stages.{entry.name}"
        try:
            mod = importlib.import_module(module_name)
        except Exception as e:
            logger.debug("[fs_scanner] import %s failed: %s", module_name, e)
            continue

        stage_cls = _find_stage_class(mod, Stage)
        if stage_cls is None:
            logger.debug("[fs_scanner] no Stage subclass found in %s", module_name)
            continue

        try:
            registry.register(entry.name, "default", stage_cls)
            count += 1
            logger.debug("[fs_scanner] registered %s -> %s", entry.name, stage_cls.__name__)
        except Exception as e:
            logger.debug("[fs_scanner] register %s failed: %s", entry.name, e)

    return count


def scan_stage_artifacts(
    registry: "ArtifactRegistry",
    *,
    artifact_map: Optional[dict[str, dict[str, str]]] = None,
) -> int:
    """Stage 의 **대안 artifact** (swap-in 변형) 를 디렉토리에서 발견.

    convention: `stages/sNN_xxx/artifacts/<artifact_name>.py` 에
    Stage 서브클래스가 있으면 `registry.register(stage_id, artifact_name, cls)` 로 등록.

    이 함수는 optional — 외부 기여자가 artifacts/ 디렉토리를 만들 때만 동작.
    현재 레포에는 없으므로 count 0 가능.
    """
    from .stage import Stage

    stages_dir = _locate_stages_dir()
    if stages_dir is None:
        return 0

    count = 0
    for stage_dir in sorted(stages_dir.iterdir()):
        if not stage_dir.is_dir() or not _STAGE_DIR_RE.match(stage_dir.name):
            continue
        artifacts_dir = stage_dir / "artifacts"
        if not artifacts_dir.exists() or not artifacts_dir.is_dir():
            continue
        for py_file in sorted(artifacts_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            module_name = f"{_HARNESS_ROOT}.stages.{stage_dir.name}.artifacts.{py_file.stem}"
            try:
                mod = importlib.import_module(module_name)
            except Exception as e:
                logger.debug("[fs_scanner] import artifact %s failed: %s", module_name, e)
                continue
            cls = _find_stage_class(mod, Stage)
            if cls is None:
                continue
            try:
                registry.register(stage_dir.name, py_file.stem, cls)
                count += 1
            except Exception as e:
                logger.debug("[fs_scanner] register artifact %s failed: %s", module_name, e)
    return count


def scan_stage_strategies() -> int:
    """Stage 내부 `strategies/` 디렉토리를 훑어 Strategy 구현체를 자동 등록 (v0.15.3).

    Convention:
      1. `stages/sNN_xxx/strategies/<slot>__<impl>.py` — 밑줄 2개로 slot/impl 분리
      2. `stages/sNN_xxx/strategies/<slot>/<impl>.py` — 슬롯 서브디렉토리 방식

    두 convention 모두 지원. Strategy 서브클래스가 export 되면
    `register_strategy(stage_id, slot, impl, cls)` 자동 호출.

    외부 기여자는 stage 디렉토리에 파일만 드롭하면 런타임 자동 합류.
    기존 공용 `stages/strategies/*.py` 는 이 스캔 대상 **아님** (이미 코드에서
    명시 register 하므로). 이 함수는 "Stage 별 독립 디렉토리" 만 다룬다.

    반환: 등록된 Strategy 수.
    """
    from ..stages.interfaces import Strategy
    from .strategy_resolver import register_strategy

    stages_dir = _locate_stages_dir()
    if stages_dir is None or not stages_dir.exists():
        return 0

    count = 0
    for stage_dir in sorted(stages_dir.iterdir()):
        if not stage_dir.is_dir() or not _STAGE_DIR_RE.match(stage_dir.name):
            continue
        strategies_dir = stage_dir / "strategies"
        if not strategies_dir.exists() or not strategies_dir.is_dir():
            continue

        stage_id = stage_dir.name

        # convention 1: 평면 파일 `<slot>__<impl>.py`
        for py_file in sorted(strategies_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            stem = py_file.stem
            if "__" not in stem:
                # slot 접미사 없는 단일 파일 — 다음 convention 에서 처리하므로 스킵.
                continue
            slot, _, impl = stem.partition("__")
            cls = _import_strategy_class(
                f"{_HARNESS_ROOT}.stages.{stage_id}.strategies.{stem}", Strategy
            )
            if cls is None:
                continue
            try:
                register_strategy(stage_id, slot, impl, cls)
                count += 1
            except Exception as e:
                logger.debug(
                    "[fs_scanner] register_strategy %s/%s/%s failed: %s",
                    stage_id, slot, impl, e,
                )

        # convention 2: 슬롯 서브디렉토리 `<slot>/<impl>.py`
        for slot_dir in sorted(strategies_dir.iterdir()):
            if not slot_dir.is_dir() or slot_dir.name.startswith("_"):
                continue
            slot = slot_dir.name
            for py_file in sorted(slot_dir.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                impl = py_file.stem
                cls = _import_strategy_class(
                    f"{_HARNESS_ROOT}.stages.{stage_id}.strategies.{slot}.{impl}",
                    Strategy,
                )
                if cls is None:
                    continue
                try:
                    register_strategy(stage_id, slot, impl, cls)
                    count += 1
                except Exception as e:
                    logger.debug(
                        "[fs_scanner] register_strategy %s/%s/%s failed: %s",
                        stage_id, slot, impl, e,
                    )

    return count


def _import_strategy_class(module_name: str, base_cls):
    """지정 모듈을 import 하고 base_cls 서브클래스 하나 반환."""
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        logger.debug("[fs_scanner] import %s failed: %s", module_name, e)
        return None
    return _find_stage_class(mod, base_cls)


def get_stage_source_file(stage_cls: Type) -> str:
    """Stage 클래스의 소스 파일 경로(프로젝트 기준 상대 경로) 반환.

    catalog 에 `stages[].source_file` 로 넣어 LLM 이 "이 Stage 는 어디에 있고
    내부 어떤 구조인가" 보고 판단 가능.
    """
    try:
        abs_path = inspect.getsourcefile(stage_cls) or ""
        if not abs_path:
            return ""
        # xgen_harness/ 이하 상대 경로로 절단 (그 이전 prefix 는 시스템 의존적).
        p = Path(abs_path)
        parts = p.parts
        if "xgen_harness" in parts:
            idx = parts.index("xgen_harness")
            return str(Path(*parts[idx:]))
        return p.name
    except Exception:
        return ""


# ─── internals ──────────────────────────────────────────────────────

def _locate_stages_dir() -> Optional[Path]:
    """이 모듈 위치 기준으로 `stages/` 디렉토리 해석."""
    try:
        here = Path(__file__).resolve()
        # xgen_harness/core/fs_scanner.py → xgen_harness/stages
        return here.parent.parent / "stages"
    except Exception:
        return None


def _find_stage_class(mod, base: Type) -> Optional[Type]:
    """모듈 네임스페이스에서 `base` 의 서브클래스 중 하나를 반환.

    __all__ 이 있으면 우선 검사. 없으면 dir() 순회. base 자체는 제외.
    여러 후보가 있으면 alphabetical 첫 번째 (stability).
    """
    candidates: list[tuple[str, Type]] = []
    names: list[str]
    exported = getattr(mod, "__all__", None)
    if isinstance(exported, (list, tuple)) and exported:
        names = list(exported)
    else:
        names = dir(mod)

    for name in names:
        obj = getattr(mod, name, None)
        if not isinstance(obj, type):
            continue
        try:
            if issubclass(obj, base) and obj is not base:
                candidates.append((name, obj))
        except TypeError:
            continue

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]
