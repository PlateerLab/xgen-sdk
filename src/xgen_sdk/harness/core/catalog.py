"""
Catalog — 런타임에 "사용 가능한 환경"을 LLM Planner 에게 제시하기 위한 JSON.

철학 (REAL_HARNESS §5):
- 지도를 **하드코딩하지 않는다**. Stage/필드/Strategy 이름 리터럴이 이 파일 안에 0 개여야 한다.
- 전부 레지스트리(`ArtifactRegistry` / `stage_config` / `StrategyResolver` / `CapabilityRegistry`
  / `ResourceRegistry` / `ToolRegistry`) 에서 **런타임에 발견**한다.
- 이 카탈로그를 보고 LLM 이 Stage 를 고르고 파라미터를 결정한다 (`planner.py`).

외부 기여자가 `register_stage()` 나 `register_capability()` 로 새 요소를 등록하면
그 즉시 이 카탈로그에 반영된다. 즉 "환경은 자동 연동" 이 이 함수의 결론이다.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("harness.catalog")


async def get_catalog_async(
    config: Optional["HarnessConfig"] = None,  # type: ignore  # noqa: F821
    *,
    include_tools: bool = True,
    include_capabilities: bool = True,
    include_presets: bool = True,
    include_resources: bool = True,
    user_input: str = "",
    workflow_hints: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """비동기 카탈로그. ToolSource.list_tools() 를 실제로 await 해서 도구 목록까지 실측.

    Planner 는 이 async 버전을 써야 "각 스테이지별 어떤 도구 쓸지" 까지 LLM 에게
    넘길 수 있다. 동기 `get_catalog()` 는 static 정보만(fields / strategies / phase)
    담고 도구 이름 리스트는 빠진다.
    """
    catalog = get_catalog(
        config=config,
        include_tools=False,                   # async 실측으로 대체
        include_capabilities=include_capabilities,
        include_presets=include_presets,
        include_resources=include_resources,
        user_input=user_input,
        workflow_hints=workflow_hints,
    )

    if include_tools:
        catalog["tools"] = await _collect_tools_async()

    # workflow_hints 에 이식측이 이미 채워준 mcp_sessions / rag_collections 등이 있으면
    # 해당 Stage 의 fields 를 가진 entry 에 그대로 남겨두고 (이미 stages 에 포함),
    # 별도 top-level "available_resources" 도 노출해 Planner 가 "이 요청에 어떤 RAG
    # 컬렉션 쓰자" 같은 결정을 명시적으로 할 수 있게 한다.
    if workflow_hints:
        resources_hint = {
            k: v for k, v in workflow_hints.items()
            if k in ("mcp_sessions", "rag_collections", "custom_tools",
                     "capabilities", "node_tags", "folders", "files",
                     "db_connections", "ontology_collections")
        }
        if resources_hint:
            catalog["available_resources"] = resources_hint

    return catalog


async def _collect_tools_async() -> list[dict[str, Any]]:
    """ToolSource.list_tools() 를 await 해서 실제 사용 가능한 도구 이름/설명 수집."""
    try:
        from ..tools import get_tool_sources
        sources = get_tool_sources() or []
    except Exception as e:
        logger.debug("tool_sources unavailable: %s", e)
        return []

    merged: list[dict[str, Any]] = []
    for source in sources:
        try:
            list_tools = getattr(source, "list_tools", None)
            if list_tools is None:
                continue
            tools = await list_tools()
            if not isinstance(tools, list):
                continue
            source_label = type(source).__name__
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                merged.append({
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "source": source_label,
                })
        except Exception as e:
            logger.debug("tool source %s list_tools failed: %s", type(source).__name__, e)
    return merged


def get_catalog(
    config: Optional["HarnessConfig"] = None,  # type: ignore  # noqa: F821
    *,
    include_tools: bool = True,
    include_capabilities: bool = True,
    include_presets: bool = True,
    include_resources: bool = False,
    user_input: str = "",
    workflow_hints: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """런타임 카탈로그 반환.

    Parameters
    ----------
    config : HarnessConfig, optional
        전달 시 현재 활성 스테이지만 표시 (`is_stage_active` 기반).
        None 이면 등록된 전체 Stage.
    include_tools, include_capabilities, include_presets : bool
        각 섹션 포함 여부. Plan 단계에서 필요 없으면 끄는 게 토큰 절약.
    include_resources : bool
        MCP 세션/RAG 컬렉션 같이 호스트가 주입한 동적 리소스. 기본 off —
        엔진 단독 실행(Compile/MCP)에서는 빈 값.
    user_input : str
        Planner 에 같이 묶어 보낼 사용자 요청. Plan 의 근거.
    workflow_hints : dict, optional
        이식 측이 전달한 추가 컨텍스트 (예: 현재 thread 의 최근 Plan, 운영 시간대 등).

    Returns
    -------
    dict
        JSON 직렬화 가능한 카탈로그. Planner 프롬프트에 그대로 임베드.
    """
    catalog: dict[str, Any] = {
        "schema_version": 1,
        "user_input": user_input,
    }

    catalog["stages"] = _collect_stages(config)
    catalog["required_stages"] = _collect_required_stages()
    catalog["orchestrators"] = _collect_orchestrators()
    catalog["providers"] = _collect_providers()
    catalog["phases"] = _collect_phases()

    if include_capabilities:
        catalog["capabilities"] = _collect_capabilities()
    if include_tools:
        catalog["tools"] = _collect_tools()
    if include_presets:
        catalog["presets"] = _collect_presets()
    if include_resources:
        catalog["resources"] = _collect_resources(config)

    if workflow_hints:
        catalog["workflow_hints"] = dict(workflow_hints)

    return catalog


# ───────────────────────────────────────────────────────────────────
#  Stage 카탈로그
# ───────────────────────────────────────────────────────────────────

def _collect_stages(config) -> list[dict[str, Any]]:
    """ArtifactRegistry 의 describe_all() 을 래핑해 Planner 가 바로 쓸 수 있는 형태로 변환.

    describe_all() 은 이미 stage_config.fields / strategies / phase / order / required 까지
    전부 JSON 화해서 주므로 여기서는 재구성만 한다. **Stage 이름 / 필드 이름 / Strategy 이름
    리터럴이 이 함수 안에 0 개** 임을 유지하는 것이 허브 정신의 지표.
    """
    from .registry import _get_default_registry

    registry = _get_default_registry()
    raw = registry.describe_all(config)

    # describe_all 결과 + stage_config.py 의 self-describing 필드(when_to_use /
    # when_to_skip / cost_hint) 를 같이 싣는다. Planner 는 이 세 필드만 보고
    # "이 Stage 를 이번에 쓸지" 를 결정한다. 시스템 프롬프트 가이드 제거의 핵심.
    #
    # 주의: registry.describe_all() 의 "config" 블록은 네 필드 (description_ko
    # / description_en / fields / behavior) 만 실어준다. self-describing 세 필드는
    # stage_config 원본(get_stage_config) 에서 직접 꺼내야 카탈로그에 실린다.
    from .stage_config import get_stage_config

    stages: list[dict[str, Any]] = []
    for entry in raw:
        cfg = entry.get("config") or {}
        raw_cfg = get_stage_config(entry["stage_id"]) or {}
        stages.append({
            "stage_id": entry["stage_id"],
            "display_name": entry.get("display_name", ""),
            "phase": entry.get("phase", ""),
            "order": entry.get("order", 0),
            "required": entry.get("required", False),
            "active": entry.get("active", True),
            "artifacts": entry.get("artifacts", []),
            "current_artifact": entry.get("current_artifact", "default"),
            # v0.15.2 — 파일 경로 노출. LLM 이 "이 Stage 는 어디에 있는지" 파악 → 내부
            # 구조(strategies/ / variants/ / tools/) 를 유추해 자율 조립.
            "source_file": entry.get("source_file", ""),
            # v0.15.0 — Strategy 상세(name + description + is_default) 포함.
            # Planner 가 "이 Stage 에 어떤 impl 들이 있고 각각 뭘 하는지" 알고 고름.
            # v0.15.2 — 각 Strategy 도 slot / source_file 을 포함.
            "strategies": entry.get("strategies", []),
            # v0.12.0 self-describing — Stage 저자가 직접 선언한 사용/제외 기준.
            # v0.17.0 — machine meta 만 카탈로그에 실음. description_ko/display_name_ko
            # /behavior 같은 UI 전용 필드는 제거 (LLM 안 읽음 + 토큰 낭비). UI 는 별도
            # 엔드포인트(get_all_stage_configs) 경로로 받는다.
            "when_to_use": raw_cfg.get("when_to_use", ""),
            "when_to_skip": raw_cfg.get("when_to_skip", ""),
            "cost_hint": raw_cfg.get("cost_hint", ""),
            "fields": _clean_fields(cfg.get("fields", [])),
            # v0.15.0 재귀적 자율주행 — 파라미터로 전달되는 **도구 슬롯** 설명을
            # Stage 별로 자기서술 필드(tool_slots)로 뽑아 Planner 에게 노출.
            # Planner 가 이걸 보고 "이 요청엔 rag_collections=[X] 를 쓰자" 결정.
            "tool_slots": raw_cfg.get("tool_slots", []),
        })
    return stages


def _clean_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Planner 프롬프트용 필드 슬림화.

    v0.17.0 — label/description(한국어 UI 리터럴) 은 싣지 않음. Planner 는 id/type/
    default/제약(options/min/max) 만 보면 파라미터 값 결정 가능. UI 전용 자연어는
    UI 경로(get_all_stage_configs)에서만 소비.

    options_source 가 달린 동적 필드는 options 가 비어있을 수 있다. 실제 options
    해석은 이식/호스트 측 OptionsRegistry 에서 한다 — 카탈로그 안에 밀어넣지
    않아야 토큰 절약.
    """
    cleaned = []
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        entry: dict[str, Any] = {
            "id": f.get("id", ""),
            "type": f.get("type", ""),
            "default": f.get("default"),
        }
        # options 는 enum 타입일 때만 유지 (options_source 원격 해석은 제외)
        if isinstance(f.get("options"), list) and f["options"]:
            entry["options"] = f["options"]
        if "min" in f:
            entry["min"] = f["min"]
        if "max" in f:
            entry["max"] = f["max"]
        if f.get("required"):
            entry["required"] = True
        cleaned.append(entry)
    return cleaned


def _collect_required_stages() -> list[str]:
    """Planner 가 함부로 제외하지 못하게 막을 필수 Stage 집합."""
    from .config import REQUIRED_STAGES
    return sorted(REQUIRED_STAGES)


def _collect_orchestrators() -> list[dict[str, Any]]:
    """OrchestratorRegistry 전수 — LLM 이 orchestrator_hint 고를 때 근거로 사용.

    엔진 기본 5개 + entry_points 로 등록된 외부 패턴이 여기 합산된다.
    하드코딩된 목록이 이 함수 안에 0 개이며, 레지스트리 상태 변화가 즉시 반영.
    """
    try:
        from .orchestrator_registry import get_orchestrator_specs
        return get_orchestrator_specs()
    except Exception as e:
        logger.debug("orchestrator registry unavailable: %s", e)
        return []


def _collect_providers() -> list[dict[str, Any]]:
    """ProviderRegistry 전수. entry_points 자동 발견된 외부 provider 도 합류.

    Planner 가 "이 요청엔 어떤 provider 가 나을까" 판단 근거 / 프론트 드롭다운 원천.
    provider 이름 리터럴 0.
    """
    try:
        from ..providers import list_providers, get_default_model, PROVIDER_CONTEXT_LIMITS
        out: list[dict[str, Any]] = []
        for name in list_providers():
            out.append({
                "name": name,
                "default_model": get_default_model(name),
                "context_limit": PROVIDER_CONTEXT_LIMITS.get(name, 0),
            })
        return out
    except Exception as e:
        logger.debug("providers unavailable in catalog: %s", e)
        return []


def _collect_phases() -> list[dict[str, Any]]:
    """PhaseRegistry 전수. 외부 phase (post_egress 등) 도 자동 합류."""
    try:
        from .phase_registry import get_phase_specs
        return get_phase_specs()
    except Exception as e:
        logger.debug("phase registry unavailable: %s", e)
        return []


# ───────────────────────────────────────────────────────────────────
#  Capability / Tool / Preset / Resource
# ───────────────────────────────────────────────────────────────────

def _collect_capabilities() -> list[dict[str, Any]]:
    """CapabilityRegistry 의 전체 스펙을 Planner 가 이해 가능한 slim 포맷으로."""
    try:
        from ..capabilities import get_default_registry
        registry = get_default_registry()
        specs = list(registry.list_all())
    except Exception as e:
        logger.debug("capabilities unavailable in catalog: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for spec in specs:
        try:
            out.append({
                "name": getattr(spec, "name", ""),
                "description": getattr(spec, "description", ""),
                "provider_kind": getattr(getattr(spec, "provider_kind", None), "value", ""),
                "required_params": [
                    getattr(p, "name", "") for p in getattr(spec, "required_params", []) or []
                ],
                "optional_params": [
                    getattr(p, "name", "") for p in getattr(spec, "optional_params", []) or []
                ],
            })
        except Exception as e:
            logger.debug("skip capability serialization: %s", e)
    return out


def _collect_tools() -> list[dict[str, Any]]:
    """Tool 카탈로그 placeholder.

    ToolSource.list_tools() 가 async 이기 때문에 동기 `get_catalog()` 경로에서는
    이름만 뽑는다. Phase 1 Planner 는 Stage · Capability 중심이라 Tool 개별 선택을
    하지 않으므로 이 항목이 비어도 동작. Phase 2 에서 `get_catalog_async()` 로 확장.
    """
    try:
        from ..tools import get_tool_sources
        sources = get_tool_sources() or []
    except Exception as e:
        logger.debug("tool_sources unavailable: %s", e)
        return []

    return [{"source_type": type(s).__name__} for s in sources]


def _collect_presets() -> list[dict[str, Any]]:
    """PRESETS 레지스트리 전량 — Planner 가 프리셋 추천할 수 있게."""
    try:
        from .presets import PRESETS
    except Exception as e:
        logger.debug("presets unavailable: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for name, preset in (PRESETS or {}).items():
        try:
            out.append({
                "name": name,
                "description": getattr(preset, "description", ""),
                "disabled_stages": sorted(getattr(preset, "disabled_stages", set()) or []),
                "active_strategies": dict(getattr(preset, "active_strategies", {}) or {}),
            })
        except Exception as e:
            logger.debug("skip preset %s: %s", name, e)
    return out


def _collect_resources(config) -> dict[str, Any]:
    """호스트 주입 리소스(MCP 세션, RAG 컬렉션 등). 엔진 단독 실행에서는 비어있다.

    ResourceRegistry 는 워크플로우 실행 컨텍스트에서만 채워지므로 카탈로그 단계에서는
    **아직 비어있는 상태가 정상**. 이식 측이 Plan 전에 load_all() 해줘야 하는 부분.
    """
    try:
        from ..adapters.resource_registry import ResourceRegistry  # noqa: F401
    except Exception:
        return {}
    # Phase 1 에서는 placeholder — Phase 2 에서 ResourceRegistry 를 state 에서 읽어 확장.
    return {}
