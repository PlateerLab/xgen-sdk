"""ReproBundle — 메시지·정제기억·재현가능 config 를 묶는 '새로운 형상 관리 단위' (v1.24).

비전 핵심: "작성자가 보지도 않는 코드보다 **무슨 생각으로·어떤 과정으로** 만들었는지가
더 중요" + "메시지 + 정제 장기기억 = 재현성" + "기존에 없던 형상 관리 방식".

코드(레거시 산출물)가 아니라 **의도(왜) + 과정(어떻게) + 재현가능 config(무엇을 재실행)**
를 형상 관리 단위로 삼는다. 한 번들 =
  - intent_memory : RefinedMemory (redacted 의도/결과, 원문 미보유 — 프라이버시 floor)
  - config_fingerprint : config 스냅샷의 결정적 해시 (spec.json 동치 — "무엇을 재실행")
  - quality : judge_score / gate 통과 여부 (결과가 훌륭한가)
  - provenance : State Spine 계층키 (tenant/project/thread/interaction/run)
  - bundle_hash : 위를 묶은 결정적 식별자 → 같은 의도+config = 같은 hash (형상 동일성)

정직한 재현성 정의(상위문서 L5): config 는 fingerprint 로 **완전 동치 재실행** 보장.
LLM 비결정성·외부 부작용은 미보장 → quality(같은 평가축 통과)로 보강한다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .refine import RefinedMemory


def _canonical(obj: Any) -> str:
    """결정적 canonical JSON (키 정렬·공백 고정) — 같은 내용 → 같은 문자열."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_fingerprint(config: dict[str, Any]) -> str:
    """config 스냅샷의 결정적 지문(sha256[:16]). spec.json 동치 재실행 식별자.

    휘발성·비결정 필드(런타임 식별자 등)는 제외하고 정책면만 해싱한다.
    """
    snap = {k: v for k, v in (config or {}).items()
            if k not in ("execution_id", "interaction_id", "run_id", "_schema_version")}
    return hashlib.sha256(_canonical(snap).encode("utf-8")).hexdigest()[:16]


@dataclass
class ReproBundle:
    """재현 번들 — 형상 관리 1단위."""
    bundle_id: str
    intent_memory: Optional[RefinedMemory] = None
    config_fingerprint: str = ""
    judge_score: Optional[float] = None
    gate_passed: Optional[bool] = None
    provenance: dict[str, Any] = field(default_factory=dict)
    spec_ref: str = ""                 # 산출물 spec.json 위치(MinIO/Spine)

    def bundle_hash(self) -> str:
        """의도 + config + 품질을 묶은 결정적 식별자. 같은 내용 → 같은 hash."""
        payload = {
            "intent": (self.intent_memory.intent if self.intent_memory else ""),
            "config_fingerprint": self.config_fingerprint,
            "judge_score": self.judge_score,
            "gate_passed": self.gate_passed,
        }
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]

    def reproduces(self, config: dict[str, Any]) -> bool:
        """주어진 config 가 이 번들과 동치 재실행인가 (fingerprint 일치)."""
        return bool(self.config_fingerprint) and config_fingerprint(config) == self.config_fingerprint

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["intent_memory"] = self.intent_memory.to_dict() if self.intent_memory else None
        d["bundle_hash"] = self.bundle_hash()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReproBundle":
        im = data.get("intent_memory")
        return cls(
            bundle_id=str(data["bundle_id"]),
            intent_memory=RefinedMemory.from_dict(im) if im else None,
            config_fingerprint=str(data.get("config_fingerprint", "")),
            judge_score=data.get("judge_score"),
            gate_passed=data.get("gate_passed"),
            provenance=dict(data.get("provenance", {}) or {}),
            spec_ref=str(data.get("spec_ref", "")),
        )

    def to_markdown(self) -> str:
        """형상 단위의 사람용 표면 — 코드가 아닌 '의도·과정·재현 좌표'."""
        lines = [f"# 재현 번들 {self.bundle_id}", "", f"`{self.bundle_hash()}`", ""]
        if self.intent_memory:
            lines += [self.intent_memory.to_markdown(), ""]
        lines += [f"**config 지문:** `{self.config_fingerprint}` (동치 재실행 식별자)"]
        if self.judge_score is not None:
            lines += [f"**품질:** judge={self.judge_score}"
                      + (f", gate={'통과' if self.gate_passed else '미통과'}"
                         if self.gate_passed is not None else "")]
        if self.spec_ref:
            lines += [f"**spec:** {self.spec_ref}"]
        if self.provenance:
            lines += ["", "**좌표:** " + " · ".join(f"{k}={v}" for k, v in self.provenance.items())]
        return "\n".join(lines)


def build_repro_bundle(
    *,
    bundle_id: str,
    config: dict[str, Any],
    intent_memory: Optional[RefinedMemory] = None,
    judge_score: Optional[float] = None,
    gate_passed: Optional[bool] = None,
    provenance: Optional[dict[str, Any]] = None,
    spec_ref: str = "",
) -> ReproBundle:
    """config 스냅샷 + 정제기억 + 품질 → ReproBundle. config 는 fingerprint 로만 보관."""
    return ReproBundle(
        bundle_id=bundle_id,
        intent_memory=intent_memory,
        config_fingerprint=config_fingerprint(config or {}),
        judge_score=judge_score,
        gate_passed=gate_passed,
        provenance=dict(provenance or {}),
        spec_ref=spec_ref,
    )
