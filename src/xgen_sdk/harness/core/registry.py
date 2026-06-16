"""
ArtifactRegistry — 스테이지 ↔ 구현체 매핑

artifact 시스템: stage_id → { artifact_name → StageClass }
프리셋에 따라 파이프라인 스테이지 목록을 빌드.

플러그인 확장:
  - entry_points(group="xgen_harness.stages") 로 외부 패키지에서 스테이지 자동 등록
  - register_stage() 공개 API 로 런타임 직접 등록
"""

import logging
import sys
from typing import Optional, Type

from .config import HarnessConfig, ALL_STAGES
from .stage import Stage, STAGE_DISPLAY_NAMES

logger = logging.getLogger("harness.registry")

# alias → stage_id 매핑
_ALIASES: dict[str, str] = {}
for sid in STAGE_DISPLAY_NAMES:
    # "s07_act" → aliases: "act", "ACT", "7"
    short = sid.split("_", 1)[1] if "_" in sid else sid
    num = sid[1:3] if sid.startswith("s") else ""
    _ALIASES[short] = sid
    _ALIASES[short.lower()] = sid
    _ALIASES[short.upper()] = sid
    _ALIASES[STAGE_DISPLAY_NAMES[sid]] = sid
    _ALIASES[STAGE_DISPLAY_NAMES[sid].lower()] = sid
    if num:
        _ALIASES[num] = sid
        _ALIASES[str(int(num))] = sid  # "07" → "7"


class ArtifactRegistry:
    """스테이지별 아티팩트(구현체) 레지스트리"""

    def __init__(self):
        # stage_id → { artifact_name → StageClass }
        self._registry: dict[str, dict[str, Type[Stage]]] = {}

    def register(self, stage_id: str, artifact_name: str, stage_class: Type[Stage]) -> None:
        if stage_id not in self._registry:
            self._registry[stage_id] = {}
        if artifact_name in self._registry[stage_id]:
            prev = self._registry[stage_id][artifact_name]
            if prev is not stage_class:
                logger.warning(
                    "Artifact 덮어쓰기: %s/%s (%s → %s)",
                    stage_id, artifact_name, prev.__name__, stage_class.__name__,
                )
        self._registry[stage_id][artifact_name] = stage_class
        logger.debug("Registered artifact: %s/%s", stage_id, artifact_name)

    def resolve_stage_id(self, key: str) -> str:
        """alias/display/numeric → stage_id 해석"""
        if key in self._registry:
            return key
        resolved = _ALIASES.get(key) or _ALIASES.get(key.lower())
        if resolved:
            return resolved
        raise KeyError(f"Unknown stage key: {key!r}")

    def get(self, stage_id: str, artifact_name: str = "default") -> Type[Stage]:
        """스테이지 클래스 반환"""
        artifacts = self._registry.get(stage_id)
        if not artifacts:
            raise KeyError(f"No artifacts registered for stage: {stage_id}")
        cls = artifacts.get(artifact_name)
        if not cls:
            if "default" in artifacts:
                logger.warning("Artifact %r not found for %s, using default", artifact_name, stage_id)
                return artifacts["default"]
            raise KeyError(f"Artifact {artifact_name!r} not found for stage {stage_id}")
        return cls

    def list_artifacts(self, stage_id: str) -> list[str]:
        return list(self._registry.get(stage_id, {}).keys())

    def list_stages(self) -> list[str]:
        return sorted(self._registry.keys())

    def build_pipeline_stages(self, config: HarnessConfig) -> list[Stage]:
        """설정에 따라 스테이지 인스턴스 목록 빌드"""
        active_ids = config.get_active_stage_ids()
        stages: list[Stage] = []

        for stage_id in active_ids:
            artifact_name = config.get_artifact_for_stage(stage_id)
            try:
                stage_cls = self.get(stage_id, artifact_name)
                stages.append(stage_cls())
            except KeyError as e:
                logger.error("Failed to build stage %s: %s", stage_id, e)
                continue

        stages.sort(key=lambda s: s.order)
        return stages

    def describe_all(self, config: Optional[HarnessConfig] = None) -> list[dict]:
        """모든 등록된 스테이지의 설명 목록 (API/UI용).

        stage_config.py의 설정 스키마도 포함하여,
        UI에서 스테이지 클릭 시 설정 필드를 렌더링할 수 있다.
        """
        from .stage_config import get_stage_config
        from .config import REQUIRED_STAGES

        active_ids = config.get_active_stage_ids() if config else list(self._registry.keys())
        descriptions = []
        for stage_id in sorted(self._registry.keys()):
            artifacts = self._registry[stage_id]
            default_cls = artifacts.get("default")
            if default_cls:
                desc = default_cls().describe()
                desc.active = stage_id in active_ids
                stage_cfg = get_stage_config(stage_id)
                current_artifact = (
                    config.get_artifact_for_stage(stage_id) if config else "default"
                )
                # v0.15.2 — Stage 와 각 Strategy 의 실제 소스 파일 경로까지 노출.
                # LLM / 프론트가 "이 Stage 는 어디에 있고 내부 구조는 어떤가" 를 보고 판단.
                from .fs_scanner import get_stage_source_file
                stage_source = get_stage_source_file(default_cls)
                strategies_out: list[dict] = []
                for s in desc.strategies:
                    entry: dict = {
                        "name": s.name,
                        "description": s.description,
                        "is_default": s.is_default,
                    }
                    # Strategy impl 의 소스 파일도 StrategyResolver 가 알고 있으면 꺼내준다.
                    try:
                        # 모듈 전역 _REGISTRY 는 (stage_id, slot_name, impl_name) → cls.
                        # 해당 Stage + impl 이름이 일치하는 엔트리를 찾아 slot 과 파일경로 노출.
                        from .strategy_resolver import _REGISTRY as _STRAT_REG, _ensure_defaults_registered
                        _ensure_defaults_registered()
                        for (sid_k, slot_k, impl_k), cls in _STRAT_REG.items():
                            if sid_k == stage_id and impl_k == s.name:
                                entry["source_file"] = get_stage_source_file(cls)
                                entry["slot"] = slot_k
                                break
                    except Exception:
                        pass
                    strategies_out.append(entry)

                descriptions.append({
                    "stage_id": desc.stage_id,
                    "display_name": desc.display_name,
                    "display_name_ko": desc.display_name_ko,
                    "phase": desc.phase,
                    "order": desc.order,
                    "role": desc.role,
                    "active": desc.active,
                    "required": stage_id in REQUIRED_STAGES,
                    "artifacts": list(artifacts.keys()),
                    "current_artifact": current_artifact,
                    "source_file": stage_source,
                    "strategies": strategies_out,
                    # stage_config: UI 렌더링용 설정 스키마. v1.7.1 — cherry-pick 풀고
                    # stage_cfg 통째로 노출. _inject_visibility_meta 가 박는
                    # expose_strategy_picker, _inject_stage_meta 가 박는 progressive_threshold,
                    # 외부 Stage 가 자기 stage_config 에 박은 임의 키 (bypass_ko/en, icon,
                    # cost_hint 등) 모두 자동 합류 — 프론트 확장성 정합.
                    "config": dict(stage_cfg) if stage_cfg else None,
                })
        return sorted(descriptions, key=lambda d: d["order"])

    @classmethod
    def default(cls) -> "ArtifactRegistry":
        """기본 아티팩트가 등록된 레지스트리 생성"""
        registry = cls()
        _register_default_stages(registry)
        return registry


def _register_default_stages(registry: ArtifactRegistry) -> None:
    """기본 스테이지 등록 — v0.15.2 파일시스템 스캔.

    과거 `from ..stages.s01_input import InputStage` 같은 리터럴 import 12 건을
    제거. 이제 `fs_scanner.scan_default_stages()` 가 `xgen_harness/stages/`
    디렉토리를 훑어 `sNN_xxx/` 패턴 디렉토리의 Stage 서브클래스를 자동 등록.

    외부 기여자는 `stages/s04_tool_lotte/` 디렉토리만 만들고 `__init__.py` 에서
    Stage 서브클래스를 export 하면 엔진 코드 수정 0.
    """
    from .fs_scanner import scan_default_stages, scan_stage_artifacts

    count = scan_default_stages(registry)
    logger.debug("[registry] fs_scanner: %d default stages", count)

    # 같은 Stage 의 대안 artifact (swap-in 변형) 도 디렉토리에서 자동 발견.
    # convention: `stages/sNN_xxx/artifacts/<name>.py` → registry.register(sNN, name, cls)
    art_count = scan_stage_artifacts(registry)
    if art_count:
        logger.debug("[registry] fs_scanner: %d stage artifacts", art_count)

    # 멀티에이전트 planner 는 `orchestrator/` 디렉토리에 있어 일반 Stage 스캔 대상 외 —
    # v1.0: s00_harness 의 multi_agent strategy 로 register (구 s05_strategy 슬롯 삭제됨).
    try:
        from ..orchestrator.multi_agent_planner import MultiAgentPlannerStage
        registry.register("s00_harness", "multi_agent", MultiAgentPlannerStage)
    except ImportError:
        pass

    # 외부 패키지 플러그인 (entry_points 경로) 탐색 — `xgen_harness.stages` 그룹.
    _discover_plugin_stages(registry)


def _discover_plugin_stages(registry: ArtifactRegistry) -> None:
    """Discover stages from installed packages via entry_points."""
    try:
        if sys.version_info >= (3, 10):
            from importlib.metadata import entry_points
            eps = entry_points(group="xgen_harness.stages")
        else:
            from importlib.metadata import entry_points
            eps = entry_points().get("xgen_harness.stages", [])

        for ep in eps:
            try:
                stage_class = ep.load()
                # ep.name 형식:
                #   "s04_tool"          → (stage_id="s04_tool", artifact="default")
                #   "s04_tool__lotte"   → (stage_id="s04_tool", artifact="lotte")
                # __ 구분자로 같은 슬롯에 외부 artifact 를 swap-in 가능.
                if "__" in ep.name:
                    stage_id, artifact_name = ep.name.split("__", 1)
                else:
                    stage_id, artifact_name = ep.name, "default"
                registry.register(stage_id, artifact_name, stage_class)
                logger.info("Plugin stage registered: %s/%s (from ep '%s')",
                            stage_id, artifact_name, ep.name)
            except Exception as e:
                logger.warning("Failed to load plugin stage %s: %s", ep.name, e)
    except Exception as e:
        # v0.11.21 — importlib.metadata 자체가 실패하는 환경(예: editable install 조합)에서는
        # 플러그인 발견이 불가능해도 엔진 부팅은 계속. 디버그로 흔적 남김.
        logger.debug("Plugin stage discovery skipped (no entry_points backend): %s", e)


# ── 싱글턴 레지스트리 (기본 인스턴스) ──────────────────────────────
_DEFAULT_REGISTRY: Optional[ArtifactRegistry] = None


def _get_default_registry() -> ArtifactRegistry:
    """기본 레지스트리 싱글턴 반환 (lazy init)."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ArtifactRegistry.default()
    return _DEFAULT_REGISTRY


def register_stage(stage_id: str, artifact_name: str, stage_class: Type[Stage]) -> None:
    """외부 코드에서 스테이지를 직접 등록하는 공개 API.

    Usage::

        from xgen_harness.core.registry import register_stage
        register_stage("s99_custom", "default", MyCustomStage)
    """
    _get_default_registry().register(stage_id, artifact_name, stage_class)
    logger.info("Stage registered via public API: %s/%s", stage_id, artifact_name)
