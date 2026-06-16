"""Progress artifact — 장기실행(multi-session) 에이전트의 작업상태 인계.

## 배경 (하네스 엔지니어링 철학)
Anthropic "Effective harnesses for long-running agents" 의 핵심: 에이전트는 분리된
세션에서 일하고 **각 세션은 이전 기억 없이 시작** 한다. 해법은 context 압축이 아니라
세션 밖에 사는 **구조화된 진행 기록**(claude-progress.txt + feature-list JSON)이다.
s06_context 의 압축은 *한 세션 안* 의 context engineering 일 뿐, *세션 간* 일관성은
이 progress artifact 가 책임진다.

`ProgressLog` 는 JSON 직렬화 가능한 작업 항목 목록이다. SessionStore([[store]])에
실어 다음 세션이 `pending()` 으로 "다음에 할 일" 을 즉시 복원한다. **머신 식별자 +
검증 절차 + pass/fail 상태** 만 담고 자연어 지시는 담지 않는다 (확장성·무하드코딩).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class ProgressStatus(str, Enum):
    """작업 항목 상태. JSON 직렬화를 위해 str 혼합 Enum."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class ProgressItem:
    """단일 작업 항목 — feature-list 의 한 줄.

    id            — 안정 식별자 (세션 간 불변).
    description   — 무엇을 하는지 (머신 라벨).
    status        — ProgressStatus.
    verification  — 완료를 어떻게 검증하는가 (단계 절차). Anthropic 권장: 검증 없이
                    done 으로 못 넘어가게 강제하는 핵심 필드.
    notes         — 직전 세션이 다음 세션에 남기는 메모 (실패 사유·차단 원인 등).
    """
    id: str
    description: str = ""
    status: ProgressStatus = ProgressStatus.PENDING
    verification: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProgressItem":
        return cls(
            id=str(data["id"]),
            description=str(data.get("description", "")),
            status=ProgressStatus(data.get("status", "pending")),
            verification=str(data.get("verification", "")),
            notes=str(data.get("notes", "")),
        )


@dataclass
class ProgressLog:
    """작업 항목 모음 — 세션 밖에 영속되는 진행 기록.

    JSON 으로 직렬화해 SessionStore 에 싣는다. JSON 을 쓰는 이유(Anthropic):
    모델이 Markdown 보다 JSON 을 함부로 덮어쓰지 않는다 → 진행 기록 보존성 ↑.
    """
    items: list[ProgressItem] = field(default_factory=list)

    # ── 변경 ──
    def add(self, item: ProgressItem) -> "ProgressLog":
        if any(i.id == item.id for i in self.items):
            raise ValueError(f"중복 ProgressItem id: {item.id!r}")
        self.items.append(item)
        return self

    def get(self, item_id: str) -> Optional[ProgressItem]:
        return next((i for i in self.items if i.id == item_id), None)

    def update_status(
        self, item_id: str, status: ProgressStatus, notes: str = ""
    ) -> ProgressItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"ProgressItem 없음: {item_id!r}")
        item.status = status
        if notes:
            item.notes = notes
        return item

    # ── 조회 ──
    def pending(self) -> list[ProgressItem]:
        """다음에 할 일 — 아직 done/failed 가 아닌 항목 (세션 부트스트랩용)."""
        return [
            i for i in self.items
            if i.status in (ProgressStatus.PENDING, ProgressStatus.IN_PROGRESS, ProgressStatus.BLOCKED)
        ]

    def next_pending(self) -> Optional[ProgressItem]:
        """가장 앞선 미완 항목 (한 세션 = 한 항목 권장 패턴)."""
        p = self.pending()
        return p[0] if p else None

    def is_complete(self) -> bool:
        """모든 항목이 done 인가 (조기 완료 선언 방지용 — 외부가 이걸로 게이팅)."""
        return bool(self.items) and all(i.status == ProgressStatus.DONE for i in self.items)

    def summary(self) -> dict[str, int]:
        out = {s.value: 0 for s in ProgressStatus}
        for i in self.items:
            out[i.status.value] += 1
        out["total"] = len(self.items)
        return out

    # ── 직렬화 ──
    def to_dict(self) -> dict[str, Any]:
        return {"items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProgressLog":
        return cls(items=[ProgressItem.from_dict(d) for d in (data or {}).get("items", [])])

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ProgressLog":
        return cls.from_dict(json.loads(text))
