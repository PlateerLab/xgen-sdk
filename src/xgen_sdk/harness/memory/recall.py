"""Recall Workspace — 에이전트의 작업기억 보존소.

검색·추론 에이전트가 긴 작업 동안 발견한 중요 정보를 "커지는 대화 transcript" 가 아니라
별도 작업기억에 보존한다. 모델이 매 턴 무엇을 봤는지/무엇이 유효한지/무엇을 확인했는지를
프롬프트에 다 이고 가면, 의미 결정과 단순 기록 관리를 동시에 떠안아 비효율적이다. 그래서:

- policy(LLM) 는 `keep`/`check`/`discard` 로 **무엇을 남길지** 의미 결정만 한다.
- 엔진(이 모듈) 이 **중복제거 · 우선순위 정렬 · 정원(cap) · 검토 기록 · compact 렌더** 를 관리한다.

## Progressive Disclosure 정합
RecallSet 은 PD([[store]] 의 pd_stores)의 확장이다. 전체 본문은 `pd_stores["recall"]` 로
보존돼 `fetch_pd("recall", id)` 로 **step-in**, compact 뷰는 `render()` 로 **step-out**.
세션 압축(s06)이 messages 를 잘라내도 RecallSet 은 state 측에 살아남아 보존된다.

## 무하드코딩
cap / 렌더 예산은 전부 파라미터. 매직넘버를 로직에 박지 않는다(기본값만 둔다).
도메인 어휘(코퍼스·reranker 등) 없음 — provider/도구 소스 agnostic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .dedupe import content_fingerprint

# 기본 정원 — 데이터타입 기본값일 뿐 로직 상수 아님 (config.recall_cap 으로 override).
DEFAULT_RECALL_CAP = 50


class Priority(str, Enum):
    """보존 항목 우선순위. JSON 직렬화를 위해 str 혼합 Enum."""
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        """정렬/eviction 용 순위 (높을수록 우선)."""
        return _PRIORITY_RANK[self]

    @classmethod
    def coerce(cls, value: Any) -> "Priority":
        """문자열/Enum/None → Priority. 알 수 없으면 NORMAL."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return cls.NORMAL


_PRIORITY_RANK = {
    Priority.CRITICAL: 3,
    Priority.HIGH: 2,
    Priority.NORMAL: 1,
    Priority.LOW: 0,
}


@dataclass
class RecallItem:
    """작업기억 한 항목.

    id          — 안정 식별자 (LLM 이 부여하거나 source/chunk id).
    content     — 보존 본문(또는 snippet). 전체 본문은 pd_stores 로 step-in 보존.
    source      — 출처(파일/URL/컬렉션 등).
    priority    — Priority 태그. 엔진이 cap eviction·렌더 순서에 사용.
    score       — 검색 점수 등 보조 정렬 신호.
    checked     — None=미확인 / True/False = check 결과.
    note        — 확인 메모(왜 통과/실패).
    turn        — 보존된 loop iteration (recency).
    fingerprint — 내용 지문 (dedup 키).
    """
    id: str
    content: str = ""
    source: str = ""
    priority: Priority = Priority.NORMAL
    score: float = 0.0
    checked: Optional[bool] = None
    note: str = ""
    turn: int = 0
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "source": self.source,
            "priority": self.priority.value,
            "score": self.score,
            "checked": self.checked,
            "note": self.note,
            "turn": self.turn,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecallItem":
        return cls(
            id=str(data["id"]),
            content=str(data.get("content", "")),
            source=str(data.get("source", "")),
            priority=Priority.coerce(data.get("priority", "normal")),
            score=float(data.get("score", 0.0) or 0.0),
            checked=data.get("checked", None),
            note=str(data.get("note", "")),
            turn=int(data.get("turn", 0) or 0),
            fingerprint=str(data.get("fingerprint", "")),
        )


@dataclass
class RecallSet:
    """작업기억 보존소 — 우선순위 태깅 + 중복제거 + 정원(cap) 된 항목 집합.

    policy 는 `keep`/`check`/`discard` 로 의미 결정만, 엔진(이 객체)이 dedup·cap·정렬·
    렌더를 관리한다. JSON 직렬화 가능 → SessionStore([[store]]) 로 세션 간 인계.
    """
    cap: int = DEFAULT_RECALL_CAP
    items: list[RecallItem] = field(default_factory=list)

    # ── 조회 ──
    def get(self, item_id: str) -> Optional[RecallItem]:
        return next((i for i in self.items if i.id == item_id), None)

    def _find_by_fingerprint(self, fp: str) -> Optional[RecallItem]:
        if not fp:
            return None
        return next((i for i in self.items if i.fingerprint == fp), None)

    def ranked(self) -> list[RecallItem]:
        """우선순위 → score → recency 내림차순. 렌더/반환의 표준 순서."""
        return sorted(
            self.items,
            key=lambda i: (i.priority.rank, i.score, i.turn),
            reverse=True,
        )

    # ── 변경 (policy 의 의미 결정) ──
    def keep(
        self,
        *,
        id: str,
        content: str,
        source: str = "",
        priority: Any = Priority.NORMAL,
        score: float = 0.0,
        turn: int = 0,
    ) -> RecallItem:
        """항목을 작업기억에 보존. 동일 id 또는 동일 내용지문이면 갱신(중복 누적 방지).

        우선순위는 상향만(하향 금지) — 한 번 중요하다고 본 항목을 약화시키지 않음.
        cap 초과 시 최저 우선순위+오래된 것부터 eviction.
        """
        prio = Priority.coerce(priority)
        fp = content_fingerprint(content)
        existing = self.get(id) or self._find_by_fingerprint(fp)
        if existing is not None:
            if content:
                existing.content = content
            if source:
                existing.source = source
            if prio.rank > existing.priority.rank:
                existing.priority = prio
            if score:
                existing.score = score
            if turn:
                existing.turn = turn
            existing.fingerprint = fp
            return existing

        item = RecallItem(
            id=id, content=content, source=source,
            priority=prio, score=score, turn=turn, fingerprint=fp,
        )
        self.items.append(item)
        self._enforce_cap()
        return item

    def check(self, item_id: str, ok: bool, note: str = "") -> Optional[RecallItem]:
        """항목의 확인 결과 기록 (재확인 방지). 없으면 None."""
        it = self.get(item_id)
        if it is None:
            return None
        it.checked = bool(ok)
        if note:
            it.note = note
        return it

    def discard(self, item_id: str) -> bool:
        """항목 제거. 제거했으면 True."""
        before = len(self.items)
        self.items = [i for i in self.items if i.id != item_id]
        return len(self.items) != before

    def _enforce_cap(self) -> None:
        """cap>0 이고 초과면 최저 우선순위 → 오래된 → 저점수 순으로 eviction."""
        if self.cap > 0 and len(self.items) > self.cap:
            self.items.sort(key=lambda i: (i.priority.rank, i.turn, i.score))
            overflow = len(self.items) - self.cap
            del self.items[:overflow]

    # ── step-out 렌더 ──
    def render(self, *, max_items: int = 0, max_chars: int = 0, header: bool = True) -> str:
        """compact 뷰 (step-out). 우선순위 순. max_items/max_chars 예산 존중(0=무제한).

        본문 전체가 아니라 한 줄 요약만 — 전체는 fetch_pd("recall", id) 로 step-in.
        """
        ordered = self.ranked()
        if max_items > 0:
            ordered = ordered[:max_items]
        if not ordered:
            return "[recall workspace: empty]"

        lines: list[str] = []
        if header:
            lines.append(f"[recall workspace: {len(self.items)} item(s), cap={self.cap}]")
        used = len(lines[0]) if lines else 0
        for it in ordered:
            mark = "✓" if it.checked is True else ("✗" if it.checked is False else "·")
            snippet = " ".join((it.content or "").split())
            if len(snippet) > 160:
                snippet = snippet[:160] + "…"
            src = f" ⟨{it.source}⟩" if it.source else ""
            line = f"- ({it.priority.value} {mark}) {it.id}: {snippet}{src}"
            if max_chars > 0 and used + len(line) + 1 > max_chars:
                lines.append(f"… (+{len(ordered) - len(lines) + (1 if header else 0)} more, budget reached)")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    # ── 직렬화 (세션 인계) ──
    def to_dict(self) -> dict[str, Any]:
        return {"cap": self.cap, "items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecallSet":
        data = data or {}
        return cls(
            cap=int(data.get("cap", DEFAULT_RECALL_CAP) or DEFAULT_RECALL_CAP),
            items=[RecallItem.from_dict(d) for d in data.get("items", [])],
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "RecallSet":
        return cls.from_dict(json.loads(text))
