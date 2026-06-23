"""ProjectLifecycle — 프로젝트 전 생애주기 형상 관리 (v1.24).

비전: "XGEN 으로 XGEN 관리 — 기존에 없던 형상 관리 방식" + "프로젝트 전 생애 주기 관리
(산출물/테스트 자동화)" + "코드 기반 프로젝트가 아니더라도 확장 가능 — 일반 프로젝트
관리 도구". 사용자 액션 흐름(챗 → test/simulation → share/publish → 배포)을 게이트가
있는 상태기계로 모델링한다.

generic — phase 의미는 도메인 무관(코드/문서/기획 무엇이든). 각 step 은 산출물 참조
(ReproBundle hash / tool_id / deploy_ref)만 들고, 게이트(앞 단계 통과)로 순서를 강제한다.
= "테스트 후에만 publish, publish 후에만 deploy". 엔진은 상태기계·게이트만, 실행은 이식.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional

from .progress import ProgressStatus


class LifecyclePhase(str, Enum):
    """전 생애주기 단계 (비전 사용자 액션 흐름)."""
    INTAKE = "intake"        # 챗/메시지 — 생각·아이디어·지시 입력
    SIMULATE = "simulate"    # test/simulation (sandbox)
    PUBLISH = "publish"      # share/publish (테스트 통과분 툴 영속화)
    DEPLOY = "deploy"        # docker → k8s
    MANAGE = "manage"        # 형상/장기기억 정제·공유


# 게이트: 각 phase 진입 전제 — 직전 단계가 통과(done+gate)여야 한다.
_ORDER = [LifecyclePhase.INTAKE, LifecyclePhase.SIMULATE, LifecyclePhase.PUBLISH,
          LifecyclePhase.DEPLOY, LifecyclePhase.MANAGE]


@dataclass
class LifecycleStep:
    """한 단계 — 산출물 참조 + 게이트 결과."""
    phase: LifecyclePhase
    status: ProgressStatus = ProgressStatus.PENDING
    gate_passed: Optional[bool] = None   # 이 단계의 품질게이트(예: judge θ) 통과?
    artifact_ref: str = ""               # ReproBundle hash / tool_id / deploy_ref
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["phase"] = self.phase.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LifecycleStep":
        return cls(
            phase=LifecyclePhase(data["phase"]),
            status=ProgressStatus(data.get("status", "pending")),
            gate_passed=data.get("gate_passed"),
            artifact_ref=str(data.get("artifact_ref", "")),
            note=str(data.get("note", "")),
        )


@dataclass
class ProjectLifecycle:
    """프로젝트 1개의 전 생애주기 형상 — 게이트 상태기계."""
    project_id: str
    steps: dict[str, LifecycleStep] = field(default_factory=dict)  # phase.value → step

    def _step(self, phase: LifecyclePhase) -> LifecycleStep:
        s = self.steps.get(phase.value)
        if s is None:
            s = LifecycleStep(phase=phase)
            self.steps[phase.value] = s
        return s

    def can_advance(self, phase: LifecyclePhase) -> bool:
        """이 phase 로 진입 가능한가 — 직전 단계가 done + 게이트 통과(있으면)."""
        idx = _ORDER.index(phase)
        if idx == 0:
            return True
        prev = self.steps.get(_ORDER[idx - 1].value)
        if prev is None or prev.status != ProgressStatus.DONE:
            return False
        return prev.gate_passed is not False  # None(게이트 없음) 또는 True 면 통과

    def advance(
        self,
        phase: LifecyclePhase,
        *,
        status: ProgressStatus = ProgressStatus.DONE,
        gate_passed: Optional[bool] = None,
        artifact_ref: str = "",
        note: str = "",
    ) -> LifecycleStep:
        """phase 를 전진. 게이트 미충족이면 ValueError(블랙박스 방지 — 사유 노출)."""
        if not self.can_advance(phase):
            prev = _ORDER[max(0, _ORDER.index(phase) - 1)]
            raise ValueError(f"{phase.value} 진입 불가 — 직전 단계 {prev.value} 미통과")
        s = self._step(phase)
        s.status = status
        if gate_passed is not None:
            s.gate_passed = gate_passed
        if artifact_ref:
            s.artifact_ref = artifact_ref
        if note:
            s.note = note
        return s

    def current_phase(self) -> Optional[LifecyclePhase]:
        """가장 멀리 진행된(done) 단계."""
        done = [p for p in _ORDER if self.steps.get(p.value) and self.steps[p.value].status == ProgressStatus.DONE]
        return done[-1] if done else None

    def next_phase(self) -> Optional[LifecyclePhase]:
        """다음에 진입할 단계 (없으면 None = 완료)."""
        for p in _ORDER:
            s = self.steps.get(p.value)
            if s is None or s.status != ProgressStatus.DONE:
                return p
        return None

    def is_complete(self) -> bool:
        return all(self.steps.get(p.value) and self.steps[p.value].status == ProgressStatus.DONE
                   for p in _ORDER)

    def to_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id,
                "steps": {k: v.to_dict() for k, v in self.steps.items()}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectLifecycle":
        return cls(
            project_id=str(data["project_id"]),
            steps={k: LifecycleStep.from_dict(v) for k, v in (data.get("steps") or {}).items()},
        )

    def to_markdown(self) -> str:
        """전 생애주기 표면 — 코드 아닌 '어디까지 왔나 + 산출물 좌표'."""
        lines = [f"# 프로젝트 생애주기 {self.project_id}", ""]
        for p in _ORDER:
            s = self.steps.get(p.value)
            if s is None:
                mark, extra = "○", ""
            else:
                mark = {"done": "●", "in_progress": "◐", "failed": "✗",
                        "blocked": "⏸", "pending": "○"}.get(s.status.value, "○")
                g = "" if s.gate_passed is None else (" ✓gate" if s.gate_passed else " ✗gate")
                extra = (f" — {s.artifact_ref}" if s.artifact_ref else "") + g
            lines.append(f"- {mark} **{p.value}**{extra}")
        return "\n".join(lines)
