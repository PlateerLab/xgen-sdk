"""SF8 — 규약(constitution). Hermes 의 SOUL.md 에 대응하되, 텍스트가 아니라 **집행**이다.

Hermes 는 페르소나를 `SOUL.md` 로 이어가고 "학습하지 말 것" 규칙을 스킬 프롬프트 안에
문장으로 둔다. 문장으로 둔 규칙은 모델이 지키면 지켜지고 안 지키면 안 지켜진다 —
그리고 페르소나 파일은 에이전트가 스스로 고치므로 **조용히 표류**한다(그 표류를 알
방법이 없다는 게 진짜 문제다).

여기서는 세 가지를 다르게 한다.

1. **금지는 게이트가 집행한다.** `never_learn` 은 프롬프트가 아니라 `check_candidate()`
   로 걸러진다. 모델의 선의에 기대지 않는다.
2. **규약은 에이전트가 못 고친다.** `propose()` 는 **차이만** 돌려준다. 적용은 사람이
   `adopt()` 를 부르는 것으로만 일어나고 이력이 남는다.
3. **표류가 보인다.** `diff()` 가 무엇이 언제 바뀌었는지 줄 단위로 말한다.

파일 형식은 agentskills 프론트매터와 같은 모양이라(`---` YAML + 본문) 다른 도구가
읽어도 깨지지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .model import SkillCandidate

DEFAULT_IDENTITY = "Jermes"
DEFAULT_ROLE = "끝난 실행에서 재사용 가능한 절차를 배우고, 검증된 것만 남기는 큐레이터"


@dataclass
class Constitution:
    """에이전트가 스스로 바꿀 수 없는 부분."""

    identity: str = DEFAULT_IDENTITY
    role: str = DEFAULT_ROLE
    principles: list[str] = field(default_factory=lambda: [
        "검증되지 않은 것을 검증된 것처럼 제시하지 않는다.",
        "증거 없이 지우지 않는다 — 내려갈 때도 이력과 함께 남긴다.",
        "0건일 때는 왜 0건인지 말한다.",
    ])
    never_learn: list[str] = field(default_factory=lambda: [
        # 배우면 안 되는 것 = 오래가지 않거나, 배우는 순간 위험해지는 것.
        r"비밀번호|password|api[_-]?key|secret|token",
        r"특정 날짜에만 맞는|only on \d{4}-\d{2}-\d{2}",
        r"검증을 건너뛰|skip (?:the )?(?:verification|gate|bench)",
        r"사람 승인 없이|without (?:human )?approval",
    ])
    approval_required_scopes: list[str] = field(default_factory=lambda: ["project", "org"])
    version: str = "1.0.0"
    history: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 집행

    def check_candidate(self, candidate: SkillCandidate) -> str | None:
        """규약 위반이면 이유, 아니면 None. `safety_check` 와 같은 계약이라
        게이트에 그대로 꽂힌다."""
        text = " ".join([
            candidate.name or "", candidate.rationale or "", candidate.when_to_use or "",
            " ".join(candidate.procedure or []), " ".join(candidate.pitfalls or []),
            " ".join(candidate.verification or []), str(candidate.payload or {}),
        ])
        for pattern in self.never_learn:
            try:
                match = re.search(pattern, text, re.IGNORECASE)
            except re.error:
                continue      # 잘못된 규칙 하나가 집행 전체를 멈추게 두지 않는다
            if match:
                return f"규약 위반(never_learn): {pattern!r} 이 {match.group(0)!r} 에 걸림"
        return None

    def needs_human_approval(self, scope: str) -> bool:
        return scope in self.approval_required_scopes

    # ------------------------------------------------------------ 변경 통제

    def propose(self, **changes) -> list[str]:
        """제안만 한다 — **적용하지 않는다**. 에이전트가 자기 규약을 바꾸는 경로는 없다."""
        lines: list[str] = []
        for key, value in changes.items():
            if not hasattr(self, key) or key in ("history", "version"):
                lines.append(f"거부: {key} 는 제안 대상이 아니다")
                continue
            current = getattr(self, key)
            if current == value:
                continue
            lines.append(f"{key}: {current!r} -> {value!r}")
        return lines

    def adopt(self, changes: dict, approved_by: str) -> list[str]:
        """사람이 승인했을 때만 적용된다. 승인자 없이 부르면 거부."""
        if not approved_by.strip():
            raise ValueError("규약 변경에는 승인자가 필요하다")
        applied = self.propose(**changes)
        applied = [line for line in applied if not line.startswith("거부:")]
        for key, value in changes.items():
            if hasattr(self, key) and key not in ("history", "version"):
                setattr(self, key, value)
        if applied:
            major, minor, patch = (self.version.split(".") + ["0", "0"])[:3]
            self.version = f"{major}.{int(minor) + 1}.0"
            self.history.append(f"{self.version} by {approved_by}: " + "; ".join(applied))
        return applied

    # ------------------------------------------------------------ 표류 감시

    def diff(self, other: "Constitution") -> list[str]:
        """무엇이 달라졌는지 줄 단위로. 표류를 눈에 보이게 하는 것이 목적이다."""
        lines: list[str] = []
        for field_name in ("identity", "role", "version"):
            mine, theirs = getattr(self, field_name), getattr(other, field_name)
            if mine != theirs:
                lines.append(f"{field_name}: {mine!r} -> {theirs!r}")
        for field_name in ("principles", "never_learn", "approval_required_scopes"):
            mine, theirs = set(getattr(self, field_name)), set(getattr(other, field_name))
            for gone in sorted(mine - theirs):
                lines.append(f"{field_name} 삭제: {gone}")
            for added in sorted(theirs - mine):
                lines.append(f"{field_name} 추가: {added}")
        return lines

    # ------------------------------------------------------------ 직렬화

    def to_markdown(self) -> str:
        """agentskills 프론트매터와 같은 모양 — 다른 도구가 읽어도 안 깨진다."""
        def block(name: str, values: Sequence[str]) -> str:
            return f"{name}:\n" + "".join(f'  - "{v}"\n' for v in values)

        return (
            "---\n"
            f'name: {self.identity.lower()}-constitution\n'
            f'description: "{self.role}"\n'
            f'metadata:\n  version: "{self.version}"\n'
            "---\n\n"
            f"# {self.identity}\n\n{self.role}\n\n"
            "## Principles\n" + "".join(f"- {p}\n" for p in self.principles) +
            "\n## Never learn\n" + "".join(f"- `{p}`\n" for p in self.never_learn) +
            "\n## Approval required\n" +
            "".join(f"- {s}\n" for s in self.approval_required_scopes) +
            ("\n## History\n" + "".join(f"- {h}\n" for h in self.history)
             if self.history else "")
        )

    @classmethod
    def from_markdown(cls, text: str) -> "Constitution":
        """to_markdown 의 역. 못 읽는 줄은 조용히 버리지 않고 기본값으로 남긴다."""
        def section(title: str) -> list[str]:
            match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)",
                              text, re.MULTILINE | re.DOTALL)
            if not match:
                return []
            return [re.sub(r"^[-*]\s*", "", line).strip().strip("`")
                    for line in match.group(1).splitlines() if line.strip().startswith(("-", "*"))]

        identity = re.search(r"^# (.+)$", text, re.MULTILINE)
        version = re.search(r'version:\s*"?([0-9.]+)"?', text)
        role = re.search(r'description:\s*"([^"]*)"', text)
        constitution = cls(
            identity=(identity.group(1).strip() if identity else DEFAULT_IDENTITY),
            role=(role.group(1) if role else DEFAULT_ROLE),
            version=(version.group(1) if version else "1.0.0"),
        )
        for name, values in (("principles", section("Principles")),
                             ("never_learn", section("Never learn")),
                             ("approval_required_scopes", section("Approval required"))):
            if values:
                setattr(constitution, name, values)
        constitution.history = section("History")
        return constitution
