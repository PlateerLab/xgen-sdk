"""ActivityFeed — '사용자 활동 현황'(집단지성 흐름) 정제 피드 (v1.24).

비전: "팀즈 같은 UI 에서 멤버들이 무엇을 꿀렁이는지 실시간 모니터링 — **메시지 정제 필요**"
+ "집단지성의 흐름이 관리될 수 있음".

원문 메시지를 그대로 노출하지 않는다 — `refine`(redaction + intent 추출)을 거친
**정제된 한 줄**만 활동현황으로 흘린다(프라이버시). 누가·무엇을·어떤 상태인지 +
provenance(run/thread)만. 엔진은 자료구조·집계만 제공하고(무하드코딩), 실시간 전송
(SSE/WS)·렌더는 이식/프론트가 담당한다.

seq 는 호출자 제공 단조 증가값 — 엔진은 시계에 의존하지 않아 결정적·테스트가능.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .refine import refine_message


@dataclass
class ActivityEvent:
    """활동 1건 — 정제된(원문 아님) 멤버 활동."""
    seq: int              # 단조 증가 순서(호출자 제공)
    actor: str            # 멤버 식별자
    kind: str = "chat"    # chat | test | publish | forge | config | deploy
    intent: str = ""      # 정제·redacted 한 줄 ("무엇을") — 원문 미보유
    status: str = "active"  # active | done | failed | blocked
    ref: dict[str, Any] = field(default_factory=dict)  # run_id/thread_id 등 provenance

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActivityEvent":
        return cls(
            seq=int(data.get("seq", 0)),
            actor=str(data.get("actor", "")),
            kind=str(data.get("kind", "chat")),
            intent=str(data.get("intent", "")),
            status=str(data.get("status", "active")),
            ref=dict(data.get("ref", {}) or {}),
        )


def activity_from_message(
    *,
    seq: int,
    actor: str,
    raw_message: str,
    kind: str = "chat",
    status: str = "active",
    ref: Optional[dict[str, Any]] = None,
) -> ActivityEvent:
    """raw 메시지 → 정제된 ActivityEvent. 원문은 보유하지 않는다(프라이버시).

    refine 의 intent(redacted 첫 문장)만 활동현황으로 노출.
    """
    refined = refine_message(raw_message, "", memory_id=f"act-{seq}")
    return ActivityEvent(seq=seq, actor=actor, kind=kind,
                         intent=refined.intent, status=status, ref=dict(ref or {}))


@dataclass
class ActivityFeed:
    """멤버 활동 집계 — Teams 식 '무엇을 꿀렁이는지' 표면."""
    events: list[ActivityEvent] = field(default_factory=list)

    def record(self, event: ActivityEvent) -> "ActivityFeed":
        self.events.append(event)
        return self

    def live(self) -> dict[str, ActivityEvent]:
        """각 멤버의 **최신** 활동 (현재 무엇을 하고 있나)."""
        latest: dict[str, ActivityEvent] = {}
        for e in self.events:
            cur = latest.get(e.actor)
            if cur is None or e.seq > cur.seq:
                latest[e.actor] = e
        return latest

    def active_members(self) -> list[str]:
        """지금 active 상태인 멤버 (정렬)."""
        return sorted(a for a, e in self.live().items() if e.status == "active")

    def stream(self, since_seq: int = 0) -> list[ActivityEvent]:
        """since_seq 이후 활동 — 실시간 폴링용(SSE/WS 백엔드)."""
        return [e for e in self.events if e.seq > since_seq]

    def by_actor(self, actor: str) -> list[ActivityEvent]:
        return [e for e in self.events if e.actor == actor]

    def to_dict(self) -> dict[str, Any]:
        return {"events": [e.to_dict() for e in self.events]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActivityFeed":
        return cls(events=[ActivityEvent.from_dict(d) for d in (data or {}).get("events", [])])

    def to_markdown(self) -> str:
        """활동현황 표면 — 멤버별 현재 활동(정제됨)."""
        live = self.live()
        if not live:
            return "## 활동현황\n\n(활동 없음)"
        lines = ["## 활동현황", ""]
        for actor in sorted(live):
            e = live[actor]
            mark = {"active": "🟢", "done": "✓", "failed": "✗", "blocked": "⏸"}.get(e.status, "·")
            lines.append(f"- {mark} **{actor}** [{e.kind}] {e.intent or '(…)'}")
        return "\n".join(lines)
