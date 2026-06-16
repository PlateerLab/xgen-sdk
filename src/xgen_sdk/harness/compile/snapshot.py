"""
WorkflowSnapshot — 컴파일에 필요한 전체 상태를 고정하는 단일 JSON.

스냅샷 1장으로 워크플로우를 결정적으로 재현한다.
실행 시 외부 참조는 external_inputs 의 런타임 주입값 뿐.

스키마 버전 (`compile_version`) 변경 시에는 별도 이식 규칙 문서 필요.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .external_inputs import (
    ExternalInputSpec,
    parse_declared,
    scan_placeholders,
    merge_scanned,
    specs_to_dict,
)


SNAPSHOT_VERSION = "1.0"

# PyPI 호환 이름 검증 (PEP 503 유사, prefix 는 외부 규칙).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}[a-z0-9]$")
# PEP 440 버전 간이 체크.
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}([a-zA-Z0-9.\-+]*)$")


class SnapshotValidationError(ValueError):
    """스냅샷 검증 실패."""


@dataclass
class WorkflowSnapshot:
    """컴파일 산출물 내부에 포함될 결정적 재현 단위.

    불변성:
      - 만들어진 후에는 수정하지 않음 (컴파일 타임에 동결).
      - ``to_json`` → 받은쪽에서 ``from_json`` 하면 동일한 실행 환경 재현 가능.
    """

    gallery_name: str
    gallery_version: str
    harness_version: str                      # ">=0.10.0" 등
    harness_config: dict[str, Any]            # HarnessConfig.to_dict()
    workflow_data: dict[str, Any] = field(default_factory=dict)
    dependencies: dict[str, str] = field(default_factory=dict)
    external_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    compile_version: str = SNAPSHOT_VERSION

    # ─────────────────────────────────────────────
    # Factory
    # ─────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        *,
        harness_config: Any,
        workflow_data: Optional[dict[str, Any]] = None,
        gallery_name: str,
        gallery_version: str = "0.1.0",
        harness_version: Optional[str] = None,
        dependencies: Optional[dict[str, str]] = None,
        extra_metadata: Optional[dict[str, Any]] = None,
        auto_scan_inputs: bool = True,
    ) -> "WorkflowSnapshot":
        """HarnessConfig + workflow_data → 결정적 스냅샷.

        - ``harness_config`` 는 ``HarnessConfig`` 인스턴스 혹은 이미 dict.
        - ``harness_version`` 미지정 시 현재 패키지 버전으로 ``>=X.Y.Z`` 기록.
        - ``auto_scan_inputs`` 가 True 면 (기본) ``workflow_data`` + ``stage_params``
          에서 ``${VAR}`` 스캔 → external_inputs 보완.
        """
        config_dict = _as_dict(harness_config)
        workflow_data = workflow_data or {}

        declared_raw = config_dict.get("external_inputs") or {}
        declared_specs = parse_declared(declared_raw)
        if auto_scan_inputs:
            scanned = scan_placeholders(
                workflow_data,
                config_dict.get("stage_params"),
                config_dict.get("system_prompt"),
                config_dict.get("capability_params"),
            )
            merged = merge_scanned(declared_specs, scanned)
        else:
            merged = declared_specs

        inputs_as_dict = specs_to_dict(merged)
        config_dict["external_inputs"] = inputs_as_dict

        if harness_version is None:
            harness_version = _current_harness_spec()

        metadata = {
            "created_at": int(time.time()),
            "created_at_iso": _utcnow_iso(),
        }
        if extra_metadata:
            metadata.update({k: v for k, v in extra_metadata.items() if v is not None})

        snapshot = cls(
            gallery_name=gallery_name,
            gallery_version=gallery_version,
            harness_version=harness_version,
            harness_config=config_dict,
            workflow_data=workflow_data,
            dependencies=dict(dependencies or {}),
            external_inputs=inputs_as_dict,
            metadata=metadata,
        )
        snapshot.validate()
        return snapshot

    # ─────────────────────────────────────────────
    # Serialization
    # ─────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True, default=_json_default)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowSnapshot":
        if not isinstance(data, dict):
            raise SnapshotValidationError("snapshot data must be a dict")
        # 알 수 없는 키는 버려서 forward-compat 확보.
        allowed = {
            "gallery_name", "gallery_version", "harness_version",
            "harness_config", "workflow_data", "dependencies",
            "external_inputs", "metadata", "compile_version",
        }
        kwargs = {k: v for k, v in data.items() if k in allowed}
        snap = cls(**kwargs)
        snap.validate()
        return snap

    @classmethod
    def from_json(cls, text: str) -> "WorkflowSnapshot":
        return cls.from_dict(json.loads(text))

    # ─────────────────────────────────────────────
    # Validation
    # ─────────────────────────────────────────────

    def validate(self) -> None:
        if not _NAME_RE.match(self.gallery_name):
            raise SnapshotValidationError(
                f"invalid gallery_name '{self.gallery_name}' "
                "(must match ^[a-z0-9][a-z0-9_-]*[a-z0-9]$ 64 chars)"
            )
        if not _VERSION_RE.match(self.gallery_version):
            raise SnapshotValidationError(
                f"invalid gallery_version '{self.gallery_version}' (PEP 440)"
            )
        if not isinstance(self.harness_config, dict):
            raise SnapshotValidationError("harness_config must be dict")
        if not isinstance(self.workflow_data, dict):
            raise SnapshotValidationError("workflow_data must be dict")
        if not self.compile_version:
            raise SnapshotValidationError("compile_version missing")


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _as_dict(config: Any) -> dict[str, Any]:
    """HarnessConfig 또는 dict 를 모두 받아 dict 반환."""
    if isinstance(config, dict):
        return dict(config)
    to_dict = getattr(config, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    raise SnapshotValidationError(
        f"harness_config must be HarnessConfig or dict, got {type(config).__name__}"
    )


def _current_harness_spec() -> str:
    """컴파일 시점 엔진 버전을 기준으로 wheel 이 요구할 xgen-harness 버전 스펙 생성.

    엔진 버전 import 실패 시 unbounded 스펙("") 반환 — 어떤 엔진 버전과도 호환.
    하드코딩된 버전 fallback 은 stale 방지를 위해 쓰지 않는다.
    """
    try:
        from .. import __version__
    except Exception as _e:  # pragma: no cover — 엔진 import 자체 실패는 극단 상황
        return ""
    return f">={__version__}"


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _json_default(o: Any) -> Any:
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, tuple):
        return list(o)
    if hasattr(o, "to_dict") and callable(o.to_dict):
        return o.to_dict()
    if hasattr(o, "value"):  # Enum
        return o.value
    raise TypeError(f"object of type {type(o).__name__} is not JSON serializable")


def load_snapshot(path: str | os.PathLike[str]) -> WorkflowSnapshot:
    """파일 경로에서 snapshot.json 을 로드. 런타임에 wheel 내부에서 호출됨."""
    with open(path, "r", encoding="utf-8") as f:
        return WorkflowSnapshot.from_json(f.read())
