"""
Materializer — capability name 리스트를 실제 Tool 인스턴스로 변환

s04_tool가 config.capabilities를 읽어 이 모듈로 전달하면
Registry에서 spec 조회 → tool_factory 호출 → Tool 인스턴스 생성.

라이브러리는 Tool 만드는 법을 모름 — 전적으로 tool_factory에 위임.
Adapter가 factory를 채운 CapabilitySpec을 Registry에 등록해야 함.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..tools.base import Tool
from .registry import CapabilityRegistry, get_default_registry
from .schema import CapabilitySpec

logger = logging.getLogger("harness.capabilities.materializer")


@dataclass
class MaterializationReport:
    """capability → Tool 변환 결과 리포트"""

    tools: list[Tool] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)      # 성공한 capability name
    unknown: list[str] = field(default_factory=list)       # registry에 없음
    no_factory: list[str] = field(default_factory=list)    # factory 미등록
    failed: list[tuple[str, str]] = field(default_factory=list)  # (name, error)

    @property
    def success(self) -> bool:
        return not (self.unknown or self.no_factory or self.failed)

    def summary(self) -> str:
        parts = [f"resolved={len(self.resolved)}"]
        if self.unknown:
            parts.append(f"unknown={len(self.unknown)}")
        if self.no_factory:
            parts.append(f"no_factory={len(self.no_factory)}")
        if self.failed:
            parts.append(f"failed={len(self.failed)}")
        return ", ".join(parts)


def materialize_capabilities(
    names: list[str],
    *,
    registry: Optional[CapabilityRegistry] = None,
    capability_params: Optional[dict[str, dict]] = None,
) -> MaterializationReport:
    """
    capability name 리스트 → Tool 인스턴스 리스트.

    Args:
        names: HarnessConfig.capabilities 값 (예: ["retrieval.web_search"])
        registry: 사용할 레지스트리 (None이면 전역 기본값)
        capability_params: capability별 override 파라미터 (HarnessConfig.capability_params)

    Returns:
        MaterializationReport — tools + 실패 내역
    """
    reg = registry or get_default_registry()
    params_map = capability_params or {}
    report = MaterializationReport()

    for name in names:
        spec = reg.get(name)
        if spec is None:
            report.unknown.append(name)
            logger.warning("[Materializer] Unknown capability: %s", name)
            continue

        if spec.tool_factory is None:
            report.no_factory.append(name)
            logger.warning(
                "[Materializer] Capability %s has no tool_factory — Adapter 등록 필요",
                name,
            )
            continue

        factory_config = _build_factory_config(spec, params_map.get(name, {}))

        try:
            tool = spec.tool_factory(factory_config)
        except Exception as e:  # tool_factory는 외부 제공 → 넓게 잡음
            report.failed.append((name, f"{type(e).__name__}: {e}"))
            logger.error("[Materializer] tool_factory failed for %s: %s", name, e)
            continue

        if tool is None:
            report.failed.append((name, "factory returned None"))
            continue

        report.tools.append(tool)
        report.resolved.append(name)

    if report.unknown or report.no_factory or report.failed:
        logger.info("[Materializer] %s", report.summary())

    return report


def _build_factory_config(spec: CapabilitySpec, override: dict) -> dict:
    """tool_factory에 전달할 config dict 조립.

    우선순위: override > spec.params default > spec.extra
    """
    config: dict[str, Any] = {}

    # 기본값 먼저 (optional params의 default)
    for param in spec.params:
        if param.default is not None:
            config[param.name] = param.default

    # override 적용
    config.update(override)

    # 메타 정보도 전달 (factory가 필요하면 꺼내씀)
    config["__capability_name__"] = spec.name
    config["__provider_kind__"] = spec.provider_kind.value
    config["__provider_ref__"] = spec.provider_ref
    if spec.extra:
        config["__extra__"] = dict(spec.extra)

    return config


def merge_into_state(
    report: MaterializationReport,
    state,  # PipelineState — 순환 import 피하려고 타입 생략
) -> int:
    """
    Materialization 결과를 PipelineState에 반영.

    - state.tool_definitions에 API 포맷 추가 (중복 방지)
    - state.metadata["tool_registry"]에 Tool 인스턴스 등록
    - state.metadata["capability_bindings"]에 매핑 기록

    Returns:
        실제로 추가된 도구 수
    """
    if "tool_registry" not in state.metadata:
        state.metadata["tool_registry"] = {}
    if "capability_bindings" not in state.metadata:
        state.metadata["capability_bindings"] = {}

    existing = {td.get("name") for td in state.tool_definitions if isinstance(td, dict)}
    added = 0

    for tool, cap_name in zip(report.tools, report.resolved):
        api_def = tool.to_api_format()
        tool_name = api_def.get("name", tool.name)

        # 도구 인스턴스 등록 (항상)
        state.metadata["tool_registry"][tool_name] = tool
        state.metadata["capability_bindings"][cap_name] = tool_name

        # 정의 중복 방지
        if tool_name in existing:
            logger.debug(
                "[Materializer] Tool %s already in definitions — skipping API def merge",
                tool_name,
            )
            continue

        state.tool_definitions.append(api_def)
        existing.add(tool_name)
        added += 1

    return added
