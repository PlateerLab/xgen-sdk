"""StateProvider/StateRecorder 참조 구현 — memory 모듈을 stateful loop seam 에 연결.

효율 원칙(매 iteration·서브에이전트 누적/누수 방지):
- Provider 는 **stateless 재계산** — 호출마다 소스에서 bounded(cap·char_budget) 뷰만
  합성, 내부에 아무것도 쌓지 않는다. 컨텍스트 비대(prompt 무한증식) 차단.
- Recorder 는 **int seq 만** 보유 — 매 회차 1건을 persist 콜백으로 외부(spine 등)에
  넘기고 즉시 참조 해제. 프로세스 메모리에 이벤트를 누적하지 않는다.
- 둘 다 DB 무관(순수 메커니즘) — 소스/persist 는 이식측 콜백. 서브에이전트마다 새
  인스턴스를 쓰면 상태 공유가 없어 누수가 구조적으로 불가능.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence, Union

from .recall import RecallSet
from .refine import RefinedMemory, refine_message
from .activity import activity_from_message

# 소스: 고정 컬렉션 또는 state 의존 fresh fetch 콜백
_RecallSrc = Union[RecallSet, Callable[[Any], Optional[RecallSet]], None]
_RefinedSrc = Union[Sequence[RefinedMemory], Callable[[Any], Sequence[RefinedMemory]], None]


class MemoryStateProvider:
    """정제 장기기억 + 작업기억 → bounded state 뷰(markdown). 누적 없음."""

    def __init__(
        self,
        *,
        recall: _RecallSrc = None,
        refined: _RefinedSrc = None,
        max_recall: int = 8,
        max_refined: int = 5,
        max_lessons: int = 3,
        char_budget: int = 2000,
    ) -> None:
        self._recall = recall
        self._refined = refined
        self.max_recall = max_recall
        self.max_refined = max_refined
        self.max_lessons = max_lessons
        self.char_budget = char_budget

    def get_state_view(self, state) -> Optional[str]:
        parts: list[str] = []
        # Reflexion: 이번 런의 최근 실패 교훈(in-run, bounded) — 같은 실수 반복 회피.
        lessons = (getattr(state, "metadata", None) or {}).get("loop_lessons") or []
        if lessons:
            parts.append("### 최근 시도 교훈 (반복 회피)")
            for l in list(lessons)[-self.max_lessons:]:
                line = f"- {l.get('intent', '')}".rstrip()
                if l.get("outcome"):
                    line += f" → {l['outcome']}"
                parts.append(line)
        refined = self._resolve(self._refined, state) or []
        if refined:
            parts.append("### 장기기억(정제)")
            for m in list(refined)[: self.max_refined]:
                line = f"- {m.intent}".rstrip()
                if m.outcome:
                    line += f" → {m.outcome}"
                parts.append(line)
        recall = self._resolve(self._recall, state)
        if recall is not None:
            ranked = recall.ranked()[: self.max_recall]
            if ranked:
                parts.append("### 작업기억")
                for it in ranked:
                    parts.append(f"- {it.content}".rstrip())
        view = "\n".join(parts).strip()
        if not view:
            return None
        if len(view) > self.char_budget:
            view = view[: self.char_budget].rstrip() + "\n…(생략)"
        return view

    @staticmethod
    def _resolve(src, state):
        if src is None:
            return None
        return src(state) if callable(src) else src


class MemoryStateRecorder:
    """iteration 경계 incremental 기록 — persist 콜백으로 외부 위임. seq(int)만 보유."""

    def __init__(
        self,
        persist: Callable[[str, dict], None],
        *,
        actor: str = "harness",
        refine_on_complete: bool = True,
        max_lessons: int = 3,
    ) -> None:
        self._persist = persist
        self.actor = actor
        self.refine_on_complete = refine_on_complete
        self.max_lessons = max_lessons
        self._seq = 0

    def record_iteration(self, state, decision: str) -> None:
        self._seq += 1
        ref = self._ref(state)
        done = decision in ("complete", "abort")
        ev = activity_from_message(
            seq=self._seq,
            actor=self.actor,
            raw_message=self._last_user(state),
            kind="harness",
            status="done" if done else "active",
            ref=ref,
        )
        self._persist("activity", ev.to_dict())
        if decision == "retry":
            # Reflexion: 실패 회차를 교훈으로 정제 → in-run bounded buffer + 영속.
            lesson = refine_message(
                self._last_user(state), self._last_assistant(state),
                memory_id=f"lesson-{ref.get('run_id', 'run')}-{self._seq}",
                provenance={**ref, "kind": "lesson", "iteration": self._seq},
            )
            self._push_lesson(state, lesson.to_dict())
            self._persist("lesson", lesson.to_dict())
        if self.refine_on_complete and decision == "complete":
            mem = refine_message(
                self._last_user(state),
                self._last_assistant(state),
                memory_id=f"{ref.get('run_id', 'run')}-{self._seq}",
                provenance=ref,
            )
            self._persist("refined_memory", mem.to_dict())
        # ev/mem/lesson 은 함수 종료와 함께 해제 — 프로세스 누적 없음(버퍼는 bounded).

    def _push_lesson(self, state, lesson: dict) -> None:
        """in-run 교훈 bounded 버퍼(state.metadata['loop_lessons']) — provider 가 읽음.
        max_lessons 로 FIFO 캡 → 매 retry 누적돼도 무한증식 차단."""
        meta = getattr(state, "metadata", None)
        if meta is None:
            return
        buf = meta.get("loop_lessons")
        if not isinstance(buf, list):
            buf = []
            meta["loop_lessons"] = buf
        buf.append(lesson)
        if len(buf) > self.max_lessons:
            del buf[: len(buf) - self.max_lessons]

    @staticmethod
    def _ref(state) -> dict:
        meta = getattr(state, "metadata", None) or {}
        return {k: meta.get(k) for k in ("run_id", "thread_id", "interaction_id") if meta.get(k)}

    @staticmethod
    def _last_user(state) -> str:
        for m in reversed(getattr(state, "messages", []) or []):
            if m.get("role") == "user":
                c = m.get("content")
                return c if isinstance(c, str) else str(c)
        return ""

    @staticmethod
    def _last_assistant(state) -> str:
        return (getattr(state, "last_assistant_text", "") or
                getattr(state, "final_output", "") or "")
